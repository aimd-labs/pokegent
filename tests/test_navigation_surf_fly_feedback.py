"""Surf and Fly blocked routes name the readable missing badge or party carrier."""

from types import SimpleNamespace

import pytest
from test_planner_fossil_step import _FakeGraph

from pokegent.games.gen3 import mechanics_constants as K
from pokegent.games.gen3 import mechanics_ids as I
from pokegent.games.gen3.firered.reader.schemas import (
    FlagSet,
    Health,
    Player,
    ReadError,
    StateSnapshot,
    VarTable,
)
from pokegent.navigation.adapter import NavigationAdapter
from pokegent.navigation.capabilities import missing_field_move_fact
from pokegent.navigation.models import (
    NavigationEdge,
    NavigationNode,
    NavigationPlan,
    NavigationRequirement,
    NavigationTarget,
)
from pokegent.options.models import DecisionFrame, Legality, Option, to_system_one_payload

START = NavigationNode(1, 59, 10, 10)
SHORE = NavigationNode(1, 59, 11, 10)
WATER = NavigationNode(1, 59, 12, 10)
EXIT = NavigationNode(1, 58, 1, 1)
TARGET = NavigationTarget(1, 60)
HEALTHY = {
    "save_blocks": 1.0,
    "player": 1.0,
    "party": 1.0,
    "bag": 1.0,
    "flags": 1.0,
    "vars": 1.0,
    "pokedex": 1.0,
}

#: requirement key -> (move id, badge, move name, badge name)
MOVES = {
    "surf": (I.MOVE_SURF, K.BADGE_SOUL, "Surf", "Soul"),
    "fly": (I.MOVE_FLY, K.BADGE_THUNDER, "Fly", "Thunder"),
}


def _player(badges=()):
    return Player(
        name="Test",
        gender="male",
        tid=0,
        sid=0,
        map_group=1,
        map_num=59,
        map_layout_id=0,
        x=10,
        y=10,
        facing="N",
        money=0,
        coins=0,
        badges=badges,
    )


def _map_state(badges=()):
    return SimpleNamespace(
        identity=SimpleNamespace(group=1, number=59, name="MtMoon_B2F"),
        player=SimpleNamespace(
            avatar_x=10,
            avatar_y=10,
            facing="N",
            surfing=False,
            biking=False,
            badges=badges,
        ),
        objects=(),
        strength_active=False,
    )


def _snapshot(*, badges=(), move_ids=(), confidence=HEALTHY, player=True, usable=True, flags=()):
    party = (
        SimpleNamespace(
            species=6,
            slot=0,
            moves=tuple(SimpleNamespace(move_id=move_id) for move_id in move_ids),
        ),
    )
    if not usable:
        snap = StateSnapshot.create(
            rom_profile="synthetic",
            snapshot_seq=1,
            emulator_frame=0,
            health=Health(
                valid=False,
                read_errors=(
                    ReadError(
                        subsystem="test", code="invalid", message="invented read failure"
                    ),
                ),
                confidence=dict(confidence),
            ),
        )
        return snap.model_copy(update={"map": _map_state(badges)})
    snap = StateSnapshot.create(
        rom_profile="synthetic",
        snapshot_seq=1,
        emulator_frame=0,
        health=Health(valid=True, confidence=dict(confidence)),
        player=_player(badges),
        flags=FlagSet(set_ids=tuple(flags)) if flags is not None else None,
        vars=VarTable(nonzero=()),
    )
    snap = snap.model_copy(update={"party": party, "map": _map_state(badges)})
    if not player:
        snap = snap.model_copy(update={"player": None, "party": ()})
    return snap


def _requirement(key, *, met=False):
    _, _, move_name, badge_name = MOVES[key]
    return NavigationRequirement(key, f"{move_name} and {badge_name} Badge", met, "party")


def _plan(key, *, met=False, stable_key=None):
    requirement = _requirement(key, met=met)
    edge = NavigationEdge(
        stable_key or f"{key}:1:59:10:10:E",
        START,
        SHORE,
        key,
        4,
        (requirement,),
        direction="E",
        executor_target=(("kind", "walk"), ("step_dir", "E")),
    )
    return NavigationPlan(
        START,
        TARGET,
        4,
        (edge,),
        unmet=() if met else (requirement.label,),
    )


def _option(key):
    _, _, move_name, _ = MOVES[key]
    if key == "surf":
        return Option(
            id="surf:1:59:11:10",
            label=f"{move_name} across the water",
            legality=Legality.LEGAL,
            executor_ref="field.surf_start",
            facts={"target": {"kind": "surf", "map": [1, 59], "x": 11, "y": 10}},
        )
    return Option(
        id=f"fly:{move_name}",
        label=f"{move_name} to MtMoon_B1F",
        legality=Legality.LEGAL,
        executor_ref="field.fly",
        facts={"target": {"kind": "fly", "dest": [1, 60]}},
    )


def _adapter():
    return NavigationAdapter(_FakeGraph(names={(1, 59): "MtMoon_B2F", (1, 60): "MtMoon_B1F"}))


def _fact(key, *, badges=(), carrier=False):
    _, badge, move_name, badge_name = MOVES[key]
    move_ids = (MOVES[key][0],) if carrier else ()
    fact = missing_field_move_fact(_snapshot(badges=badges, move_ids=move_ids), key)
    assert fact is not None
    assert fact["move"] == move_name
    assert fact["badge"] == badge_name
    return fact


@pytest.mark.parametrize("key", ["surf", "fly"])
def test_both_missing_names_the_move_and_the_badge(key):
    _, _, move_name, badge_name = MOVES[key]

    fact = _fact(key)

    assert fact == {
        "move": move_name,
        "badge": badge_name,
        "carrier_missing": True,
        "badge_missing": True,
    }
    text = _adapter().blocked_route(_plan(key), snapshot=_snapshot())
    assert text is not None
    assert f"no party member knows {move_name}" in text
    assert f"{badge_name} Badge is not obtained" in text


@pytest.mark.parametrize("key", ["surf", "fly"])
def test_missing_carrier_only_names_the_party_move(key):
    _, badge, move_name, badge_name = MOVES[key]

    fact = _fact(key, badges=(badge,))

    assert fact == {
        "move": move_name,
        "badge": badge_name,
        "carrier_missing": True,
        "badge_missing": False,
    }
    text = _adapter().blocked_route(_plan(key), snapshot=_snapshot(badges=(badge,)))
    assert text is not None
    assert f"no party member knows {move_name} ({badge_name} Badge obtained)" in text
    assert "is not obtained" not in text


@pytest.mark.parametrize("key", ["surf", "fly"])
def test_missing_badge_only_names_the_badge(key):
    _, _, move_name, badge_name = MOVES[key]

    fact = _fact(key, carrier=True)

    assert fact == {
        "move": move_name,
        "badge": badge_name,
        "carrier_missing": False,
        "badge_missing": True,
    }
    text = _adapter().blocked_route(_plan(key), snapshot=_snapshot(move_ids=(MOVES[key][0],)))
    assert text is not None
    assert f"the {badge_name} Badge is not obtained (party has {move_name})" in text
    assert "no party member knows" not in text


@pytest.mark.parametrize("key", ["surf", "fly"])
def test_met_path_produces_no_gap(key):
    _, badge, _, _ = MOVES[key]
    snap = _snapshot(badges=(badge,), move_ids=(MOVES[key][0],))
    adapter = _adapter()

    assert missing_field_move_fact(snap, key) is None
    assert adapter.blocked_route(_plan(key, met=True), snapshot=snap) is None


@pytest.mark.parametrize(
    "confidence",
    [
        {},
        {"party": 0.0, "player": 1.0},
        {"party": 1.0, "player": 0.0},
        {"party": 1.0},
    ],
)
@pytest.mark.parametrize("key", ["surf", "fly"])
def test_absent_or_degraded_confidence_never_asserts_a_negative(key, confidence):
    plan = _plan(key)
    base = _adapter().blocked_route(plan)
    snap = _snapshot(confidence=confidence)

    assert _adapter().blocked_route(plan, snapshot=snap) == base
    assert missing_field_move_fact(snap, key) is None


@pytest.mark.parametrize("key", ["surf", "fly"])
def test_unusable_snapshot_or_missing_player_never_asserts_a_negative(key):
    plan = _plan(key)
    base = _adapter().blocked_route(plan)

    for snap in (
        _snapshot(usable=False),
        _snapshot(player=False),
    ):
        assert _adapter().blocked_route(plan, snapshot=snap) == base
        assert missing_field_move_fact(snap, key) is None


def test_fly_gap_ignores_map_and_visited_gates():
    # An indoor map with no visited flags set: the gap still reports only carrier/badge.
    no_visited = _snapshot(move_ids=(I.MOVE_FLY,))
    assert missing_field_move_fact(no_visited, "fly") == {
        "move": "Fly",
        "badge": "Thunder",
        "carrier_missing": False,
        "badge_missing": True,
    }
    both = _snapshot(badges=(K.BADGE_THUNDER,), move_ids=(I.MOVE_FLY,))
    assert missing_field_move_fact(both, "fly") is None


def test_annotate_exposes_the_gap_to_the_blocked_option():
    _, _, move_name, badge_name = MOVES["surf"]
    options, _ = _adapter().annotate(
        [_option("surf")], _snapshot(move_ids=(I.MOVE_SURF,)), None, plan=_plan("surf")
    )

    facts = options[0].facts
    assert "Soul Badge is not obtained" in facts["route_blocked"]
    assert f"party has {move_name}" in facts["route_blocked"]
    assert facts["field_move_gap"] == {
        "move": move_name,
        "badge": badge_name,
        "carrier_missing": False,
        "badge_missing": True,
    }


def _route_with_prefix(key, *, snapshot):
    """A chooseable first step whose route continues onto the blocked surf/fly edge."""
    requirement = _requirement(key)
    warp = NavigationEdge(
        "warp:1:59:0",
        EXIT,
        SHORE,
        "warp",
        2,
        executor_target=(("kind", "warp"), ("warp", 0)),
    )
    blocked = NavigationEdge(
        f"{key}:1:59:11:10:E",
        SHORE,
        WATER,
        key,
        4,
        (requirement,),
        direction="E",
        executor_target=(("kind", "walk"), ("step_dir", "E")),
    )
    plan = NavigationPlan(
        EXIT, TARGET, 6, (warp, blocked), unmet=(requirement.label,)
    )
    option = Option(
        id="warp:1:59:0",
        label="Take the exit",
        legality=Legality.LEGAL,
        executor_ref="field.warp",
        facts={"target": {"kind": "warp", "index": 0}},
    )
    return _adapter().annotate([option], snapshot, None, plan=plan)[0]


@pytest.mark.parametrize("key", ["surf", "fly"])
def test_gap_reaches_payload_criteria_on_the_next_step(key):
    _, _, move_name, badge_name = MOVES[key]
    carrier_snapshot = _snapshot(move_ids=(MOVES[key][0],))
    options = _route_with_prefix(key, snapshot=carrier_snapshot)
    option = options[0]
    assert option.chooseable
    assert option.facts["next_route_step"] is True
    assert option.facts["field_move_gap"]["badge_missing"] is True

    frame = DecisionFrame(
        decision_id=f"prereqs168-{key}",
        run_id="",
        snapshot_seq=1,
        scene_id="field.free",
        scene_confidence=1.0,
        state_fingerprint="sha256:" + "c" * 64,
        options=(option,),
    )

    criteria = to_system_one_payload(frame)["criteria"][option.id]
    assert f"{badge_name} Badge not obtained" in criteria
    assert f"party has {move_name}" in criteria
