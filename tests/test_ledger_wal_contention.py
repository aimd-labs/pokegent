"""RED: concurrent ledger open must survive real WAL activation contention.

Held for parent/security PLAN-REVIEW; no production retry exists yet, so
test_concurrent_open_survives_real_wal_contention is expected to fail closed at
ledger.py:230 with an actual SQLITE_BUSY from two real SQLite connections.
"""

import sqlite3
import stat
import threading
import time
from contextlib import closing

import pytest

from pokegent import ledger

WAL = "PRAGMA journal_mode=WAL"


def _patch_connect(monkeypatch, original, path, connection_class):
    def connect(database, *args, **kwargs):
        if str(database) == str(path):
            kwargs.setdefault("factory", connection_class)
        return original(database, *args, **kwargs)

    monkeypatch.setattr(ledger.sqlite3, "connect", connect)


def test_concurrent_open_survives_real_wal_contention(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite3"
    a_committed, b_reserved, a_wal_done = (threading.Event() for _ in range(3))
    attempts, results = [], {}
    original_connect = sqlite3.connect
    original_migrate = ledger._migrate

    class Observed(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            label = threading.current_thread().name
            if label == "A" and statement == WAL:
                try:
                    cursor = super().execute(statement, *args, **kwargs)
                except sqlite3.OperationalError as exc:
                    attempts.append(getattr(exc, "sqlite_errorcode", None))
                    a_wal_done.set()
                    raise
                attempts.append("wal")
                return cursor
            cursor = super().execute(statement, *args, **kwargs)
            if label == "B" and statement == "BEGIN IMMEDIATE":
                b_reserved.set()
                assert a_wal_done.wait(2), "A never attempted WAL"
            return cursor

    def gated_migrate(db):
        label = threading.current_thread().name
        if label == "B":
            assert a_committed.wait(2), "A did not commit its migration"
        original_migrate(db)
        if label == "A":
            a_committed.set()
            assert b_reserved.wait(2), "B did not reserve after A's commit"

    def initialize(label):
        try:
            with closing(ledger.open_ledger(path)) as db:
                results[label] = (
                    db.execute("PRAGMA user_version").fetchone()[0],
                    db.execute("PRAGMA foreign_keys").fetchone()[0],
                    db.execute("PRAGMA journal_mode").fetchone()[0],
                    db.execute("PRAGMA busy_timeout").fetchone()[0],
                )
        except BaseException as exc:
            results[label] = exc

    workers = [threading.Thread(target=initialize, args=(name,), name=name, daemon=True)
               for name in ("A", "B")]
    _patch_connect(monkeypatch, original_connect, path, Observed)
    monkeypatch.setattr(ledger, "_migrate", gated_migrate)
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
        assert not [worker.name for worker in workers if worker.is_alive()]
    finally:
        for event in (a_committed, b_reserved, a_wal_done):
            event.set()
        for worker in workers:
            if worker.ident is not None:
                worker.join(5)
        monkeypatch.setattr(ledger.sqlite3, "connect", original_connect)
        monkeypatch.setattr(ledger, "_migrate", original_migrate)

    assert results.get("A") == (ledger.SCHEMA_VERSION, 1, "wal", 1000), results.get("A")
    assert results.get("B") == (ledger.SCHEMA_VERSION, 1, "wal", 1000), results.get("B")
    assert sqlite3.SQLITE_BUSY in attempts, attempts
    with closing(ledger.open_ledger(path)) as db:
        assert db.execute("PRAGMA application_id").fetchone()[0] == ledger.APPLICATION_ID
        assert db.execute("PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_exclusive_reservation_is_bounded_and_closed(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite3"
    path.touch()  # unowned empty file: real migration, then delete-mode WAL attempt
    migrated, held, release = (threading.Event() for _ in range(3))
    attempts, opened = [], []
    original_connect = sqlite3.connect
    original_migrate = ledger._migrate

    class Tracked(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            if statement == WAL:
                attempts.append(time.monotonic())
            return super().execute(statement, *args, **kwargs)

    def connect(database, *args, **kwargs):
        if str(database) == str(path):
            kwargs["factory"] = Tracked
            connection = original_connect(database, *args, **kwargs)
            opened.append(connection)
            return connection
        return original_connect(database, *args, **kwargs)

    def hold_writer():
        assert migrated.wait(2), "ledger migration was not reached"
        with closing(original_connect(str(path), timeout=1, isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE")
            held.set()
            assert release.wait(5), "exclusive reservation was not released"
            db.rollback()

    def gated_migrate(db):
        original_migrate(db)
        migrated.set()
        assert held.wait(2), "writer did not reserve after the migration"

    worker = threading.Thread(target=hold_writer, daemon=True)
    monkeypatch.setattr(ledger.sqlite3, "connect", connect)
    monkeypatch.setattr(ledger, "_migrate", gated_migrate)
    started = time.monotonic()
    try:
        worker.start()
        with pytest.raises(sqlite3.OperationalError) as info:
            ledger.open_ledger(path)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        worker.join(5)
        monkeypatch.setattr(ledger.sqlite3, "connect", original_connect)
        monkeypatch.setattr(ledger, "_migrate", original_migrate)
    assert not worker.is_alive()
    assert getattr(info.value, "sqlite_errorcode", None) == sqlite3.SQLITE_BUSY
    assert elapsed < 2, elapsed
    assert attempts, "no real WAL attempt observed"
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


def test_nonbusy_wal_error_propagates_without_retry(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite3"
    with closing(ledger.open_ledger(path)):
        pass
    original_connect = sqlite3.connect
    attempts = []

    class Refusing(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            if statement == WAL:
                attempts.append(statement)
                error = sqlite3.OperationalError("synthetic non-busy WAL failure")
                error.sqlite_errorcode = sqlite3.SQLITE_LOCKED
                error.sqlite_errorname = "SQLITE_LOCKED"
                raise error
            return super().execute(statement, *args, **kwargs)

    _patch_connect(monkeypatch, original_connect, path, Refusing)
    with pytest.raises(sqlite3.OperationalError) as info:
        ledger.open_ledger(path)
    assert getattr(info.value, "sqlite_errorcode", None) == sqlite3.SQLITE_LOCKED
    assert len(attempts) == 1, attempts


def test_foreign_schema_refused_before_wal(tmp_path):
    path = tmp_path / "foreign.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA user_version=999")
        db.commit()
    original = path.read_bytes()
    with pytest.raises(ledger.LedgerSchemaError, match="schema version"):
        ledger.open_ledger(path)
    assert path.read_bytes() == original
    assert not path.with_name(path.name + "-wal").exists()
