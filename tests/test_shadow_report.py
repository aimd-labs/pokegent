"""Synthetic offline checks for the shadow observation report; no provider or game input."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from addons.system_one.hybrid import HybridDecider
from pokegent import server
from pokegent.events import EventType, MemoryEventLog
from pokegent.loop import Runner
from pokegent.options.models import ChoiceResult, DecisionFrame, Legality, Option
from tools.shadow_report import (
    MAX_LINE_BYTES,
    ReportError,
    build_report,
    main,
)

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "shadow_report.py"
REPORT_KEYS = {
    "tool", "version", "event_type", "authority", "sources", "scanned", "records",
    "no_records", "strata", "agreement_semantics", "action_acceptance", "accuracy",
    "verified_progress", "eligibility", "note",
}
STRATUM_KEYS = {
    "run_id", "scene_id", "provider", "model_id", "records", "attempts_observed",
    "attempts_failed", "agreement", "measured_ms",
}


def _fingerprint(digit="0"):
    return "sha256:" + digit * 64


def frame(*, decision_id="dec-1", fingerprint="0"):
    return DecisionFrame(
        decision_id=decision_id,
        run_id="synthetic",
        snapshot_seq=1,
        scene_id="field.free",
        scene_confidence=1,
        state_fingerprint=_fingerprint(fingerprint),
        options=tuple(
            Option(id=key, label=key, legality=Legality.LEGAL, executor_ref="test.action")
            for key in ("sign", "walk")
        ),
    )


class Bridge:
    """Stand in for the external agent transport; never opens a socket."""

    port = 19760

    def __init__(self, *, choice="walk"):
        self.choice = choice
        self.offers = []

    def bind_agent_request(self, callback):
        self.callback = callback

    def bind_pending_frame(self, refresh):
        self.refresh = refresh

    def bind_decision_valid(self, valid):
        self.valid = valid

    def offer(self, offered, **kwargs):
        self.offers.append((offered, kwargs))
        return ChoiceResult(
            option_id=self.choice,
            provider_id="agent",
            decision_id=offered.decision_id,
        )

    def take_decided_frame(self):
        return None

    def discard_stale_decision(self):
        pass


def hybrid(monkeypatch, events):
    bridge = Bridge()
    monkeypatch.setattr("addons.system_one.hybrid.ExternalDecider", lambda *a, **k: bridge)
    system1 = SimpleNamespace(
        decide=lambda _frame: ChoiceResult(
            option_id="sign",
            confidence=0.1,
            provider_id="system1",
            model_id="model-x",
        )
    )
    return (
        HybridDecider(system1, port=19760, external_timeout=1, events=events, shadow=True),
        bridge,
    )


def runner(decider, events):
    executor = SimpleNamespace(perceiver=object(), bind_perceiver=lambda _p: None)
    return Runner(decider=decider, executor=executor, events=events, perceiver=object())


@pytest.fixture
def no_sockets(monkeypatch):
    monkeypatch.setattr(
        server,
        "ThreadingHTTPServer",
        lambda *args: SimpleNamespace(
            server_address=("127.0.0.1", 19760),
            serve_forever=lambda: None,
            shutdown=lambda: None,
            server_close=lambda: None,
        ),
    )


def dump(events, path):
    path.write_text(
        "".join(
            json.dumps(event.model_dump(mode="json"), sort_keys=True) + "\n"
            for event in events.events
        ),
        encoding="utf-8",
    )


def shadow_event(**overrides):
    details = {
        "option_id": "sign",
        "model_id": "m1",
        "provider_id": "local",
        "confidence": 0.5,
        "measured_ms": 10.0,
        "accepted": False,
        "authority": "shadow",
        "agreement": False,
        "accuracy": "unknown",
        "agent_option_id": "walk",
    }
    details.update(overrides.pop("details", {}))
    base = {
        "schema_rev": 1,
        "id": "run-a:1",
        "run_id": "run-a",
        "seq": 1,
        "observed_at": "2026-01-01T00:00:00.000+00:00",
        "type": "decision.systemone.completed",
        "scene_id": "scene-1",
        "request_id": "dec-1",
        "provider": "local",
        "outcome": "observed",
        "source": "runner",
        "public_visibility": False,
    }
    base.update(overrides)
    base["details"] = details
    return base


def raw(path, records):
    path.write_text(
        "".join(json.dumps(record, allow_nan=True) + "\n" for record in records),
        encoding="utf-8",
    )


def run_cli(*paths):
    return subprocess.run(
        [sys.executable, str(TOOL), *map(str, paths)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )


def report_of(result):
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return json.loads(result.stdout)


def test_real_runner_shadow_event_is_consumed_without_acceptance(monkeypatch, no_sockets,
                                                                 tmp_path):
    events = MemoryEventLog("run-real")
    decider, _bridge = hybrid(monkeypatch, events)
    assert runner(decider, events)._decide(frame())[1] == "walk"
    path = tmp_path / "events.jsonl"
    dump(events, path)

    report = report_of(run_cli(path))

    assert report["records"] == 1 and report["scanned"] == 3
    row = report["strata"][0]
    assert set(row) == STRATUM_KEYS
    assert (row["run_id"], row["scene_id"], row["provider"], row["model_id"]) == (
        "run-real", "field.free", "system1", "model-x")
    assert (row["attempts_observed"], row["attempts_failed"]) == (1, 0)
    assert row["agreement"] == {"true": 0, "false": 1, "unknown": 0}
    assert row["measured_ms"]["count"] == 1 and row["measured_ms"]["p50"] >= 0
    assert report["accuracy"] is None
    assert report["verified_progress"] is None
    assert report["eligibility"] == "not_evaluated"
    assert report["action_acceptance"] == "not_inferred"
    assert "accepted" not in report


def test_mixed_strata_count_attempts_agreement_and_latency(tmp_path):
    path = tmp_path / "synthetic.jsonl"
    raw(path, [
        shadow_event(details={"agreement": True, "measured_ms": 10.0}),
        shadow_event(details={"agreement": False, "measured_ms": 20.0}),
        shadow_event(outcome="failed", details={"agreement": None, "measured_ms": None}),
        shadow_event(details={"agreement": None, "measured_ms": 30.0}),
        shadow_event(details={"agreement": True, "measured_ms": 40.0}),
        shadow_event(run_id="run-b", scene_id="scene-2", provider="remote",
                     details={"provider_id": "remote", "model_id": "m2",
                              "agreement": None, "measured_ms": None}),
    ])

    report = report_of(run_cli(path))

    assert (report["records"], report["scanned"]) == (6, 6)
    first = [row for row in report["strata"] if row["run_id"] == "run-a"][0]
    assert first["provider"] == "local" and first["model_id"] == "m1"
    assert (first["attempts_observed"], first["attempts_failed"]) == (4, 1)
    assert first["agreement"] == {"true": 2, "false": 1, "unknown": 2}
    assert first["measured_ms"] == {"count": 4, "missing": 1, "p50": 20.0, "p95": 40.0}
    second = [row for row in report["strata"] if row["run_id"] == "run-b"][0]
    assert second["measured_ms"] == {"count": 0, "missing": 1, "p50": None, "p95": None}
    assert report["accuracy"] is None and report["verified_progress"] is None
    assert report["eligibility"] == "not_evaluated"


def test_other_well_formed_events_are_ignored(tmp_path):
    path = tmp_path / "mixed.jsonl"
    raw(path, [
        shadow_event(outcome="accepted", details={"accepted": True, "authority": "runner"}),
        {"type": EventType.DECISION_AGENT_COMPLETED.value, "run_id": "run-a", "seq": 2,
         "details": {"authority": "shadow", "accepted": False}},
        {"type": "scene.entered", "run_id": "run-a", "seq": 3, "details": {}},
        shadow_event(seq=4),
    ])

    report = report_of(run_cli(path))

    assert report["records"] == 1
    assert report["scanned"] == 4
    assert report["strata"][0]["attempts_observed"] == 1


def test_missing_identity_time_and_agreement_stay_unknown(tmp_path):
    event = shadow_event(scene_id=None, provider=None)
    event["details"].pop("provider_id")
    event["details"].pop("model_id")
    event["details"].pop("agreement")
    event["details"].pop("measured_ms")
    path = tmp_path / "old.jsonl"
    raw(path, [event])

    report = report_of(run_cli(path))

    row = report["strata"][0]
    assert (row["scene_id"], row["provider"], row["model_id"]) == (None, None, None)
    assert row["agreement"] == {"true": 0, "false": 0, "unknown": 1}
    assert row["measured_ms"] == {"count": 0, "missing": 1, "p50": None, "p95": None}


def test_no_shadow_events_reports_nothing_observed(tmp_path):
    path = tmp_path / "plain.jsonl"
    raw(path, [
        {"type": EventType.DECISION_AGENT_COMPLETED.value, "run_id": "run-a", "seq": 1,
         "outcome": "accepted", "details": {}},
        {"type": EventType.DECISION_SYSTEMONE_COMPLETED.value, "run_id": "run-a", "seq": 2,
         "outcome": "accepted", "details": {"accepted": True, "authority": "runner"}},
    ])

    result = run_cli(path)
    report = report_of(result)

    assert report["records"] == 0 and report["no_records"] is True
    assert report["strata"] == []
    assert report["accuracy"] is None and report["verified_progress"] is None
    assert report["eligibility"] == "not_evaluated"


def test_malformed_json_names_only_path_and_line(tmp_path):
    path = tmp_path / "torn.jsonl"
    path.write_text('{"details": {"secret": "TORN_SECRET"}\n', encoding="utf-8")

    result = run_cli(path)

    assert result.returncode != 0 and result.stdout == ""
    assert f"{path}:1:" in result.stderr and "malformed JSON" in result.stderr
    assert "TORN_SECRET" not in result.stderr


@pytest.mark.parametrize(("mutate", "field"), [
    (lambda e: e["details"].update(measured_ms=float("nan")), "measured_ms"),
    (lambda e: e["details"].update(measured_ms=float("inf")), "measured_ms"),
    (lambda e: e["details"].update(measured_ms=-1.0), "measured_ms"),
    (lambda e: e["details"].update(measured_ms=True), "measured_ms"),
    (lambda e: e["details"].update(measured_ms=10 ** 400), "measured_ms"),
    (lambda e: e["details"].update(agreement="yes"), "agreement"),
    (lambda e: e["details"].update(accepted=True), "accepted"),
    (lambda e: e["details"].pop("accepted"), "accepted"),
    (lambda e: e.update(outcome="accepted"), "outcome"),
    (lambda e: e.pop("run_id"), "run_id"),
    (lambda e: e["details"].update(provider_id=5), "provider"),
    (lambda e: e["details"].update(model_id="x" * 257), "model_id"),
])
def test_invalid_relevant_fields_fail_without_echo(tmp_path, mutate, field):
    event = shadow_event()
    mutate(event)
    path = tmp_path / "bad.jsonl"
    raw(path, [event])

    result = run_cli(path)

    assert result.returncode != 0 and result.stdout == ""
    assert f"field {field}" in result.stderr
    assert "Traceback" not in result.stderr
    assert str(10 ** 400) not in result.stderr


def test_record_strata_and_line_bounds_fail(tmp_path):
    path = tmp_path / "many.jsonl"
    raw(path, [shadow_event(seq=i) for i in range(3)])
    with pytest.raises(ReportError, match="record limit"):
        build_report([path], max_records=2)

    other = tmp_path / "strata.jsonl"
    raw(other, [shadow_event(scene_id=f"scene-{i}") for i in range(3)])
    with pytest.raises(ReportError, match="strata limit"):
        build_report([other], max_strata=2)

    long = tmp_path / "long.jsonl"
    long.write_bytes(b"x" * (MAX_LINE_BYTES + 1) + b"\n")
    result = run_cli(long)
    assert result.returncode != 0 and result.stdout == ""
    assert "line exceeds" in result.stderr


def test_output_is_allowlisted_and_input_is_untouched(tmp_path):
    path = tmp_path / "synthetic.jsonl"
    raw(path, [shadow_event(details={"error": "ERROR_SECRET", "raw": "RAW_SECRET"})])
    raw_bytes = path.read_bytes()
    before = sorted(p.name for p in tmp_path.iterdir())

    report = report_of(run_cli(path))

    assert set(report) == REPORT_KEYS
    assert all(set(stratum) == STRATUM_KEYS for stratum in report["strata"])
    assert "ERROR_SECRET" not in json.dumps(report)
    assert "RAW_SECRET" not in json.dumps(report)
    assert path.read_bytes() == raw_bytes
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_missing_file_is_a_nonzero_error_without_stdout(tmp_path):
    result = run_cli(tmp_path / "absent.jsonl")

    assert result.returncode != 0 and result.stdout == ""
    assert "absent.jsonl" in result.stderr


def test_main_returns_zero_for_valid_and_nonzero_for_bad(tmp_path, capsys):
    good = tmp_path / "good.jsonl"
    raw(good, [shadow_event()])
    assert main([str(good)]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["records"] == 1 and out.err == ""

    bad = tmp_path / "bad.jsonl"
    bad.write_text("{\n", encoding="utf-8")
    assert main([str(bad)]) != 0
    out = capsys.readouterr()
    assert out.out == "" and "malformed JSON" in out.err
