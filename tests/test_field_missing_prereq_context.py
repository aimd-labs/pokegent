"""Field frames name the proven missing badge or carrier of a blocked route (#168).

The builder's navigation block used to drop the readable detail that the option layer
already proved: it called ``NavigationAdapter.blocked_route`` without the snapshot and a
later fallback could overwrite an enriched reason.  These tests drive the real
``FieldBuilder`` and assert the text reaches both the objective/planner context and the
System One payload state.
"""

from types import SimpleNamespace

import pytest
from test_field_targets_hidden_flags import _state
from test_planner_fossil_step import _FakeGraph
from test_probe_primitives import snapshot

from pokegent.actions.field.builder import FieldBuilder
from pokegent.games.gen3 import mechanics_constants as K
from pokegent.games.gen3 import mechanics_ids as I
from pokegent.games.gen3.firered.maps.map_schema import WarpInfo
from pokegent.navigation.adapter import NavigationAdapter, NavigationLookup
from pokegent.navigation.models import (
    NavigationEdge,
    NavigationNode,
    NavigationPlan,
    NavigationRequirement,
    NavigationTarget,
)
from pokegent.options.models import DecisionFrame, to_system_one_payload
from pokegent.options.registry import BuildContext
from pokegent.state.scenes import SceneResult

START = NavigationNode(0, 0, 2, 2)
BOULDER = NavigationNode(0, 0, 3, 2)
TARGET = NavigationTarget(1, 0)
HEALTHY = {
    "save_blocks": 1.0,
    "player": 1.0,
    "party": 1.0,
    "bag": 1.0,
    "flags": 1.0,
    "vars": 1.0,
    "pokedex": 1.0,
}
BASE = "route blocked by boulder at DemoMap (3,2)"
MOVES = {
    "strength": (None, "Strength", K.BADGE_RAINBOW, "Rainbow"),
    "surf": (I.MOVE_SURF, "Surf", K.BADGE_SOUL, "Soul"),
    "fly": (I.MOVE_FLY, "Fly", K.BADGE_THUNDER, "Thunder"),
}
EDGE_KIND = {"strength": "field_move", "surf": "surf", "fly": "fly"}


def _room():
    """A walkable room with one door exit, so the frame is never empty."""
    return _state(()).model_copy(
        update={
            "warps": (
                WarpInfo(
                    index=1,
                    x=2,
                    y=0,
                    elevation=0,
                    dest_group=1,
                    dest_number=0,
                    dest_name="InventedNext",
                    dest_warp_id=0,
                    dynamic=False,
                    tile_behavior=None,
                    tile_kind="door",
                    trigger="door",
                    trigger_direction=None,
                ),
            )
        }
    )


def _snapshot(*, badges=(K.BADGE_RAINBOW,), carrier=None, confidence=HEALTHY):
    base = snapshot()
    party = ()
    if carrier is not None:
        party = (
            SimpleNamespace(
                slot=0,
                species=6,
                moves=(SimpleNamespace(move_id=carrier),),
            ),
        )
    return base.model_copy(
        update={
            "map": _room(),
            "party": party,
            "player": base.player.model_copy(update={"badges": badges}),
            "health": base.health.model_copy(update={"confidence": dict(confidence)}),
        }
    )


def _plan(
    key="strength", *, met=False, cost=6, unmet=None, blocked_route_fact=None
):
    move, name, badge, badge_name = MOVES[key]
    label = f"{name} and {badge_name} Badge"
    requirement = NavigationRequirement(key, label, met, "party")
    edge = NavigationEdge(
        f"{key}:1",
        START,
        BOULDER,
        EDGE_KIND[key],
        6,
        (requirement,),
        executor_target=(
            ("kind", "obstacle"),
            ("role", "boulder"),
            ("local_id", 1),
            ("at", (3, 2)),
        ),
    )
    return NavigationPlan(
        START,
        TARGET,
        cost,
        (edge,),
        unmet=tuple(unmet if unmet is not None else (() if met else (label,))),
        blocked_route_fact=blocked_route_fact,
    )


def _build(plan, snap):
    graph = _FakeGraph(names={(0, 0): "DemoMap", (1, 0): "InventedNext"})
    graph.scripted_warp_arrival = lambda *_args, **_kwargs: None
    adapter = NavigationAdapter(graph)
    adapter.plan_result = lambda *_args, **_kwargs: NavigationLookup(plan)
    builder = FieldBuilder(field_menu=False, navigation=adapter)
    ctx = BuildContext(
        snapshot=snap,
        scene=SceneResult(scene_id="field.free", confidence=1, detector="invented"),
    )
    return builder, ctx, builder.build(ctx)


def _payload(draft, snap):
    frame = DecisionFrame.from_options(
        draft.options,
        decision_id="prereqs168",
        run_id="",
        snapshot_seq=snap.snapshot_seq,
        scene_id="field.free",
        scene_confidence=1.0,
        state_fingerprint=draft.state_fingerprint,
        context=draft.context,
    )
    return to_system_one_payload(frame)


def test_blocked_strength_context_names_the_proven_missing_carrier():
    snap = _snapshot(badges=(K.BADGE_RAINBOW,), carrier=None)
    _builder, _ctx, draft = _build(_plan(), snap)

    reason = draft.context["blocked_route"]
    assert BASE in reason
    assert "no party member knows Strength" in reason
    assert "Rainbow Badge obtained" in reason
    # The route is blocked, so no offered option claims to be its next step; the door
    # exit and ordinary movement choices remain.
    assert not any(
        option.chooseable and option.facts.get("route_step") for option in draft.options
    )
    assert any(
        option.chooseable
        and option.facts.get("target", {}).get("kind") == "warp"
        for option in draft.options
    )
    assert "no party member knows Strength" in _payload(draft, snap)["state"][
        "blocked_route"
    ]


def test_blocked_strength_context_names_the_proven_missing_badge():
    snap = _snapshot(badges=(), carrier=MOVES["strength"][0] or I.MOVE_STRENGTH)
    _builder, _ctx, draft = _build(_plan(), snap)

    reason = draft.context["blocked_route"]
    assert "Rainbow Badge is not obtained" in reason
    assert "party has Strength" in reason
    assert "no party member knows Strength" not in reason


def test_unreadable_party_confidence_keeps_the_unproved_reason():
    snap = _snapshot(badges=(K.BADGE_RAINBOW,), carrier=None, confidence={})
    _builder, _ctx, draft = _build(_plan(), snap)

    assert draft.context["blocked_route"] == BASE


def test_fully_met_route_does_not_invent_a_prerequisite():
    snap = _snapshot(badges=(K.BADGE_RAINBOW,), carrier=I.MOVE_STRENGTH)
    _builder, _ctx, draft = _build(_plan(met=True), snap)

    assert "blocked_route" not in draft.context


def test_budget_exhaustion_alone_never_synthesizes_a_missing_move():
    snap = _snapshot(badges=(K.BADGE_RAINBOW,), carrier=None)
    _builder, _ctx, draft = _build(
        _plan(cost=None, unmet=(), blocked_route_fact="objective route search budget exhausted"),
        snap,
    )

    assert draft.context["blocked_route"] == "objective route search budget exhausted"
    assert "no party member knows Strength" not in draft.context["blocked_route"]


@pytest.mark.parametrize("key", ["surf", "fly"])
def test_other_field_move_requirements_keep_their_owning_facts(key):
    move, name, badge, badge_name = MOVES[key]
    snap = _snapshot(badges=(badge,), carrier=None)
    _builder, _ctx, draft = _build(_plan(key), snap)

    reason = draft.context["blocked_route"]
    assert f"no party member knows {name}" in reason
    assert f"{badge_name} Badge obtained" in reason
