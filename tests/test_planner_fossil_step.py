"""A route blocked only by the Mt Moon fossil choice is a decision step, not a dead end."""

import json
from types import SimpleNamespace

from pokegent.events import EventLog
from pokegent.goals.models import Objective, Target
from pokegent.goals.validate import ResolvedObjective, ResolvedTarget
from pokegent.navigation.adapter import NavigationAdapter
from pokegent.navigation.models import (
    CapabilityState,
    NavigationEdge,
    NavigationNode,
    NavigationPlan,
    NavigationRequirement,
    NavigationTarget,
)
from pokegent.navigation.planner import (
    NavigationPlanner,
    next_semantic_edge,
)
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


def _route(*, blocked: bool = True, arrival=ARRIVAL) -> tuple[NavigationEdge, ...]:
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
        NavigationEdge("warp:60:1", FOSSIL_TILE, arrival, "warp", 2),
    )


def _dead_end_route() -> tuple[NavigationEdge, ...]:
    """A fossil on the only path, but the target map is disconnected past it.

    Both searches explore the whole reachable area and find no route, so the frame used to be
    empty. The decision is still the next thing the agent must do.
    """
    walk, fossil, _warp = _route()
    return (walk, fossil)


def _unrelated_fossil_route() -> tuple[NavigationEdge, ...]:
    """A reachable fossil choice that does not lead to the target at all.

    The fossil exits toward a third map; nothing connects to the target map. The no-route
    fallback still names the decision, but it must not claim it serves the objective.
    """
    return (
        NavigationEdge("walk:11:8", START, APPROACH, "walk", 4),
        NavigationEdge(
            "fossil:2",
            APPROACH,
            FOSSIL_TILE,
            "field_move",
            6,
            (FOSSIL_REQUIREMENT,),
            executor_target=(("kind", "obstacle"), ("role", "fossil"), ("local_id", 2)),
        ),
        NavigationEdge(
            "warp:61:1", FOSSIL_TILE, NavigationNode(1, 61, 1, 1), "warp", 2
        ),
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


def _snapshot(*, group=1, number=59, x=10, y=10):
    state = SimpleNamespace(
        identity=SimpleNamespace(group=group, number=number, name="MtMoon_B2F"),
        player=SimpleNamespace(
            avatar_x=x,
            avatar_y=y,
            facing="N",
            surfing=False,
            biking=False,
            badges=(),
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


def _resolved() -> ResolvedObjective:
    return ResolvedObjective(
        objective=Objective(
            objective_id="reach_cerulean",
            revision=1,
            kind="reach",
            target=Target(map="Cerulean City"),
        ),
        target=ResolvedTarget(group=1, number=60, map_name="MtMoon_B1F"),
        probes=(),
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


def test_planner_does_not_treat_a_mixed_edge_as_a_choice():
    graph = _FakeGraph(
        (
            NavigationEdge("walk:11:8", START, APPROACH, "walk", 4),
            NavigationEdge(
                "field_move:2",
                APPROACH,
                FOSSIL_TILE,
                "field_move",
                6,
                (FOSSIL_REQUIREMENT, CUT_REQUIREMENT),
            ),
            NavigationEdge("warp:60:1", FOSSIL_TILE, ARRIVAL, "warp", 2),
        )
    )

    plan = NavigationPlanner(graph).plan(START, TARGET, CapabilityState())

    assert plan.unmet == ("fossil", "Cut and Cascade Badge")
    assert plan.blocked_choice_step is None


def test_planner_does_not_treat_a_fossil_then_cut_route_as_a_choice():
    """A decision after which the route still needs Cut is not a step the agent can decide."""
    graph = _FakeGraph(
        (
            NavigationEdge("walk:11:8", START, APPROACH, "walk", 4),
            NavigationEdge(
                "fossil:2",
                APPROACH,
                FOSSIL_TILE,
                "field_move",
                6,
                (FOSSIL_REQUIREMENT,),
            ),
            NavigationEdge(
                "cut:9", FOSSIL_TILE, ARRIVAL, "field_move", 6, (CUT_REQUIREMENT,)
            ),
        )
    )

    plan = NavigationPlanner(graph).plan(START, TARGET, CapabilityState())

    assert plan.unmet == ("fossil", "Cut and Cascade Badge")
    assert plan.blocked_choice_step is None


def test_planner_names_the_decision_when_no_route_exists():
    # No warp past the fossil: both searches find no route, which is the live incident shape.
    planner = NavigationPlanner(_FakeGraph(_dead_end_route(), names={(1, 59): "MtMoon_B2F"}))

    plan = planner.plan(START, TARGET, CapabilityState())

    assert plan.cost is None
    assert plan.route == ()
    assert plan.unmet == ("fossil",)
    assert plan.blocked_choice_step == "fossil:2"
    assert "next decision: fossil at MtMoon_B2F (13,7)" in plan.blocked_route_fact


def test_planner_does_not_name_a_capability_as_a_decision():
    graph = _FakeGraph(
        (NavigationEdge("walk:11:8", START, APPROACH, "walk", 4),
         NavigationEdge("cut:9", APPROACH, FOSSIL_TILE, "field_move", 6, (CUT_REQUIREMENT,))),
        names={(1, 59): "MtMoon_B2F"},
    )

    plan = NavigationPlanner(graph).plan(START, TARGET, CapabilityState())

    assert plan.cost is None
    assert plan.route == ()
    assert plan.blocked_choice_step is None


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

    adapter = NavigationAdapter(_FakeGraph(names={(1, 59): "MtMoon_B2F", (1, 60): "MtMoon_B1F"}))
    options, _ = adapter.annotate(
        [_fossil_option(), _exit_option()], SimpleNamespace(map=None), None, plan=plan
    )
    fossil = {option.id: option for option in options}["fossil:2"]

    assert fossil.facts["next_route_step"] is True
    assert fossil.facts["serves_goal"] is True
    assert fossil.facts["route_blocked"].startswith("route blocked by fossil at MtMoon_B2F")
    assert fossil.irreversible is True
    assert fossil.risk is Risk.HIGH


def test_no_route_plan_advertises_the_fossil_through_plan_and_annotate():
    """planner -> adapter.plan -> annotate: the empty frame names the decision."""
    graph = _FakeGraph(_dead_end_route(), names={(1, 59): "MtMoon_B2F", (1, 60): "MtMoon_B1F"})
    adapter = NavigationAdapter(graph)

    plan = adapter.plan(_snapshot(), _resolved())

    assert plan is not None
    assert plan.cost is None
    assert plan.route == ()

    options, annotated = adapter.annotate(
        [_fossil_option(), _exit_option()], _snapshot(), _resolved(), plan=plan
    )
    facts = {option.id: option.facts for option in options}

    assert annotated is plan
    assert facts["fossil:2"]["next_route_step"] is True
    assert facts["fossil:2"]["route_step"] is True
    assert facts["fossil:2"]["route_needs"] == ["fossil"]
    assert facts["fossil:2"]["blocked_choice"] == "fossil:2"
    assert "no_route" not in facts["fossil:2"]
    # No route to the target is known, so the step must not claim it serves the objective.
    assert "serves_goal" not in facts["fossil:2"]
    assert "on_route" not in facts["fossil:2"]
    # The way back out is legal, but it is not the route step.
    assert facts["warp:1:3:1"]["no_route"] is True
    assert "next_route_step" not in facts["warp:1:3:1"]
    assert plan.blocked_choice_step == "fossil:2"


def test_fully_explored_unrelated_target_names_the_decision_without_claiming_it():
    """The blocked search always completes here: the fossil does not lead to the target.

    The decision is still the only actionable step, but the frame must not advertise it as
    serving the objective or as being on its route.
    """
    graph = _FakeGraph(
        _unrelated_fossil_route(), names={(1, 59): "MtMoon_B2F", (1, 60): "MtMoon_B1F"}
    )
    adapter = NavigationAdapter(graph)

    plan = adapter.plan(_snapshot(), _resolved())

    assert plan is not None
    assert plan.cost is None
    assert plan.route == ()
    assert plan.blocked_choice_step == "fossil:2"

    options, _ = adapter.annotate(
        [_fossil_option(), _exit_option()], _snapshot(), _resolved(), plan=plan
    )
    facts = {option.id: option.facts for option in options}

    assert facts["fossil:2"]["next_route_step"] is True
    assert facts["fossil:2"]["blocked_choice"] == "fossil:2"
    assert "serves_goal" not in facts["fossil:2"]
    assert "on_route" not in facts["fossil:2"]


def test_real_search_counter_stops_at_expansion_limit(tmp_path):
    events = EventLog(tmp_path, "budget-test")
    planner = NavigationPlanner(_FakeGraph(_route()), events=events)
    calls = []
    original_frontier = planner._choice_frontier

    def record_frontier(*args, **kwargs):
        calls.append(True)
        return original_frontier(*args, **kwargs)

    planner._choice_frontier = record_frontier
    try:
        plan = planner.plan(START, TARGET, limit=1)
    finally:
        events.close()

    assert plan.cost is None and plan.route == () and not plan.reachable
    assert plan.explored == 1 and plan.blocked_choice_step is None
    assert "search budget exhausted after 1 states" in plan.blocked_route_fact
    assert calls == [True]  # preserve the original choice-frontier fallback
    records = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert records and all(row["type"] == "plan_budget_exceeded" for row in records)
    assert all(row["public_visibility"] for row in records)
    assert {row["details"]["phase"] for row in records} == {"available", "blocked"}
    assert all(row["details"]["expanded"] == row["details"]["limit"] == 1 for row in records)


def test_objective_budget_abandons_route_and_does_not_change_general_default(tmp_path):
    graph = _FakeGraph(_route(blocked=False))
    events = EventLog(tmp_path, "objective-budget-test")
    adapter = NavigationAdapter(graph, events=events)
    bounded = adapter.plan(_snapshot(), _resolved(), objective_budget=1)
    assert bounded is None
    record = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
    assert record["type"] == "plan_budget_exceeded"
    assert record["public_visibility"] is True
    assert record["details"]["scope"] == "objective"
    assert record["details"]["explored"] == 1
    ordinary = adapter.plan(_snapshot(), _resolved())
    assert ordinary is not None
    assert ordinary.cost == 10_012 and len(ordinary.route) == 3
    assert ordinary.unmet == ("Cut and Cascade Badge",)
    events.close()


def test_adapter_plan_cache_separates_bounded_and_general_calls(monkeypatch):
    from test_probe_primitives import snapshot
    from test_story_continuation import invented_state

    adapter = NavigationAdapter(_FakeGraph())
    state = invented_state()
    snap = snapshot().model_copy(update={"map": state})
    calls = []

    def plan(start, target, *_args, **_kwargs):
        calls.append(True)
        return NavigationPlan(start, target, None, blocked_route_fact="synthetic")

    monkeypatch.setattr(adapter.planner, "plan", plan)
    adapter.plan(snap, _resolved())
    adapter.plan(snap, _resolved(), objective_budget=75_000)
    adapter.plan(snap, _resolved())
    assert calls == [True, True]
    assert len(adapter._plan_cache) == 2
