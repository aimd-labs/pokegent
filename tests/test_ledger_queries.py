import sqlite3
from contextlib import closing

import pytest

from pokegent.ledger import LedgerSchemaError, open_ledger
from pokegent.ledger_queries import incidents, milestones, open_readonly, query, status


def seeded(path):
    db = open_ledger(path)
    db.executemany(
        "INSERT INTO saves(id,source_ref) VALUES(?,?)", [("a", "opaque"), ("b", "opaque")]
    )
    db.executemany(
        "INSERT INTO runs(id,run_id,save_id,source_ref) VALUES(?,?,?,?)",
        [("ra", "same-run", "a", "opaque"), ("rb", "same-run", "b", "opaque")],
    )
    db.execute(
        "INSERT INTO milestones(id,segment_id,save_id,kind,value,observation,source_ref) "
        "VALUES('m','ra','a','badge','unknown','achievement','opaque')"
    )
    db.execute(
        "INSERT INTO incidents(id,segment_id,kind,source_ref) VALUES('i','ra','blackout','opaque')"
    )
    db.commit()
    db.close()


def test_scoped_queries_keep_unknowns_and_refs_inert(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    seeded(path)
    with closing(open_readonly(path)) as db:
        result = status(db, "a")
        assert result["runs"][0]["wall_seconds"] is None
        assert result["objectives"] == []
        assert result["milestones"][0]["observation"] == "achievement"
        assert incidents(db, "a")[0]["issue_url"] is None
        assert milestones(db, "a")[0]["source_ref"] == "opaque"
        assert incidents(db, "b") == []
        assert query(
            db, "WITH chosen AS (SELECT id FROM saves WHERE id=?) SELECT id FROM chosen", ("b",)
        ) == [{"id": "b"}]
        with pytest.raises(KeyError):
            status(db, "missing")


def test_absent_file_not_created_and_owned_schema_required(tmp_path):
    absent = tmp_path / "absent.db"
    with pytest.raises(FileNotFoundError):
        open_readonly(absent)
    assert not absent.exists()
    foreign = tmp_path / "foreign.db"
    sqlite3.connect(foreign).close()
    before = foreign.read_bytes()
    with pytest.raises(LedgerSchemaError):
        open_readonly(foreign)
    assert foreign.read_bytes() == before


@pytest.mark.parametrize("tamper", ["version", "catalog"])
def test_owned_but_future_or_tampered_catalog_refused_unchanged(tmp_path, tamper):
    path = tmp_path / "tampered.db"
    db = open_ledger(path)
    if tamper == "version":
        db.execute("PRAGMA user_version=99")
    else:
        db.execute("CREATE VIEW unexpected AS SELECT 1")
    db.close()
    before = path.read_bytes()
    with pytest.raises(LedgerSchemaError):
        open_readonly(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO saves(id,source_ref) VALUES('x','x')",
        "UPDATE saves SET label='x'",
        "DELETE FROM saves",
        "CREATE TABLE injected(x)",
        "WITH x AS (SELECT 1) INSERT INTO saves(id,source_ref) SELECT 'x','x' FROM x",
        "ATTACH DATABASE ':memory:' AS extra",
        "PRAGMA user_version=7",
        "SELECT load_extension('not-a-real-extension')",
        "SELECT writefile('/tmp/not-written','x')",
    ],
)
def test_sql_authorizer_refuses_mutations_and_file_functions(tmp_path, sql):
    path = tmp_path / "ledger.sqlite3"
    seeded(path)
    before = path.read_bytes()
    with closing(open_readonly(path)) as db, pytest.raises(sqlite3.DatabaseError):
        query(db, sql)
    assert path.read_bytes() == before


def test_query_parameters_and_row_limit(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    seeded(path)
    with closing(open_readonly(path)) as db:
        assert query(db, "SELECT id FROM saves WHERE id=?", ("a",)) == [{"id": "a"}]
        with pytest.raises(ValueError, match="row limit"):
            query(db, "SELECT id FROM saves ORDER BY id", max_rows=1)


def test_recursive_query_is_interrupted_and_connection_closes(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    seeded(path)
    db = open_readonly(path)
    with closing(db), pytest.raises(sqlite3.OperationalError, match="interrupted"):
        query(
            db,
            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n",
        )
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT randomblob(2147483647)",
        "SELECT printf('%1000000000s','x')",
        "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<20) "
        "SELECT group_concat(printf('%500000s','x')) FROM n",
    ],
)
def test_large_builtin_values_and_aggregates_are_bounded(tmp_path, sql):
    path = tmp_path / "ledger.sqlite3"
    seeded(path)
    with closing(open_readonly(path)) as db, pytest.raises(sqlite3.Error):
        query(db, sql)


def test_cumulative_result_bytes_are_bounded(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    seeded(path)
    sql = (
        "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100) "
        "SELECT hex(randomblob(200000)) FROM n"
    )
    with closing(open_readonly(path)) as db, pytest.raises(ValueError, match="byte limit"):
        query(db, sql)
