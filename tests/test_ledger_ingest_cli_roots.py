import json

from pokegent import cli


def _event(run_id, seq=1):
    return {
        "schema_rev": 1,
        "id": f"{run_id}:{seq}",
        "run_id": run_id,
        "seq": seq,
        "observed_at": "2026-11-21T12:00:00+00:00",
        "type": "runtime.started",
        "source": "runner",
        "details": {},
    }


def test_runs_root_skips_unknown_and_ingests_valid(tmp_path, capsys):
    root = tmp_path / "runs"
    root.mkdir()
    (root / "booting").mkdir()
    valid = root / "valid"
    valid.mkdir()
    (valid / "summary.json").write_text(json.dumps({"run_id": "valid-run"}))
    db_path = tmp_path / "ledger.sqlite3"
    assert (
        cli.main(
            [
                "ledger",
                "ingest",
                "--save",
                "save",
                "--runs-dir",
                str(root),
                "--db",
                str(db_path),
                "--json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert [r["run_id"] for r in result["runs"]] == ["valid-run"]
    assert result["skipped"][0]["status"] == "unknown"


def test_runs_root_accepts_more_than_one_hundred_valid_dirs(tmp_path, capsys):
    root = tmp_path / "runs"
    root.mkdir()
    for index in range(101):
        run = root / f"run-{index:03}"
        run.mkdir()
        (run / "summary.json").write_text(json.dumps({"run_id": f"run-{index}"}))
    db_path = tmp_path / "ledger.sqlite3"
    assert (
        cli.main(
            [
                "ledger",
                "ingest",
                "--save",
                "save",
                "--runs-dir",
                str(root),
                "--db",
                str(db_path),
                "--json",
            ]
        )
        == 0
    )
    assert len(json.loads(capsys.readouterr().out)["runs"]) == 101


def test_runs_root_isolates_identity_and_ingest_failures_with_rollback(tmp_path, capsys):
    root = tmp_path / "runs"
    root.mkdir()
    ingest_error = root / "01-ingest-error"
    ingest_error.mkdir()
    (ingest_error / "events.jsonl").write_text(
        json.dumps(_event("bad", 1)) + "\nnot-json\n"
    )
    good = root / "02-good"
    good.mkdir()
    (good / "events.jsonl").write_text(json.dumps(_event("good")) + "\n")
    identity_error = root / "03-identity-error"
    identity_error.mkdir()
    (identity_error / "events.jsonl").write_text('{"schema_rev":1}\n')
    db_path = tmp_path / "ledger.sqlite3"

    argv = [
        "ledger", "ingest", "--save", "save", "--runs-dir", str(root),
        "--db", str(db_path), "--json",
    ]
    assert cli.main(argv) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert [item["run_id"] for item in result["runs"]] == ["good"]
    failures = {item["directory"]: item["reason"] for item in result["failed"]}
    assert set(failures) == {str(ingest_error), str(identity_error)}
    assert "malformed complete event line" in failures[str(ingest_error)]
    assert "run_id" in failures[str(identity_error)]
    assert "FAILED" in captured.err

    from pokegent.ledger import open_ledger

    with open_ledger(db_path) as db:
        assert db.execute("SELECT run_id FROM runs").fetchall() == [("good",)]
        assert db.execute("SELECT count(*) FROM ingested_events").fetchone() == (1,)

    assert cli.main(argv) == 0
    rerun = json.loads(capsys.readouterr().out)
    assert rerun["runs"][0]["complete_events_processed"] == 0
    assert len(rerun["failed"]) == 2


def test_runs_root_all_attempts_failed_returns_nonzero(tmp_path, capsys):
    root = tmp_path / "runs"
    root.mkdir()
    malformed = root / "malformed"
    malformed.mkdir()
    (malformed / "events.jsonl").write_text('{"schema_rev":1}\n')
    db_path = tmp_path / "ledger.sqlite3"

    assert cli.main([
        "ledger", "ingest", "--save", "save", "--runs-dir", str(root),
        "--db", str(db_path), "--json",
    ]) == 2
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["runs"] == []
    assert result["failed"][0]["directory"] == str(malformed)
    assert "FAILED" in captured.err
    assert not db_path.exists()


def test_runs_root_all_ingestions_failed_returns_nonzero_and_rolls_back(tmp_path, capsys):
    root = tmp_path / "runs"
    root.mkdir()
    malformed = root / "malformed-after-identity"
    malformed.mkdir()
    (malformed / "events.jsonl").write_text(
        json.dumps(_event("bad")) + "\nnot-json\n"
    )
    db_path = tmp_path / "ledger.sqlite3"

    assert cli.main([
        "ledger", "ingest", "--save", "save", "--runs-dir", str(root),
        "--db", str(db_path), "--json",
    ]) == 2
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["runs"] == []
    assert result["failed"][0]["directory"] == str(malformed)
    assert "malformed complete event line" in result["failed"][0]["reason"]
    assert "FAILED" in captured.err

    from pokegent.ledger import open_ledger

    with open_ledger(db_path) as db:
        assert db.execute("SELECT count(*) FROM saves").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM ingested_events").fetchone() == (0,)


def test_symlinked_ledger_parent_is_rejected_without_creation(tmp_path, capsys):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    run = tmp_path / "run"
    run.mkdir()
    (run / "summary.json").write_text(json.dumps({"run_id": "run"}))
    target = linked / "ledger.sqlite3"
    assert (
        cli.main(
            [
                "ledger",
                "ingest",
                "--save",
                "save",
                "--run-dir",
                str(run),
                "--db",
                str(target),
            ]
        )
        == 2
    )
    assert not (real / "ledger.sqlite3").exists()
    assert "symlinked directory" in capsys.readouterr().err
