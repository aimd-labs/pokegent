"""Synthetic event sequences through the viewer's operator and public projections."""

import pytest

from pokegent.dashboard.public import public_telemetry
from pokegent.dashboard.state import ViewerState
from pokegent.dashboard.telemetry import build_telemetry
from pokegent.events import EventType, MemoryEventLog
from pokegent.options.models import ChoiceResult, DecisionFrame, Legality, Option


@pytest.fixture
def frame():
    return DecisionFrame(
        decision_id="synthetic-choice",
        run_id="synthetic-run",
        snapshot_seq=1,
        scene_id="field.free",
        scene_confidence=1,
        state_fingerprint="sha256:" + "0" * 64,
        options=(Option(
            id="wait", label="Wait", legality=Legality.LEGAL, executor_ref="test.wait"
        ),),
    )


def viewer(mode, frame):
    state = ViewerState(run_id=frame.run_id, decider=mode, max_steps=1, realtime=False)
    events = MemoryEventLog(frame.run_id)
    events.add_listener(state.on_event)
    state.on_decider_start(frame)
    return state, events


def assert_controller(state, decided_by, awaiting_agent):
    view = state.view()
    record = view["pending"] or view["decisions"][-1]
    operator = build_telemetry(view)
    public = public_telemetry(operator)
    for decision in (record, operator["pokegent"]["decision"], public["pokegent"]["decision"]):
        assert decision["decided_by"] == decided_by
        assert decision["awaiting_agent"] is awaiting_agent
    assert public["pokejev"] == public["pokegent"]
    assert public["pokegent"]["decision"]["source_label"] == state.decider.upper()
    assert operator["pokegent"]["decision"]["source"] == state.decider


@pytest.mark.parametrize("owner", ["system1", "agent"])
def test_hybrid_decision(frame, owner):
    state, events = viewer("hybrid", frame)
    assert_controller(state, None, False)
    if owner == "agent":
        events.emit(EventType.DECISION_AGENT_REQUESTED, request_id=frame.decision_id,
                    outcome="waiting")
        assert_controller(state, None, True)
    state.on_decider_call(frame, ChoiceResult(option_id="wait", provider_id=owner), None, 12)
    events.emit(
        EventType.DECISION_AGENT_COMPLETED if owner == "agent"
        else EventType.DECISION_SYSTEMONE_COMPLETED,
        request_id=frame.decision_id, outcome="accepted", details={"decided_by": owner},
    )
    assert_controller(state, owner, False)


def test_hybrid_escalation_waits_for_agent(frame):
    state, events = viewer("hybrid", frame)
    events.emit(EventType.DECISION_SYSTEMONE_COMPLETED, request_id=frame.decision_id,
                outcome="rejected", details={"decided_by": "system1"})
    assert_controller(state, None, False)
    events.emit(EventType.DECISION_AGENT_REQUESTED, request_id="another-choice", outcome="waiting")
    assert_controller(state, None, False)
    events.emit(EventType.DECISION_AGENT_REQUESTED, request_id=frame.decision_id, outcome="waiting")
    assert_controller(state, None, True)
    state.on_decider_call(frame, None, RuntimeError("synthetic failure"), 12)
    assert_controller(state, None, False)


def test_stopped_hybrid_is_no_longer_awaiting_agent(frame):
    state, events = viewer("hybrid", frame)
    events.emit(EventType.DECISION_AGENT_REQUESTED, request_id=frame.decision_id, outcome="waiting")
    state.finish(stop_reason="stopped", exit_code=0)
    assert_controller(state, None, False)


@pytest.mark.parametrize("mode", ["system1", "agent", "random"])
def test_non_hybrid_controller_is_unknown(frame, mode):
    state, events = viewer(mode, frame)
    events.emit(EventType.DECISION_AGENT_REQUESTED, request_id=frame.decision_id, outcome="waiting")
    assert_controller(state, None, False)
    state.on_decider_call(frame, ChoiceResult(option_id="wait", provider_id=mode), None, 12)
    events.emit(EventType.DECISION_AGENT_COMPLETED, request_id=frame.decision_id,
                outcome="accepted", details={"decided_by": "agent"})
    assert_controller(state, None, False)


def test_unknown_hybrid_controller_stays_null(frame):
    state, events = viewer("hybrid", frame)
    state.on_decider_call(frame, ChoiceResult(option_id="wait"), None, 12)
    events.emit(EventType.DECISION_SYSTEMONE_COMPLETED, request_id=frame.decision_id,
                outcome="accepted", details={"decided_by": "unknown"})
    assert_controller(state, None, False)
