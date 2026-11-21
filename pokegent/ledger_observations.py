"""Pure RAM observation projection and explicit offline ledger insertion.

This module is not imported by the game loop. Callers provide all identity and
provenance; observation rows report what was visible at that observation time.
Clock samples are stored separately from milestones, including known-clock-only
observations; no milestone is fabricated for a clock sample.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping

from pokegent.games.gen3.firered.rom import constants as C

_MAP_ID = re.compile(r"(0|[1-9][0-9]*):(0|[1-9][0-9]*)\Z")
_DEX = re.compile(r"seen:(0|[1-9][0-9]*)/owned:(0|[1-9][0-9]*)\Z")


def _integer(value: Any, *, minimum: int, maximum: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be an integer from {minimum} through {maximum}")
    return value


def milestone_payload(
    observation: Mapping[str, Any], selected_flag_ids: tuple[int, ...] = ()
) -> dict[str, Any]:
    """Translate known operator facts; validate them before creating ledger values."""
    if not isinstance(observation, Mapping):
        raise ValueError("observation must be a mapping")
    seq = _integer(
        observation.get("snapshot_seq"), minimum=0, maximum=2**63 - 1, field="snapshot_seq"
    )
    profile = observation.get("rom_profile")
    if not isinstance(profile, str) or not profile.strip():
        raise ValueError("rom_profile must be explicit")
    seconds = observation.get("game_seconds")
    if seconds is not None:
        seconds = _integer(seconds, minimum=0, maximum=2**63 - 1, field="game_seconds")
    rows: set[tuple[str, str, str]] = set()
    badges = observation.get("badges")
    if badges is not None:
        rows.update(
            ("badge", str(_integer(x, minimum=1, maximum=8, field="badge")), "possession")
            for x in badges
        )
    for key, kind, maximum in (("key_items", "key_item", 65535), ("hms", "hm", 346)):
        items = observation.get(key)
        if items is not None:
            for item in items:
                item_id = _integer(item, minimum=1, maximum=maximum, field=key)
                if kind == "hm" and item_id < 339:
                    raise ValueError("HM item ID must be from 339 through 346")
                rows.add((kind, str(item_id), "possession"))
    map_id = observation.get("map")
    if map_id is not None:
        if not isinstance(map_id, str) or not _MAP_ID.fullmatch(map_id):
            raise ValueError("map must be a numeric group:number identity")
        if any(int(part) > 255 for part in map_id.split(":")):
            raise ValueError("map group and number must fit the RAM byte fields")
        rows.add(("map_first_visit", map_id, "possession"))
    dex = observation.get("pokedex")
    if dex is not None:
        if not isinstance(dex, Mapping):
            raise ValueError("pokedex must be a known count mapping or None")
        seen = _integer(dex.get("seen"), minimum=0, maximum=C.NATIONAL_DEX_COUNT, field="seen")
        owned = _integer(dex.get("owned"), minimum=0, maximum=C.NATIONAL_DEX_COUNT, field="owned")
        rows.add(("pokedex_count", f"seen:{seen}/owned:{owned}", "possession"))
    flags = observation.get("flags")
    selected = tuple(
        _integer(x, minimum=1, maximum=C.FLAGS_COUNT - 1, field="selected flag")
        for x in selected_flag_ids
    )
    if flags is not None:
        known = {_integer(x, minimum=1, maximum=C.FLAGS_COUNT - 1, field="flag") for x in flags}
        rows.update(("story_flag", str(flag), "possession") for flag in selected if flag in known)
    return {
        "snapshot_seq": seq,
        "rom_profile": profile,
        "game_seconds": seconds,
        "milestones": [list(row) for row in sorted(rows)],
    }


def _validate_payload(
    payload: Mapping[str, Any],
) -> tuple[int, str, int | None, list[tuple[str, str, str]]]:
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be a mapping")
    seq = _integer(payload.get("snapshot_seq"), minimum=0, maximum=2**63 - 1, field="snapshot_seq")
    profile = payload.get("rom_profile")
    if not isinstance(profile, str) or not profile.strip():
        raise ValueError("rom_profile must be explicit")
    seconds = payload.get("game_seconds")
    if seconds is not None:
        seconds = _integer(seconds, minimum=0, maximum=2**63 - 1, field="game_seconds")
    raw = payload.get("milestones")
    if not isinstance(raw, list):
        raise ValueError("milestones must be a list")
    rows = []
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            raise ValueError("each milestone must have kind, value and observation")
        kind, value, observation = row
        if not all(isinstance(x, str) for x in row):
            raise ValueError("milestone fields must be strings")
        if observation not in {"achievement", "possession"}:
            raise ValueError("input observations must be achievement or possession")
        if kind == "badge":
            _integer(int(value), minimum=1, maximum=8, field="badge")
        elif kind in {"key_item", "hm", "story_flag"}:
            number = int(value)
            limit = 65535 if kind == "key_item" else 346 if kind == "hm" else C.FLAGS_COUNT - 1
            _integer(number, minimum=1 if kind != "story_flag" else 1, maximum=limit, field=kind)
            if kind == "hm" and number < 339:
                raise ValueError("HM item ID must be from 339 through 346")
            if str(number) != value:
                raise ValueError(f"{kind} value must be canonical decimal")
        elif kind == "map_first_visit":
            if not _MAP_ID.fullmatch(value):
                raise ValueError("map value must be group:number")
            if any(int(part) > 255 for part in value.split(":")):
                raise ValueError("map group and number must fit the RAM byte fields")
        elif kind == "pokedex_count":
            match = _DEX.fullmatch(value)
            if not match or any(int(n) > C.NATIONAL_DEX_COUNT for n in match.groups()):
                raise ValueError("pokedex_count value must be seen:N/owned:N")
        else:
            raise ValueError("unsupported milestone kind")
        rows.append((kind, value, observation))
    if len(set(rows)) != len(rows):
        raise ValueError("duplicate milestone rows")
    return seq, profile, seconds, sorted(rows)


def ingest_observation(
    db: sqlite3.Connection,
    *,
    save_id: str,
    segment_id: str,
    observation_id: str,
    observed_at: str,
    source_ref: str,
    payload: Mapping[str, Any],
) -> int:
    """Record atomically; earlier map backfills add first-known provenance, never replace."""
    if not db.in_transaction:
        raise ValueError("caller must start a transaction before observation ingestion")
    if not all(
        isinstance(x, str) and x.strip()
        for x in (save_id, segment_id, observation_id, observed_at, source_ref)
    ):
        raise ValueError("observation identity and provenance must be explicit")
    try:
        instant = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at must be an ISO-8601 timestamp") from exc
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("observed_at must include a timezone")
    observed_at = (
        instant.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    seq, profile, seconds, normalized = _validate_payload(payload)
    if (
        db.execute("SELECT 1 FROM runs WHERE id=? AND save_id=?", (segment_id, save_id)).fetchone()
        is None
    ):
        raise ValueError("save/segment attribution does not match")
    row = db.execute("SELECT rom_profile FROM saves WHERE id=?", (save_id,)).fetchone()
    if row[0] is None:
        db.execute("UPDATE saves SET rom_profile=? WHERE id=?", (profile, save_id))
    elif row[0] != profile:
        raise ValueError("observation ROM profile conflicts with the save")

    event_id = f"ram-observation:{observation_id}"
    digest_input = json.dumps(
        {
            "save_id": save_id,
            "segment_id": segment_id,
            "observation_id": observation_id,
            "observed_at": observed_at,
            "source_ref": source_ref,
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.sha256(digest_input.encode()).hexdigest()
    receipt = db.execute(
        "SELECT content_sha256 FROM ingested_events WHERE segment_id=? AND event_id=?",
        (segment_id, event_id),
    ).fetchone()
    if receipt:
        if receipt[0] != digest:
            raise ValueError("observation identity reused with different content")
        clock = db.execute(
            "SELECT observed_at,game_seconds FROM observation_clocks "
            "WHERE segment_id=? AND event_id=?",
            (segment_id, event_id),
        ).fetchone()
        if clock is None:
            db.execute(
                "INSERT INTO observation_clocks(segment_id,event_id,observed_at,game_seconds) "
                "VALUES(?,?,?,?)",
                (segment_id, event_id, observed_at, seconds),
            )
        elif tuple(clock) != (observed_at, seconds):
            raise ValueError("observation clock receipt conflicts with its content digest")
        return 0

    namespaced_ref = f"ram-observation:{source_ref}"
    source_id = (
        "ram-source:" + hashlib.sha256(f"{segment_id}\0{namespaced_ref}".encode()).hexdigest()
    )
    source = db.execute(
        "SELECT id FROM ingest_sources WHERE segment_id=? AND source_ref=? AND generation=0",
        (segment_id, namespaced_ref),
    ).fetchone()
    if source is None:
        db.execute(
            "INSERT INTO ingest_sources(id,segment_id,source_ref,generation,byte_offset) "
            "VALUES(?,?,?,0,0)",
            (source_id, segment_id, namespaced_ref),
        )
    else:
        source_id = source[0]
    db.execute(
        "INSERT INTO ingested_events(segment_id,event_id,source_id,seq,byte_offset,"
        "observed_at,content_sha256) VALUES(?,?,?,?,0,?,?)",
        # Event seq is 1-based by schema; the real snapshot_seq (including zero) is hashed above.
        (segment_id, event_id, source_id, max(1, seq), observed_at, digest),
    )
    db.execute(
        "INSERT INTO observation_clocks(segment_id,event_id,observed_at,game_seconds) "
        "VALUES(?,?,?,?)",
        (segment_id, event_id, observed_at, seconds),
    )

    previous = db.execute(
        "SELECT c.game_seconds FROM observation_clocks c JOIN runs r ON r.id=c.segment_id "
        "WHERE r.save_id=? AND c.game_seconds IS NOT NULL AND c.observed_at<? "
        "ORDER BY c.observed_at DESC,c.event_id DESC LIMIT 1",
        (save_id, observed_at),
    ).fetchone()
    rollback = seconds is not None and previous is not None and seconds < previous[0]
    inserted = 0
    for kind, value, observation in normalized:
        if (
            kind == "map_first_visit"
            and db.execute(
                "SELECT 1 FROM milestones m JOIN runs r ON r.id=m.segment_id "
                "WHERE r.save_id=? AND m.kind='map_first_visit' AND m.value=? "
                "AND m.wall_at<=? LIMIT 1",
                (save_id, value, observed_at),
            ).fetchone()
        ):
            continue
        identity = f"{segment_id}\0{save_id}\0{observation_id}\0{kind}\0{value}"
        row_id = hashlib.sha256(identity.encode()).hexdigest()
        db.execute(
            "INSERT INTO milestones(id,segment_id,save_id,kind,value,observation,"
            "game_seconds,wall_at,source_ref,source_event_id,observed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                row_id,
                segment_id,
                save_id,
                kind,
                value,
                "rollback" if rollback else observation,
                seconds,
                observed_at,
                source_ref,
                observation_id,
                observed_at,
            ),
        )
        inserted += 1
    return inserted
