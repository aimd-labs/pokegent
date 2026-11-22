"""Hybrid escalates an irreversible System One pick to the agent, whatever its confidence."""

from types import SimpleNamespace

import pytest

from addons.system_one import hybrid
from addons.system_one.hybrid import IRREVERSIBLE_ESCALATION, HybridDecider
from pokegent.events import EventType, MemoryEventLog
from pokegent.options.models import ChoiceResult, DecisionFrame, Legality, Option, Risk


def _fossil_yes() -> Option:
    """The Dome/Helix YES option, marked on the model field and in its facts."""
    return Option(
        id="yes",
        label="YES",
        description="Chooses this fossil; the other fossil is gone after this choice.",
        legality=Legality.LEGAL,
        executor_ref="dialog.answer",
        facts={
            "prompt_kind": "fossil_choice",
            "affirmative": True,
            "irreversible": True,
            "other_fossil_gone": True,
        },
        risk=Risk.HIGH,
        semantic_prompt="Do you want the Dome Fossil?",
        effect_description="Chooses this fossil; the other fossil is gone after this choice.",
        irreversible=True,
    )


def _facts_only_irreversible() -> Option:
    """An item use that carries its lasting effect in facts only (no prompt text)."""
    return Option(
        id="item:rare_candy",
        label="Use Rare Candy",
        legality=Legality.LEGAL,
        executor_ref="items.use",
        facts={"irreversible": True, "stat_changes": {"level": 1}},
    )


def _decline() -> Option:
    return Option(
        id="no",
        label="NO",
        legality=Legality.LEGAL,
        executor_ref="dialog.answer",
        facts={"prompt_kind": "fossil_choice", "affirmative": False},
    )


def _frame(*options: Option) -> DecisionFrame:
    return DecisionFrame(
        decision_id="decision-1",
        run_id="run-1",
        snapshot_seq=7,
        scene_id="dialog.message",
        scene_confidence=0.9,
        state_fingerprint="sha256:" + "0" * 64,
        options=options,
    )


def _claim(option_id: str, *, confidence: float = 0.9) -> ChoiceResult:
    return ChoiceResult(
        option_id=option_id,
        probabilities={option_id: confidence},
        confidence=confidence,
        model_id="mock",
        provider_id="mock",
        decision_id="decision-1",
    )


@pytest.fixture
def decider_factory(monkeypatch):
    def build(claim: ChoiceResult, *, events=None):
        class Bridge:
            port = 19760

            def __init__(self, *args, **kwargs):
                self.offers: list[dict] = []

            def bind_agent_request(self, callback):
                self.callback = callback

            def offer(self, offered, **kwargs):
                self.offers.append(kwargs)
                self.callback("synthetic-frame", offered)
                return ChoiceResult(option_id="no", confidence=1.0, provider_id="agent")

            def record_action_result(self, *args, **kwargs):
                pass

        monkeypatch.setattr(hybrid, "ExternalDecider", Bridge)
        system1 = SimpleNamespace(
            decide=lambda _frame: claim,
            health=lambda: SimpleNamespace(state="closed"),
        )
        decider = HybridDecider(system1, port=19760, external_timeout=1, events=events)
        return decider, decider.external

    return build


def test_irreversible_pick_escalates_despite_high_confidence(decider_factory):
    events = MemoryEventLog("synthetic")
    decider, external = decider_factory(_claim("yes", confidence=0.9), events=events)

    result = decider.decide(_frame(_fossil_yes(), _decline()))

    assert len(external.offers) == 1
    offer = external.offers[0]
    assert offer["wait_timeout"] == 300.0
    assert offer["interruptible_wait"] is True
    # The agent must decide; System One's suggestion is never the default.
    assert offer["default"] is None
    assert offer["metadata"]["escalation_reason"] == IRREVERSIBLE_ESCALATION
    assert offer["metadata"]["system1"]["option_id"] == "yes"
    assert result.provider_id == "agent"
    assert decider.last_decision["decided_by"] == "agent"
    assert decider.last_decision["system1_option_id"] == "yes"
    rejected = events.of_type(EventType.DECISION_SYSTEMONE_COMPLETED)
    assert [(e.outcome, e.details["escalation_reason"]) for e in rejected] == [
        ("rejected", IRREVERSIBLE_ESCALATION)
    ]


def test_facts_only_irreversible_pick_escalates(decider_factory):
    decider, external = decider_factory(_claim("item:rare_candy", confidence=0.95))

    decider.decide(_frame(_facts_only_irreversible(), _decline()))

    assert len(external.offers) == 1
    assert external.offers[0]["metadata"]["escalation_reason"] == IRREVERSIBLE_ESCALATION


def test_irreversible_beats_low_confidence_as_the_reason(decider_factory):
    decider, external = decider_factory(_claim("yes", confidence=0.1))

    decider.decide(_frame(_fossil_yes(), _decline()))

    assert external.offers[0]["metadata"]["escalation_reason"] == IRREVERSIBLE_ESCALATION


def test_pick_that_is_not_irreversible_is_unchanged(decider_factory):
    decider, external = decider_factory(_claim("no", confidence=0.9))

    result = decider.decide(_frame(_fossil_yes(), _decline()))

    assert external.offers == []
    assert result.option_id == "no"
    assert result.provider_id == "mock"
    assert decider.last_decision == {"decided_by": "system1", "escalated": False}


def test_irreversible_choice_helper_ignores_unknown_and_missing_picks():
    frame = _frame(_fossil_yes(), _decline())

    assert HybridDecider._irreversible_choice(frame, None) is False
    assert HybridDecider._irreversible_choice(frame, _claim("outside")) is False
    assert HybridDecider._irreversible_choice(frame, _claim("yes")) is True
    assert HybridDecider._irreversible_choice(frame, _claim("no")) is False
