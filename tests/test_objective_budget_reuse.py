"""An abandoned bounded objective search is an exact-input UNKNOWN, not a reusable route.

The field builder asks navigation for a route on every perception.  A perception retry
(``Runner._handle_no_options`` settle-and-reread) rebuilds the same frame while the player has
not moved, so the same bounded search used to run again and exhaust the same 75k expansions.
These tests drive the real ``Perceiver -> BuilderRegistry -> FieldBuilder -> NavigationAdapter``
chain with a real frozen ``MapState`` and only the ROM graph edges replaced by a counting fixture.
"""

from types import SimpleNamespace

from test_probe_primitives import snapshot
from test_story_continuation import invented_state

import pokegent.navigation.planner as planner_module
from pokegent.actions import Registries
from pokegent.actions.field.builder import FieldBuilder
from pokegent.goals.models import Objective, Target
from pokegent.goals.source import GoalView
from pokegent.goals.validate import ResolvedObjective, ResolvedTarget
from pokegent.navigation.adapter import NavigationAdapter
from pokegent.navigation.models import NavigationEdge, NavigationNode
from pokegent.pipeline import Perceiver
from pokegent.state.scenes import FunctionDetector, PriorityClass, SceneResult


class SearchGraph:
    """Edges the real planner walks; each expansion is a ROM-graph lookup we can count."""

    def __init__(self, state):
        self.maps = {(0, 0): state}
        self.calls = 0
        self.provider = SimpleNamespace(puzzles=lambda **_kwargs: {})

    def node(self, map_key, x, y):
        return NavigationNode(*map_key, x, y)

    def state(self, map_key):
        return self.maps.get(tuple(map_key))

    def map_route(self, start, target):
        return (tuple(start), tuple(target))

    def stateful_flags_toward(self, _start, _target):
        return frozenset()

    def counted_trainers(self, *_args):
        return ()

    def route_script_unknown(self, _plan):
        return None

    def edges_from(self, node, _capabilities=None, *, live=None, include_unmet=False):
        self.calls += 1
        start = NavigationNode(0, 0, 1, 2)
        if node == start:
            return (
                NavigationEdge(
                    "walk:2:2", start, NavigationNode(0, 0, 2, 2), "walk", 1
                ),
            )
        return ()


def _resolved(*, objective_id="talk_objective", revision=1) -> ResolvedObjective:
    return ResolvedObjective(
        Objective(
            objective_id=objective_id,
            revision=revision,
            kind="talk",
            target=Target(map="DemoLab"),
        ),
        ResolvedTarget(group=0, number=0, npc=1),
        (),
    )


def _state():
    # Move the invented NPC next to the start tile so the target is a local interaction.
    original = invented_state()
    return original.model_copy(
        update={"objects": (original.objects[0].model_copy(update={"x": 2, "y": 1}),)}
    )


def _snapshot(state=None, *, flag=False):
    return snapshot(flag=flag).model_copy(update={"map": state or _state()})


def test_perceiver_retry_reuses_abandoned_unknown(monkeypatch):
    """The real perception retry caller must not repeat the abandoned search."""
    monkeypatch.setattr(planner_module, "OBJECTIVE_FRAME_PLAN_BUDGET", 1)
    state = _state()
    resolved = _resolved()
    graph = SearchGraph(state)
    adapter = NavigationAdapter(graph)
    builder = FieldBuilder(
        field_menu=False,
        objectives=lambda goal_id: resolved if goal_id == "talk_objective" else None,
        navigation=adapter,
    )
    registries = Registries.new()
    registries.builders.register_scenes(("field.free",), builder)
    registries.detectors.register(
        FunctionDetector(
            "synthetic_field",
            PriorityClass.FIELD,
            lambda _snapshot: SceneResult(
                scene_id="field.free", confidence=1, detector="synthetic_field"
            ),
        )
    )
    source = SimpleNamespace(read_snapshot=lambda: _snapshot(state))
    perceiver = Perceiver(
        source,
        registries.detectors,
        registries.builders,
        run_id="synthetic",
        goal=SimpleNamespace(
            current=lambda: GoalView("talk_objective", summary="talk to the DemoLab NPC")
        ),
    )

    first = perceiver.perceive("d-000001")
    calls_after_first = graph.calls
    second = perceiver.perceive("d-000002")

    assert calls_after_first > 0
    assert graph.calls == calls_after_first
    assert first.kind is second.kind
    # The unknown stays unknown: it is never turned into a route or a no-path proof.
    assert adapter._plan_cache and all(
        not hasattr(entry, "cost") for entry in adapter._plan_cache.values()
    )


def test_same_request_reuses_only_the_unknown_result():
    state = _state()
    graph = SearchGraph(state)
    adapter = NavigationAdapter(graph)

    assert adapter.plan(_snapshot(state), _resolved(), objective_budget=1) is None
    calls_after_first = graph.calls
    assert adapter.plan(_snapshot(state), _resolved(), objective_budget=1) is None
    assert graph.calls == calls_after_first
    assert calls_after_first > 0
    assert all(
        not hasattr(entry, "cost") for entry in adapter._plan_cache.values()
    )


def test_objective_revision_retries_the_abandoned_search():
    state = _state()
    graph = SearchGraph(state)
    adapter = NavigationAdapter(graph)

    assert adapter.plan(_snapshot(state), _resolved(), objective_budget=1) is None
    calls_after_first = graph.calls
    assert adapter.plan(
        _snapshot(state), _resolved(revision=2), objective_budget=1
    ) is None
    assert graph.calls > calls_after_first


def test_budget_change_retries_and_can_find_the_route():
    state = _state()
    graph = SearchGraph(state)
    adapter = NavigationAdapter(graph)

    assert adapter.plan(_snapshot(state), _resolved(), objective_budget=1) is None
    calls_after_first = graph.calls
    plan = adapter.plan(_snapshot(state), _resolved(), objective_budget=2)
    assert graph.calls > calls_after_first
    assert plan is not None
    assert plan.cost == 1
    calls_after_plan = graph.calls
    assert adapter.plan(_snapshot(state), _resolved(), objective_budget=2) is plan
    assert graph.calls == calls_after_plan


def test_live_geometry_change_retries_the_abandoned_search():
    state = _state()
    graph = SearchGraph(state)
    adapter = NavigationAdapter(graph)

    assert adapter.plan(_snapshot(state), _resolved(), objective_budget=1) is None
    calls_after_first = graph.calls
    changed = state.model_copy(
        update={
            "grid": state.grid.model_copy(
                update={"blocks": tuple([0] * (state.grid.columns * state.grid.rows))}
            )
        }
    )
    assert adapter.plan(_snapshot(changed), _resolved(), objective_budget=1) is None
    assert graph.calls > calls_after_first


def test_party_and_flags_change_retries_the_abandoned_search():
    state = _state()
    graph = SearchGraph(state)
    adapter = NavigationAdapter(graph)

    assert adapter.plan(_snapshot(state), _resolved(), objective_budget=1) is None
    calls_after_first = graph.calls
    assert adapter.plan(
        _snapshot(state, flag=True), _resolved(), objective_budget=1
    ) is None
    assert graph.calls > calls_after_first


def test_unhashable_stand_in_bypasses_the_cache():
    state = SimpleNamespace(
        identity=SimpleNamespace(group=1, number=59, name="MtMoon_B2F"),
        player=SimpleNamespace(
            avatar_x=10, avatar_y=10, facing="N", surfing=False, biking=False, badges=()
        ),
        objects=(),
        strength_active=False,
    )
    snap = SimpleNamespace(
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
    graph = SearchGraph(_state())
    adapter = NavigationAdapter(graph)
    resolved = _resolved()

    assert adapter.plan(snap, resolved, objective_budget=1) is None
    calls_after_first = graph.calls
    assert adapter.plan(snap, resolved, objective_budget=1) is None
    assert graph.calls > calls_after_first


def test_generic_planner_default_is_unchanged_and_still_cached():
    state = _state()
    graph = SearchGraph(state)
    adapter = NavigationAdapter(graph)

    plan = adapter.plan(_snapshot(state), _resolved())
    calls_after_first = graph.calls
    again = adapter.plan(_snapshot(state), _resolved())

    assert plan is not None
    assert again is plan
    assert calls_after_first > 0
    assert graph.calls == calls_after_first


def test_disabled_plan_cache_keeps_the_bounded_search_repeating():
    state = _state()
    graph = SearchGraph(state)
    adapter = NavigationAdapter(graph, plan_cache_size=0)

    assert adapter.plan(_snapshot(state), _resolved(), objective_budget=1) is None
    calls_after_first = graph.calls
    assert adapter.plan(_snapshot(state), _resolved(), objective_budget=1) is None
    assert graph.calls > calls_after_first
    assert not adapter._plan_cache
