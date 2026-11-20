"""Synthetic unknown screens through the real perception, hybrid, runner and executor."""

from types import SimpleNamespace

import pytest

from addons.system_one.hybrid import HybridDecider
from pokegent.actions import Registries
from pokegent.actions._exec.executor import Executor
from pokegent.actions.unknown import UnknownBuilder, register_unknown_recovery
from pokegent.errors import ErrorCode
from pokegent.events import EventType, MemoryEventLog
from pokegent.games.gen3.firered.reader.schemas import (
    FlagSet,
    Health,
    Player,
    ReadError,
    StateSnapshot,
    VarTable,
)
from pokegent.games.gen3.firered.reader.ui_schemas import (
    FieldEvidence,
    MainEvidence,
    MenuEvidence,
    ScriptEvidence,
    TaskEvidence,
    TextEvidence,
    UiEvidence,
)
from pokegent.loop import EscalationKind, Runner, RunnerConfig, RunStatus
from pokegent.options.models import ChoiceResult, DeciderTimeout, to_system_one_payload
from pokegent.pipeline import Perceiver, PerceptionKind
from pokegent.state.scenes import FunctionDetector, PriorityClass, SceneResult
from pokegent.user_config import load_user_config


@pytest.fixture
def harness(monkeypatch):
    def build(*, enabled=True, hybrid=True, valid_ui=True, locked=False, choices=("tap:b",),
              invalid_after=None, progress_at=(), busy=False, stop_wait=False,
              no_progress_enabled=True, oscillate=False):
        state = SimpleNamespace(frame=0, seq=0, actions=0, money=0, cursor=0, valid=True)
        calls, offers, results = [], [], []
        events = MemoryEventLog("synthetic")

        class Source:
            def read_snapshot(self):
                state.seq += 1
                section = dict(valid=True, confidence=1)
                ui = UiEvidence(
                    valid=valid_ui, main=MainEvidence(**section, state=state.cursor),
                    tasks=TaskEvidence(**section), text=TextEvidence(**section),
                    script=ScriptEvidence(**section), menu=MenuEvidence(**section),
                    field=FieldEvidence(**section), controls_locked=locked,
                )
                return StateSnapshot.create(
                    rom_profile="synthetic", snapshot_seq=state.seq, emulator_frame=state.frame,
                    health=Health(valid=state.valid, read_errors=() if state.valid else (
                        ReadError(subsystem="test", code="corrupt", message="synthetic failure"),
                    )),
                    player=Player(name="Test", gender="male", tid=0, sid=0, map_group=0,
                                  map_num=0, map_layout_id=0, x=state.cursor, y=0,
                                  money=state.money, coins=0) if state.valid else None,
                    flags=FlagSet() if state.valid else None,
                    vars=VarTable() if state.valid else None,
                ).with_ui(ui)

        class Actor:
            def advance_frames(self, frames):
                state.frame += frames
                return frames

            def tap(self, buttons, hold_frames=2, wait_frames=1):
                state.actions += 1
                state.frame += hold_frames + wait_frames
                if state.actions in progress_at:
                    state.money += 1
                if invalid_after == state.actions:
                    state.valid = False
                if oscillate:
                    state.cursor = 1 - state.cursor
                return hold_frames

            def release_all(self):
                pass

        class Bridge:
            port = 19760

            def __init__(self, *args, **kwargs):
                pass

            def bind_agent_request(self, callback):
                self.callback = callback

            def offer(self, frame, **kwargs):
                offers.append((frame, kwargs))
                self.callback("synthetic-frame", frame)
                if stop_wait:
                    raise DeciderTimeout("interrupted")
                return ChoiceResult(option_id=choices[(len(offers) - 1) % len(choices)],
                                    provider_id="agent")

            def record_action_result(self, frame, result, **kwargs):
                results.append(result)

            def update_briefing(self, *args, **kwargs):
                pass

            def bind_pending_frame(self, refresh):
                pass

            def take_decided_frame(self):
                return None

            def clear_autoplay_pending(self):
                pass

        monkeypatch.setattr("addons.system_one.hybrid.ExternalDecider", Bridge)
        system1 = SimpleNamespace(
            decide=lambda frame: calls.append(frame) or ChoiceResult(option_id="wait:30"),
        )
        decider = HybridDecider(
            system1, port=19760, external_timeout=1, events=events,
            escalate_unknown_scene=enabled, escalate_on_no_progress=no_progress_enabled,
        ) if hybrid else system1
        registries = Registries.new()
        if hybrid and enabled:
            register_unknown_recovery(registries)
        if busy:
            registries.detectors.register(FunctionDetector(
                "busy-unknown", PriorityClass.UNKNOWN,
                lambda s: SceneResult(scene_id="scene.unknown", confidence=1, busy=True),
            ))
        perceiver = Perceiver(Source(), registries.detectors, registries.builders,
                              allow_unknown_scene=hybrid and enabled)
        executor = Executor(Actor(), registries.executors, registries.verifiers, perceiver)
        runner = Runner(decider=decider, executor=executor, perceiver=perceiver, events=events,
                        config=RunnerConfig(max_steps=30, no_progress_limit=3,
                                            invalid_retries=0))
        return SimpleNamespace(runner=runner, decider=decider, executor=executor,
                               perceiver=perceiver, state=state, offers=offers,
                               results=results, calls=calls, events=events, registries=registries)
    return build


def test_unknown_offered_to_agent_with_bounded_real_execution(harness):
    h = harness()
    summary = h.runner.run()
    assert summary.status is RunStatus.SAFE_PAUSE
    assert summary.verdict == "no_progress"
    assert len(h.offers) == h.state.actions == 3
    assert not h.calls
    for frame, request in h.offers:
        assert frame.scene_id == "scene.unknown"
        assert set(frame.chooseable_ids()) == {
            "wait:30", "tap:b", "tap:up", "tap:down", "tap:left", "tap:right",
        }
        assert request["metadata"]["escalation_reason"] == "unknown_scene"
        assert request["wait_timeout"] is None and request["interruptible_wait"]
        assert "screenshot" in str(to_system_one_payload(frame))
    assert all(r.verified_outcome is True and r.progressed is False for r in h.results)
    assert all(r.elapsed_frames == 3 and len(r.input_trace) == 1 for r in h.results)
    assert len(h.events.of_type(EventType.DECISION_AGENT_COMPLETED)) == 3
    assert all(e.details["escalation_reason"] == "unknown_scene" for e in
               h.events.of_type(EventType.DECISION_AGENT_REQUESTED))
    assert not h.events.of_type(EventType.DECISION_SYSTEMONE_COMPLETED)


def test_stale_unknown_decisions_cannot_renew_agent_window(harness):
    from contextlib import contextmanager
    from threading import Event

    h = harness()
    h.executor.actor.realtime_idle_while_deciding = True

    @contextmanager
    def stale_wait(guard):
        changed = Event()
        changed.set()
        yield SimpleNamespace(changed=changed)

    h.executor.actor.decision_wait = stale_wait
    h.perceiver.source.decision_guard = lambda *args, **kwargs: None
    h.decider.external.discard_stale_decision = lambda: None
    summary = h.runner.run()
    assert summary.status is RunStatus.SAFE_PAUSE
    assert h.state.actions == 3
    assert summary.counters["stale_decisions"] == 9
    assert summary.counters["frozen_decisions"] == 3
    assert len(h.offers) == 12


@pytest.mark.parametrize("kwargs", [{"enabled": False}, {"hybrid": False}])
def test_disabled_and_other_modes_retain_escalation(harness, kwargs):
    h = harness(**kwargs)
    assert h.runner.run().status in {RunStatus.ESCALATED, RunStatus.SAFE_PAUSE}
    assert not h.offers and not h.state.actions
    assert h.events.of_type(EventType.ESCALATION_REQUESTED)[0].outcome == "unknown_scene"


@pytest.mark.parametrize("kwargs", [
    {"valid_ui": False}, {"locked": True}, {"no_progress_enabled": False},
])
def test_wait_only_and_independent_opt_in_remain_agent_bounded(harness, kwargs):
    h = harness(choices=("wait:30",), **kwargs)
    assert h.runner.run().status is RunStatus.SAFE_PAUSE
    assert len(h.offers) == len(h.results) == 3
    assert not h.state.actions
    assert all(r.verified_outcome is True and r.elapsed_frames == 30 for r in h.results)
    if kwargs.get("valid_ui") is False or kwargs.get("locked"):
        assert all(f.chooseable_ids() == ("wait:30",) for f, _ in h.offers)


def test_alternating_options_and_position_changes_cannot_renew_window(harness):
    h = harness(choices=("tap:left", "tap:right", "tap:b"), oscillate=True)
    assert h.runner.run().status is RunStatus.SAFE_PAUSE
    assert len(h.offers) == 3


def test_only_executor_proven_durable_progress_rearms_window(harness):
    h = harness(progress_at=(2,))
    assert h.runner.run().status is RunStatus.SAFE_PAUSE
    assert len(h.offers) == 5
    assert h.results[1].progressed is True


@pytest.mark.parametrize("progress", [False, True])
def test_recognized_scene_does_not_itself_rearm_agent_window(harness, progress):
    h = harness(progress_at=(1,) if progress else ())
    # Synthetic recognized scene: re-use the bounded test primitives to exercise routing.
    h.registries.builders.register_scenes(("dialog.end",), UnknownBuilder())
    h.registries.detectors.register(FunctionDetector(
        "recognized", PriorityClass.FIELD_DIALOG,
        lambda s: SceneResult(scene_id="dialog.end", confidence=1) if h.state.actions else None,
    ))
    assert h.runner.run().status is RunStatus.SAFE_PAUSE
    assert bool(h.calls) is progress
    if not progress:
        assert len(h.offers) == 3
        assert h.offers[1][1]["metadata"]["escalation_reason"] == "no_progress"


def test_invalid_ram_after_input_does_not_authorize_another_press(harness):
    h = harness(invalid_after=1)
    assert h.runner.run().status is RunStatus.SAFE_PAUSE
    assert len(h.offers) == h.state.actions == 1
    assert h.results[0].error_code is ErrorCode.E_SNAPSHOT_INVALID


def test_unsupported_state_during_recovery_still_pauses(harness):
    h = harness()
    h.registries.detectors.register(FunctionDetector(
        "unsupported", PriorityClass.SYSTEM,
        lambda s: SceneResult(scene_id="system.unsupported_rom", confidence=1)
        if h.state.actions else None,
    ))
    assert h.runner.run().status is RunStatus.SAFE_PAUSE
    assert len(h.offers) == h.state.actions == 1


def test_busy_unknown_never_offers_buttons(harness):
    h = harness(busy=True)
    assert h.perceiver.perceive("test").frame is None
    assert h.runner.run().status is RunStatus.SAFE_PAUSE
    assert not h.state.actions and not h.offers


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("settles_needed", [1, 2])
def test_brief_busy_unknown_settles_inside_recovery_window(
    harness, monkeypatch, enabled, settles_needed,
):
    h = harness(enabled=enabled, choices=("wait:30",))
    # Reuse synthetic primitives for a recognized scene, independently of the unknown opt-out.
    if not enabled:
        register_unknown_recovery(h.registries)
    h.registries.builders.register_scenes(("dialog.end",), UnknownBuilder())
    transition_end = settles_needed * h.runner.config.settle_frames
    h.registries.detectors.register(FunctionDetector(
        "transition", PriorityClass.FIELD_DIALOG,
        lambda s: SceneResult(scene_id="scene.unknown", confidence=1, busy=True)
        if h.state.frame < transition_end else SceneResult(scene_id="dialog.end", confidence=1),
    ))
    assert h.runner._escalate(
        EscalationKind.NO_PROGRESS, "dialog.end", ErrorCode.E_NO_PROGRESS, {},
    ) is None
    assert h.runner._hybrid_no_progress_active
    assert h.perceiver.perceive("transition").frame is None
    settles = []
    settle = h.executor.settle

    def record_settle(frames):
        settles.append(frames)
        return settle(frames)

    monkeypatch.setattr(h.executor, "settle", record_settle)
    summary = h.runner.run()
    assert summary.status is RunStatus.SAFE_PAUSE
    assert summary.verdict == "no_progress"
    assert settles == [h.runner.config.settle_frames] * settles_needed
    assert len(h.offers) == len(h.results) == h.runner.config.no_progress_limit
    assert all(frame.scene_id == "dialog.end" for frame, _ in h.offers)
    assert not h.state.actions
    assert not any(e.outcome == "unknown_scene" for e in
                   h.events.of_type(EventType.ESCALATION_REQUESTED))


@pytest.mark.parametrize("change, code", [
    ("cursor", ErrorCode.E_FINGERPRINT_CHANGED),
    ("money", ErrorCode.E_FINGERPRINT_CHANGED),
    ("valid", ErrorCode.E_SNAPSHOT_INVALID),
])
def test_stale_unknown_choice_rejected_without_input(harness, change, code):
    h = harness()
    frame = h.perceiver.perceive("test").frame
    assert frame is not None
    setattr(h.state, change, False if change == "valid" else 1)
    result = h.executor.execute(frame, "tap:b")
    assert result.error_code is code
    assert not result.input_trace and result.elapsed_frames == 0
    assert not h.state.actions


def test_scene_change_before_execution_rejects_unknown_choice(harness):
    h = harness()
    frame = h.perceiver.perceive("test").frame
    h.registries.detectors.register(FunctionDetector(
        "recognized", PriorityClass.FIELD_DIALOG,
        lambda s: SceneResult(scene_id="dialog.end", confidence=1),
    ))
    assert h.perceiver.perceive("fresh").kind is PerceptionKind.NO_BUILDER
    result = h.executor.execute(frame, "tap:b")
    assert result.error_code is ErrorCode.E_SCENE_CHANGED
    assert not result.input_trace and not h.state.actions


def test_unoffered_choice_and_agent_interruption_send_no_input(harness):
    h = harness(choices=("tap:a",))
    assert h.runner.run().status is RunStatus.SAFE_PAUSE
    assert not h.state.actions
    assert h.events.of_type(EventType.OPTION_REJECTED)
    h = harness(stop_wait=True)
    assert h.runner.run().status is RunStatus.INTERRUPTED
    assert not h.state.actions


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_unknown_config_requires_boolean(harness, tmp_path, value):
    path = tmp_path / "config.yaml"
    path.write_text("hybrid:\n  escalate_unknown_scene: " + repr(value) + "\n")
    with pytest.raises(ValueError, match="escalate_unknown_scene"):
        load_user_config(path, environ={})
    with pytest.raises(ValueError, match="escalate_unknown_scene"):
        harness(enabled=value)


def test_unknown_config_default(tmp_path):
    assert load_user_config(tmp_path / "missing", environ={})["hybrid"]["escalate_unknown_scene"]

