"""Bounded explicit-path ingestion for runner events and end records.

Callers supply identities and files and own the SQLite transaction and connection.
Stored references are inert values and are never loaded by this module.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_CHUNK = 64 * 1024
_PREFIX = 4096
_MAX_RECORD = 2 * 1024 * 1024
_ID = re.compile(r"^[^|]+\|([0-9]+)\|([0-9]+)\|([0-9a-f]{64})\|([0-9]+)$")


class IngestError(ValueError):
    """Input, source identity or event identity cannot be safely reconciled."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _stamp(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise IngestError("observed_at must be a nonempty timestamp string")
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IngestError("observed_at is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IngestError("observed_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _event(raw: bytes, run_id: str) -> tuple[dict[str, Any], str]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IngestError("malformed complete event line") from exc
    if not isinstance(value, dict):
        raise IngestError("event envelope must be an object")
    allowed = {
        "schema_rev",
        "id",
        "run_id",
        "seq",
        "observed_at",
        "emulator_frame",
        "type",
        "scene_id",
        "request_id",
        "provider",
        "outcome",
        "source",
        "public_visibility",
        "details",
    }
    if value.keys() - allowed:
        raise IngestError("event envelope has unknown fields")
    required = {"schema_rev", "id", "run_id", "seq", "observed_at", "type", "source", "details"}
    if not required.issubset(value):
        raise IngestError("event envelope is missing required fields")
    if type(value["schema_rev"]) is not int or value["schema_rev"] != 1:
        raise IngestError("unsupported event schema revision")
    if value["run_id"] != run_id:
        raise IngestError("event run_id does not match caller identity")
    if type(value["seq"]) is not int or value["seq"] < 1:
        raise IngestError("event seq must be an integer >= 1")
    expected_id = f"{run_id}:{value['seq']}"
    if value["id"] != expected_id:
        raise IngestError("event id does not match run_id and seq")
    if not isinstance(value["type"], str) or not re.fullmatch(
        r"[a-z][a-z_]*(?:\.[a-z][a-z_]*)+", value["type"]
    ):
        raise IngestError("event type is malformed")
    if (
        not isinstance(value["source"], str)
        or not value["source"]
        or not isinstance(value["details"], dict)
    ):
        raise IngestError("event source or details are malformed")
    if "public_visibility" in value and type(value["public_visibility"]) is not bool:
        raise IngestError("public_visibility must be a boolean")
    if not isinstance(value["observed_at"], str):
        raise IngestError("observed_at must be a timestamp string")
    _stamp(value["observed_at"])
    for key in ("scene_id", "request_id", "provider", "outcome"):
        if key in value and value[key] is not None and not isinstance(value[key], str):
            raise IngestError(f"event {key} must be a string or null")
    if (
        "emulator_frame" in value
        and value["emulator_frame"] is not None
        and (type(value["emulator_frame"]) is not int or value["emulator_frame"] < 0)
    ):
        raise IngestError("emulator_frame must be a nonnegative integer or null")
    try:
        digest = hashlib.sha256(_canonical(value)).hexdigest()
    except (TypeError, ValueError) as exc:
        raise IngestError("event envelope contains a non-JSON value") from exc
    return value, digest


def _read_record(path: str | os.PathLike[str] | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        with open(path, "rb") as source:
            data = source.read(_MAX_RECORD + 1)
    except FileNotFoundError:
        return None
    if len(data) > _MAX_RECORD:
        raise IngestError(f"end record exceeds {_MAX_RECORD} bytes")
    try:
        result = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IngestError("malformed end record") from exc
    if not isinstance(result, dict):
        raise IngestError("end record must be an object")
    return result


def _check_prefix(fd: int, length: int, expected: str) -> None:
    data = os.pread(fd, length, 0)
    if len(data) != length:
        raise IngestError("source prefix was truncated")
    if hashlib.sha256(data).hexdigest() != expected:
        raise IngestError("source prefix was rewritten")


def _source_id(
    db: sqlite3.Connection, segment_id: str, run_id: str, path: Path, fd: int
) -> tuple[str, int, int]:
    stat = os.fstat(fd)
    rows = db.execute(
        "SELECT id,source_ref,generation,byte_offset FROM ingest_sources "
        "WHERE segment_id=? ORDER BY generation DESC",
        (segment_id,),
    ).fetchall()
    matching: tuple[str, int, int] | None = None
    prior_generation = -1
    for sid, source_ref, generation, offset in rows:
        m = _ID.fullmatch(source_ref)
        if not m or source_ref.split("|", 1)[0] != path.name:
            continue
        prior_generation = max(prior_generation, generation)
        dev, ino, prefix_hash, prefix_len = int(m[1]), int(m[2]), m[3], int(m[4])
        if (dev, ino) == (stat.st_dev, stat.st_ino) and stat.st_size >= offset:
            _check_prefix(fd, prefix_len, prefix_hash)
            _verify_boundary(db, fd, segment_id, run_id, offset)
            matching = (sid, generation, offset)
            break
    if matching:
        return matching
    generation = prior_generation + 1
    prefix_len = min(_PREFIX, stat.st_size)
    prefix = os.pread(fd, prefix_len, 0)
    if len(prefix) != prefix_len:
        raise IngestError("source changed while reading initial prefix")
    prefix_hash = hashlib.sha256(prefix).hexdigest()
    # Keep a digest of the generation's initial prefix; appends never redefine it.
    source_ref = f"{path.name}|{stat.st_dev}|{stat.st_ino}|{prefix_hash}|{prefix_len}"
    sid = hashlib.sha256(f"{segment_id}\0{source_ref}\0{generation}".encode()).hexdigest()
    db.execute(
        "INSERT INTO ingest_sources(id,segment_id,source_ref,generation,byte_offset) "
        "VALUES(?,?,?,?,0)",
        (sid, segment_id, source_ref, generation),
    )
    return sid, generation, 0


def _verify_boundary(
    db: sqlite3.Connection, fd: int, segment_id: str, run_id: str, offset: int
) -> None:
    if offset == 0:
        return
    start = max(0, offset - _CHUNK - 1)
    tail = os.pread(fd, offset - start, start)
    if len(tail) != offset - start or not tail.endswith(b"\n"):
        raise IngestError("source cursor does not end at a complete event")
    previous = tail.rfind(b"\n", 0, len(tail) - 1)
    line = tail[previous + 1 : -1].rstrip(b"\r")
    if len(line) > _CHUNK:
        raise IngestError("last committed event boundary exceeds line limit")
    event, digest = _event(line, run_id)
    row = db.execute(
        "SELECT content_sha256 FROM ingested_events WHERE segment_id=? AND event_id=?",
        (segment_id, event["id"]),
    ).fetchone()
    if row is None or row[0] is None:
        raise IngestError("last committed event cannot prove cursor identity")
    if digest != row[0]:
        raise IngestError("last committed event boundary was rewritten")


def _read_line(fd: int, start: int) -> tuple[bytes | None, int]:
    line = bytearray()
    cursor = start
    while len(line) <= _CHUNK:
        part = os.pread(fd, min(8192, _CHUNK + 2 - len(line)), cursor)
        if not part:
            return (bytes(line), cursor) if b"\n" in line else (None, start)
        newline = part.find(b"\n")
        if newline >= 0:
            line.extend(part[:newline])
            return bytes(line).rstrip(b"\r"), cursor + newline + 1
        line.extend(part)
        cursor += len(part)
    raise IngestError("event line exceeds bounded input size")


def _ensure_run(db: sqlite3.Connection, save_id: str, segment_id: str, run_id: str) -> None:
    row = db.execute("SELECT id,run_id,save_id FROM runs WHERE id=?", (segment_id,)).fetchone()
    if row:
        if row != (segment_id, run_id, save_id):
            raise IngestError("caller save/segment/upstream run identity conflicts with ledger")
        return
    if db.execute("SELECT 1 FROM saves WHERE id=?", (save_id,)).fetchone() is None:
        db.execute("INSERT INTO saves(id,source_ref) VALUES(?,?)", (save_id, "caller"))
    db.execute(
        "INSERT INTO runs(id,run_id,save_id,source_ref) VALUES(?,?,?,?)",
        (segment_id, run_id, save_id, "caller"),
    )


def _reconcile_objective(
    db: sqlite3.Connection,
    segment_id: str,
    revision: int,
    objective_id: str,
    kind: str,
    set_by: str | None,
    *,
    source_ref: str,
    source_event_id: str | None,
    observed_at: str | None,
) -> None:
    key = (segment_id, revision, objective_id)
    prior = db.execute(
        "SELECT kind,set_by,accepted FROM objectives "
        "WHERE segment_id=? AND revision=? AND objective_id=?",
        key,
    ).fetchone()
    if prior is not None and (
        prior[0] != kind
        or prior[2] != 1
        or (prior[1] is not None and set_by is not None and prior[1] != set_by)
    ):
        raise IngestError("objective source conflicts with existing history")
    if prior is None:
        db.execute(
            "INSERT INTO objectives(id,segment_id,revision,objective_id,kind,set_by,accepted,"
            "accepted_at,source_ref,source_event_id,observed_at) VALUES(?,?,?,?,?,?,1,?,?,?,?)",
            (
                f"{segment_id}:{revision}:{objective_id}",
                segment_id,
                revision,
                objective_id,
                kind,
                set_by,
                observed_at,
                source_ref,
                source_event_id,
                observed_at,
            ),
        )
    else:
        db.execute(
            "UPDATE objectives SET set_by=COALESCE(set_by,?),accepted_at=COALESCE(accepted_at,?),"
            "source_ref=CASE WHEN ? IS NULL THEN source_ref ELSE ? END,"
            "source_event_id=COALESCE(source_event_id,?),observed_at=COALESCE(observed_at,?) "
            "WHERE segment_id=? AND revision=? AND objective_id=?",
            (set_by, observed_at, source_event_id, source_ref, source_event_id, observed_at, *key),
        )


def _project_event(
    db: sqlite3.Connection, segment_id: str, source_ref: str, event: dict[str, Any]
) -> None:
    typ = event["type"]
    eid, at = event["id"], _stamp(event["observed_at"])
    if typ == "runtime.started":
        db.execute(
            "UPDATE runs SET started_at=COALESCE(started_at,?) WHERE id=?",
            (at, segment_id),
        )
    elif (
        typ == "battle.won"
        or typ == "battle.lost"
        or typ == "battle.fled"
        or typ == "battle.caught"
    ):
        db.execute(
            "INSERT INTO battles(id,segment_id,result,ended_at,source_ref,source_event_id,"
            "observed_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                f"{segment_id}:{eid}",
                segment_id,
                typ.removeprefix("battle."),
                at,
                source_ref,
                eid,
                at,
            ),
        )
    elif typ == "navigation.plan_budget_exceeded":
        db.execute(
            "INSERT INTO incidents(id,segment_id,kind,started_at,source_ref,source_event_id,"
            "observed_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (f"{segment_id}:{eid}", segment_id, "plan_budget_exceeded", at, source_ref, eid, at),
        )
    elif typ == "progress.goal_changed":
        revision, objective_id = (
            event["details"].get("revision"),
            event["details"].get("objective_id"),
        )
        kind, set_by = event["details"].get("kind"), event["details"].get("set_by")
        if (
            type(revision) is int
            and revision >= 1
            and isinstance(objective_id, str)
            and objective_id
            and isinstance(kind, str)
            and kind
            and (set_by is None or isinstance(set_by, str))
        ):
            _reconcile_objective(
                db,
                segment_id,
                revision,
                objective_id,
                kind,
                set_by,
                source_ref=source_ref,
                source_event_id=eid,
                observed_at=at,
            )
    elif typ == "progress.milestone_completed":
        revision, objective_id = (
            event["details"].get("revision"),
            event["details"].get("objective_id"),
        )
        if (
            type(revision) is int
            and revision >= 1
            and isinstance(objective_id, str)
            and objective_id
        ):
            db.execute(
                "UPDATE objectives SET done=1,done_at=? WHERE segment_id=? AND revision=? "
                "AND objective_id=?",
                (at, segment_id, revision, objective_id),
            )
    elif typ == "progress.stuck" and (
        event.get("outcome") == "busy" or event["details"].get("error_code") == "E_BUSY_STUCK"
    ):
        db.execute(
            "INSERT INTO incidents(id,segment_id,kind,started_at,source_ref,source_event_id,"
            "observed_at) VALUES(?,?,?,?,?,?,?)",
            (f"{segment_id}:{eid}", segment_id, "busy_stuck", at, source_ref, eid, at),
        )
    elif typ == "runtime.stopped":
        reason = event["details"].get("stop_reason")
        if reason is not None and not isinstance(reason, str):
            raise IngestError("runtime stop_reason must be a string")
        db.execute(
            "UPDATE runs SET ended_at=COALESCE(ended_at,?),"
            "exit_reason=COALESCE(exit_reason,?) WHERE id=?",
            (at, reason, segment_id),
        )
    elif typ == "escalation.requested":
        db.execute(
            "INSERT INTO incidents(id,segment_id,kind,started_at,source_ref,source_event_id,"
            "observed_at) VALUES(?,?,?,?,?,?,?)",
            (f"{segment_id}:{eid}", segment_id, "escalation", at, source_ref, eid, at),
        )
    elif typ.startswith("checkpoint."):
        # Checkpoint references are deliberately not loaded or treated as verified saves.
        return


def ingest_run(
    db: sqlite3.Connection,
    *,
    save_id: str,
    segment_id: str,
    run_id: str,
    event_paths: Iterable[str | os.PathLike[str]],
    summary_path: str | os.PathLike[str] | None = None,
    exit_path: str | os.PathLike[str] | None = None,
    max_lines: int = 1000,
) -> int:
    """Admit at most ``max_lines`` complete events and reconcile explicit end records.

    Call inside ``with db:`` so fact projection, event identity and cursor advancement
    commit or roll back together. Pass rotated paths oldest-first, then the active file.
    Missing event/end files are normal unknowns.
    """
    if not all(isinstance(v, str) and v for v in (save_id, segment_id, run_id)):
        raise ValueError("save_id, segment_id and run_id must be nonempty strings")
    if type(max_lines) is not int or not 1 <= max_lines <= 10000:
        raise ValueError("max_lines must be an integer in [1,10000]")
    _ensure_run(db, save_id, segment_id, run_id)
    admitted = 0
    for raw_path in event_paths:
        path = Path(raw_path)
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            continue
        try:
            sid, _generation, offset = _source_id(db, segment_id, run_id, path, fd)
            size = os.fstat(fd).st_size
            if size < offset:
                raise IngestError("source truncated during ingestion")
            while admitted < max_lines:
                start = offset
                line, next_offset = _read_line(fd, start)
                if line is None:
                    break  # incomplete final line is never advanced
                event, digest = _event(line, run_id)
                existing = db.execute(
                    "SELECT content_sha256 FROM ingested_events WHERE segment_id=? AND event_id=?",
                    (segment_id, event["id"]),
                ).fetchone()
                if existing:
                    if existing[0] is None or existing[0] != digest:
                        raise IngestError(f"event ID content conflict: {event['id']}")
                else:
                    _project_event(db, segment_id, path.name, event)
                    db.execute(
                        "INSERT INTO ingested_events(segment_id,event_id,source_id,seq,byte_offset,"
                        "observed_at,content_sha256) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (
                            segment_id,
                            event["id"],
                            sid,
                            event["seq"],
                            start,
                            _stamp(event["observed_at"]),
                            digest,
                        ),
                    )
                offset = next_offset
                db.execute("UPDATE ingest_sources SET byte_offset=? WHERE id=?", (offset, sid))
                admitted += 1
            if admitted >= max_lines:
                break
        finally:
            os.close(fd)
    summary = _read_record(summary_path)
    exit_record = _read_record(exit_path)
    for record, label in ((summary, "summary"), (exit_record, "exit")):
        if record is not None and record.get("run_id", run_id) != run_id:
            raise IngestError(f"{label} run_id conflicts with caller identity")
    if summary is not None:
        wall = summary.get("wall_seconds")
        if wall is not None and (
            type(wall) not in (int, float) or not math.isfinite(wall) or wall < 0
        ):
            raise IngestError("summary wall_seconds must be nonnegative")
        verdict, stop_reason = summary.get("verdict"), summary.get("stop_reason")
        if verdict is not None and not isinstance(verdict, str):
            raise IngestError("summary verdict must be a string")
        if stop_reason is not None and not isinstance(stop_reason, str):
            raise IngestError("summary stop_reason must be a string")
        db.execute(
            "UPDATE runs SET verdict=?,exit_reason=?,wall_seconds=? "
            "WHERE id=? AND run_id=? AND save_id=?",
            (verdict, stop_reason, wall, segment_id, run_id, save_id),
        )
        objective = summary.get("objective")
        active = objective.get("active") if isinstance(objective, dict) else None
        if isinstance(active, dict):
            oid, rev, kind, setter = (
                active.get(k) for k in ("objective_id", "revision", "kind", "set_by")
            )
            if (
                isinstance(oid, str)
                and oid
                and type(rev) is int
                and rev >= 1
                and isinstance(kind, str)
                and kind
                and (setter is None or isinstance(setter, str))
            ):
                _reconcile_objective(
                    db,
                    segment_id,
                    rev,
                    oid,
                    kind,
                    setter,
                    source_ref="summary.json",
                    source_event_id=None,
                    observed_at=None,
                )
    if exit_record is not None:
        verdict = exit_record.get("verdict")
        if verdict is not None and not isinstance(verdict, str):
            raise IngestError("exit verdict must be a string")
        db.execute(
            "UPDATE runs SET verdict=COALESCE(?,verdict) WHERE id=? AND run_id=? AND save_id=?",
            (verdict, segment_id, run_id, save_id),
        )
    return admitted
