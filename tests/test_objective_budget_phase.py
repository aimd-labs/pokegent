"""#196: the single objective-budget event names the search phase that spent the allowance.

Each of the planner's five expansion owners takes a fixed phase from its own callsite.  These
tests drive the real ``NavigationPlanner`` (and the adapter's objective frame for the end-to-end
case) over tiny synthetic graphs, so every assertion is about the existing single event rather
than a second event, a new schema field layer, or a full-allowance rehearsal.
"""

from __future__ import annotations

import pytest
from test_objective_budget_reuse import SearchGraph, _resolved, _snapshot, _state

import pokegent.navigation.planner as planner_module
from pokegent.events import EventType, MemoryEventLog
from pokegent.navigation.adapter import NavigationAdapter
from pokegent.navigation.models import (
    NavigationEdge,
    NavigationNode,
    NavigationRequirement,
    NavigationTarget,
)
from pokegent.navigation.planner import NavigationPlanner, objective_frame_budget

START = NavigationNode(0, 0, 1, 2)
FACT = planner_module.OBJECTIVE_FRAME_BUDGET_FACT


def _walk(source, destination):
    return NavigationEdge(f"walk:{destination.x}:{destination.y}", source, destination, "walk", 1)


class PhaseGraph:
    """A short countable map, optionally with one capability-blocked edge off the start."""

    def __init__(self, *, blocked_edge=False, length=2):
        self.calls = 0
        self.maps = {}
        self.blocked_edge = blocked_edge
        self.nodes = tuple(NavigationNode(0, 0, 1 + index, 2) for index in range(length))

    def node(self, map_key, x, y):
        return NavigationNode(*map_key, x, y)

    def state(self, map_key):
        return self.maps.get(tuple(map_key))

    def stateful_flags_toward(self, _start, _target):
        return frozenset()

    def edges_from(self, node, _capabilities=None, *, live=None, include_unmet=False):
        self.calls += 1
        edges = []
        index = next((i for i, candidate in enumerate(self.nodes) if candidate == node), None)
        if index is not None and index + 1 < len(self.nodes):
            edges.append(_walk(node, self.nodes[index + 1]))
        if self.blocked_edge and include_unmet and node == START:
            edges.append(
                NavigationEdge(
                    "walk:blocked",
                    START,
                    NavigationNode(0, 0, 3, 2),
                    "walk",
                    1,
                    requirements=(
                        NavigationRequirement("badge", "Cascade Badge", False, "badge"),
                    ),
                )
            )
        return tuple(edges)


def _records(events):
    return events.of_type(EventType.PLAN_BUDGET_EXCEEDED)


def _assert_objective_event(record, *, phase, explored, limit):
    details = dict(record.details)
    assert record.public_visibility is True
    assert details["scope"] == "objective"
    assert details["reason"] == "expansion_limit"
    assert details["phase"] == phase
    assert details["explored"] == explored
    assert details["limit"] == limit
    assert details["explored"] <= details["limit"]


def test_available_and_blocked_searches_keep_their_existing_phase_vocabulary():
    events = MemoryEventLog("synthetic")
    planner = NavigationPlanner(PhaseGraph(length=4), events=events)
    with objective_frame_budget(events, 1):
        plan = planner.plan(START, NavigationTarget(0, 0, x=9, y=2))
    assert plan.cost is None and plan.blocked_route_fact == FACT
    [available] = _records(events)
    _assert_objective_event(available, phase="available", explored=1, limit=1)

    events = MemoryEventLog("synthetic")
    planner = NavigationPlanner(PhaseGraph(blocked_edge=True, length=2), events=events)
    with objective_frame_budget(events, 2):
        plan = planner.plan(START, NavigationTarget(0, 0, x=9, y=2))
    assert plan.cost is None and plan.blocked_route_fact == FACT
    [blocked] = _records(events)
    _assert_objective_event(blocked, phase="blocked", explored=2, limit=2)


@pytest.mark.parametrize(
    ("phase", "run"),
    [
        (
            "choice_frontier",
            lambda planner: planner.plan_next_decision(START, NavigationTarget(0, 0)),
        ),
        ("maps", lambda planner: planner.plan_to_maps(START, frozenset({(9, 9)}))),
        ("tiles", lambda planner: planner.plan_to_tiles(START, (0, 0), frozenset({(9, 9)}))),
        (
            "corridor",
            lambda planner: planner.plan_to_corridor_frontier(
                START, NavigationTarget(1, 0), ((0, 0), (1, 0))
            ),
        ),
    ],
)
def test_other_expansion_owners_report_their_fixed_phase(phase, run):
    events = MemoryEventLog("synthetic")
    planner = NavigationPlanner(PhaseGraph(length=2), events=events)
    with objective_frame_budget(events, 1):
        assert run(planner) is None
    [record] = _records(events)
    _assert_objective_event(record, phase=phase, explored=1, limit=1)


def test_one_objective_event_per_scope_even_across_owners():
    events = MemoryEventLog("synthetic")
    planner = NavigationPlanner(PhaseGraph(length=2), events=events)
    with objective_frame_budget(events, 1):
        assert planner.plan_to_maps(START, frozenset({(9, 9)})) is None
        assert planner.plan_to_tiles(START, (0, 0), frozenset({(9, 9)})) is None
        assert planner.plan_next_decision(START, NavigationTarget(0, 0)) is None
    [record] = _records(events)
    _assert_objective_event(record, phase="maps", explored=1, limit=1)


def test_adapter_objective_frame_event_carries_the_phase():
    state = _state()
    events = MemoryEventLog("synthetic")
    adapter = NavigationAdapter(SearchGraph(state), events=events)

    assert adapter.plan(_snapshot(state), _resolved(), objective_budget=1) is None
    [record] = _records(events)
    _assert_objective_event(record, phase="available", explored=1, limit=1)


def test_reachable_search_emits_no_budget_event():
    events = MemoryEventLog("synthetic")
    planner = NavigationPlanner(PhaseGraph(length=3), events=events)
    with objective_frame_budget(events, 2):
        plan = planner.plan(START, NavigationTarget(0, 0, x=2, y=2))
    assert plan.cost == 1 and plan.route
    assert _records(events) == []
