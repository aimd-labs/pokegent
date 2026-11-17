"""A wild or train target that has no reachable candidate map still names the next decision.

Issue #21: ``plan_to_maps`` (wild) and ``plan_to_tiles`` (train) returned a no-route plan
directly, without the bounded current-map decision search that ``plan`` runs (the Mt Moon
fossil shape).  A wild target behind a decision produced a frame with no route step at all.
The adapter now runs ``plan_next_decision`` on that no-route path so the agent still gets the
decision step (e.g. choose the fossil) instead of an empty frame.
"""

from types import SimpleNamespace

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

FOSSIL_REQUIREMENT = NavigationRequirement("fossil", "fossil", False, "rom")

START = NavigationNode(1, 59, 10, 10)
APPROACH = NavigationNode(1, 59, 11, 8)
FOSSIL_TILE = NavigationNode(1, 59, 13, 7)
CANDIDATE_MAP = (1, 60)


class _FakeGraph:
    """The navigation-graph surface the planner and the option adapter read."""

    def __init__(self, edges=(), *, names=None):
        self._edges: dict[NavigationNode, list[NavigationEdge]] = {}
        for edge in edges:
            self._edges.setdefault(edge.source, []).append(edge)
        self._names = names or {}
        self.maps = {}
        self.provider = SimpleNamespace(puzzles=lambda **_kwargs: {})

    def state(self, map_key):
        name = self._names.get(tuple(map_key))
        return None if name is None else SimpleNamespace(identity=SimpleNamespace(name=name))

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


def test_wild_target_blocks_into_a_decision_step_not_an_empty_frame():
    graph = _FakeGraph(
        _dead_end_route(), names={(1, 59): "MtMoon_B2F", (1, 60): "MtMoon_B1F"}
    )
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


def test_wild_target_decision_fallback_reuses_the_planner_bound():
    planner = NavigationPlanner(
        _FakeGraph(_dead_end_route(), names={(1, 59): "MtMoon_B2F", (1, 60): "MtMoon_B1F"})
    )
    decision = planner.plan_next_decision(
        START, NavigationTarget(*CANDIDATE_MAP), CapabilityState()
    )
    assert decision is not None
    assert decision.blocked_choice_step == "fossil:2"
    assert decision.cost is None
    assert decision.route == ()


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
