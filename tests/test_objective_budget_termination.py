"""#196: the real 75,000-expansion objective allowance terminates once, bounded and measured.

Prior #219 proves only a tiny monkeypatched allowance keeps its provenance.  This drives the real
``FieldBuilder -> NavigationAdapter -> NavigationPlanner`` chain at the unchanged
``OBJECTIVE_FRAME_PLAN_BUDGET`` over a synthetic counting graph larger than the allowance, inside
a fixed-wall child process so a hang is interrupted; the small reachable and unusable-snapshot
controls run at the same real allowance and emit no budget event.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_field_budget_diagnostic import _budget_reasons, _builder, _frame
from test_objective_budget_reuse import SearchGraph, _resolved, _snapshot, _state
from test_probe_primitives import snapshot

import pokegent.navigation.planner as planner_module
from pokegent.events import EventType, MemoryEventLog
from pokegent.navigation.adapter import NavigationAdapter
from pokegent.navigation.models import NavigationEdge, NavigationNode

FACT = planner_module.OBJECTIVE_FRAME_BUDGET_FACT
WALL_BOUND_S = 60.0


class ChainGraph:
    """Prebuilt one-way chain longer than the allowance; each expansion is countable.

    It starts on the live player tile and stays off the rows adjacent to the target NPC, so no
    state matches and the only stop is the objective allowance itself.
    """

    def __init__(self, state):
        self.maps = {(0, 0): state}
        self.calls = 0
        self.provider = SimpleNamespace(puzzles=lambda **_kwargs: {})
        coordinates = [(1, 2), (1, 3), *((x, 3) for x in range(2, 75_001))]
        self._edges = {}
        for source, destination in zip(coordinates, coordinates[1:]):
            start, end = NavigationNode(0, 0, *source), NavigationNode(0, 0, *destination)
            self._edges[start] = (
                NavigationEdge(f"walk:{destination[0]}:3", start, end, "walk", 1),
            )

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
        return self._edges.get(node, ())


def _measure_probe() -> dict:
    """The one measured case: real budget, real builder/adapter/planner, wall and CPU kept."""
    budget = planner_module.OBJECTIVE_FRAME_PLAN_BUDGET
    state, resolved = _state(), _resolved()
    snap = _snapshot(state)
    graph, events = ChainGraph(state), MemoryEventLog("synthetic")
    adapter = NavigationAdapter(graph, events=events)
    builder = _builder(adapter, resolved)

    started, cpu_started = time.perf_counter(), time.process_time()
    fresh = _frame(builder, snap, "d196-full-fresh")
    wall_s, cpu_s = time.perf_counter() - started, time.process_time() - cpu_started
    calls_first = graph.calls

    cached = _frame(builder, snap, "d196-full-cached")
    lookup = adapter.plan_result(snap, resolved, objective_budget=budget)
    emitted = events.of_type(EventType.PLAN_BUDGET_EXCEEDED)
    return {
        "budget": budget, "wall_s": wall_s, "cpu_s": cpu_s,
        "calls_first": calls_first, "calls_cached": graph.calls,
        "calls_lookup": graph.calls,
        "fresh_reasons": list(_budget_reasons(fresh)),
        "cached_reasons": list(_budget_reasons(cached)),
        "lookup_plan_none": lookup.plan is None, "lookup_reason": lookup.unknown_reason,
        "events": [{"type": e.type, "observed_at": e.observed_at,
                    "public": e.public_visibility, "details": dict(e.details)}
                   for e in emitted],
    }


def _bounded_probe() -> dict:
    """Run the measured case in a child process the fixed timeout can actually kill."""
    tests_dir = Path(__file__).resolve().parent
    code = (
        "import json, sys;"
        f"sys.path[:0] = [{str(tests_dir)!r}, {str(tests_dir.parent)!r}];"
        "from test_objective_budget_termination import _measure_probe;"
        "print(json.dumps(_measure_probe()))"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=WALL_BOUND_S
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"measurement exceeded the fixed {WALL_BOUND_S:g}s wall bound")
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_one_full_allowance_terminates_once_with_typed_exhaustion():
    result = _bounded_probe()
    print("MEASURED:", json.dumps(result, sort_keys=True))
    budget = planner_module.OBJECTIVE_FRAME_PLAN_BUDGET
    assert result["budget"] == budget == 75_000
    assert result["calls_first"] == budget  # one expansion per unit, never overrun
    assert result["wall_s"] < WALL_BOUND_S and result["cpu_s"] < WALL_BOUND_S
    assert result["lookup_plan_none"] is True and result["lookup_reason"] == FACT
    assert result["fresh_reasons"] == [FACT] and result["cached_reasons"] == [FACT]
    assert result["calls_cached"] == result["calls_lookup"] == result["calls_first"]
    assert len(result["events"]) == 1
    event = result["events"][0]
    assert event["type"] == EventType.PLAN_BUDGET_EXCEEDED.value and event["public"] is True
    details = event["details"]
    assert details.get("scope") == "objective"
    assert details.get("explored") == details.get("limit") == 75_000
    assert details.get("reason") == "expansion_limit"
    observed_at = event["observed_at"]
    assert observed_at and datetime.fromisoformat(observed_at).tzinfo is not None


def test_small_reachable_graph_emits_no_budget_event():
    state, events = _state(), MemoryEventLog("synthetic")
    adapter = NavigationAdapter(SearchGraph(state), events=events)
    result = adapter.plan_result(_snapshot(state), _resolved(), objective_budget=75_000)
    assert result.plan is not None and result.plan.cost == 1
    assert result.unknown_reason is None
    assert events.of_type(EventType.PLAN_BUDGET_EXCEEDED) == []


def test_unusable_snapshot_stays_unknown_without_a_budget_event():
    events = MemoryEventLog("synthetic")
    adapter = NavigationAdapter(SearchGraph(_state()), events=events)
    unusable = snapshot(usable=False).model_copy(update={"map": None})
    result = adapter.plan_result(unusable, _resolved(), objective_budget=75_000)
    assert (result.plan, result.unknown_reason) == (None, None)
    assert events.of_type(EventType.PLAN_BUDGET_EXCEEDED) == []
