"""Synthetic ledger admission, identity and transaction contracts."""

import sqlite3
from contextlib import closing

import pytest

from pokegent import ledger


@pytest.fixture
def db(tmp_path):
    with closing(ledger.open_ledger(tmp_path / "ledger.sqlite3")) as connection:
        yield connection


def save(db, identity="save-a"):
    db.execute("INSERT INTO saves(id,source_ref) VALUES (?,?)", (identity, "synthetic"))


def run(db, identity="segment-a", save_id="save-a"):
    db.execute(
        "INSERT INTO runs(id,run_id,save_id,source_ref) VALUES (?,?,?,?)",
        (identity, "repeated-run", save_id, "synthetic"),
    )


def test_initialize_reopen_preserves_sentinel(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with closing(ledger.open_ledger(path)) as db:
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION
        with db:
            save(db)
    with closing(ledger.open_ledger(path)) as db:
        assert db.execute("SELECT id,label,created_at FROM saves").fetchone() == (
            "save-a", None, None,
        )


@pytest.mark.parametrize("setup", [
    "PRAGMA user_version=999",
    "PRAGMA application_id=123",
    "CREATE TABLE unrelated (value TEXT)",
    "CREATE VIEW unrelated AS SELECT 1",
])
def test_foreign_or_future_database_refused_without_mutation(tmp_path, setup):
    path = tmp_path / "foreign.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute(setup)
        db.commit()
    original = path.read_bytes()
    with pytest.raises(ledger.LedgerSchemaError):
        ledger.open_ledger(path)
    assert path.read_bytes() == original
    assert not path.with_name(path.name + "-wal").exists()


def test_repeated_run_ids_have_distinct_segment_identity(db):
    with db:
        save(db)
        save(db, "save-b")
        run(db)
        run(db, "segment-b", "save-b")
    assert db.execute("SELECT COUNT(*) FROM runs WHERE run_id='repeated-run'").fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError):
        run(db, "orphan", "absent")


def test_milestone_cannot_claim_other_save(db):
    with db:
        save(db)
        save(db, "save-b")
        run(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO milestones(id,segment_id,save_id,kind,value,observation,source_ref) "
            "VALUES ('m','segment-a','save-b','badge','synthetic','achievement','synthetic')"
        )


@pytest.mark.parametrize("statement", [
    "INSERT INTO saves(id,mode,source_ref) VALUES ('s','competitive','synthetic')",
    "INSERT INTO runs(id,run_id,save_id,wall_seconds,source_ref) "
    "VALUES ('r','r','save-a',-1,'synthetic')",
    "INSERT INTO incidents(id,segment_id,kind,source_ref) "
    "VALUES ('i','segment-a','unknown','synthetic')",
    "INSERT INTO battles(id,segment_id,turns,source_ref) "
    "VALUES ('b','segment-a',-1,'synthetic')",
    "INSERT INTO objectives(id,segment_id,revision,objective_id,done,source_ref) "
    "VALUES ('o','segment-a',1,'synthetic',2,'synthetic')",
])
def test_constraints(db, statement):
    with db:
        save(db)
        run(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(statement)


def source(db, identity="source-a", segment="segment-a", generation=0):
    db.execute(
        "INSERT INTO ingest_sources(id,segment_id,source_ref,generation) VALUES (?,?,?,?)",
        (identity, segment, "synthetic-events", generation),
    )


def event(db, source_id="source-a", segment="segment-a"):
    db.execute(
        "INSERT INTO ingested_events(segment_id,event_id,source_id,seq,byte_offset) "
        "VALUES (?,'repeated-run:1',?,1,0)", (segment, source_id),
    )


def test_event_identity_survives_rotation_and_is_segment_scoped(db):
    with db:
        save(db)
        run(db)
        run(db, "segment-b")
        source(db)
        source(db, "source-rotated", generation=1)
        source(db, "source-b", "segment-b")
        event(db)
        event(db, "source-b", "segment-b")
    with pytest.raises(sqlite3.IntegrityError):
        event(db, "source-rotated")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO ingested_events(segment_id,event_id,source_id,seq,byte_offset) "
            "VALUES ('segment-a','other:1','source-b',1,0)"
        )


def test_caller_transaction_rolls_back_event_and_cursor(db):
    with db:
        save(db)
        run(db)
        source(db)
    with pytest.raises(sqlite3.IntegrityError), db:
        event(db)
        db.execute("UPDATE ingest_sources SET byte_offset=80 WHERE id='source-a'")
        event(db)
    assert db.execute("SELECT COUNT(*) FROM ingested_events").fetchone()[0] == 0
    assert db.execute("SELECT byte_offset FROM ingest_sources").fetchone()[0] == 0


def test_append_migration_preserves_rows_and_failure_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite3"
    with closing(ledger.open_ledger(path)) as db:
        with db:
            save(db)
    original = ledger._MIGRATIONS
    monkeypatch.setattr(ledger, "_MIGRATIONS", original + (
        ("CREATE TABLE sentinel_extra (value TEXT)", "NOT VALID SQL"),
    ))
    monkeypatch.setattr(ledger, "SCHEMA_VERSION", len(original) + 1)
    with pytest.raises(sqlite3.OperationalError):
        ledger.open_ledger(path)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == len(original)
        assert db.execute("SELECT id FROM saves").fetchone()[0] == "save-a"
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name='sentinel_extra'"
        ).fetchall()
    monkeypatch.setattr(ledger, "_MIGRATIONS", original + (
        ("ALTER TABLE saves ADD COLUMN extra TEXT",),
    ))
    with closing(ledger.open_ledger(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == len(original) + 1
        assert db.execute("SELECT id,extra FROM saves").fetchone() == ("save-a", None)


def test_concurrent_initialize(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "ledger.sqlite3"

    def initialize(_):
        with closing(ledger.open_ledger(path)) as db:
            return db.execute("PRAGMA user_version").fetchone()[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(initialize, range(2))) == [ledger.SCHEMA_VERSION] * 2


def test_import_has_no_file_or_runtime_side_effects(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    environment = dict(os.environ, PYTHONPATH=str(Path(ledger.__file__).resolve().parents[1]))
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import pokegent.ledger; "
         "assert 'pokegent.app' not in sys.modules; "
         "assert 'pokegent.emulator' not in sys.modules"],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []


def test_checkpoint_reference_is_only_data(db, tmp_path):
    target = tmp_path / "never-open-this.state"
    with db:
        save(db)
        run(db)
        db.execute(
            "INSERT INTO checkpoints(id,segment_id,path_ref,source_ref) "
            "VALUES ('c','segment-a',?,'synthetic')", (str(target),),
        )
    assert not target.exists()
    assert db.execute("SELECT game_seconds,badges FROM checkpoints").fetchone() == (None, None)


@pytest.mark.parametrize("value", ["", " ", ":memory:", "file:ledger.sqlite3", "bad\x00name"])
def test_ambiguous_path_refused(value):
    with pytest.raises(ValueError):
        ledger.open_ledger(value)


def test_new_file_is_private_and_existing_foreign_file_is_unchanged(tmp_path):
    import stat

    path = tmp_path / "owned.sqlite3"
    with closing(ledger.open_ledger(path)):
        pass
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    foreign = tmp_path / "foreign.sqlite3"
    with closing(sqlite3.connect(foreign)) as db:
        db.execute("PRAGMA user_version=1")
        db.commit()
    foreign.chmod(0o640)
    original = foreign.read_bytes()
    with pytest.raises(ledger.LedgerSchemaError):
        ledger.open_ledger(foreign)
    assert foreign.read_bytes() == original
    assert stat.S_IMODE(foreign.stat().st_mode) == 0o640


@pytest.mark.parametrize("change", [
    "CREATE TRIGGER forged AFTER INSERT ON saves BEGIN DELETE FROM runs; END",
    "ALTER TABLE saves ADD COLUMN unreviewed TEXT",
    "PRAGMA application_id=0",
])
def test_changed_owned_catalog_or_owner_refused(tmp_path, change):
    path = tmp_path / "ledger.sqlite3"
    with closing(ledger.open_ledger(path)) as db:
        with db:
            save(db)
        db.execute(change)
        db.commit()
    original = path.read_bytes()
    with pytest.raises(ledger.LedgerSchemaError):
        ledger.open_ledger(path)
    assert path.read_bytes() == original


@pytest.mark.parametrize(("table", "column", "value"), [
    ("runs", "wall_seconds", "not-a-number"),
    ("runs", "wall_seconds", -0.5),
    ("battles", "turns", 1.5),
    ("battles", "turns", "unknown"),
    ("objectives", "accepted", "true"),
    ("objectives", "done", 0.5),
    ("objectives", "revision", "unknown"),
    ("ingest_sources", "generation", 0.5),
    ("ingest_sources", "generation", "unknown"),
    ("ingest_sources", "generation", -1),
    ("ingest_sources", "byte_offset", "unknown"),
    ("ingested_events", "seq", 1.5),
    ("ingested_events", "byte_offset", "unknown"),
    ("ingested_events", "content_sha256", "invalid"),
    ("ingested_events", "content_sha256", b"a" * 64),
])
def test_numeric_and_digest_constraints(db, table, column, value):
    with db:
        save(db)
        run(db)
        source(db)
        event(db)
        db.execute("INSERT INTO battles(id,segment_id,source_ref) "
                   "VALUES ('b','segment-a','synthetic')")
        db.execute("INSERT INTO objectives(id,segment_id,revision,objective_id,source_ref) "
                   "VALUES ('o','segment-a',1,'synthetic','synthetic')")
    # Identifiers come exclusively from the parametrization above, never external input.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(f"UPDATE {table} SET {column}=?", (value,))


def test_optional_facts_preserve_null_and_digest_is_admitted(db):
    with db:
        save(db)
        run(db)
        source(db)
        event(db)
        db.execute("UPDATE ingested_events SET content_sha256=?", ("a" * 64,))
        db.execute("INSERT INTO objectives(id,segment_id,revision,objective_id,source_ref) "
                   "VALUES ('o','segment-a',1,'synthetic','synthetic')")
    facts = db.execute("SELECT accepted,done,wall_seconds,game_seconds FROM objectives").fetchone()
    assert facts == (None, None, None, None)
    assert db.execute("SELECT content_sha256 FROM ingested_events").fetchone()[0] == "a" * 64


def test_future_owned_version_refused_without_mutation(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with closing(ledger.open_ledger(path)) as db:
        with db:
            save(db)
        db.execute("PRAGMA user_version=999")
        db.commit()
    original = path.read_bytes()
    with pytest.raises(ledger.LedgerSchemaError, match="schema version"):
        ledger.open_ledger(path)
    assert path.read_bytes() == original


def test_foreign_prior_version_cannot_be_migrated(tmp_path, monkeypatch):
    path = tmp_path / "foreign.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA user_version=1")
        db.commit()
    original = path.read_bytes()
    monkeypatch.setattr(ledger, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(ledger, "_MIGRATIONS", ledger._MIGRATIONS + (
        ("CREATE TABLE should_not_exist(value TEXT)",),
    ))
    with pytest.raises(ledger.LedgerSchemaError, match="not owned"):
        ledger.open_ledger(path)
    assert path.read_bytes() == original
