"""A route blocked only by the Mt Moon fossil choice is a decision step, not a dead end."""

from types import SimpleNamespace

from pokegent.navigation.adapter import NavigationAdapter
from pokegent.navigation.capabilities import requirement
from pokegent.navigation.models import (
    CapabilityState,
    NavigationEdge,
    NavigationNode,
    NavigationPlan,
    NavigationRequirement,
    NavigationTarget,
)
from pokegent.navigation.planner import NavigationPlanner, next_semantic_edge
from pokegent.options.models import Legality, Option, Risk

FOSSIL_REQUIREMENT = NavigationRequirement("fossil", "fossil", False, "rom")
CUT_REQUIREMENT = NavigationRequirement("cut", "Cut and Cascade Badge", False, "party")

START = NavigationNode(1, 59, 10, 10)
APPROACH = NavigationNode(1, 59, 11, 8)
FOSSIL_TILE = NavigationNode(1, 59, 13, 7)
ARRIVAL = NavigationNode(1, 60, 5, 5)
TARGET = NavigationTarget(1, 60)


class _FakeGraph:
    """The navigation-graph surface the planner and the option adapter read."""

    def __init__(self, edges=(), *, names=None):
        self._edges: dict[NavigationNode, list[NavigationEdge]] = {}
        for edge in edges:
            self._edges.setdefault(edge.source, []).append(edge)
        self._names = names or {}
        self.maps = {}

    def state(self, map_key):
        name = self._names.get(tuple(map_key))
        return None if name is None else SimpleNamespace(identity=SimpleNamespace(name=name))

    def stateful_flags_toward(self, _start, _target):
        return frozenset()

    def route_script_unknown(self, _plan):
        return None

    def edges_from(self, node, _capabilities=None, *, live=None, include_unmet=False):
        edges = self._edges.get(node, ())
        return tuple(edges) if include_unmet else tuple(edge for edge in edges if edge.available)


def _route(*, blocked: bool = True) -> tuple[NavigationEdge, ...]:
    blocker = (FOSSIL_REQUIREMENT,) if blocked else (CUT_REQUIREMENT,)
    return (
        NavigationEdge("walk:11:8", START, APPROACH, "walk", 4),
        NavigationEdge(
            "fossil:2",
            APPROACH,
            FOSSIL_TILE,
            "field_move",
            6,
            blocker,
            executor_target=(("kind", "obstacle"), ("role", "fossil"), ("local_id", 2)),
        ),
        NavigationEdge("warp:60:1", FOSSIL_TILE, ARRIVAL, "warp", 2),
    )


def _fossil_option() -> Option:
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


def test_planner_keeps_the_fossil_edge_as_the_blocked_choice_step():
    planner = NavigationPlanner(_FakeGraph(_route()))
    plan = planner.plan(START, TARGET, CapabilityState())

    assert plan.cost == 12 + 10_000  # one unmet edge carries the blocked-search penalty
    assert plan.unmet == ("fossil",)
    assert plan.blocked_choice_step == "fossil:2"
    assert [edge.stable_key for edge in plan.route] == ["walk:11:8", "fossil:2", "warp:60:1"]
    # The fossil is the executor-sized next step after the walking prefix.
    assert next_semantic_edge(plan).stable_key == "fossil:2"


def test_planner_leaves_a_capability_block_unresolved():
    planner = NavigationPlanner(_FakeGraph(_route(blocked=False)))
    plan = planner.plan(START, TARGET, CapabilityState())

    assert plan.unmet == ("Cut and Cascade Badge",)
    assert plan.blocked_choice_step is None


def test_planner_does_not_treat_a_mixed_requirement_as_a_choice():
    edge = NavigationEdge(
        "field_move:2",
        APPROACH,
        FOSSIL_TILE,
        "field_move",
        6,
        (FOSSIL_REQUIREMENT, CUT_REQUIREMENT),
    )
    graph = _FakeGraph(
        (
            NavigationEdge("walk:11:8", START, APPROACH, "walk", 4),
            edge,
            NavigationEdge("warp:60:1", FOSSIL_TILE, ARRIVAL, "warp", 2),
        )
    )

    plan = NavigationPlanner(graph).plan(START, TARGET, CapabilityState())

    assert plan.unmet == ("fossil", "Cut and Cascade Badge")
    assert plan.blocked_choice_step is None


def test_corridor_frontier_reports_a_choice_step():
    corridor = (START.map_key, ARRIVAL.map_key)
    # The frontier only tracks closed edges that cross to the next corridor map, so the
    # fossil edge is synthetic here; its choice requirement is the real contract.
    graph = _FakeGraph(
        (
            NavigationEdge("walk:11:8", START, APPROACH, "walk", 4),
            NavigationEdge(
                "fossil:2",
                APPROACH,
                ARRIVAL,
                "field_move",
                6,
                (FOSSIL_REQUIREMENT,),
                executor_target=(("kind", "obstacle"), ("role", "fossil"), ("local_id", 2)),
            ),
        )
    )
    plan = NavigationPlanner(graph).plan_to_corridor_frontier(
        START, TARGET, corridor, CapabilityState()
    )

    assert plan is not None
    assert plan.unmet == ("fossil",)
    assert plan.blocked_choice_step == "fossil:2"
    assert plan.route[-1].stable_key == "fossil:2"


def _annotate(plan: NavigationPlan):
    adapter = NavigationAdapter(_FakeGraph(names={(1, 59): "MtMoon_B2F", (1, 60): "MtMoon_B1F"}))
    options, _ = adapter.annotate(
        [_fossil_option(), _exit_option()],
        SimpleNamespace(map=None),
        None,
        plan=plan,
    )
    return {option.id: option for option in options}


def test_no_route_through_a_fossil_still_advertises_the_fossil_step():
    # The incident's shape: the search returned no path, but the only blocker is the
    # fossil choice, so the option that takes it is the route step.
    plan = NavigationPlan(
        START,
        TARGET,
        None,
        unmet=("fossil",),
        blocked_route_fact="route blocked by fossil at MtMoon_B2F (13,7)",
        blocked_choice_step="fossil:2",
    )

    options = _annotate(plan)

    fossil = options["fossil:2"]
    assert fossil.facts["next_route_step"] is True
    assert fossil.facts["route_step"] is True
    assert fossil.facts["on_route"] is True
    assert fossil.facts["serves_goal"] is True
    assert fossil.facts["route_needs"] == ["fossil"]
    assert fossil.facts["blocked_choice"] == "fossil:2"
    assert "no_route" not in fossil.facts
    assert fossil.irreversible is True
    assert fossil.risk is Risk.HIGH
    # The way back out is still legal, but it is not the route.
    assert options["warp:1:3:1"].facts["no_route"] is True
    assert "next_route_step" not in options["warp:1:3:1"].facts


def test_choice_edge_inside_the_route_is_marked_as_the_step():
    route = _route()
    plan = NavigationPlan(
        START,
        TARGET,
        sum(edge.cost for edge in route),
        route,
        unmet=("fossil",),
        blocked_choice_step="fossil:2",
    )

    options = _annotate(plan)

    fossil = options["fossil:2"]
    assert fossil.facts["next_route_step"] is True
    assert fossil.facts["serves_goal"] is True
    assert fossil.facts["route_blocked"].startswith("route blocked by fossil at MtMoon_B2F")
    assert options["warp:1:3:1"].facts.get("next_route_step") is not True


def test_fossil_requirement_is_never_met_by_a_capability():
    # This is why the fossil edge is a decision step and not something the world search
    # can unblock on its own.
    assert requirement("fossil", CapabilityState()).met is False
