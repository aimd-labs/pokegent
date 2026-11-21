"""Explicit, bounded adapter from runner output directories to the local ledger."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import stat
import sys
import time
import urllib.request
from contextlib import closing
from pathlib import Path

_MAX_RUNS = 1000
_MAX_DIRECTORY_ENTRIES = 10000
_MAX_EVENT_SEGMENTS = 1000


class MissingRunIdentity(ValueError):
    """An incomplete/new run directory has no available upstream identity yet."""


def add_parser(parent):
    p = parent.add_parser("ingest", help="Ingest explicit local runner output")
    p.add_argument("--save", required=True)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-dir", type=Path)
    source.add_argument("--runs-dir", type=Path)
    p.add_argument("--db")
    p.add_argument("--live-home")
    p.add_argument("--label")
    p.add_argument("--mode", choices=("casual", "speedrun"))
    p.add_argument("--rom-profile")
    p.add_argument("--max-lines", type=int, default=1000)
    p.add_argument("--segment-id")
    telemetry = p.add_mutually_exclusive_group()
    telemetry.add_argument("--viewer-port", type=int)
    telemetry.add_argument("--telemetry-file", type=Path)
    p.add_argument("--story-flag", type=int, action="append", default=[])
    p.add_argument("--json", action="store_true")
    return p


def _identity(paths, directory):
    from pokegent.ledger_ingest import IngestError

    paths = [*paths, directory / "events.jsonl"]
    ids = set()
    for path in paths:
        try:
            with path.open("rb") as f:
                line = f.readline(65538)
        except FileNotFoundError:
            continue
        if len(line) > 65536 and not line.endswith(b"\n"):
            raise IngestError("first event exceeds identity read limit")
        if line:
            if not line.endswith(b"\n"):
                continue
            try:
                event = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise IngestError("malformed first complete event") from exc
            if (
                not isinstance(event, dict)
                or not isinstance(event.get("run_id"), str)
                or not event["run_id"]
            ):
                raise IngestError("first event has no run_id")
            ids.add(event["run_id"])
            break
    for name in ("summary.json", "exit.json"):
        path = directory / name
        try:
            with path.open("rb") as f:
                raw = f.read(2 * 1024 * 1024 + 1)
        except FileNotFoundError:
            continue
        if len(raw) > 2 * 1024 * 1024:
            raise IngestError(f"{name} exceeds 2097152 bytes")
        try:
            record = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IngestError(f"malformed {name}") from exc
        if not isinstance(record, dict):
            raise IngestError(f"{name} must be an object")
        if isinstance(record.get("run_id"), str) and record["run_id"]:
            ids.add(record["run_id"])
    if len(ids) != 1:
        if not ids:
            raise MissingRunIdentity("run identity is not available yet")
        raise IngestError("run identity missing or conflicting across event/summary/exit")
    return ids.pop()


def _db_path(args):
    if args.db:
        return Path(args.db).expanduser()
    from pokegent.cli import live_home

    return live_home(args.live_home) / "ledger.sqlite3"


def _safe_db(path):
    parent = path.parent
    for ancestor in (parent, *parent.parents):
        if ancestor.is_symlink():
            raise ValueError("ledger path cannot contain a symlinked directory")
    st = parent.stat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & stat.S_IWOTH:
        raise ValueError("ledger parent must be an owned, non-world-writable directory")
    if path.is_symlink():
        raise ValueError("ledger database cannot be a symlink")


def _event_paths(directory):
    from pokegent.events import segment_paths

    entries = list(itertools.islice(directory.iterdir(), _MAX_DIRECTORY_ENTRIES + 1))
    if len(entries) > _MAX_DIRECTORY_ENTRIES:
        raise ValueError(f"run directory exceeds bounded entry limit of {_MAX_DIRECTORY_ENTRIES}")
    paths = segment_paths(directory)
    if len(paths) > _MAX_EVENT_SEGMENTS:
        raise ValueError(
            f"run directory exceeds bounded event segment limit of {_MAX_EVENT_SEGMENTS}"
        )
    return paths


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _telemetry(args):
    if args.telemetry_file:
        with args.telemetry_file.expanduser().open("rb") as f:
            raw = f.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError("telemetry file exceeds 1048576 bytes")
    else:
        if type(args.viewer_port) is not int or not 1 <= args.viewer_port <= 65535:
            raise ValueError("viewer port must be from 1 through 65535")
        request = urllib.request.Request(
            f"http://127.0.0.1:{args.viewer_port}/ops/telemetry",
            headers={"Accept": "application/json"},
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
        )
        with opener.open(request, timeout=2) as response:
            raw = response.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError("telemetry response exceeds 1048576 bytes")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("telemetry document must be an object")
    return value


def _observation(document, run_id, source_ref, selected_flags, *, as_of=None):
    game = document.get("game")
    run = document.get("run")
    if not isinstance(run, dict) or run.get("run_id") != run_id:
        return None, "telemetry run_id does not match selected run"
    game_id = game.get("run_id") if isinstance(game, dict) else None
    if game_id is not None and game_id != run_id:
        return None, "telemetry game.run_id conflicts with selected run"
    state = document.get("state")
    block = state.get("observation") if isinstance(state, dict) else None
    if not isinstance(block, dict):
        return None, "telemetry has no typed state.observation block"
    freshness = document.get("freshness")
    state_freshness = freshness.get("state") if isinstance(freshness, dict) else None
    updated_at = state_freshness.get("updated_at") if isinstance(state_freshness, dict) else None
    if type(updated_at) not in (int, float) or not math.isfinite(updated_at):
        return None, "telemetry state freshness timestamp is unavailable"
    ttl = state_freshness.get("ttl_s") if isinstance(state_freshness, dict) else None
    if type(ttl) not in (int, float) or not math.isfinite(ttl) or ttl <= 0:
        return None, "telemetry state freshness TTL is unavailable"
    now = time.time()
    if as_of is not None and (
        type(as_of) not in (int, float)
        or not math.isfinite(as_of)
        or as_of > now + 1
    ):
        return None, "telemetry file capture timestamp is invalid"
    age = (now if as_of is None else as_of) - updated_at
    if age < -1 or age > ttl:
        return None, "telemetry state observation is stale or from the future"
    # Validate timestamp and payload before opening the writer transaction.
    from datetime import datetime, timezone

    try:
        observed_at = (
            datetime.fromtimestamp(updated_at, tz=timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        from pokegent.ledger_observations import milestone_payload

        payload = milestone_payload(block, tuple(selected_flags))
    except (ValueError, TypeError, OverflowError, OSError) as exc:
        return None, f"invalid telemetry observation: {exc}"
    import hashlib

    identity = (
        "ram:"
        + hashlib.sha256(
            f"{run_id}\0{payload['snapshot_seq']}\0{source_ref}\0{observed_at}".encode()
        ).hexdigest()
    )
    return (identity, observed_at, payload), None


def _one(args, directory, db, telemetry_doc=None, telemetry_error=None):
    from pokegent.ledger_ingest import IngestError, ingest_run

    if not directory.is_dir() or directory.is_symlink():
        raise ValueError(f"run directory must be an explicit real directory: {directory}")
    paths = _event_paths(directory)
    run_id = _identity(paths, directory)
    segment_id = (
        args.segment_id
        or "segment:" + hashlib.sha256(str(directory.resolve()).encode()).hexdigest()
    )
    db.execute("BEGIN IMMEDIATE")
    try:
        exists = db.execute("SELECT 1 FROM saves WHERE id=?", (args.save,)).fetchone()
        if exists:
            prior = db.execute(
                "SELECT label,mode,rom_profile FROM saves WHERE id=?", (args.save,)
            ).fetchone()
            for selected, known, name in (
                (args.mode, prior[1], "mode"),
                (args.rom_profile, prior[2], "ROM profile"),
            ):
                if selected and known and selected != known:
                    raise IngestError(f"selected {name} conflicts with saved identity")
            db.execute(
                "UPDATE saves SET label=COALESCE(?,label),mode=COALESCE(?,mode),"
                "rom_profile=COALESCE(?,rom_profile) WHERE id=?",
                (args.label, args.mode, args.rom_profile, args.save),
            )
        else:
            db.execute(
                "INSERT INTO saves(id,label,mode,rom_profile,source_ref) VALUES(?,?,?,?,?)",
                (args.save, args.label, args.mode, args.rom_profile, "operator"),
            )
        count = ingest_run(
            db,
            save_id=args.save,
            segment_id=segment_id,
            run_id=run_id,
            event_paths=[*paths, directory / "events.jsonl"],
            summary_path=directory / "summary.json",
            exit_path=directory / "exit.json",
            max_lines=args.max_lines,
        )
        ram = {"status": "unavailable", "reason": telemetry_error or "not requested"}
        if telemetry_doc is not None:
            source_ref = (
                "telemetry-file" if args.telemetry_file else f"viewer-port:{args.viewer_port}"
            )
            observation, reason = _observation(
                telemetry_doc,
                run_id,
                source_ref,
                args.story_flag,
                as_of=(telemetry_doc.get("generated_at") if args.telemetry_file else None),
            )
            if observation:
                from pokegent.ledger_observations import ingest_observation

                observation_id, observed_at, payload = observation
                ingest_observation(
                    db,
                    save_id=args.save,
                    segment_id=segment_id,
                    observation_id=observation_id,
                    observed_at=observed_at,
                    source_ref=source_ref,
                    payload=payload,
                )
                ram = {"status": "available", "observation_id": observation_id}
            else:
                ram = {"status": "unavailable", "reason": reason}
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return {
        "directory": str(directory),
        "run_id": run_id,
        "segment_id": segment_id,
        "complete_events_processed": count,
        "limit": args.max_lines,
        "ram": ram,
    }


def run(args):
    import sqlite3
    import urllib.error

    from pokegent.ledger import open_ledger
    from pokegent.ledger_ingest import IngestError

    try:
        if not args.save.strip() or args.max_lines < 1 or args.max_lines > 10000:
            raise ValueError("save must be nonempty and max-lines must be in [1,10000]")
        if args.segment_id and args.runs_dir:
            raise ValueError("--segment-id is only valid with --run-dir")
        if args.viewer_port is not None and not 1 <= args.viewer_port <= 65535:
            raise ValueError("viewer port must be from 1 through 65535")
        if args.runs_dir:
            root = args.runs_dir.expanduser()
            if not root.is_dir() or root.is_symlink():
                raise ValueError("runs directory must be an explicit real directory")
            entries = list(itertools.islice(root.iterdir(), _MAX_DIRECTORY_ENTRIES + 1))
            if len(entries) > _MAX_DIRECTORY_ENTRIES:
                raise ValueError(
                    f"runs directory exceeds bounded entry limit of {_MAX_DIRECTORY_ENTRIES}"
                )
            dirs = sorted(p for p in entries if p.is_dir() and not p.is_symlink())
            if len(dirs) > _MAX_RUNS:
                raise ValueError(f"runs directory exceeds bounded limit of {_MAX_RUNS}")
            if not dirs:
                raise ValueError("runs directory contains no real run directories")
        else:
            dirs = [args.run_dir.expanduser()]
        # Validate every identity before opening/creating the selected ledger.

        valid_dirs, skipped, failed = [], [], []
        for directory in dirs:
            if not directory.is_dir() or directory.is_symlink():
                raise ValueError(f"run directory must be an explicit real directory: {directory}")
            try:
                _identity(_event_paths(directory), directory)
            except MissingRunIdentity as exc:
                skipped.append(
                    {"directory": str(directory), "status": "unknown", "reason": str(exc)}
                )
            except IngestError as exc:
                if args.run_dir:
                    raise
                failed.append(
                    {"directory": str(directory), "status": "failed", "reason": str(exc)}
                )
            else:
                valid_dirs.append(directory)
        if not valid_dirs:
            result = {"save": args.save, "runs": [], "skipped": skipped, "failed": failed}
            print(
                json.dumps(result, sort_keys=True)
                if args.json
                else f"No runs ingested; {len(skipped)} run identities unavailable"
            )
            if failed:
                for item in failed:
                    print(f"FAILED {item['directory']}: {item['reason']}", file=sys.stderr)
                return 2
            return 0
        telemetry_doc, telemetry_error = None, None
        if args.telemetry_file or args.viewer_port is not None:
            try:
                telemetry_doc = _telemetry(args)
            except (OSError, ValueError, urllib.error.URLError, TimeoutError) as exc:
                telemetry_error = f"telemetry unavailable: {type(exc).__name__}: {exc}"
        path = _db_path(args)
        _safe_db(path)
        with closing(open_ledger(path)) as db:
            results = []
            for directory in valid_dirs:
                try:
                    results.append(_one(args, directory, db, telemetry_doc, telemetry_error))
                except IngestError as exc:
                    if args.run_dir:
                        raise
                    failed.append(
                        {"directory": str(directory), "status": "failed", "reason": str(exc)}
                    )
        result = {"save": args.save, "runs": results, "skipped": skipped, "failed": failed}
        if args.json:
            print(json.dumps(result, sort_keys=True, ensure_ascii=True))
        else:
            for item in results:
                print(
                    f"{item['run_id']}: processed "
                    f"{item['complete_events_processed']} complete events "
                    f"(limit {item['limit']})"
                )
        for item in failed:
            print(f"FAILED {item['directory']}: {item['reason']}", file=sys.stderr)
        return 2 if valid_dirs and not results else 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
