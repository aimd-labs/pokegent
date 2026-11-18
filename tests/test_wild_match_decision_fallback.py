"""A wild or train target that has no reachable candidate map still names the next decision.

Issue #21: ``plan_to_maps`` (wild) and ``plan_to_tiles`` (train) returned a no-route plan
directly, without the bounded current-map decision search that ``plan`` runs (the Mt Moon
fossil shape).  A wild target behind a decision produced a frame with no route step at all.
The adapter now runs ``plan_next_decision`` on that no-route path so the agent still gets the
decision step (e.g. choose the fossil) instead of an empty frame.
"""

from types import SimpleNamespace

import pytest

from pokegent.goals.models import Objective, Target
from pokegent.goals.validate import ResolvedObjective, ResolvedTarget
from pokegent.navigation.adapter import NavigationAdapter
from pokegent.navigation.models import (
    CapabilityState,
    NavigationEdge,
    NavigationNode,
    NavigationRequirement,
    NavigationTarget,
)
from pokegent.navigation.planner import NavigationPlanner
from pokegent.options.models import Legality, Option, Risk

FOSSIL_REQUIREMENT = NavigationRequirement("fossil", "fossil", False, "rom")

START = NavigationNode(1, 59, 10, 10)
APPROACH = NavigationNode(1, 59, 11, 8)
FOSSIL_TILE = NavigationNode(1, 59, 13, 7)
CANDIDATE_MAP = (1, 60)


class _FakeGraph:
    """The navigation-graph surface the planner and the option adapter read."""

    def __init__(self, edges=(), *, names=None, destination=None):
        self._edges: dict[NavigationNode, list[NavigationEdge]] = {}
        for edge in edges:
            self._edges.setdefault(edge.source, []).append(edge)
        self._names = names or {}
        # A training destination map must expose a usable encounter tile so
        # ``_train_target`` asks the planner for a tile route that only the fossil
        # blocks.  ``destination`` is a ``SimpleNamespace`` with ``layout`` and
        # ``tile(x, y)`` meeting the adapter's encounter-tile read.
        self._destination = destination
        self.maps = {}
        self.provider = SimpleNamespace(puzzles=lambda **_kwargs: {})

    def state(self, map_key):
        name = self._names.get(tuple(map_key))
        if name is None:
            return None
        if self._destination is not None and tuple(map_key) == CANDIDATE_MAP:
            return self._destination
        return SimpleNamespace(identity=SimpleNamespace(name=name))

    def node(self, map_key, x, y):
        return NavigationNode(map_key[0], map_key[1], x, y)

    def map_route(self, start, target):
        return (tuple(start), tuple(target))

    def counted_trainers(self, *_args):
        return ()

    def stateful_flags_toward(self, _start, _target):
        return frozenset()

    def route_script_unknown(self, _plan):
        return None

    def edges_from(self, node, _capabilities=None, *, live=None, include_unmet=False):
        edges = self._edges.get(node, ())
        return tuple(edges) if include_unmet else tuple(edge for edge in edges if edge.available)


def _dead_end_route() -> tuple[NavigationEdge, ...]:
    """A fossil on the only path and no way through to any candidate map."""
    return (
        NavigationEdge("walk:11:8", START, APPROACH, "walk", 4),
        NavigationEdge(
            "fossil:2",
            APPROACH,
            FOSSIL_TILE,
            "field_move",
            6,
            (FOSSIL_REQUIREMENT,),
        ),
    )


def _snapshot(*, x=10, y=10):
    state = SimpleNamespace(
        identity=SimpleNamespace(group=1, number=59, name="MtMoon_B2F"),
        player=SimpleNamespace(
            avatar_x=x, avatar_y=y, facing="N", surfing=False, biking=False, badges=()
        ),
        objects=(),
        strength_active=False,
    )
    return SimpleNamespace(
        map=state,
        player=state.player,
        party=(),
        bag=(),
        flags=SimpleNamespace(set_ids=()),
        vars=SimpleNamespace(nonzero=()),
        ui=None,
        dynamic_warp=None,
        usable=True,
    )


def _wild_resolved(mission_map: tuple[int, int] = CANDIDATE_MAP) -> ResolvedObjective:
    """A dex (wild) objective whose encounter candidate map is behind the fossil."""
    return ResolvedObjective(
        objective=Objective(
            objective_id="catch_wild",
            revision=1,
            kind="dex",
            target=Target(species=10),
        ),
        target=ResolvedTarget(group=1, number=59, map_name="MtMoon_B2F"),
        probes=(),
        navigation_target=None,
        navigation_reason="rom-wild",
        navigation_map_keys=frozenset({mission_map}),
    )


def _train_resolved() -> ResolvedObjective:
    """A training objective whose target map holds an encounter tile behind the fossil."""
    return ResolvedObjective(
        objective=Objective(
            objective_id="train_mtmoon",
            revision=1,
            kind="train",
            target=Target(map="MtMoon_B1F"),
        ),
        target=ResolvedTarget(group=1, number=60, map_name="MtMoon_B1F"),
        probes=(),
    )


def _encounter_destination() -> SimpleNamespace:
    """A fake Mt Moon B1F whose bottom-left tile is a usable grass tile."""

    def tile(x, y):
        walkable = (x, y) == (0, 0)
        return SimpleNamespace(
            walkable=walkable, tall_grass=walkable, encounter="land" if walkable else None
        )

    return SimpleNamespace(
        identity=SimpleNamespace(name="MtMoon_B1F"),
        layout=SimpleNamespace(width=3, height=3),
        tile=tile,
    )


def _fossil_option() -> Option:
    """The irreversible Mt Moon fossil choice; annotate must keep its risk metadata."""
    return Option(
        id="fossil:2",
        label="Choose this fossil",
        description="Chooses this fossil; the other fossil is gone after this choice.",
        legality=Legality.LEGAL,
        executor_ref="field.interact",
        facts={
            "target": {
                "kind": "obstacle",
                "role": "fossil",
                "map": [1, 59],
                "local_id": 2,
                "at": [13, 7],
            },
            "obstacle": "fossil",
            "irreversible": True,
        },
        risk=Risk.HIGH,
        semantic_prompt="Choose this fossil?",
        effect_description="The other fossil is gone.",
        irreversible=True,
    )


def _exit_option() -> Option:
    return Option(
        id="warp:1:3:1",
        label="Go through the door",
        legality=Legality.LEGAL,
        executor_ref="field.warp",
        facts={"target": {"kind": "warp", "map": [1, 59], "dest": [3, 19], "index": 1}},
    )


def test_wild_target_blocks_into_a_decision_step_not_an_empty_frame():
    graph = _FakeGraph(_dead_end_route(), names={(1, 59): "MtMoon_B2F", (1, 60): "MtMoon_B1F"})
    adapter = NavigationAdapter(graph)

    plan = adapter.plan(_snapshot(), _wild_resolved())

    assert plan is not None
    assert plan.cost is None
    # No route to any candidate wild map is known...
    assert plan.route == ()
    # ...but the frame names the decision the agent must take (the decision fact
    # replaces the generic wild-map fact).
    assert "next decision: fossil" in (plan.blocked_route_fact or "")
    assert plan.blocked_choice_step == "fossil:2"
    assert plan.unmet == ("fossil",)


@pytest.mark.parametrize(
    "build_resolved",
    [_wild_resolved, _train_resolved],
    ids=["wild", "train"],
)
def test_no_route_decision_step_survives_plan_and_annotate(build_resolved):
    """A wild-dex or train objective behind the fossil names the decision through the
    adapter, and annotate keeps risk/irreversible metadata on that option.

    Issue #21 regression: both ``plan_to_maps`` (wild) and ``plan_to_tiles`` (train)
    used to return the no-route plan without running the bounded decision search, so
    the frame had no ``blocked_choice_step`` and the decision option never got its
    ``next_route_step`` facts. Removing either fallback must fail this test.
    """
    graph = _FakeGraph(
        _dead_end_route(),
        names={(1, 59): "MtMoon_B2F", (1, 60): "MtMoon_B1F"},
        destination=_encounter_destination(),
    )
    adapter = NavigationAdapter(graph)
    resolved = build_resolved()

    plan = adapter.plan(_snapshot(), resolved)

    assert plan is not None
    assert plan.cost is None
    assert plan.blocked_choice_step == "fossil:2"

    options, annotated = adapter.annotate(
        [_fossil_option(), _exit_option()], _snapshot(), resolved, plan=plan
    )
    by_id = {option.id: option for option in options}

    fossil = by_id["fossil:2"]
    assert fossil.facts["next_route_step"] is True
    assert fossil.facts["route_step"] is True
    assert fossil.facts["route_needs"] == ["fossil"]
    assert fossil.facts["blocked_choice"] == "fossil:2"
    assert "no_route" not in fossil.facts
    assert "serves_goal" not in fossil.facts
    # Annotate must not strip the decision option's risk/irreversible metadata.
    assert fossil.irreversible is True
    assert fossil.risk is Risk.HIGH

    exit_opt = by_id["warp:1:3:1"]
    assert exit_opt.facts["no_route"] is True
    assert "next_route_step" not in exit_opt.facts
    assert annotated is plan


def test_wild_target_decision_fallback_reuses_the_planner_bound():
    planner = NavigationPlanner(
        _FakeGraph(_dead_end_route(), names={(1, 59): "MtMoon_B2F", (1, 60): "MtMoon_B1F"})
    )
    decision = planner.plan_next_decision(
        START,
        NavigationTarget(*CANDIDATE_MAP),
        CapabilityState(),
        limit=10,
    )
    assert decision is not None
    assert decision.blocked_choice_step == "fossil:2"
    assert decision.cost is None
    assert decision.route == ()
    # The search is bounded: a limit too small to reach the decision must not return
    # an invented step just because the graph is empty past the budget.
    exhausted = planner.plan_next_decision(
        START,
        NavigationTarget(*CANDIDATE_MAP),
        CapabilityState(),
        limit=1,
    )
    assert exhausted is None


def test_wild_target_without_route_layout_still_returns_a_plan():
    graph = _FakeGraph(
        (),
        names={(1, 59): "MtMoon_B2F", (1, 60): "MtMoon_B1F"},
    )
    adapter = NavigationAdapter(graph)
    plan = adapter.plan(_snapshot(), _wild_resolved())

    # Empty graph, no reachable candidate, no decision either -> a no-route plan with a
    # diagnostic fact, never a None crash.
    assert plan is not None
    assert plan.cost is None
    assert plan.blocked_route_fact
