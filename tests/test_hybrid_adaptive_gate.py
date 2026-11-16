"""Adaptive routing uses synthetic frames and executor outcomes; no socket or model."""

from types import SimpleNamespace

import pytest

from addons.system_one.hybrid import HybridDecider
from pokegent.actions._exec.models import ActionResult, ActionStatus
from pokegent.events import EventType, MemoryEventLog
from pokegent.options.models import ChoiceResult, DecisionFrame, Legality, Option, StaleDecision
from pokegent.user_config import load_user_config


def frame(n=0, *, state="0", scene="field.free"):
    return DecisionFrame(
        decision_id=f"choice-{n}", run_id="synthetic", snapshot_seq=n,
        scene_id=scene, scene_confidence=1, state_fingerprint="sha256:" + state * 64,
        options=tuple(Option(id=key, label=key, legality=Legality.LEGAL,
                             executor_ref="test.wait") for key in ("sign", "walk")),
    )


@pytest.fixture
def hybrid(monkeypatch):
    class Bridge:
        port = 19760

        def __init__(self, *args, **kwargs):
            self.offers = []

        def bind_agent_request(self, callback):
            self.callback = callback

        def offer(self, offered, **kwargs):
            self.offers.append(kwargs)
            self.callback("synthetic-frame", offered)
            return ChoiceResult(option_id="walk", provider_id="agent")

        def record_action_result(self, *args, **kwargs):
            pass

    monkeypatch.setattr("addons.system_one.hybrid.ExternalDecider", Bridge)
    calls = []
    system1 = SimpleNamespace(option="sign")
    system1.decide = lambda f: (
        calls.append(f) or ChoiceResult(option_id=system1.option, confidence=0.36,
                                       provider_id="system1")
    )
    decider = HybridDecider(system1, port=19760, external_timeout=1,
                            events=MemoryEventLog("synthetic"))
    return decider, calls


def act(decider, f, *, option=None, **changes):
    if option is not None:
        decider.system1.option = option
    choice = decider.decide(f)
    result = ActionResult(status=ActionStatus.APPLIED, option_id=option or choice.option_id,
                          before_hash="same", after_hash="same", verified_outcome=True,
                          **changes)
    decider.record_action_result(f, result)
    return choice


def test_third_unchanged_sign_goes_directly_to_agent(hybrid):
    decider, calls = hybrid
    assert act(decider, frame()).provider_id == "system1"
    assert act(decider, frame(1)).provider_id == "system1"
    assert act(decider, frame(2)).provider_id == "agent"
    assert len(calls) == 2
    assert decider.external.offers[-1]["metadata"]["escalation_reason"] == "repeated_option"
    event = decider._events.events[-1]
    assert event.type == EventType.DECISION_AGENT_REQUESTED
    assert event.details["escalation_reason"] == "repeated_option"
    assert act(decider, frame(3)).provider_id == "system1"


def test_stale_choice_preserves_executed_repeat_streak(hybrid):
    decider, calls = hybrid
    decider.external.discard_stale_decision = lambda: None
    assert act(decider, frame()).provider_id == "system1"
    decider.discard_stale_decision()
    assert act(decider, frame(1)).provider_id == "system1"
    decider.discard_stale_decision()
    assert act(decider, frame(2)).provider_id == "agent"
    assert len(calls) == 2


def test_stale_forced_agent_choice_does_not_renew_system1_budget(hybrid, monkeypatch):
    decider, calls = hybrid
    act(decider, frame())
    act(decider, frame(1))
    offer = decider.external.offer

    def stale(*args, **kwargs):
        raise StaleDecision("synthetic animation")

    monkeypatch.setattr(decider.external, "offer", stale)
    with pytest.raises(StaleDecision):
        decider.decide(frame(2))
    assert decider._repeat_count == 2
    monkeypatch.setattr(decider.external, "offer", offer)
    assert act(decider, frame(3)).provider_id == "agent"
    assert len(calls) == 2


@pytest.mark.parametrize("change", ["state", "scene", "option", "progress", "forced"])
def test_streak_resets(hybrid, change):
    decider, calls = hybrid
    act(decider, frame())
    if change == "forced":
        decider.record_action_result(frame(99), ActionResult(status="applied", option_id="sign"))
    second = frame(1, state="1" if change == "state" else "0",
                   scene="dialog.message" if change == "scene" else "field.free")
    act(decider, second, option="walk" if change == "option" else None,
        progressed=True if change == "progress" else None)
    assert decider.decide(frame(2, state="1" if change == "state" else "0",
                                scene=second.scene_id)).provider_id == "system1"
    assert len(calls) == 3


def test_verified_hash_change_resets_but_explicit_no_progress_does_not(hybrid):
    decider, _ = hybrid
    for n, progressed in enumerate((None, False)):
        f = frame(n)
        decider.decide(f)
        decider.record_action_result(f, ActionResult(
            status="applied", option_id="sign", verified_outcome=True,
            before_hash="before", after_hash="after", progressed=progressed,
        ))
    assert decider.decide(frame(2)).provider_id == "system1"


def test_configurable_threshold(hybrid):
    decider, calls = hybrid
    decider.force_agent_after_repeats = 1
    act(decider, frame())
    assert decider.decide(frame(1)).provider_id == "agent"
    assert len(calls) == 1


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "2", None])
def test_invalid_repeat_config(tmp_path, hybrid, value):
    config = tmp_path / "config.yaml"
    config.write_text("hybrid:\n  force_agent_after_repeats: " + repr(value) + "\n")
    with pytest.raises(ValueError, match="force_agent_after_repeats"):
        load_user_config(config, environ={})
    decider, _ = hybrid
    with pytest.raises(ValueError, match="force_agent_after_repeats"):
        HybridDecider(decider.system1, port=19760, external_timeout=1,
                      force_agent_after_repeats=value)


def test_default_repeat_config(tmp_path):
    assert load_user_config(tmp_path / "missing", environ={})["hybrid"][
        "force_agent_after_repeats"
    ] == 2


def test_changed_hash_with_explicit_no_progress_still_escalates(hybrid):
    decider, calls = hybrid
    for n in range(2):
        f = frame(n)
        decider.decide(f)
        decider.record_action_result(f, ActionResult(
            status="applied", option_id="sign", verified_outcome=True,
            before_hash="before", after_hash="after", progressed=False,
        ))
    assert decider.decide(frame(2)).provider_id == "agent"
    assert len(calls) == 2
