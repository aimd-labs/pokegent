import json

from pokegent import ledger
from pokegent.cli import main
from pokegent.ledger_observations import ingest_observation


def test_cli_baseline_and_splits_use_real_observations(tmp_path, capsys):
    path = tmp_path / "ledger.sqlite3"
    db = ledger.open_ledger(path)
    for save, segment, seconds in (("a", "ra", 100), ("b", "rb", 120)):
        db.execute(
            "INSERT INTO saves(id,mode,rom_profile,source_ref) VALUES(?, 'casual','fr','test')",
            (save,),
        )
        db.execute(
            "INSERT INTO runs(id,run_id,save_id,source_ref) VALUES(?,?,?,'test')",
            (segment, segment, save),
        )
        ingest_observation(
            db,
            save_id=save,
            segment_id=segment,
            observation_id="obs-" + save,
            observed_at="2026-01-01T00:00:00Z",
            source_ref="test",
            payload={
                "snapshot_seq": 1,
                "rom_profile": "fr",
                "game_seconds": seconds,
                "milestones": [["badge", "1", "achievement"]],
            },
        )
    db.commit()
    db.close()
    for save in ("a", "b"):
        assert (
            main(
                [
                    "ledger",
                    "baseline",
                    "--save",
                    save,
                    "--group",
                    "same",
                    "--game-origin-seconds",
                    "0",
                    "--db",
                    str(path),
                ]
            )
            == 0
        )
    capsys.readouterr()
    assert main(["ledger", "splits", "--save", "a", "--db", str(path), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["milestones"][0]["delta_seconds"] == -20
    assert (
        main(
            [
                "ledger",
                "baseline",
                "--save",
                "a",
                "--group",
                "changed",
                "--game-origin-seconds",
                "0",
                "--db",
                str(path),
            ]
        )
        == 2
    )


def test_baseline_does_not_create_missing_database(tmp_path, capsys):
    path = tmp_path / "missing.sqlite3"
    assert (
        main(
            [
                "ledger",
                "baseline",
                "--save",
                "x",
                "--group",
                "g",
                "--game-origin-seconds",
                "0",
                "--db",
                str(path),
            ]
        )
        == 2
    )
    assert not path.exists()
