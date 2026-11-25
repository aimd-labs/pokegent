"""Blocked field-move routes name the readable missing badge or party carrier."""

from types import SimpleNamespace

from test_planner_fossil_step import _FakeGraph

from pokegent.games.gen3 import mechanics_constants as K
from pokegent.games.gen3 import mechanics_ids as I
from pokegent.navigation.adapter import NavigationAdapter
from pokegent.navigation.capabilities import missing_field_move_detail
from pokegent.navigation.models import (
    NavigationEdge,
    NavigationNode,
    NavigationPlan,
    NavigationRequirement,
    NavigationTarget,
)
from pokegent.options.models import Legality, Option

START = NavigationNode(1, 59, 10, 10)
BOULDER = NavigationNode(1, 59, 13, 7)
TARGET = NavigationTarget(1, 60)
STRENGTH = NavigationRequirement("strength", "Strength and Rainbow Badge", False, "party")


def _snapshot(*, badges=(), carrier=True, usable=True, player=True):
    party = ()
    if carrier:
        party = (
            SimpleNamespace(
                species=6, moves=(SimpleNamespace(move_id=I.MOVE_STRENGTH),)
            ),
        )
    player_obj = (
        SimpleNamespace(
            avatar_x=10,
            avatar_y=10,
            facing="N",
            surfing=False,
            biking=False,
            badges=badges,
        )
        if player
        else None
    )
    state = SimpleNamespace(
        identity=SimpleNamespace(group=1, number=59, name="MtMoon_B2F"),
        player=player_obj,
        objects=(),
        strength_active=False,
    )
    return SimpleNamespace(
        map=state,
        player=player_obj,
        party=party,
        bag=(),
        flags=SimpleNamespace(set_ids=()),
        vars=SimpleNamespace(nonzero=()),
        ui=None,
        dynamic_warp=None,
        usable=usable,
    )


def _plan(*, blocked_route_fact=None, unmet=STRENGTH.label, requirement=STRENGTH):
    edge = NavigationEdge(
        "strength:2",
        START,
        BOULDER,
        "field_move",
        6,
        (requirement,),
        executor_target=(
            ("kind", "obstacle"),
            ("role", "boulder"),
            ("local_id", 2),
            ("at", (13, 7)),
        ),
    )
    return NavigationPlan(
        START,
        TARGET,
        6,
        (edge,),
        unmet=(unmet,),
        blocked_route_fact=blocked_route_fact,
    )


def _strength_option():
    return Option(
        id="strength:2",
        label="Use Strength on the boulder",
        legality=Legality.LEGAL,
        executor_ref="field.push_boulder",
        facts={
            "target": {
                "kind": "obstacle",
                "role": "boulder",
                "map": [1, 59],
                "local_id": 2,
                "at": [13, 7],
            },
            "obstacle": "boulder",
        },
    )


def _adapter():
    return NavigationAdapter(_FakeGraph(names={(1, 59): "MtMoon_B2F", (1, 60): "MtMoon_B1F"}))


def test_known_badge_without_a_carrier_names_the_missing_party_move():
    text = _adapter().blocked_route(
        _plan(), snapshot=_snapshot(badges=(K.BADGE_RAINBOW,), carrier=False)
    )

    assert text is not None
    assert "no party member knows Strength" in text
    assert "Rainbow Badge is not obtained" not in text
    assert text.startswith("route blocked by boulder at MtMoon_B2F (13,7)")


def test_known_carrier_without_the_badge_names_the_missing_badge():
    text = _adapter().blocked_route(_plan(), snapshot=_snapshot(carrier=True, badges=()))

    assert text is not None
    assert "Rainbow Badge is not obtained" in text
    assert "party has Strength" in text
    assert "no party member knows Strength" not in text


def test_both_missing_names_both_parts():
    text = _adapter().blocked_route(
        _plan(), snapshot=_snapshot(carrier=False, badges=())
    )

    assert text is not None
    assert "no party member knows Strength" in text
    assert "Rainbow Badge is not obtained" in text


def test_unreadable_snapshot_never_asserts_a_missing_carrier_or_badge():
    plan = _plan()
    base = _adapter().blocked_route(plan)
    degraded = _snapshot(usable=True, carrier=False)
    degraded.health = SimpleNamespace(valid=True, confidence={"party": 0.0})

    for snap in (
        _snapshot(usable=False, carrier=False),
        _snapshot(player=False, carrier=False),
        degraded,
    ):
        assert _adapter().blocked_route(plan, snapshot=snap) == base
        assert missing_field_move_detail(snap, "strength") is None


def test_unrelated_requirements_and_budget_exhaustion_keep_existing_text():
    adapter = _adapter()
    unknown = _plan(
        unmet="flag 0x123",
        requirement=NavigationRequirement("flag:0x123", "flag 0x123", False, "flag"),
    )
    assert adapter.blocked_route(unknown, snapshot=_snapshot(carrier=False)) == (
        "route blocked by boulder at MtMoon_B2F (13,7)"
    )

    exhausted = _plan(blocked_route_fact="search budget exhausted after 1 states")
    assert adapter.blocked_route(
        exhausted, snapshot=_snapshot(carrier=False, badges=())
    ) == "search budget exhausted after 1 states"


def test_fully_met_route_keeps_no_blocked_fact():
    plan = _plan(
        unmet=(),
        requirement=NavigationRequirement("strength", "Strength and Rainbow Badge", True, "party"),
    )

    assert _adapter().blocked_route(plan, snapshot=_snapshot(carrier=False)) is None


def test_annotate_exposes_the_enriched_blocked_route_to_options():
    plan = _plan()
    snap = _snapshot(carrier=True, badges=())
    options, _ = _adapter().annotate([_strength_option()], snap, None, plan=plan)

    facts = options[0].facts
    assert facts["next_route_step"] is True
    assert "Rainbow Badge is not obtained" in facts["route_blocked"]
    assert "party has Strength" in facts["route_blocked"]
