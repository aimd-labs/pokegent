"""Opt-in framed-choice validity: logged only, never gating or replacing a result.

A provider answer can have a legal ``option_id`` while its ``probabilities`` mention an option
that was never offered. ``outcome: ok`` then only means the model answered; the runner's
``check_choice`` would still reject the claim. With the default-off flag, the calibration record
marks that mismatch; without it the old record shape is untouched.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from addons.system_one.factory import build_decider
from addons.system_one.policy import CalibrationPolicy, SystemOneConfig
from addons.system_one.provider import SyncDecider
from addons.system_one.telemetry import CalibrationLog, DecisionTelemetry
from pokegent.options.models import (
    ChoiceResult,
    DecisionFrame,
    Legality,
    Option,
    check_choice,
)
from tools.calibration_report import ReportError, build_report

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "calibration_report.py"
FIELD = "framed_choice_valid"


def frame() -> DecisionFrame:
    return DecisionFrame(
        decision_id="dec-validity-1",
        run_id="synthetic",
        snapshot_seq=1,
        scene_id="field.free",
        scene_confidence=1,
        state_fingerprint="sha256:" + "0" * 64,
        options=tuple(
            Option(id=key, label=key, legality=Legality.LEGAL, executor_ref="test.action")
            for key in ("tap:a", "tap:b")
        ),
    )


class FakeProvider:
    """Answers one framed question; ``result`` may be a result or an exception to raise."""

    provider_id = "system1"
    provider_version = "fake"

    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def decide(self, *, state, question, request_id, timeout_s):
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    async def health(self):
        raise AssertionError("health is not part of this test")


def answer(option_id="tap:a", probabilities=None, decision_id="dec-validity-1"):
    return ChoiceResult(
        option_id=option_id,
        probabilities={"tap:a": 0.9} if probabilities is None else probabilities,
        confidence=0.9,
        model_id="model",
        provider_id="system1",
        decision_id=decision_id,
    )


def make_decider(path, result, *, enabled):
    provider = FakeProvider(result)
    decider = SyncDecider(
        provider,
        frame_choice_validity=enabled,
        telemetry=DecisionTelemetry(calibration=CalibrationLog(path)),
        timeout_s=2.0,
    )
    return decider, provider


def logged(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_hidden_probability_key_is_logged_invalid_but_not_rejected(tmp_path):
    f = frame()
    hidden = answer(probabilities={"tap:a": 0.8, "hidden": 0.2})
    assert check_choice(f, hidden) is not None  # the runner already rejects this claim

    decider, _ = make_decider(tmp_path / "on.jsonl", hidden, enabled=True)
    try:
        result = decider.decide(f)
    finally:
        decider.close()

    assert result.option_id == "tap:a"  # the decider does not replace the answer
    record = logged(tmp_path / "on.jsonl")[0]
    assert record["outcome"] == "ok"  # a response was produced; that is all this label means
    assert record[FIELD] is False


def test_legal_choice_and_probabilities_are_logged_valid(tmp_path):
    f = frame()
    decider, _ = make_decider(tmp_path / "on.jsonl", answer(), enabled=True)
    try:
        decider.decide(f)
    finally:
        decider.close()

    assert logged(tmp_path / "on.jsonl")[0][FIELD] is True


def test_provider_failure_logs_unknown_not_invalid(tmp_path):
    f = frame()
    decider, _ = make_decider(tmp_path / "on.jsonl", RuntimeError("synthetic"), enabled=True)
    try:
        with pytest.raises(Exception):
            decider.decide(f)
    finally:
        decider.close()

    record = logged(tmp_path / "on.jsonl")[0]
    assert record["outcome"] == "error"
    assert record[FIELD] is None


def test_flag_off_keeps_the_legacy_record_shape(tmp_path):
    f = frame()
    decider, _ = make_decider(
        tmp_path / "off.jsonl",
        answer(probabilities={"tap:a": 0.8, "hidden": 0.2}),
        enabled=False,
    )
    try:
        decider.decide(f)
    finally:
        decider.close()

    record = logged(tmp_path / "off.jsonl")[0]
    assert record["outcome"] == "ok"
    assert FIELD not in record


def test_agent_provider_uses_runner_allow_all_parity():
    from addons.system_one.questions import QuestionLimits, plan_decision

    f = frame().model_copy(
        update={
            "all_options": (
                Option(
                    id="tap:c",
                    label="tap:c",
                    legality=Legality.LEGAL,
                    executor_ref="test.action",
                ),
            )
        }
    )
    plan = plan_decision(f, QuestionLimits())
    agent = answer(option_id="tap:c", probabilities={"tap:c": 0.9})
    agent = agent.model_copy(update={"provider_id": "agent"})
    decider = SyncDecider(FakeProvider(agent), frame_choice_validity=True)
    try:
        assert decider._record(f, plan, [], agent, None, 0.0)[FIELD] is True
        external = agent.model_copy(update={"provider_id": "external"})
        assert decider._record(f, plan, [], external, None, 0.0)[FIELD] is True
        system1 = agent.model_copy(update={"provider_id": "system1"})
        assert decider._record(f, plan, [], system1, None, 0.0)[FIELD] is False
    finally:
        decider.close()


@pytest.mark.parametrize("raw", ["true", "yes", 1, 0, None])
def test_policy_flag_is_a_strict_bool(raw):
    with pytest.raises(ValidationError):
        CalibrationPolicy(frame_choice_validity=raw)


def test_policy_flag_is_off_by_default():
    assert CalibrationPolicy().frame_choice_validity is False
    assert CalibrationPolicy(frame_choice_validity=True).frame_choice_validity is True


def test_factory_forwards_the_flag_and_the_log_field(tmp_path):
    config = SystemOneConfig(
        enabled=False,
        calibration={"frame_choice_validity": True, "log_path": str(tmp_path / "factory.jsonl")},
    )
    decider = build_decider(config, provider=FakeProvider(answer()))
    try:
        assert decider.frame_choice_validity is True
        decider.decide(frame())
    finally:
        decider.close()

    assert logged(tmp_path / "factory.jsonl")[0][FIELD] is True


# -- offline report ---------------------------------------------------------------------------


def report_record(**overrides):
    base = {
        "decision_id": "d1",
        "run_id": "run-a",
        "snapshot_seq": 1,
        "scene_id": "route-1",
        "goal_id": "goal",
        "option_set_hash": "hash",
        "option_count": 2,
        "state": {"PLANTED_STATE": "STATE_SECRET"},
        "instructions": "PROMPT_SECRET",
        "criteria": {"a": "CRITERIA_SECRET"},
        "plan": {},
        "option_order": "frame",
        "sent_order": ["a", "b"],
        "stages": [],
        "choice": "a",
        "probabilities": {"a": 1.0},
        "confidence": 0.9,
        "model_id": "m1",
        "provider_id": "system1",
        "provider_version": "pv1",
        "prompt_schema_version": "ps1",
        "truncated": False,
        "state_reduced": False,
        "server_truncated": False,
        "usage_in": 1,
        "usage_out": 2,
        "attempts": 1,
        "gate": "disabled",
        "outcome": "ok",
        "error_code": None,
        "error": None,
        "measured_ms": 10.0,
    }
    base.update(overrides)
    return base


def write_log(path, records):
    log = CalibrationLog(path)
    try:
        for item in records:
            log.append(item)
    finally:
        log.close()


def run_cli(*args):
    return subprocess.run(
        [sys.executable, str(TOOL), *map(str, args)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )


def test_report_default_does_not_change_the_stratum_shape(tmp_path):
    path = tmp_path / "legacy.jsonl"
    write_log(path, [report_record()])

    report = build_report([path])

    assert "choice_validity" not in report["strata"][0]


def test_report_counts_valid_invalid_unknown_per_stratum(tmp_path):
    path = tmp_path / "validity.jsonl"
    write_log(path, [
        report_record(**{FIELD: True}),
        report_record(**{FIELD: False}),
        report_record(**{FIELD: None}),
        report_record(),  # old log: no field at all
        report_record(run_id="run-b", **{FIELD: True}),
    ])

    report = build_report([path], choice_validity=True)

    by_run = {stratum["run_id"]: stratum for stratum in report["strata"]}
    assert by_run["run-a"]["choice_validity"] == {"valid": 1, "invalid": 1, "unknown": 2}
    assert by_run["run-b"]["choice_validity"] == {"valid": 1, "invalid": 0, "unknown": 0}


def test_report_rejects_malformed_field_without_echoing_it(tmp_path):
    path = tmp_path / "bad.jsonl"
    write_log(path, [report_record(**{FIELD: "NOT_A_BOOL_SECRET"})])

    with pytest.raises(ReportError, match=FIELD) as raised:
        build_report([path], choice_validity=True)
    assert "NOT_A_BOOL_SECRET" not in str(raised.value)


def test_report_cli_switch_is_opt_in(tmp_path):
    path = tmp_path / "validity.jsonl"
    write_log(path, [report_record(**{FIELD: False})])

    legacy = run_cli(path)
    assert legacy.returncode == 0
    assert "choice_validity" not in json.loads(legacy.stdout)["strata"][0]

    enabled = run_cli("--choice-validity", path)
    assert enabled.returncode == 0
    counts = json.loads(enabled.stdout)["strata"][0]["choice_validity"]
    assert counts == {"valid": 0, "invalid": 1, "unknown": 0}
