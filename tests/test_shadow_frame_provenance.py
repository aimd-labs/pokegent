"""Shadow observations carry the observed frame identity, never the current frame's."""

from types import SimpleNamespace

from test_hybrid_shadow import Bridge, _fingerprint, _raising, _system1, frame, hybrid, runner

from addons.system_one.provider import ProviderUnavailable
from pokegent.events import EventType, MemoryEventLog


def _observed_frame(*, goal_id="goal-1", goal_revision=3, **kwargs):
    return frame(goal_id=goal_id, **kwargs).model_copy(
        update={"goal_revision": goal_revision}
    )


def test_matched_shadow_observation_records_full_provenance(monkeypatch):
    events = MemoryEventLog("synthetic")
    decider, _ = hybrid(monkeypatch, system1=_system1(), events=events, shadow=True)
    offered = _observed_frame()
    assert runner(decider, events)._decide(offered)[1] == "walk"

    completed = events.of_type(EventType.DECISION_SYSTEMONE_COMPLETED)
    assert len(completed) == 1
    details = completed[0].details
    assert completed[0].request_id == "dec-1"
    assert details["snapshot_seq"] == 1
    assert details["state_fingerprint"] == _fingerprint("0")
    assert details["goal_id"] == "goal-1"
    assert details["goal_revision"] == 3
    assert details["current_decision_id"] == "dec-1"
    assert details["frame_matches_current"] is True
    assert details["agreement"] is False
    assert details["accepted"] is False
    assert details["authority"] == "shadow"
    assert details["option_id"] == "sign"
    assert details["accuracy"] == "unknown"


def test_refreshed_current_frame_cannot_rewrite_observed_identity(monkeypatch):
    events = MemoryEventLog("synthetic")
    refreshed = frame(decision_id="dec-2", fingerprint="2")
    decider, _ = hybrid(
        monkeypatch,
        system1=_system1(),
        bridge=Bridge(choice="walk", refreshed=refreshed),
        events=events,
        shadow=True,
    )
    chosen_frame, option_id = runner(decider, events)._decide(frame())

    assert option_id == "walk" and chosen_frame.decision_id == "dec-2"
    completed = events.of_type(EventType.DECISION_SYSTEMONE_COMPLETED)
    assert len(completed) == 1
    details = completed[0].details
    assert completed[0].request_id == "dec-1"
    assert details["snapshot_seq"] == 1
    assert details["state_fingerprint"] == _fingerprint("0")
    assert details["goal_id"] is None
    assert details["goal_revision"] is None
    assert details["current_decision_id"] == "dec-2"
    assert details["frame_matches_current"] is False
    assert details["agreement"] is None
    assert details["option_id"] == "sign"
    agent = events.of_type(EventType.DECISION_AGENT_COMPLETED)
    assert len(agent) == 1 and agent[0].request_id == "dec-2"


def test_failed_shadow_keeps_identity_with_unknown_agreement(monkeypatch):
    events = MemoryEventLog("synthetic")
    calls = []

    def decide(offered):
        calls.append(offered)
        raise ProviderUnavailable("synthetic outage")

    decider, _ = hybrid(
        monkeypatch,
        system1=SimpleNamespace(decide=decide),
        events=events,
        shadow=True,
    )
    assert runner(decider, events)._decide(frame())[1] == "walk"

    assert len(calls) == 1
    completed = events.of_type(EventType.DECISION_SYSTEMONE_COMPLETED)
    assert len(completed) == 1
    details = completed[0].details
    assert completed[0].outcome == "failed"
    assert details["snapshot_seq"] == 1
    assert details["state_fingerprint"] == _fingerprint("0")
    assert details["goal_id"] is None
    assert details["goal_revision"] is None
    assert details["current_decision_id"] == "dec-1"
    assert details["frame_matches_current"] is True
    assert details["agreement"] is None
    assert details["option_id"] is None
    assert details["provider_id"] is None
    assert details["model_id"] is None
    assert details["accepted"] is False
    assert details["authority"] == "shadow"
    assert len(events.of_type(EventType.DECISION_AGENT_COMPLETED)) == 1
    assert not any(
        item.type == EventType.DECISION_SYSTEMONE_COMPLETED and item.outcome == "accepted"
        for item in events.events
    )


def test_missing_shadow_claim_emits_no_observation():
    events = MemoryEventLog("synthetic")
    dummy = SimpleNamespace(decide=_raising(ProviderUnavailable("unused")).decide)
    runner(dummy, events)._emit_shadow_observation(frame(), {}, None)
    assert not events.of_type(EventType.DECISION_SYSTEMONE_COMPLETED)
