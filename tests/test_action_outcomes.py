"""Synthetic offline checks for measured action outcomes; no provider or game input."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pokegent.actions._exec.models import ActionResult, ActionStatus
from pokegent.errors import ErrorCode
from pokegent.events import EventType, MemoryEventLog
from pokegent.loop import Runner
from pokegent.options.models import ChoiceResult, DecisionFrame, Legality, Option
from tools.action_outcomes import (
    MAX_LINE_BYTES,
    ReportError,
    build_report,
    main,
)

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "action_outcomes.py"
REPORT_KEYS = {
    "tool", "version", "sources", "scanned", "records", "no_records",
    "uncorrelated_terminals", "strata", "denominator",
    "verified_semantics", "accuracy", "story_completion", "durable_outcome",
    "eligibility", "note",
}
ROW_KEYS = {
    "run_id", "scene_id", "source", "provider", "model_id", "terminal_actions",
    "status", "verified", "progressed", "verified_progressed",
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


class AgentDecider:
    last_decision: dict = {}

    def __init__(self, choice="walk", *, provider="agent", model="m1"):
        self.choice = choice
        self.provider = provider
        self.model = model

    def decide(self, _frame):
        return ChoiceResult(
            option_id=self.choice,
            provider_id=self.provider,
            model_id=self.model,
        )


class FakeExecutor:
    actor = None
    last_emulator_frame = None
    perceiver = object()

    def __init__(self, results):
        self.results = list(results)

    def bind_perceiver(self, _perceiver):
        pass

    def execute(self, _frame, _option_id):
        return self.results.pop(0)


def real_runner(results, *, choice="walk", provider="agent", model="m1"):
    events = MemoryEventLog("run-real")
    runner = Runner(
        decider=AgentDecider(choice, provider=provider, model=model),
        executor=FakeExecutor(results),
        events=events,
        perceiver=object(),
    )
    return runner, events


def dump(events, path):
    path.write_text(
        "".join(
            json.dumps(event.model_dump(mode="json"), sort_keys=True) + "\n"
            for event in events.events
        ),
        encoding="utf-8",
    )


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


def envelope(event_type, *, run_id="run-a", request_id="dec-1", scene_id="field.free",
             provider=None, outcome=None, details=None, seq=1):
    return {
        "schema_rev": 1,
        "id": f"{run_id}:{seq}",
        "run_id": run_id,
        "seq": seq,
        "observed_at": "2026-01-01T00:00:00.000+00:00",
        "type": event_type,
        "scene_id": scene_id,
        "request_id": request_id,
        "provider": provider,
        "outcome": outcome,
        "source": "runner",
        "public_visibility": False,
        "details": details or {},
    }


def claim(*, run_id="run-a", request_id="dec-1", scene_id="field.free", option_id="walk",
          provider="agent", model="m1", outcome="accepted", accepted=None,
          authority=None, seq=1, event_type=EventType.DECISION_AGENT_COMPLETED.value):
    details = {
        "option_id": option_id,
        "model_id": model,
        "latency_ms": 1.0,
        "measured_ms": 1.0,
        "confidence": 0.9,
        "truncated": False,
    }
    if accepted is not None:
        details["accepted"] = accepted
    if authority is not None:
        details["authority"] = authority
    return envelope(event_type, run_id=run_id, request_id=request_id, scene_id=scene_id,
                    provider=provider, outcome=outcome, details=details, seq=seq)


def start(*, run_id="run-a", request_id="dec-1", scene_id="field.free", option_id="walk",
          source="decider", seq=1):
    return envelope(
        EventType.ACTION_STARTED.value,
        run_id=run_id,
        request_id=request_id,
        scene_id=scene_id,
        details={"option_id": option_id, "source": source, "executor_ref": "test.action"},
        seq=seq,
    )


def terminal(*, run_id="run-a", request_id="dec-1", scene_id="field.free", option_id="walk",
             status="applied", verified=True, progressed=True, error_code=None, seq=1,
             event_type=None):
    event_type = event_type or {
        "applied": EventType.ACTION_VERIFIED.value,
        "rejected": EventType.OPTION_REJECTED.value,
        "interrupted": EventType.ACTION_INTERRUPTED.value,
        "timed_out": EventType.ACTION_FAILED.value,
        "uncertain": EventType.ACTION_FAILED.value,
    }[status]
    return envelope(
        event_type,
        run_id=run_id,
        request_id=request_id,
        scene_id=scene_id,
        outcome=status,
        details={
            "option_id": option_id,
            "status": status,
            "error_code": error_code,
            "reason": "",
            "elapsed_frames": 0,
            "before_hash": None,
            "after_hash": None,
            "verified": verified,
            "progressed": progressed,
            "dialog_fingerprint": None,
            "dialog_text": [],
            "inputs": 0,
        },
        seq=seq,
    )


def test_real_runner_agent_decision_applied_verified_true_is_attributed(tmp_path):
    runner, events = real_runner([
        ActionResult(status=ActionStatus.APPLIED, verified_outcome=True, progressed=True),
    ])
    chosen = runner._decide(frame())
    assert isinstance(chosen, tuple)
    runner._act(frame(), "walk", source="decider")
    path = tmp_path / "events.jsonl"
    dump(events, path)

    report = report_of(run_cli(path))

    assert set(report) == REPORT_KEYS
    assert report["records"] == 1 and report["no_records"] is False
    assert report["uncorrelated_terminals"] == 0
    row = report["strata"][0]
    assert set(row) == ROW_KEYS
    assert (row["run_id"], row["scene_id"], row["source"], row["provider"],
            row["model_id"]) == ("run-real", "field.free", "decider", "agent", "m1")
    assert row["terminal_actions"] == 1
    assert row["status"] == {"applied": 1, "rejected": 0, "interrupted": 0,
                             "timed_out": 0, "uncertain": 0}
    assert row["verified"] == {"true": 1, "false": 0, "unknown": 0}
    assert row["progressed"] == {"true": 1, "false": 0, "unknown": 0}
    assert row["verified_progressed"] == 1
    assert report["denominator"] == "attributed_terminal_actions"
    assert report["accuracy"] is None and report["story_completion"] is None
    assert report["durable_outcome"] is None
    assert report["eligibility"] == "not_evaluated"


def test_real_scene_event_without_request_and_empty_model_are_unknown(tmp_path):
    runner, events = real_runner([
        ActionResult(status=ActionStatus.APPLIED, verified_outcome=True, progressed=True),
    ], model="")
    runner._emit(EventType.SCENE_ENTERED, scene_id="field.free")
    current = frame()
    assert isinstance(runner._decide(current), tuple)
    runner._act(current, "walk", source="decider")
    path = tmp_path / "events.jsonl"
    dump(events, path)

    report = report_of(run_cli(path))

    assert report["records"] == 1
    row = report["strata"][0]
    assert (row["provider"], row["model_id"]) == ("agent", None)
    assert row["verified"]["true"] == 1


def test_real_runner_event_name_is_not_verification_proof(tmp_path):
    runner, events = real_runner([
        ActionResult(status=ActionStatus.APPLIED, verified_outcome=True, progressed=True),
        ActionResult(status=ActionStatus.APPLIED, verified_outcome=False, progressed=True),
        ActionResult(status=ActionStatus.APPLIED, verified_outcome=None, progressed=None),
    ])
    for index in range(3):
        current = frame(decision_id=f"dec-{index}", fingerprint=str(index))
        assert isinstance(runner._decide(current), tuple)
        runner._act(current, "walk", source="decider")
    path = tmp_path / "events.jsonl"
    dump(events, path)

    report = report_of(run_cli(path))

    row = report["strata"][0]
    assert row["terminal_actions"] == 3
    assert row["status"]["applied"] == 3
    assert row["verified"] == {"true": 1, "false": 1, "unknown": 1}
    assert row["progressed"] == {"true": 2, "false": 0, "unknown": 1}
    assert row["verified_progressed"] == 1


def test_rejected_system1_then_accepted_route_attributes_only_route(tmp_path):
    runner, events = real_runner([
        ActionResult(status=ActionStatus.APPLIED, verified_outcome=True, progressed=True),
    ])
    current = frame(decision_id="dec-route")
    runner._emit(
        EventType.DECISION_SYSTEMONE_COMPLETED,
        scene_id=current.scene_id,
        request_id=current.decision_id,
        provider="system1",
        outcome="rejected",
        details={"option_id": "sign", "model_id": "s1-model", "vetoed_by": "route_step"},
    )
    runner._emit(
        EventType.DECISION_ROUTE,
        scene_id=current.scene_id,
        request_id=current.decision_id,
        provider="route",
        outcome="accepted",
        details={"option_id": "walk", "decided_by": "route",
                 "executor_ref": "test.action", "reason": "route_step_veto"},
    )
    runner._act(current, "walk", source="route")
    path = tmp_path / "events.jsonl"
    dump(events, path)

    report = report_of(run_cli(path))

    assert report["records"] == 1
    row = report["strata"][0]
    assert (row["source"], row["provider"], row["model_id"]) == ("route", "route", None)
    assert row["verified"]["true"] == 1


def test_shadow_claim_on_same_request_does_not_capture_agent_result(tmp_path):
    runner, events = real_runner([
        ActionResult(status=ActionStatus.APPLIED, verified_outcome=True, progressed=True),
    ])
    current = frame(decision_id="dec-shadow")
    assert isinstance(runner._decide(current), tuple)
    runner._emit(
        EventType.DECISION_SYSTEMONE_COMPLETED,
        scene_id=current.scene_id,
        request_id=current.decision_id,
        provider="system1",
        outcome="observed",
        details={
            "option_id": "sign", "model_id": "shadow-model", "provider_id": "system1",
            "confidence": 0.2, "measured_ms": 1.0, "accepted": False,
            "authority": "shadow", "agreement": True, "accuracy": "unknown",
            "agent_option_id": "walk",
        },
    )
    runner._act(current, "walk", source="decider")
    path = tmp_path / "events.jsonl"
    dump(events, path)

    report = report_of(run_cli(path))

    assert report["records"] == 1
    row = report["strata"][0]
    assert (row["provider"], row["model_id"]) == ("agent", "m1")


def test_real_runner_interrupted_rejected_and_failed_terminals(tmp_path):
    runner, events = real_runner([
        ActionResult(status=ActionStatus.INTERRUPTED,
                     error_code=ErrorCode.E_INTERRUPTED_SCENE, progressed=False),
        ActionResult.rejected(ErrorCode.E_STALE_DECISION, "stale"),
        ActionResult(status=ActionStatus.UNCERTAIN,
                     error_code=ErrorCode.E_POSTCONDITION_NOT_MET,
                     verified_outcome=False, progressed=False),
    ])
    for index in range(3):
        current = frame(decision_id=f"dec-{index}", fingerprint=str(index))
        assert isinstance(runner._decide(current), tuple)
        runner._act(current, "walk", source="decider")
    path = tmp_path / "events.jsonl"
    dump(events, path)

    report = report_of(run_cli(path))

    row = report["strata"][0]
    assert row["terminal_actions"] == 3
    assert row["status"] == {"applied": 0, "rejected": 1, "interrupted": 1,
                             "timed_out": 0, "uncertain": 1}
    assert row["verified"]["false"] == 1 and row["verified"]["unknown"] == 2
    assert {event.type for event in events.events} >= {
        EventType.ACTION_INTERRUPTED.value,
        EventType.OPTION_REJECTED.value,
        EventType.ACTION_FAILED.value,
    }


def test_terminal_without_accepted_claim_or_start_is_uncorrelated(tmp_path):
    path = tmp_path / "orphan.jsonl"
    raw(path, [terminal(seq=1), terminal(request_id="dec-2", seq=2)])

    report = report_of(run_cli(path))

    assert report["records"] == 0 and report["no_records"] is True
    assert report["strata"] == []
    assert report["uncorrelated_terminals"] == 2


def test_rejected_claim_with_start_and_terminal_is_uncorrelated(tmp_path):
    path = tmp_path / "rejected.jsonl"
    raw(path, [
        claim(outcome="rejected", event_type=EventType.DECISION_SYSTEMONE_COMPLETED.value,
              provider="system1", seq=1),
        start(seq=2),
        terminal(seq=3),
    ])

    report = report_of(run_cli(path))

    assert report["records"] == 0
    assert report["strata"] == []
    assert report["uncorrelated_terminals"] == 1


def test_option_mismatch_fails_without_echo(tmp_path):
    path = tmp_path / "mismatch.jsonl"
    raw(path, [
        claim(option_id="walk", seq=1),
        start(option_id="sign", seq=2),
        terminal(option_id="sign", seq=3),
    ])

    result = run_cli(path)

    assert result.returncode != 0 and result.stdout == ""
    assert "field option_id" in result.stderr
    assert "Traceback" not in result.stderr


def test_duplicate_terminal_fails(tmp_path):
    path = tmp_path / "duplicate.jsonl"
    raw(path, [
        claim(seq=1),
        start(seq=2),
        terminal(seq=3),
        terminal(seq=4),
    ])

    result = run_cli(path)

    assert result.returncode != 0 and result.stdout == ""
    assert "field request_id" in result.stderr
    assert "Traceback" not in result.stderr


def test_missing_provider_model_and_scene_stay_null_unknown(tmp_path):
    path = tmp_path / "unknown.jsonl"
    raw(path, [
        claim(provider=None, model=None, seq=1),
        start(scene_id=None, seq=2),
        terminal(scene_id=None, seq=3),
    ])

    report = report_of(run_cli(path))

    row = report["strata"][0]
    assert (row["scene_id"], row["provider"], row["model_id"]) == (None, None, None)
    assert row["terminal_actions"] == 1


def test_irrelevant_bad_values_are_ignored_and_relevant_bad_values_fail_without_echo(
        tmp_path):
    ignored = tmp_path / "ignored.jsonl"
    raw(ignored, [
        envelope("scene.entered", details={"verified": "yes", "progressed": float("nan"),
                                           "secret": "RAW_SECRET"}),
    ])
    report = report_of(run_cli(ignored))
    assert report["records"] == 0 and "RAW_SECRET" not in json.dumps(report)

    bad_verified = tmp_path / "bad-verified.jsonl"
    raw(bad_verified, [claim(seq=1), start(seq=2), terminal(verified=1, seq=3)])
    result = run_cli(bad_verified)
    assert result.returncode != 0 and result.stdout == ""
    assert "field verified" in result.stderr and "Traceback" not in result.stderr

    bad_accepted = tmp_path / "bad-accepted.jsonl"
    raw(bad_accepted, [claim(accepted="no", seq=1)])
    result = run_cli(bad_accepted)
    assert result.returncode != 0 and result.stdout == ""
    assert "field accepted" in result.stderr

    nan_progress = tmp_path / "bad-progress.jsonl"
    raw(nan_progress, [claim(seq=1), start(seq=2),
                       terminal(progressed=float("nan"), seq=3)])
    result = run_cli(nan_progress)
    assert result.returncode != 0 and result.stdout == ""
    assert "field progressed" in result.stderr
    assert "nan" not in result.stderr.lower()


def test_malformed_and_deep_json_name_only_path_and_line(tmp_path):
    torn = tmp_path / "torn.jsonl"
    torn.write_text('{"details": {"secret": "TORN_SECRET"}\n', encoding="utf-8")
    result = run_cli(torn)
    assert result.returncode != 0 and result.stdout == ""
    assert f"{torn}:1:" in result.stderr and "malformed JSON" in result.stderr
    assert "TORN_SECRET" not in result.stderr

    deep = tmp_path / "deep.jsonl"
    deep.write_bytes(b'{"x":' + b"[" * 5000 + b"]" * 5000 + b"}\n")
    result = run_cli(deep)
    assert result.returncode != 0 and result.stdout == ""
    assert f"{deep}:1:" in result.stderr and "malformed JSON" in result.stderr


def test_record_pending_strata_and_line_bounds_fail(tmp_path):
    many = tmp_path / "many.jsonl"
    raw(many, [envelope("scene.entered", seq=i) for i in range(3)])
    with pytest.raises(ReportError, match="record limit"):
        build_report([many], max_records=2)

    pending = tmp_path / "pending.jsonl"
    raw(pending, [claim(request_id=f"dec-{i}", seq=i) for i in range(3)])
    with pytest.raises(ReportError, match="pending limit"):
        build_report([pending], max_pending=2)

    strata = tmp_path / "strata.jsonl"
    records = []
    for i in range(3):
        records.extend([
            claim(scene_id=f"scene-{i}", request_id=f"dec-{i}", seq=3 * i + 1),
            start(scene_id=f"scene-{i}", request_id=f"dec-{i}", seq=3 * i + 2),
            terminal(scene_id=f"scene-{i}", request_id=f"dec-{i}", seq=3 * i + 3),
        ])
    raw(strata, records)
    with pytest.raises(ReportError, match="strata limit"):
        build_report([strata], max_strata=2)

    long = tmp_path / "long.jsonl"
    long.write_bytes(b"x" * (MAX_LINE_BYTES + 1) + b"\n")
    result = run_cli(long)
    assert result.returncode != 0 and result.stdout == ""
    assert "line exceeds" in result.stderr


def test_output_allowlist_privacy_and_input_untouched(tmp_path):
    path = tmp_path / "private.jsonl"
    event = terminal(seq=3)
    event["details"].update(reason="ERROR_SECRET", dialog_text=["RAW_SECRET"],
                            before_hash="HASH_SECRET", executor_ref="EXEC_SECRET",
                            option_id="walk")
    raw(path, [claim(seq=1), start(seq=2), event])
    raw_bytes = path.read_bytes()
    before = sorted(p.name for p in tmp_path.iterdir())

    report = report_of(run_cli(path))

    assert set(report) == REPORT_KEYS
    assert all(set(row) == ROW_KEYS for row in report["strata"])
    serialized = json.dumps(report)
    for secret in ("ERROR_SECRET", "RAW_SECRET", "HASH_SECRET", "EXEC_SECRET"):
        assert secret not in serialized
    assert path.read_bytes() == raw_bytes
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_cli_no_records_deterministic_and_missing_file(tmp_path):
    plain = tmp_path / "plain.jsonl"
    raw(plain, [envelope("scene.entered")])
    first = report_of(run_cli(plain))
    second = report_of(run_cli(plain))
    assert first == second
    assert first["records"] == 0 and first["no_records"] is True
    assert first["strata"] == []

    missing = run_cli(tmp_path / "absent.jsonl")
    assert missing.returncode != 0 and missing.stdout == ""
    assert "absent.jsonl" in missing.stderr

    good = tmp_path / "good.jsonl"
    raw(good, [claim(seq=1), start(seq=2), terminal(seq=3)])
    assert main([str(good)]) == 0
