import json
import time
from datetime import datetime, timezone

from pokegent import cli
from pokegent.ledger import open_ledger


def test_real_cli_ingest_is_idempotent_and_keeps_queries(tmp_path, capsys):
    run = tmp_path / "run"
    run.mkdir()
    event = {
        "schema_rev": 1,
        "id": "real:1",
        "run_id": "real",
        "seq": 1,
        "observed_at": "2026-11-21T12:00:00+00:00",
        "type": "runtime.started",
        "source": "runner",
        "details": {},
    }
    (run / "events.jsonl").write_text(json.dumps(event) + "\n")
    db_path = tmp_path / "ledger.sqlite3"
    argv = [
        "ledger",
        "ingest",
        "--save",
        "save",
        "--run-dir",
        str(run),
        "--db",
        str(db_path),
        "--json",
    ]
    assert cli.main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["runs"][0]["complete_events_processed"] == 1
    assert cli.main(argv) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["runs"][0]["complete_events_processed"] == 0
    with open_ledger(db_path) as db:
        assert db.execute("SELECT count(*) FROM ingested_events").fetchone()[0] == 1
    assert cli.main(["ledger", "status", "--save", "save", "--db", str(db_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["save"]["id"] == "save"


def test_invalid_identity_does_not_create_database(tmp_path, capsys):
    run = tmp_path / "run"
    run.mkdir()
    (run / "events.jsonl").write_text('{"schema_rev":1}\n')
    db_path = tmp_path / "ledger.sqlite3"
    assert (
        cli.main(
            ["ledger", "ingest", "--save", "save", "--run-dir", str(run), "--db", str(db_path)]
        )
        == 2
    )
    assert not db_path.exists()
    assert "run_id" in capsys.readouterr().err


def _telemetry(run_id="real", *, updated_at=None, generated_at=None, snapshot_seq=4, badges=None):
    updated_at = time.time() if updated_at is None else updated_at
    generated_at = time.time() if generated_at is None else generated_at
    return {
        "generated_at": generated_at,
        "game": {"run_id": run_id},
        "run": {"run_id": run_id},
        "freshness": {"state": {"updated_at": updated_at, "ttl_s": 2.0}},
        "state": {
            "observation": {
                "snapshot_seq": snapshot_seq,
                "rom_profile": "firered_us_rev0",
                "game_seconds": 12,
                "badges": badges,
                "key_items": None,
                "hms": None,
                "map": None,
                "pokedex": None,
                "flags": None,
            }
        },
    }


def test_actual_telemetry_file_ingest_uses_stable_freshness_and_pairing(tmp_path, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    event = {
        "schema_rev": 1,
        "id": "real:1",
        "run_id": "real",
        "seq": 1,
        "observed_at": "2026-11-21T12:00:00+00:00",
        "type": "runtime.started",
        "source": "runner",
        "details": {},
    }
    (run_dir / "events.jsonl").write_text(json.dumps(event) + "\n")
    telemetry_path = tmp_path / "telemetry.json"
    stamp = time.time()
    telemetry_path.write_text(json.dumps(_telemetry(updated_at=stamp, badges=[1])))
    db_path = tmp_path / "ledger.sqlite3"
    argv = [
        "ledger",
        "ingest",
        "--save",
        "save",
        "--run-dir",
        str(run_dir),
        "--db",
        str(db_path),
        "--telemetry-file",
        str(telemetry_path),
        "--json",
    ]
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["runs"][0]["ram"]["status"] == "available"
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["runs"][0]["ram"]["status"] == "available"
    telemetry_path.write_text(json.dumps(_telemetry(updated_at=stamp, badges=[2])))
    assert cli.main(argv) == 2  # Same run/seq/source/freshness, different facts conflicts.
    assert "identity reused" in capsys.readouterr().err
    telemetry_path.write_text(json.dumps(_telemetry(run_id="other", updated_at=stamp)))
    assert cli.main(argv) == 0
    result = json.loads(capsys.readouterr().out)["runs"][0]["ram"]
    assert result["status"] == "unavailable" and "does not match" in result["reason"]
    telemetry_path.write_text(json.dumps({"run": {"run_id": "real"}, "state": {}}))
    assert cli.main(argv) == 0
    result = json.loads(capsys.readouterr().out)["runs"][0]["ram"]
    assert "typed state.observation" in result["reason"]
    with open_ledger(db_path) as db:
        assert db.execute("SELECT count(*) FROM observation_clocks").fetchone()[0] == 1
        assert db.execute(
            "SELECT kind,value,observation FROM milestones"
        ).fetchone() == ("badge", "1", "possession")


def test_unavailable_viewer_does_not_block_event_ingest(tmp_path, capsys, monkeypatch):
    import urllib.error

    import pokegent.ledger_ingest_cli as ingest_cli

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    event = {
        "schema_rev": 1,
        "id": "real:1",
        "run_id": "real",
        "seq": 1,
        "observed_at": "2026-11-21T12:00:00+00:00",
        "type": "runtime.started",
        "source": "runner",
        "details": {},
    }
    (run_dir / "events.jsonl").write_text(json.dumps(event) + "\n")
    monkeypatch.setattr(
        ingest_cli.urllib.request,
        "build_opener",
        lambda *_: type(
            "Opener",
            (),
            {
                "open": lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    urllib.error.URLError("offline")
                )
            },
        )(),
    )
    db_path = tmp_path / "ledger.sqlite3"
    assert (
        cli.main(
            [
                "ledger",
                "ingest",
                "--save",
                "save",
                "--run-dir",
                str(run_dir),
                "--db",
                str(db_path),
                "--viewer-port",
                "19700",
                "--json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)["runs"][0]
    assert result["complete_events_processed"] == 1
    assert result["ram"]["status"] == "unavailable"
    assert "URLError" in result["ram"]["reason"]


def test_historical_file_freshness_uses_capture_time_not_observation_time(tmp_path, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    event = {
        "schema_rev": 1, "id": "real:1", "run_id": "real", "seq": 1,
        "observed_at": "2026-11-21T12:00:00+00:00", "type": "runtime.started",
        "source": "runner", "details": {},
    }
    (run_dir / "events.jsonl").write_text(json.dumps(event) + "\n")
    capture = time.time() - 3600
    observed = capture - 1
    telemetry_path = tmp_path / "telemetry.json"
    telemetry_path.write_text(json.dumps(_telemetry(
        updated_at=observed, generated_at=capture, badges=[1]
    )))
    db_path = tmp_path / "ledger.sqlite3"
    argv = [
        "ledger", "ingest", "--save", "save", "--run-dir", str(run_dir),
        "--db", str(db_path), "--telemetry-file", str(telemetry_path), "--json",
    ]
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["runs"][0]["ram"]["status"] == "available"
    telemetry_path.write_text(json.dumps(_telemetry(
        updated_at=capture - 10, generated_at=capture, badges=[2]
    )))
    assert cli.main(argv) == 0
    assert "stale" in json.loads(capsys.readouterr().out)["runs"][0]["ram"]["reason"]
    with open_ledger(db_path) as db:
        at = db.execute("SELECT observed_at FROM observation_clocks").fetchone()[0]
        expected = datetime.fromtimestamp(observed, timezone.utc).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        assert at == expected
        assert db.execute("SELECT count(*) FROM observation_clocks").fetchone()[0] == 1
        assert db.execute("SELECT value FROM milestones").fetchone()[0] == "1"


def test_real_cli_partial_event_resumes_then_rewrite_fails_closed(tmp_path, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    event = {
        "schema_rev": 1, "id": "real:1", "run_id": "real", "seq": 1,
        "observed_at": "2026-11-21T12:00:00+00:00", "type": "runtime.started",
        "source": "runner", "details": {},
    }
    second = {**event, "id": "real:2", "seq": 2}
    first_bytes = (json.dumps(event) + "\n").encode()
    second_bytes = json.dumps(second).encode()
    log = run_dir / "events.jsonl"
    log.write_bytes(first_bytes + second_bytes[:-4])
    db_path = tmp_path / "ledger.sqlite3"
    argv = [
        "ledger", "ingest", "--save", "save", "--run-dir", str(run_dir),
        "--db", str(db_path), "--json",
    ]
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["runs"][0]["complete_events_processed"] == 1
    log.write_bytes(first_bytes + second_bytes + b"\n")
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["runs"][0]["complete_events_processed"] == 1
    changed = {**second, "source": "driver"}
    log.write_bytes(first_bytes + (json.dumps(changed) + "\n").encode())
    assert cli.main(argv) == 2
    error = capsys.readouterr().err
    assert any(word in error for word in ("cursor", "conflict", "prefix"))
    with open_ledger(db_path) as db:
        assert db.execute("SELECT count(*) FROM ingested_events").fetchone()[0] == 2


def test_mocked_viewer_transport_is_loopback_bounded_and_proxy_free(tmp_path, capsys, monkeypatch):
    from io import BytesIO
    from unittest.mock import Mock
    from urllib.request import ProxyHandler

    import pokegent.ledger_ingest_cli as ingest_cli

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    event = {
        "schema_rev": 1,
        "id": "real:1",
        "run_id": "real",
        "seq": 1,
        "observed_at": "2026-11-21T12:00:00+00:00",
        "type": "runtime.started",
        "source": "runner",
        "details": {},
    }
    (run_dir / "events.jsonl").write_text(json.dumps(event) + "\n")
    payload = json.dumps(_telemetry()).encode()
    response = BytesIO(payload)
    response.status = 200
    response.__enter__ = lambda: response
    response.__exit__ = lambda *_: None
    opener = Mock()
    opener.open.return_value = response
    build = Mock(return_value=opener)
    monkeypatch.setattr(ingest_cli.urllib.request, "build_opener", build)
    db_path = tmp_path / "ledger.sqlite3"
    assert (
        cli.main(
            [
                "ledger",
                "ingest",
                "--save",
                "save",
                "--run-dir",
                str(run_dir),
                "--db",
                str(db_path),
                "--viewer-port",
                "19700",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["runs"][0]["ram"]["status"] == "available"
    request = opener.open.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:19700/ops/telemetry"
    assert opener.open.call_args.kwargs["timeout"] == 2
    assert any(isinstance(h, ProxyHandler) and h.proxies == {} for h in build.call_args.args)
    assert "127.0.0.1" in request.full_url
