"""Explicit-path SQLite run ledger. No runtime hooks or ingestion.

Wall instants are caller-normalized UTC ISO-8601 text; durations/game clocks are
seconds. Unknown facts remain NULL. References are inert data, never loaded.
Callers supply stable save/segment IDs, own transactions, and close connections.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from os import PathLike

APPLICATION_ID = 0x50474C44  # PGLD

_MIGRATIONS = ((
    """CREATE TABLE saves (
        id TEXT NOT NULL PRIMARY KEY, label TEXT, rom_profile TEXT,
        mode TEXT CHECK(mode IN ('casual','speedrun')), created_at TEXT,
        source_ref TEXT NOT NULL, source_event_id TEXT, observed_at TEXT
    )""",
    """CREATE TABLE runs (
        id TEXT NOT NULL PRIMARY KEY, run_id TEXT NOT NULL,
        save_id TEXT NOT NULL REFERENCES saves(id), release TEXT,
        started_at TEXT, ended_at TEXT, verdict TEXT, exit_reason TEXT,
        wall_seconds REAL CHECK(wall_seconds IS NULL OR
            (typeof(wall_seconds) IN ('integer','real') AND wall_seconds>=0)),
        source_ref TEXT NOT NULL, source_event_id TEXT, observed_at TEXT,
        UNIQUE(id,save_id)
    )""",
    """CREATE TABLE checkpoints (
        id TEXT NOT NULL PRIMARY KEY, segment_id TEXT NOT NULL REFERENCES runs(id),
        path_ref TEXT NOT NULL, map TEXT, badges TEXT,
        game_seconds REAL CHECK(game_seconds IS NULL OR
            (typeof(game_seconds) IN ('integer','real') AND game_seconds>=0)),
        source_ref TEXT NOT NULL, source_event_id TEXT, observed_at TEXT
    )""",
    """CREATE TABLE milestones (
        id TEXT NOT NULL PRIMARY KEY, segment_id TEXT NOT NULL, save_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN
            ('badge','key_item','hm','story_flag','map_first_visit','pokedex_count')),
        value TEXT NOT NULL,
        observation TEXT NOT NULL CHECK(observation IN ('achievement','possession','rollback')),
        game_seconds REAL CHECK(game_seconds IS NULL OR
            (typeof(game_seconds) IN ('integer','real') AND game_seconds>=0)),
        wall_at TEXT, source_ref TEXT NOT NULL, source_event_id TEXT, observed_at TEXT,
        FOREIGN KEY(segment_id,save_id) REFERENCES runs(id,save_id)
    )""",
    """CREATE TABLE incidents (
        id TEXT NOT NULL PRIMARY KEY, segment_id TEXT NOT NULL REFERENCES runs(id),
        kind TEXT NOT NULL CHECK(kind IN
            ('busy_stuck','boot_hang','blackout','escalation','plan_budget_exceeded')),
        started_at TEXT, ended_at TEXT,
        lost_seconds REAL CHECK(lost_seconds IS NULL OR
            (typeof(lost_seconds) IN ('integer','real') AND lost_seconds>=0)),
        issue_url TEXT, source_ref TEXT NOT NULL, source_event_id TEXT, observed_at TEXT
    )""",
    """CREATE TABLE battles (
        id TEXT NOT NULL PRIMARY KEY, segment_id TEXT NOT NULL REFERENCES runs(id),
        opponent TEXT, encounter_type TEXT CHECK(encounter_type IN ('trainer','wild')),
        result TEXT, turns INTEGER CHECK(turns IS NULL OR (typeof(turns)='integer' AND turns>=0)),
        started_at TEXT, ended_at TEXT,
        source_ref TEXT NOT NULL, source_event_id TEXT, observed_at TEXT
    )""",
    """CREATE TABLE objectives (
        id TEXT NOT NULL PRIMARY KEY, segment_id TEXT NOT NULL REFERENCES runs(id),
        revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=1),
        objective_id TEXT NOT NULL, kind TEXT, set_by TEXT,
        accepted INTEGER CHECK(accepted IS NULL OR
            (typeof(accepted)='integer' AND accepted IN (0,1))),
        done INTEGER CHECK(done IS NULL OR (typeof(done)='integer' AND done IN (0,1))),
        accepted_at TEXT, done_at TEXT,
        wall_seconds REAL CHECK(wall_seconds IS NULL OR
            (typeof(wall_seconds) IN ('integer','real') AND wall_seconds>=0)),
        game_seconds REAL CHECK(game_seconds IS NULL OR
            (typeof(game_seconds) IN ('integer','real') AND game_seconds>=0)),
        source_ref TEXT NOT NULL, source_event_id TEXT, observed_at TEXT,
        UNIQUE(segment_id,revision,objective_id)
    )""",
    """CREATE TABLE ingest_sources (
        id TEXT NOT NULL PRIMARY KEY, segment_id TEXT NOT NULL REFERENCES runs(id),
        source_ref TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK(typeof(generation)='integer' AND generation>=0),
        byte_offset INTEGER NOT NULL DEFAULT 0
            CHECK(typeof(byte_offset)='integer' AND byte_offset>=0),
        UNIQUE(id,segment_id), UNIQUE(segment_id,source_ref,generation)
    )""",
    """CREATE TABLE ingested_events (
        segment_id TEXT NOT NULL REFERENCES runs(id), event_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        seq INTEGER NOT NULL CHECK(typeof(seq)='integer' AND seq>=1),
        byte_offset INTEGER NOT NULL CHECK(typeof(byte_offset)='integer' AND byte_offset>=0),
        observed_at TEXT, content_sha256 TEXT CHECK(content_sha256 IS NULL OR
            (typeof(content_sha256)='text' AND length(content_sha256)=64
             AND content_sha256 NOT GLOB '*[^0-9a-f]*')),
        PRIMARY KEY(segment_id,event_id),
        FOREIGN KEY(source_id,segment_id) REFERENCES ingest_sources(id,segment_id)
    )""",
), (
    """CREATE TABLE observation_clocks (
        segment_id TEXT NOT NULL, event_id TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        game_seconds INTEGER CHECK(game_seconds IS NULL OR
            (typeof(game_seconds)='integer' AND game_seconds>=0)),
        PRIMARY KEY(segment_id,event_id),
        FOREIGN KEY(segment_id,event_id)
            REFERENCES ingested_events(segment_id,event_id)
    )""",
), (
    """ALTER TABLE saves ADD COLUMN comparison_group TEXT CHECK(
        comparison_group IS NULL OR (typeof(comparison_group)='text'
        AND length(comparison_group) BETWEEN 1 AND 128
        AND comparison_group=trim(comparison_group)))""",
    """ALTER TABLE saves ADD COLUMN game_origin_seconds INTEGER CHECK(
        game_origin_seconds IS NULL OR (typeof(game_origin_seconds)='integer'
        AND game_origin_seconds BETWEEN 0 AND 3599999))""",
),)
SCHEMA_VERSION = len(_MIGRATIONS)


class LedgerSchemaError(ValueError):
    """Database ownership, version or catalog is unsupported; no automatic repair."""


def configure_comparison(
    db: sqlite3.Connection,
    save_id: str,
    *,
    comparison_group: str,
    game_origin_seconds: int,
) -> None:
    """Set explicit immutable split identity inside the caller's transaction."""
    if not db.in_transaction:
        raise ValueError("caller must start a transaction")
    if (
        not isinstance(comparison_group, str)
        or not comparison_group.strip()
        or len(comparison_group) > 128
    ):
        raise ValueError("comparison_group must be 1..128 non-whitespace characters")
    if comparison_group != comparison_group.strip():
        raise ValueError("comparison_group cannot have surrounding whitespace")
    if (
        isinstance(game_origin_seconds, bool)
        or not isinstance(game_origin_seconds, int)
        or not 0 <= game_origin_seconds <= 3599999
    ):
        raise ValueError("game_origin_seconds must be an integer from 0 through 3599999")
    row = db.execute(
        "SELECT mode,rom_profile,comparison_group,game_origin_seconds FROM saves WHERE id=?",
        (save_id,),
    ).fetchone()
    if row is None:
        raise KeyError(save_id)
    if (
        row[0] not in {"casual", "speedrun"}
        or not isinstance(row[1], str)
        or not row[1].strip()
    ):
        raise ValueError("save mode and ROM profile must be known")
    requested = (comparison_group, game_origin_seconds)
    if row[2:] == requested:
        return
    if row[2] is not None or row[3] is not None:
        raise ValueError("comparison identity is immutable")
    db.execute(
        "UPDATE saves SET comparison_group=?,game_origin_seconds=? WHERE id=?",
        (*requested, save_id),
    )


_CATALOG = """SELECT type,name,tbl_name,sql FROM sqlite_master
    WHERE name NOT GLOB 'sqlite_*' OR name GLOB 'sqlite_autoindex_*'
    ORDER BY type,name"""


def _migrate(db: sqlite3.Connection) -> None:
    db.execute("BEGIN IMMEDIATE")
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        owner = db.execute("PRAGMA application_id").fetchone()[0]
        catalog = db.execute(_CATALOG).fetchall()
        if version < 0 or version > SCHEMA_VERSION:
            raise LedgerSchemaError("unsupported ledger schema version")
        if version == 0:
            if owner != 0 or catalog:
                raise LedgerSchemaError("ledger requires an empty unowned version-0 database")
        elif owner != APPLICATION_ID:
            raise LedgerSchemaError("database is not owned by the run ledger")
        with closing(sqlite3.connect(":memory:")) as reference:
            for migration in _MIGRATIONS[:version]:
                for statement in migration:
                    reference.execute(statement)
            if catalog != reference.execute(_CATALOG).fetchall():
                raise LedgerSchemaError("unsupported ledger catalog")
        for next_version, migration in enumerate(_MIGRATIONS[version:], start=version + 1):
            for statement in migration:
                db.execute(statement)
            db.execute(f"PRAGMA user_version={next_version}")
        db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        db.commit()
    except BaseException:
        db.rollback()
        raise


def open_ledger(path: str | PathLike[str]) -> sqlite3.Connection:
    """Open/migrate an owned file, with WAL and FK enforcement, or fail closed.

    New files have mode 0600; existing files are never chmodded. Parent directories
    must exist. SQLite handles writer contention with a one-second busy timeout.
    Use ``with db:`` for atomic fact/event/cursor writes, then explicitly close db.
    No URI, in-memory database, inferred path, or automatic recovery is supported.
    """
    raw = os.fspath(path)
    if not raw.strip() or raw == ":memory:" or raw.startswith("file:") or "\x00" in raw:
        raise ValueError("ledger requires an explicit filesystem path")
    absolute = os.path.abspath(raw)
    try:
        fd = os.open(absolute, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    db = sqlite3.connect(absolute, timeout=1, isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        _migrate(db)
        if db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
            raise LedgerSchemaError("WAL unavailable for ledger database")
        db.isolation_level = ""  # Restore sqlite3's implicit transactions for `with db:`.
        return db
    except BaseException:
        db.close()
        raise
