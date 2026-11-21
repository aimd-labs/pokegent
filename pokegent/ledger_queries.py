"""Bounded, read-only queries over an existing run ledger."""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Mapping, Sequence
from contextlib import closing
from urllib.parse import quote

from pokegent import ledger

MAX_ROWS = 10_000
QUERY_SECONDS = 2.0
MAX_VALUE_BYTES = 8 * 1024 * 1024
MAX_RESULT_BYTES = 16 * 1024 * 1024
MAX_SQL_BYTES = 64 * 1024
_DENIED_FUNCTIONS = {"load_extension", "writefile", "readfile", "printf", "format"}


def open_readonly(path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Open an existing owned ledger read-only; callers must explicitly close it."""
    raw = os.fspath(path)
    if not raw.strip() or raw == ":memory:" or raw.startswith("file:") or "\x00" in raw:
        raise ValueError("ledger requires an explicit filesystem path")
    absolute = os.path.abspath(raw)
    if not os.path.isfile(absolute):
        raise FileNotFoundError(absolute)
    uri = "file:" + quote(absolute, safe="/") + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=1.0)
    try:
        db.enable_load_extension(False)
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
        db.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_BYTES)
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        owner = db.execute("PRAGMA application_id").fetchone()[0]
        if version != ledger.SCHEMA_VERSION or owner != ledger.APPLICATION_ID:
            raise ledger.LedgerSchemaError("unsupported run ledger ownership or version")
        with closing(sqlite3.connect(":memory:")) as reference:
            for migration in ledger._MIGRATIONS[:version]:
                for statement in migration:
                    reference.execute(statement)
            expected = reference.execute(ledger._CATALOG).fetchall()
        if db.execute(ledger._CATALOG).fetchall() != expected:
            raise ledger.LedgerSchemaError("unsupported ledger catalog")
        db.set_authorizer(_authorize)
        return db
    except BaseException:
        db.close()
        raise


def _authorize(
    action: int, arg1: str | None, arg2: str | None, _database: str | None, _source: str | None
) -> int:
    if action in (
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        getattr(sqlite3, "SQLITE_RECURSIVE", -1),
    ):
        if action == sqlite3.SQLITE_FUNCTION and (arg2 or arg1 or "").lower() in _DENIED_FUNCTIONS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def query(
    db: sqlite3.Connection,
    sql: str,
    parameters: Sequence[object] | Mapping[str, object] = (),
    *,
    max_rows: int = MAX_ROWS,
    timeout: float = QUERY_SECONDS,
) -> list[dict[str, object]]:
    """Run a parameterized read query with wall-time, row, SQL and byte bounds."""
    if not 1 <= max_rows <= MAX_ROWS or not 0 < timeout <= QUERY_SECONDS:
        raise ValueError("query bounds exceed the supported limits")
    deadline = time.monotonic() + timeout
    db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    cursor = db.cursor()
    try:
        cursor.execute(sql, parameters)
        names = [column[0] for column in cursor.description or ()]
        rows = []
        size = 0
        for _ in range(max_rows + 1):
            row = cursor.fetchone()
            if row is None:
                break
            if len(rows) == max_rows:
                raise ValueError("query returned more than the row limit")
            result = dict(zip(names, row))
            size += sum(
                len(value)
                if isinstance(value, bytes)
                else len(value.encode("utf-8"))
                if isinstance(value, str)
                else 8
                for value in row
            )
            if size > MAX_RESULT_BYTES:
                raise ValueError("query result exceeded the byte limit")
            rows.append(result)
        return rows
    finally:
        cursor.close()
        db.set_progress_handler(None, 0)


def status(db: sqlite3.Connection, save_id: str) -> dict[str, object]:
    """Return runs and objectives for one save; missing facts remain NULL."""
    saves = query(
        db, "SELECT id,label,rom_profile,mode,created_at FROM saves WHERE id=?", (save_id,)
    )
    if not saves:
        raise KeyError(save_id)
    runs = query(
        db,
        "SELECT id,run_id,release,started_at,ended_at,verdict,exit_reason,wall_seconds "
        "FROM runs WHERE save_id=? ORDER BY started_at,id",
        (save_id,),
    )
    objectives = query(
        db,
        "SELECT o.segment_id,o.revision,o.objective_id,o.kind,o.set_by,o.accepted,"
        "o.done,o.accepted_at,o.done_at,o.wall_seconds,o.game_seconds "
        "FROM objectives o JOIN runs r ON r.id=o.segment_id "
        "WHERE r.save_id=? ORDER BY r.id,o.revision,o.objective_id",
        (save_id,),
    )
    history = milestones(db, save_id)
    return {"save": saves[0], "runs": runs, "objectives": objectives, "milestones": history}


def incidents(db: sqlite3.Connection, save_id: str) -> list[dict[str, object]]:
    return query(
        db,
        "SELECT i.id,i.segment_id,i.kind,i.started_at,i.ended_at,i.lost_seconds,"
        "i.issue_url,i.source_ref,i.source_event_id,i.observed_at "
        "FROM incidents i JOIN runs r ON r.id=i.segment_id "
        "WHERE r.save_id=? ORDER BY i.started_at,i.id",
        (save_id,),
    )


def milestones(db: sqlite3.Connection, save_id: str) -> list[dict[str, object]]:
    return query(
        db,
        "SELECT id,segment_id,kind,value,observation,game_seconds,wall_at,source_ref,"
        "source_event_id,observed_at FROM milestones WHERE save_id=? "
        "ORDER BY wall_at,id",
        (save_id,),
    )
