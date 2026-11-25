"""Blocked field-move routes name the readable missing badge or party carrier."""

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
BOULDER = NavigationNode(1, 59, 13, 7)
TARGET = NavigationTarget(1, 60)
STRENGTH = NavigationRequirement("strength", "Strength and Rainbow Badge", False, "party")
ROCK_SMASH = NavigationRequirement("rock_smash", "Rock Smash and Thunder Badge", False, "party")
HEALTHY = {
    "save_blocks": 1.0,
    "player": 1.0,
    "party": 1.0,
    "bag": 1.0,
    "flags": 1.0,
    "vars": 1.0,
    "pokedex": 1.0,
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


def _snapshot(*, badges=(), carrier=True, confidence=HEALTHY, player=True, usable=True):
    party = (
        (
            SimpleNamespace(
                species=6, moves=(SimpleNamespace(move_id=I.MOVE_STRENGTH),)
            ),
        )
        if carrier
        else ()
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
        flags=FlagSet(set_ids=()),
        vars=VarTable(nonzero=()),
    )
    snap = snap.model_copy(update={"party": party, "map": _map_state(badges)})
    if not player:
        snap = snap.model_copy(update={"player": None, "party": ()})
    return snap


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


def _rock_smash_plan():
    edge = NavigationEdge(
        "rock_smash:4",
        START,
        BOULDER,
        "field_move",
        6,
        (ROCK_SMASH,),
        executor_target=(
            ("kind", "obstacle"),
            ("role", "rock_smash"),
            ("local_id", 4),
            ("at", (13, 7)),
        ),
    )
    return NavigationPlan(START, TARGET, 6, (edge,), unmet=(ROCK_SMASH.label,))


def _rock_smash_option():
    return Option(
        id="rock_smash:4",
        label="Use Rock Smash on the rock",
        legality=Legality.LEGAL,
        executor_ref="field.interact",
        facts={
            "target": {
                "kind": "obstacle",
                "role": "rock_smash",
                "map": [1, 59],
                "local_id": 4,
                "at": [13, 7],
            },
            "obstacle": "rock_smash",
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


@pytest.mark.parametrize(
    "confidence",
    [
        {},
        {"party": 0.0, "player": 1.0},
        {"party": 1.0, "player": 0.0},
        {"party": 1.0},
    ],
)
def test_absent_or_degraded_section_confidence_never_asserts_a_negative(confidence):
    plan = _plan()
    base = _adapter().blocked_route(plan)
    snap = _snapshot(carrier=False, confidence=confidence)

    assert _adapter().blocked_route(plan, snapshot=snap) == base
    assert missing_field_move_fact(snap, "strength") is None


def test_unusable_snapshot_or_missing_player_never_asserts_a_negative():
    plan = _plan()
    base = _adapter().blocked_route(plan)

    for snap in (
        _snapshot(usable=False, carrier=False),
        _snapshot(player=False, carrier=False),
    ):
        assert _adapter().blocked_route(plan, snapshot=snap) == base
        assert missing_field_move_fact(snap, "strength") is None


class _RaisingConfidence:
    def __init__(self, error):
        self.error = error

    def __bool__(self):
        return True

    def get(self, *_args):
        raise self.error


@pytest.mark.parametrize("error", [MemoryError(), SystemExit()])
def test_section_confidence_errors_are_not_swallowed(error):
    snap = SimpleNamespace(
        usable=True,
        player=SimpleNamespace(badges=()),
        party=(),
        health=SimpleNamespace(confidence=_RaisingConfidence(error)),
    )

    with pytest.raises(type(error)):
        missing_field_move_fact(snap, "strength")


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
    assert facts["field_move_gap"] == {
        "move": "Strength",
        "badge": "Rainbow",
        "carrier_missing": False,
        "badge_missing": True,
    }


def test_rock_smash_requirement_maps_to_the_smash_capability_owner():
    text = _adapter().blocked_route(
        _rock_smash_plan(), snapshot=_snapshot(badges=(K.BADGE_THUNDER,), carrier=False)
    )

    assert text is not None
    assert "no party member knows Rock Smash" in text
    assert "Thunder Badge obtained" in text


def test_rock_smash_gap_reaches_payload_criteria():
    options, _ = _adapter().annotate(
        [_rock_smash_option()],
        _snapshot(badges=(K.BADGE_THUNDER,), carrier=False),
        None,
        plan=_rock_smash_plan(),
    )
    option = options[0]
    frame = DecisionFrame(
        decision_id="prereqs168-rock-smash",
        run_id="",
        snapshot_seq=1,
        scene_id="field.free",
        scene_confidence=1.0,
        state_fingerprint="sha256:" + "b" * 64,
        options=(option,),
    )

    criteria = to_system_one_payload(frame)["criteria"][option.id]
    assert "no party member knows Rock Smash" in criteria
    assert "Thunder Badge obtained" in criteria


def test_payload_criteria_show_the_missing_badge_detail():
    plan = _plan()
    options, _ = _adapter().annotate(
        [_strength_option()], _snapshot(carrier=True, badges=()), None, plan=plan
    )
    option = options[0]
    frame = DecisionFrame(
        decision_id="prereqs168",
        run_id="",
        snapshot_seq=1,
        scene_id="field.free",
        scene_confidence=1.0,
        state_fingerprint="sha256:" + "a" * 64,
        context={"blocked_route": option.facts["route_blocked"]},
        options=(option,),
    )

    criteria = to_system_one_payload(frame)["criteria"][option.id]
    assert "Rainbow Badge not obtained" in criteria
    assert "party has Strength" in criteria
