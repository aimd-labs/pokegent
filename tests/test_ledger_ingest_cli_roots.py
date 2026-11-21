import json

from pokegent import cli


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
