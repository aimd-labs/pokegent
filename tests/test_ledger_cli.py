import json

from pokegent import cli
from pokegent.ledger import open_ledger


def ledger_file(path):
    db = open_ledger(path)
    db.execute("INSERT INTO saves(id,source_ref) VALUES('a','opaque')")
    db.execute("INSERT INTO saves(id,source_ref) VALUES('b','opaque')")
    db.execute("INSERT INTO runs(id,run_id,save_id,source_ref) VALUES('ra','r','a','opaque')")
    db.execute(
        "INSERT INTO milestones(id,segment_id,save_id,kind,value,observation,source_ref) "
        "VALUES('m','ra','a','badge','unknown','achievement','opaque')"
    )
    db.execute("INSERT INTO incidents(id,segment_id,kind,source_ref) "
               "VALUES('i','ra','blackout','opaque')")
    db.commit()
    db.close()


def invoke(args, capsys):
    code = cli.main(["ledger", *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_query_commands_use_real_cli_and_scope_save(tmp_path, capsys):
    path = tmp_path / "ledger.sqlite3"
    ledger_file(path)
    code, out, err = invoke(["status", "--save", "a", "--db", str(path), "--json"], capsys)
    assert code == 0 and not err
    status = json.loads(out)
    assert status["save"]["id"] == "a"
    assert status["runs"][0]["wall_seconds"] is None
    assert status["milestones"][0]["observation"] == "achievement"

    code, out, err = invoke(["incidents", "--save", "b", "--db", str(path), "--json"], capsys)
    assert code == 0 and not err and json.loads(out) == []
    code, out, err = invoke(["milestones", "--save", "b", "--db", str(path), "--json"], capsys)
    assert code == 0 and not err and json.loads(out) == []
    code, out, err = invoke(["milestones", "--save", "a", "--db", str(path)], capsys)
    assert code == 0 and "Milestones for save a: 1" in out and not err


def test_live_home_default_missing_refusal_and_unknown_save(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("LIVE_HOME", str(home))
    code, _, err = invoke(["milestones", "--save", "a"], capsys)
    assert code == 2 and "ledger database not found" in err.lower()
    assert not home.exists()

    home.mkdir()
    ledger_file(home / "ledger.sqlite3")
    code, out, err = invoke(["status", "--save", "missing", "--json"], capsys)
    assert code == 2 and not out and "unknown save" in err
    assert (home / "ledger.sqlite3").is_file()


def test_sql_json_blob_null_and_denied_write(tmp_path, capsys):
    path = tmp_path / "ledger.sqlite3"
    ledger_file(path)
    code, out, err = invoke([
        "sql", "SELECT NULL AS n, 7 AS number, X'00FF' AS data", "--db", str(path), "--json"
    ], capsys)
    assert code == 0 and not err
    assert json.loads(out) == [{"n": None, "number": 7,
                                "data": {"type": "blob", "hex": "00ff"}}]
    before = path.read_bytes()
    code, out, err = invoke([
        "sql", "WITH x AS (SELECT 1) INSERT INTO saves(id,source_ref) SELECT 'x','x' FROM x",
        "--db", str(path),
    ], capsys)
    assert code == 2 and not out and "ERROR:" in err
    assert path.read_bytes() == before


def test_sql_default_requires_live_home_and_does_not_create(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("LIVE_HOME", raising=False)
    missing = tmp_path / "missing"
    code, _, err = invoke(["sql", "SELECT 1", "--live-home", str(missing)], capsys)
    assert code == 2 and "ledger database not found" in err.lower()
    assert not missing.exists()
