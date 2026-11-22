"""Hybrid route-step veto: an off-route System One pick cannot outvote the objective."""

from types import SimpleNamespace

import pytest

from addons.system_one import hybrid
from addons.system_one.hybrid import (
    IRREVERSIBLE_ESCALATION,
    ROUTE_STEP_ESCALATION,
    ROUTE_STEP_RISK_ESCALATION,
    HybridDecider,
)
from pokegent.events import EventType, MemoryEventLog
from pokegent.options.models import (
    OBJECTIVE_RELATION_NOT_SERVING,
    ChoiceResult,
    DecisionFrame,
    Legality,
    Option,
    Risk,
    fingerprint,
)


def _option(
    option_id: str, *, facts: dict, label: str = "Go somewhere", risk: Risk = Risk.NORMAL
) -> Option:
    return Option(
        id=option_id,
        label=label,
        legality=Legality.LEGAL,
        executor_ref="field.warp",
        facts=facts,
        risk=risk,
    )


def _exit_option() -> Option:
    """The Mt Moon 1F warp back out to Route 4, marked off the objective."""
    return _option(
        "warp:1:3:1",
        label="Go through the door",
        facts={
            "target": {"kind": "warp", "map": (1, 3), "dest": (3, 19), "index": 1},
            "objective_relation": OBJECTIVE_RELATION_NOT_SERVING,
            "serves_goal": False,
        },
    )


def _ladder_option() -> Option:
    """The planner's next route step on the way to B2F."""
    return _option(
        "warp:1:59:3",
        label="Climb the ladder",
        facts={
            "target": {"kind": "warp", "map": (1, 59), "dest": (1, 60), "index": 3},
            "route_relation": "next route step",
            "route_step": True,
            "next_route_step": True,
            "on_route": True,
            "serves_goal": True,
        },
    )


def _irreversible_step() -> Option:
    """A single next route step that cannot be undone once taken."""
    return _option(
        "warp:1:59:9",
        label="Take the one-way drop",
        facts={
            "target": {"kind": "warp", "map": (1, 59), "dest": (1, 62), "index": 9},
            "route_relation": "next route step",
            "route_step": True,
            "next_route_step": True,
            "irreversible": True,
        },
    )


def _high_risk_step() -> Option:
    """A single next route step that starts a trainer battle: high risk, but undoable."""
    return _option(
        "warp:1:59:8",
        label="Battle the trainer on the way down",
        facts={
            "target": {"kind": "warp", "map": (1, 59), "dest": (1, 63), "index": 8},
            "route_relation": "next route step",
            "route_step": True,
            "next_route_step": True,
        },
        risk=Risk.HIGH,
    )


def _on_route_option() -> Option:
    return _option(
        "warp:1:59:1",
        label="Walk through the cave",
        facts={
            "target": {"kind": "warp", "map": (1, 59), "dest": (1, 61), "index": 1},
            "route_relation": "on route",
            "on_route": True,
            "serves_goal": True,
        },
    )


def _frame(*options: Option, goal_id: str | None = "mt_moon_fossil") -> DecisionFrame:
    return DecisionFrame(
        decision_id="decision-1",
        run_id="run-1",
        snapshot_seq=7,
        scene_id="field.free",
        scene_confidence=0.9,
        state_fingerprint=fingerprint("field.free", options=[option.id for option in options]),
        goal_id=goal_id,
        options=options,
    )


def _claim(option_id: str, *, confidence: float = 0.76, decision_id="decision-1") -> ChoiceResult:
    return ChoiceResult(
        option_id=option_id,
        probabilities={option_id: confidence},
        confidence=confidence,
        model_id="mock",
        provider_id="mock",
        decision_id=decision_id,
    )


def _decider(
    claim: ChoiceResult | None,
    monkeypatch: pytest.MonkeyPatch,
    *,
    override_window_s: float = 0.0,
    events=None,
):
    """A hybrid decider whose agent lane is a recording fake (no HTTP port)."""

    class FakeExternal:
        def __init__(self, port: int, timeout: float, *, autoplay: str = "off") -> None:
            self.port = port
            self.offers: list[dict] = []

        def bind_agent_request(self, _callback) -> None:
            return None

        def offer(self, frame, **kwargs):
            self.offers.append({"frame": frame, **kwargs})
            default = kwargs.get("default")
            if default is not None:
                # An expired override window applies the local default, as the server does.
                return default
            return ChoiceResult(
                option_id="talk:1",
                confidence=1.0,
                model_id="agent",
                provider_id="agent",
                decision_id=frame.decision_id,
            )

    class FakeSystem1:
        def __init__(self) -> None:
            self.frames: list[DecisionFrame] = []

        def decide(self, frame: DecisionFrame) -> ChoiceResult:
            self.frames.append(frame)
            assert claim is not None
            return claim

        def health(self):
            return SimpleNamespace(state="closed")

    monkeypatch.setattr(hybrid, "ExternalDecider", FakeExternal)
    decider = HybridDecider(
        FakeSystem1(),
        port=19760,
        external_timeout=10.0,
        override_window_s=override_window_s,
        events=events,
    )
    return decider, decider.external


def test_off_route_pick_is_replaced_by_the_single_next_route_step() -> None:
    ladder = _ladder_option()
    frame = _frame(_exit_option(), ladder)
    claim = _claim("warp:1:3:1", confidence=0.76)

    replacement, reason = HybridDecider._route_step_veto(frame, claim)

    assert reason is None
    assert replacement is not None
    assert replacement.option_id == ladder.id
    # The replacement is the planner's step, not a System One answer: no fabricated confidence.
    assert replacement.confidence is None
    assert replacement.probabilities == {}
    assert replacement.model_id == "route-step-veto"
    assert replacement.provider_id == "hybrid"
    # The answer still belongs to the frame it was given.
    assert replacement.decision_id == frame.decision_id


def test_on_route_pick_is_kept() -> None:
    frame = _frame(_exit_option(), _on_route_option(), _ladder_option())
    claim = _claim("warp:1:59:1")

    assert HybridDecider._route_step_veto(frame, claim) == (None, None)


def test_pick_that_is_itself_the_next_route_step_is_kept() -> None:
    ladder = _ladder_option()
    frame = _frame(_exit_option(), ladder, _on_route_option())

    assert HybridDecider._route_step_veto(frame, _claim(ladder.id)) == (None, None)


def test_two_candidate_route_steps_escalate() -> None:
    second_step = _option(
        "edge:1:60:N:1:1",
        label="Take the other way down",
        facts={"route_relation": "next route step", "next_route_step": True, "serves_goal": True},
    )
    frame = _frame(_exit_option(), _ladder_option(), second_step)

    replacement, reason = HybridDecider._route_step_veto(frame, _claim("warp:1:3:1"))

    assert replacement is None
    assert reason == ROUTE_STEP_ESCALATION == "multiple_route_steps"


def test_frame_without_an_objective_is_unchanged() -> None:
    ladder = _ladder_option()
    frame = _frame(_exit_option(), ladder, goal_id=None)
    claim = _claim("warp:1:3:1")

    assert HybridDecider._route_step_veto(frame, claim) == (None, None)


def test_choice_outside_the_frame_is_unchanged() -> None:
    frame = _frame(_exit_option(), _ladder_option())

    assert HybridDecider._route_step_veto(frame, _claim("warp:9:9:9")) == (None, None)


@pytest.mark.parametrize(
    ("option_id", "target"),
    [
        ("item:14", {"kind": "item"}),
        ("bicycle:1", {"kind": "bicycle"}),
        ("talk:7", {"kind": "npc"}),
        ("heal:1", {"kind": "heal"}),
        ("trainer:2", {"kind": "trainer"}),
    ],
)
def test_off_route_non_navigation_pick_is_not_replaced(option_id: str, target: dict) -> None:
    """Items, bicycle, NPC, heal and trainer picks are not route steps to substitute."""
    pick = _option(
        option_id,
        label="Do something else",
        facts={
            "target": dict(target),
            "objective_relation": OBJECTIVE_RELATION_NOT_SERVING,
            "serves_goal": False,
        },
    )
    frame = _frame(pick, _ladder_option())

    assert HybridDecider._route_step_veto(frame, _claim(pick.id)) == (None, None)


@pytest.mark.parametrize(
    ("option_id", "target"),
    [
        ("warp:1:3:1", {"kind": "warp"}),  # doors and ladders are warp targets
        ("edge:1:60:N:1:1", {"kind": "edge"}),
        ("fly:route_4", {"kind": "fly"}),
        ("tile:4:5", {"kind": "tile"}),
    ],
)
def test_off_route_navigation_pick_is_replaced(option_id: str, target: dict) -> None:
    pick = _option(
        option_id,
        label="Leave the route",
        facts={"target": dict(target), "route_relation": "off route", "serves_goal": False},
    )
    ladder = _ladder_option()
    frame = _frame(pick, ladder)

    replacement, reason = HybridDecider._route_step_veto(frame, _claim(pick.id))

    assert reason is None
    assert replacement is not None
    assert replacement.option_id == ladder.id


def test_decide_returns_the_vetoed_step_without_waiting_for_the_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ladder = _ladder_option()
    frame = _frame(_exit_option(), ladder)
    decider, external = _decider(_claim("warp:1:3:1", confidence=0.76), monkeypatch)

    result = decider.decide(frame)

    assert result.option_id == ladder.id
    assert external.offers == []
    assert decider.last_decision == {
        "decided_by": "system1",
        "escalated": False,
        "vetoed_by": "route_step",
        "system1_option_id": "warp:1:3:1",
        "system1_confidence": 0.76,
    }


def test_non_navigation_pick_keeps_the_confidence_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _option(
        "item:14",
        label="Pick up the item",
        facts={
            "target": {"kind": "item"},
            "objective_relation": OBJECTIVE_RELATION_NOT_SERVING,
            "serves_goal": False,
        },
    )
    frame = _frame(item, _ladder_option())
    claim = _claim("item:14", confidence=0.76)
    decider, external = _decider(claim, monkeypatch)

    result = decider.decide(frame)

    assert result is claim
    assert external.offers == []
    assert "vetoed_by" not in decider.last_decision


def test_irreversible_route_step_escalates_instead_of_being_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An off-route claim plus a single irreversible step is the agent's decision, not a veto."""
    step = _irreversible_step()
    frame = _frame(_exit_option(), step)
    claim = _claim("warp:1:3:1", confidence=0.10)
    decider, external = _decider(claim, monkeypatch)

    result = decider.decide(frame)

    assert len(external.offers) == 1
    offer = external.offers[0]
    assert offer["wait_timeout"] == 300.0
    assert offer["interruptible_wait"] is True
    assert offer["default"] is None
    assert offer["metadata"]["escalation_reason"] == IRREVERSIBLE_ESCALATION
    assert offer["metadata"]["vetoed_option"] == {
        "option_id": "warp:1:3:1",
        "confidence": 0.10,
    }
    assert decider.last_decision["escalated"] is True
    assert decider.last_decision["vetoed_by"] == "route_step"
    assert result.provider_id == "agent"


def test_high_risk_route_step_escalates_instead_of_being_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single high-risk route step is not a silent replacement for an off-route claim."""
    step = _high_risk_step()
    frame = _frame(_exit_option(), step)

    replacement, reason = HybridDecider._route_step_veto(frame, _claim("warp:1:3:1"))
    assert replacement is None
    assert reason == ROUTE_STEP_RISK_ESCALATION == "high_risk_route_step"

    decider, external = _decider(_claim("warp:1:3:1"), monkeypatch)

    result = decider.decide(frame)

    assert len(external.offers) == 1
    offer = external.offers[0]
    assert offer["metadata"]["escalation_reason"] == ROUTE_STEP_RISK_ESCALATION
    assert offer["default"] is None
    assert offer["metadata"]["vetoed_by"] == "route_step"
    assert result.provider_id == "agent"
    assert decider.last_decision["decided_by"] == "agent"


def test_vetoed_metadata_is_served_in_the_override_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ladder = _ladder_option()
    frame = _frame(_exit_option(), ladder)
    decider, external = _decider(
        _claim("warp:1:3:1", confidence=0.76), monkeypatch, override_window_s=0.5
    )

    result = decider.decide(frame)

    assert len(external.offers) == 1
    offer = external.offers[0]
    assert offer["default"] is not None and offer["default"].option_id == ladder.id
    assert offer["metadata"]["vetoed_by"] == "route_step"
    assert offer["metadata"]["vetoed_option"] == {
        "option_id": "warp:1:3:1",
        "confidence": 0.76,
    }
    assert result.option_id == ladder.id
    assert result.confidence is None


def test_decide_escalates_an_ambiguous_route_step_to_the_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second_step = _option(
        "edge:1:60:N:1:1",
        label="Take the other way down",
        facts={"route_relation": "next route step", "next_route_step": True, "serves_goal": True},
    )
    frame = _frame(_exit_option(), _ladder_option(), second_step)
    decider, external = _decider(_claim("warp:1:3:1"), monkeypatch)

    result = decider.decide(frame)

    assert len(external.offers) == 1
    offer = external.offers[0]
    assert offer["wait_timeout"] == 300.0
    assert offer["interruptible_wait"] is True
    assert offer["default"] is None
    assert offer["metadata"]["escalated"] is True
    assert offer["metadata"]["escalation_reason"] == ROUTE_STEP_ESCALATION
    # The agent still sees System One's actual claim in the frame's system1 block.
    assert offer["metadata"]["system1"]["option_id"] == "warp:1:3:1"
    assert offer["metadata"]["vetoed_by"] == "route_step"
    assert result.provider_id == "agent"
    assert decider.last_decision["decided_by"] == "agent"
    assert decider.last_decision["escalation_reason"] == ROUTE_STEP_ESCALATION
    assert decider.last_decision["vetoed_by"] == "route_step"


def test_rejected_event_keeps_the_veto_and_the_original_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second_step = _option(
        "edge:1:60:N:1:1",
        label="Take the other way down",
        facts={"route_relation": "next route step", "next_route_step": True, "serves_goal": True},
    )
    frame = _frame(_exit_option(), _ladder_option(), second_step)
    events = MemoryEventLog("synthetic")
    decider, _external = _decider(_claim("warp:1:3:1", confidence=0.21), monkeypatch, events=events)

    decider.decide(frame)

    rejected = events.of_type(EventType.DECISION_SYSTEMONE_COMPLETED)
    assert len(rejected) == 1
    assert rejected[0].outcome == "rejected"
    details = rejected[0].details
    assert details["escalation_reason"] == ROUTE_STEP_ESCALATION
    assert details["vetoed_by"] == "route_step"
    assert details["vetoed_option"] == {"option_id": "warp:1:3:1", "confidence": 0.21}
    assert details["option_id"] == "warp:1:3:1"
    assert details["confidence"] == 0.21
