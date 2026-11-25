"""#196: bounded objective exhaustion keeps its exact reason in the served field frame.

The real planner produces ``blocked_route_fact="objective route search budget exhausted"``,
but the adapter abandons the search as an exact-input UNKNOWN.  These tests drive the real
``FieldBuilder``, ``NavigationAdapter`` and planner with a synthetic counting graph: the reason
must reach the existing safe diagnostics on the fresh and cached frames, while ``plan()`` stays
``None`` and no partial plan is admitted.  Target-less and no-path cases stay unknown.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_objective_budget_reuse import SearchGraph, _resolved, _snapshot, _state

import pokegent.navigation.planner as planner_module
from pokegent.actions.field.builder import FieldBuilder
from pokegent.navigation.adapter import NavigationAdapter, _BudgetExhausted
from pokegent.options.diagnostics import bounded_pause_reasons, served_diagnostics
from pokegent.options.models import DecisionFrame
from pokegent.options.registry import BuildContext
from pokegent.state.scenes import SceneResult

FACT = planner_module.OBJECTIVE_FRAME_BUDGET_FACT


class _NoPathGraph(SearchGraph):
    """A finite graph with no edge: the planner proves no path, never exhaustion."""

    def edges_from(self, node, capabilities=None, *, live=None, include_unmet=False):
        self.calls += 1
        return ()


def _builder(adapter, resolved):
    return FieldBuilder(
        field_menu=False,
        objectives=lambda goal_id: resolved if goal_id == "talk_objective" else None,
        navigation=adapter,
    )


def _frame(builder, snap, decision_id: str):
    draft = builder.build(
        BuildContext(
            snapshot=snap,
            scene=SceneResult(
                scene_id="field.free", confidence=1, detector="synthetic_field"
            ),
            goal_id="talk_objective",
        )
    )
    return DecisionFrame.from_options(
        draft.options,
        decision_id=decision_id,
        run_id="synthetic",
        snapshot_seq=snap.snapshot_seq,
        scene_id="field.free",
        scene_confidence=1.0,
        state_fingerprint=draft.state_fingerprint,
        goal_id="talk_objective",
        context=draft.context,
    )


def _budget_reasons(frame):
    return tuple(
        item["reason"]
        for item in served_diagnostics(frame)["items"]
        if item["legality"] == "UNAVAILABLE"
    )


def test_fresh_and_cached_exhaustion_both_serve_the_exact_reason(monkeypatch):
    monkeypatch.setattr(planner_module, "OBJECTIVE_FRAME_PLAN_BUDGET", 1)
    state = _state()
    snap = _snapshot(state)
    resolved = _resolved()
    graph = SearchGraph(state)
    adapter = NavigationAdapter(graph)
    builder = _builder(adapter, resolved)

    first = _frame(builder, snap, "d196-fresh")
    calls_after_first = graph.calls
    second = _frame(builder, snap, "d196-cached")

    assert calls_after_first > 0
    assert graph.calls == calls_after_first
    for frame in (first, second):
        assert _budget_reasons(frame) == (FACT,)
        assert bounded_pause_reasons(frame) == (FACT,)
    # The public contract is unchanged: exhaustion is still an exact UNKNOWN, never a plan.
    assert adapter.plan(snap, resolved, objective_budget=1) is None
    assert all(
        not hasattr(entry, "cost") for entry in adapter._plan_cache.values()
    )


def test_public_plan_result_keeps_plan_none_and_names_only_exhaustion(monkeypatch):
    monkeypatch.setattr(planner_module, "OBJECTIVE_FRAME_PLAN_BUDGET", 1)
    state = _state()
    snap = _snapshot(state)
    resolved = _resolved()
    adapter = NavigationAdapter(SearchGraph(state))

    result = adapter.plan_result(snap, resolved, objective_budget=1)

    assert result.plan is None
    assert result.unknown_reason == FACT
    assert adapter.plan(snap, resolved, objective_budget=1) is None
    assert all(isinstance(entry, _BudgetExhausted) for entry in adapter._plan_cache.values())


def test_reachable_plan_has_no_budget_diagnostic(monkeypatch):
    monkeypatch.setattr(planner_module, "OBJECTIVE_FRAME_PLAN_BUDGET", 2)
    state = _state()
    snap = _snapshot(state)
    resolved = _resolved()
    adapter = NavigationAdapter(SearchGraph(state))

    result = adapter.plan_result(snap, resolved, objective_budget=2)
    assert result.plan is not None
    assert result.plan.cost == 1
    assert result.unknown_reason is None

    frame = _frame(_builder(adapter, resolved), snap, "d196-reachable")
    assert FACT not in _budget_reasons(frame)
    assert not any(option.id == "diag:route_budget" for option in frame.diagnostics)


def test_targetless_objective_stays_unknown_without_a_reason():
    state = _state()
    snap = _snapshot(state)
    # The resolver refused a target: the adapter has nothing to search, not a budget proof.
    resolved = replace(_resolved(), navigation_reason="no ROM gym leader mapping for badge 1")
    adapter = NavigationAdapter(SearchGraph(state))

    result = adapter.plan_result(snap, resolved, objective_budget=1)
    assert result.plan is None
    assert result.unknown_reason is None

    frame = _frame(_builder(adapter, resolved), snap, "d196-targetless")
    assert _budget_reasons(frame) == ()


def test_finite_no_path_plan_stays_unknown_without_a_reason():
    state = _state()
    snap = _snapshot(state)
    # A concrete target on another map that has no graph edge: a finite no-path proof, not
    # budget exhaustion.  The adapter must not attach a budget reason to it.
    resolved = replace(
        _resolved(), target=replace(_resolved().target, group=1, number=0, npc=None)
    )
    adapter = NavigationAdapter(_NoPathGraph(state))

    result = adapter.plan_result(snap, resolved, objective_budget=75_000)
    assert result.plan is not None
    assert result.plan.cost is None
    assert result.plan.blocked_route_fact is not None
    assert result.unknown_reason is None

    frame = _frame(_builder(adapter, resolved), snap, "d196-nopath")
    assert FACT not in _budget_reasons(frame)


def test_disabled_cache_delivers_the_reason_without_caching(monkeypatch):
    monkeypatch.setattr(planner_module, "OBJECTIVE_FRAME_PLAN_BUDGET", 1)
    state = _state()
    snap = _snapshot(state)
    resolved = _resolved()
    graph = SearchGraph(state)
    adapter = NavigationAdapter(graph, plan_cache_size=0)

    result = adapter.plan_result(snap, resolved, objective_budget=1)
    calls_first = graph.calls
    assert (result.plan, result.unknown_reason) == (None, FACT)
    assert not adapter._plan_cache

    again = adapter.plan_result(snap, resolved, objective_budget=1)
    assert (again.plan, again.unknown_reason) == (None, FACT)
    assert graph.calls > calls_first
    assert not adapter._plan_cache


def test_unhashable_input_delivers_the_reason_without_caching(monkeypatch):
    monkeypatch.setattr(planner_module, "OBJECTIVE_FRAME_PLAN_BUDGET", 1)
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
    adapter = NavigationAdapter(SearchGraph(_state()))

    result = adapter.plan_result(snap, _resolved(), objective_budget=1)

    assert (result.plan, result.unknown_reason) == (None, FACT)
    assert not adapter._plan_cache


def test_revision_and_budget_changes_retry_and_revise_provenance(monkeypatch):
    state = _state()
    snap = _snapshot(state)
    graph = SearchGraph(state)
    adapter = NavigationAdapter(graph)

    first = adapter.plan_result(snap, _resolved(revision=1), objective_budget=1)
    calls_after_first = graph.calls
    assert first.unknown_reason == FACT

    revised = adapter.plan_result(snap, _resolved(revision=2), objective_budget=1)
    assert graph.calls > calls_after_first
    assert (revised.plan, revised.unknown_reason) == (None, FACT)

    calls_after_revision = graph.calls
    reachable = adapter.plan_result(snap, _resolved(revision=2), objective_budget=2)
    assert graph.calls > calls_after_revision
    assert reachable.plan is not None
    assert reachable.unknown_reason is None


def test_lru_eviction_drops_provenance_and_research_rederives_it(monkeypatch):
    monkeypatch.setattr(planner_module, "OBJECTIVE_FRAME_PLAN_BUDGET", 1)
    state = _state()
    snap = _snapshot(state)
    graph = SearchGraph(state)
    adapter = NavigationAdapter(graph, plan_cache_size=1)

    first = adapter.plan_result(snap, _resolved(revision=1), objective_budget=1)
    assert first.unknown_reason == FACT
    assert len(adapter._plan_cache) == 1

    # A different identity evicts the first entry; its provenance cannot outlive it.
    second = adapter.plan_result(snap, _resolved(revision=2), objective_budget=1)
    assert second.unknown_reason == FACT
    assert len(adapter._plan_cache) == 1
    assert all(isinstance(entry, _BudgetExhausted) for entry in adapter._plan_cache.values())

    calls_before = graph.calls
    again = adapter.plan_result(snap, _resolved(revision=1), objective_budget=1)
    assert graph.calls > calls_before
    assert (again.plan, again.unknown_reason) == (None, FACT)


@pytest.mark.parametrize("objective_budget", [1])
def test_adapter_plan_and_lookup_agree_on_every_outcome(monkeypatch, objective_budget):
    monkeypatch.setattr(planner_module, "OBJECTIVE_FRAME_PLAN_BUDGET", objective_budget)
    state = _state()
    snap = _snapshot(state)
    resolved = _resolved()
    adapter = NavigationAdapter(SearchGraph(state))

    result = adapter.plan_result(snap, resolved, objective_budget=objective_budget)
    assert adapter.plan(snap, resolved, objective_budget=objective_budget) is result.plan
