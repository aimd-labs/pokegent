"""Run synthetic no-progress windows through the runner and real hybrid routing."""

from types import SimpleNamespace

import pytest

from addons.system_one.hybrid import HybridDecider
from pokegent.actions._exec.models import ActionResult
from pokegent.errors import ErrorCode
from pokegent.events import EventType, MemoryEventLog
from pokegent.games.gen3.firered.reader.schemas import (
    FlagSet,
    Health,
    Player,
    StateSnapshot,
    VarTable,
)
from pokegent.loop import (
    EscalationAction,
    EscalationKind,
    EscalationResponse,
    Runner,
    RunnerConfig,
    RunStatus,
)
from pokegent.options.models import ChoiceResult, DeciderTimeout, DecisionFrame, Legality, Option
from pokegent.pipeline import Perception, PerceptionKind
from pokegent.state.scenes import SceneResult
from pokegent.user_config import load_user_config


@pytest.fixture
def harness(monkeypatch):
    def build(*, hybrid=True, enabled=True, progress_at=(), semantic_at=(), alternate=False,
              scene="field.free", continue_sink=False, invalid_at=None, bad_choice=False,
              action_error=None, interrupt_agent=False, no_progress_limit=2,
              no_progress_value=None):
        events = MemoryEventLog("synthetic")
        calls, executions, requests = [], [], []
        state = SimpleNamespace(x=0, money=0)

        class Bridge:
            port = 19760

            def __init__(self, *args, **kwargs):
                pass

            def bind_agent_request(self, callback):
                self.callback = callback

            def offer(self, offered, **kwargs):
                requests.append(kwargs)
                if interrupt_agent:
                    raise DeciderTimeout("interrupted")
                self.callback("synthetic-frame", offered)
                option = ("outside" if bad_choice else
                          "walk" if alternate and len(requests) % 2 else "sign")
                return ChoiceResult(option_id=option, provider_id="agent")

            def record_action_result(self, *args, **kwargs):
                pass

            def bind_pending_frame(self, refresh):
                pass

            def update_briefing(self, *args):
                pass

            def take_decided_frame(self):
                return None

            def clear_autoplay_pending(self):
                pass

        monkeypatch.setattr("addons.system_one.hybrid.ExternalDecider", Bridge)
        system1 = SimpleNamespace(
            supervisor_control=True,
            decide=lambda f: calls.append(f) or ChoiceResult(
                option_id="sign", confidence=0.9, provider_id="system1"),
        )
        decider = HybridDecider(system1, port=19760, external_timeout=1, events=events,
                                escalate_on_no_progress=enabled) if hybrid else system1

        def perceive(decision_id, *args):
            seq = len(calls) + len(requests)
            snapshot = StateSnapshot.create(
                rom_profile="synthetic", snapshot_seq=seq, health=Health(valid=True),
                player=Player(name="Test", gender="male", tid=0, sid=0, map_group=0,
                              map_num=0, map_layout_id=0, x=state.x, y=0,
                              money=state.money, coins=0),
                flags=FlagSet(), vars=VarTable(),
            )
            f = DecisionFrame(
                decision_id=decision_id, run_id="synthetic", snapshot_seq=seq,
                scene_id=scene, scene_confidence=1,
                state_fingerprint="sha256:" + str((state.x + state.money) % 10) * 64,
                options=tuple(Option(id=key, label=key, legality=Legality.LEGAL,
                                     executor_ref="test.action") for key in ("sign", "walk")),
            )
            return Perception(PerceptionKind.PAUSE if invalid_at == len(executions) else
                              PerceptionKind.READY, snapshot,
                              SceneResult(scene_id=scene, confidence=1), f)

        def execute(f, option):
            executions.append(option)
            n = len(executions)
            if action_error is not None:
                return ActionResult(status="rejected", error_code=action_error, option_id=option)
            progress = n in progress_at
            if progress:
                state.money += 1
            if n in semantic_at:
                state.x = 1 - state.x
            return ActionResult(status="applied", option_id=option,
                                progressed=True if progress else no_progress_value,
                                verified_outcome=True)

        executor = SimpleNamespace(perceiver=True, last_emulator_frame=None, execute=execute)
        sink = SimpleNamespace(escalate=lambda r: EscalationResponse(EscalationAction.CONTINUE))
        runner = Runner(decider=decider, executor=executor, events=events,
                        perceiver=SimpleNamespace(perceive=perceive),
                        escalation=sink if continue_sink else None,
                        config=RunnerConfig(max_steps=30, no_progress_limit=no_progress_limit))
        return runner, calls, executions, requests, events
    return build


def test_hybrid_gets_one_agent_window_then_safe_pause(harness):
    runner, calls, actions, requests, events = harness()
    summary = runner.run()
    assert summary.status is RunStatus.SAFE_PAUSE
    assert summary.verdict == "no_progress"
    assert len(calls) == 2
    assert len(actions) == 4
    assert len(requests) == 2
    assert all(r["metadata"]["escalation_reason"] == "no_progress" for r in requests)
    assert all(r["interruptible_wait"] and r["wait_timeout"] is None for r in requests)
    agent_events = events.of_type(EventType.DECISION_AGENT_REQUESTED)
    assert len(agent_events) == 2
    assert all(e.details["escalation_reason"] == "no_progress" for e in agent_events)
    assert len(events.of_type(EventType.DECISION_AGENT_COMPLETED)) == 2


@pytest.mark.parametrize("kwargs", [{"hybrid": False}, {"enabled": False}])
def test_non_hybrid_and_disabled_keep_original_stop(harness, kwargs):
    runner, calls, actions, requests, _ = harness(**kwargs)
    assert runner.run().status is RunStatus.SAFE_PAUSE
    assert len(calls) == len(actions) == 2
    assert not requests


def test_verified_progress_rearms_and_returns_to_system1(harness):
    runner, calls, actions, requests, _ = harness(progress_at=(3,))
    summary = runner.run()
    assert summary.status is RunStatus.SAFE_PAUSE
    assert len(calls) == 4
    assert len(actions) == 7
    assert len(requests) == 3


@pytest.mark.parametrize("progressed", [False, None])
def test_position_oscillation_cannot_reopen_recovery(harness, progressed):
    # A verified walk changes the monitor signature, but proves no durable progress.
    # Repeating that movement used to close and reopen windows until max_steps.
    runner, calls, actions, requests, events = harness(
        semantic_at=range(3, 31), no_progress_value=progressed, continue_sink=True,
    )
    summary = runner.run()
    assert summary.status is RunStatus.SAFE_PAUSE
    assert summary.verdict == "no_progress"
    assert len(calls) == 2
    assert len(actions) == 4
    assert len(requests) == 2
    assert len(events.of_type(EventType.ESCALATION_REQUESTED)) == 2


def test_repeated_sign_uses_adaptive_gate_then_bounded_no_progress_window(harness):
    runner, calls, actions, requests, events = harness(
        no_progress_limit=5, no_progress_value=False,
    )
    summary = runner.run()
    assert summary.status is RunStatus.SAFE_PAUSE
    assert summary.verdict == "no_progress"
    assert actions == ["sign"] * 10
    assert len(calls) == 4
    reasons = [r["metadata"]["escalation_reason"] for r in requests]
    # After two further System One sign reads both gates are due. Recovery wins,
    # and its first agent result resets the adaptive streak for the rest of the window.
    assert reasons == ["repeated_option"] + ["no_progress"] * 5
    assert all(r["interruptible_wait"] and r["wait_timeout"] is None for r in requests)
    assert [e.details["escalation_reason"] for e in
            events.of_type(EventType.DECISION_AGENT_REQUESTED)] == reasons
    assert len(events.of_type(EventType.DECISION_AGENT_COMPLETED)) == len(requests)


def test_recovery_progress_notification_is_optional(harness):
    runner, _, _, _, _ = harness(hybrid=False)
    runner._hybrid_no_progress_active = True
    runner._finish_no_progress_recovery()
    assert not runner._hybrid_no_progress_active


def test_alternating_agent_options_still_bounded_outside_field_monitor(harness):
    runner, calls, actions, requests, _ = harness(alternate=True, scene="dialog.message")
    assert runner.run().status is RunStatus.SAFE_PAUSE
    assert len(calls) == 2
    assert len(actions) == 4
    assert len(requests) == 2


def test_custom_continue_sink_cannot_extend_second_window(harness):
    runner, _, actions, _, _ = harness(continue_sink=True)
    assert runner.run().status is RunStatus.SAFE_PAUSE
    assert len(actions) == 4


def test_invalid_state_during_recovery_still_pauses_without_input(harness):
    runner, _, actions, requests, _ = harness(invalid_at=3)
    assert runner.run().status is RunStatus.SAFE_PAUSE
    assert len(actions) == 3
    assert len(requests) == 1


def test_agent_choice_still_validated(harness):
    runner, _, actions, requests, events = harness(bad_choice=True)
    assert runner.run().status is RunStatus.SAFE_PAUSE
    assert len(actions) == 2
    assert requests
    assert events.of_type(EventType.OPTION_REJECTED)


def test_other_escalations_never_start_recovery(harness):
    runner, _, _, requests, _ = harness()
    assert runner._escalate(EscalationKind.DECIDER_FAILED, "field.free",
                            ErrorCode.E_DECIDER_FAILED, {}) is RunStatus.SAFE_PAUSE
    assert not runner._hybrid_no_progress_active
    assert not requests


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_no_progress_config_requires_boolean(tmp_path, harness, value):
    path = tmp_path / "config.yaml"
    path.write_text("hybrid:\n  escalate_on_no_progress: " + repr(value) + "\n")
    with pytest.raises(ValueError, match="escalate_on_no_progress"):
        load_user_config(path, environ={})
    with pytest.raises(ValueError, match="escalate_on_no_progress"):
        harness(enabled=value)


def test_no_progress_config_default(tmp_path):
    assert load_user_config(tmp_path / "missing", environ={})["hybrid"]["escalate_on_no_progress"]


@pytest.mark.parametrize("error", [ErrorCode.E_STALE_SNAPSHOT, ErrorCode.E_POSTCONDITION_NOT_MET])
def test_other_no_progress_detectors_get_only_one_recovery_window(harness, error):
    runner, _, actions, requests, _ = harness(action_error=error)
    assert runner.run().status is RunStatus.SAFE_PAUSE
    assert len(actions) == 4
    assert len(requests) == 2


def test_agent_wait_interruption_still_exits_without_another_action(harness):
    runner, _, actions, requests, _ = harness(interrupt_agent=True)
    assert runner.run().status is RunStatus.INTERRUPTED
    assert len(actions) == 2
    assert len(requests) == 1
