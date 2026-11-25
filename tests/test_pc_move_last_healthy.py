"""PC MOVE placement must not swap away the last battle-capable party member.

The builder re-derives legality from a validated :class:`StateSnapshot`; these tests call the
registered ``pc_main`` handler with real storage/party models and a decoded held mon, never
planner metadata. Held evidence stays conservative: a decoded held egg is a proven non-battler,
while a non-egg held mon's HP has no held-specific reader confidence and stays unknown.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pokegent.actions.menu.system_flow import refs as R
from pokegent.actions.menu.system_flow.build_pc import _battle_capable, pc_main
from pokegent.actions.menu.system_flow.evidence import Verdict
from pokegent.actions.menu.system_flow.policy import SystemFlowOptions
from pokegent.games.gen3.firered.reader.schemas import (
    BoxMon,
    FlagSet,
    Health,
    PartyMember,
    Player,
    StateSnapshot,
    VarTable,
)
from pokegent.games.gen3.firered.reader.storage import BOX_CAPACITY, BOX_COUNT, PokemonStorage
from pokegent.options.models import Legality
from pokegent.options.registry import BuildContext
from pokegent.state.scenes import SceneResult

_UNSET = object()


def _mon(slot: int, pid: int, *, hp: int = 20, max_hp: int = 20, egg: bool = False) -> PartyMember:
    zero = {"hp": 0, "attack": 0, "defense": 0, "speed": 0, "sp_attack": 0, "sp_defense": 0}
    return PartyMember.model_validate(
        {
            "personality": pid,
            "ot_id": 1,
            "nickname": f"MON{pid}",
            "language": 2,
            "ot_name": "TEST",
            "markings": 0,
            "species": pid % 300 + 1,
            "held_item": 0,
            "experience": 0,
            "pp_bonuses": 0,
            "friendship": 70,
            "moves": (),
            "evs": zero,
            "ivs": zero,
            "is_egg": egg,
            "ability_num": 0,
            "pokerus": 0,
            "met_location": 0,
            "met_level": 5,
            "met_game": 0,
            "pokeball": 4,
            "ot_gender": 0,
            "slot": slot,
            "status": 0,
            "level": 5,
            "hp": hp,
            "max_hp": max_hp,
            "stats": {**zero, "hp": max_hp},
            "mail": 0,
        }
    )


def _box_mon(pid: int, *, egg: bool = False) -> BoxMon:
    zero = {"hp": 0, "attack": 0, "defense": 0, "speed": 0, "sp_attack": 0, "sp_defense": 0}
    return BoxMon.model_validate(
        {
            "personality": pid,
            "ot_id": 1,
            "nickname": f"MON{pid}",
            "language": 2,
            "ot_name": "TEST",
            "markings": 0,
            "species": pid % 300 + 1,
            "held_item": 0,
            "experience": 0,
            "pp_bonuses": 0,
            "friendship": 70,
            "moves": (),
            "evs": zero,
            "ivs": zero,
            "is_egg": egg,
            "ability_num": 0,
            "pokerus": 0,
            "met_location": 0,
            "met_level": 5,
            "met_game": 0,
            "pokeball": 4,
            "ot_gender": 0,
        }
    )


def _storage(held: object) -> PokemonStorage:
    boxes = tuple(
        tuple(None for _ in range(BOX_CAPACITY))
        for _ in range(BOX_COUNT)
    )
    return PokemonStorage(
        current_box=0,
        boxes=boxes,
        names=tuple(f"Box {index}" for index in range(BOX_COUNT)),
        wallpapers=tuple(range(BOX_COUNT)),
        box_option=2,
        ui_state=0,
        is_holding_mon=True,
        held_mon=held,
        held_origin_kind="box",
        held_origin_box=0,
        held_origin_slot=0,
    )


def _snapshot(
    party: tuple[PartyMember, ...],
    held: object,
    *,
    seq: int = 1,
    party_confidence: object = 1.0,
) -> StateSnapshot:
    confidence = {} if party_confidence is _UNSET else {"party": party_confidence}
    return StateSnapshot.create(
        rom_profile="synthetic",
        snapshot_seq=seq,
        health=Health(valid=True, confidence=confidence),
        player=Player(
            name="TEST",
            gender="male",
            tid=0,
            sid=0,
            map_group=0,
            map_num=0,
            map_layout_id=1,
            x=0,
            y=0,
            money=0,
            coins=0,
        ),
        party=party,
        flags=FlagSet(),
        vars=VarTable(),
        storage=_storage(held),
    )


def _place(
    party: tuple[PartyMember, ...],
    held: object,
    *,
    seq: int = 1,
    party_confidence: object = 1.0,
):
    snapshot = _snapshot(party, held, seq=seq, party_confidence=party_confidence)
    ctx = BuildContext(
        snapshot=snapshot,
        scene=SceneResult(scene_id=R.PC_MAIN, confidence=0.95),
    )
    verdict = Verdict(R.PC_MAIN, 0.95, evidence={"menu": "pc_storage", "phase": "storage"})
    draft = pc_main(ctx, snapshot, verdict, SystemFlowOptions())
    return {option.id: option for option in draft.options}


def _option(
    party: tuple[PartyMember, ...],
    held: object,
    slot: int,
    *,
    seq: int = 1,
    party_confidence: object = 1.0,
):
    return _place(party, held, seq=seq, party_confidence=party_confidence)[
        f"move:place:party:{slot}"
    ]


def test_held_egg_cannot_replace_the_sole_battle_capable_member():
    healthy, fainted, egg = _mon(0, 101), _mon(1, 102, hp=0), _mon(2, 103, egg=True)

    option = _option((healthy, fainted, egg), _box_mon(201, egg=True), 0)

    assert option.legality is Legality.UNAVAILABLE
    assert option.executor_ref is None


def test_unknown_companion_capability_keeps_the_sole_healthy_swap_unknown():
    # A companion with inconsistent HP is not proven able or unable, so "last" is unknown
    # rather than a proven last-capable refusal.
    healthy = _mon(0, 105)
    inconsistent = _mon(1, 106, hp=30, max_hp=20)

    option = _option((healthy, inconsistent), _box_mon(202, egg=True), 0)

    assert option.legality is Legality.UNKNOWN
    assert option.executor_ref is None


def test_held_egg_may_replace_a_fainted_or_egg_party_member():
    healthy, fainted, egg = _mon(0, 111), _mon(1, 112, hp=0), _mon(2, 113, egg=True)
    held = _box_mon(211, egg=True)

    options = _place((healthy, fainted, egg), held)

    assert options["move:place:party:1"].legality is Legality.LEGAL
    assert options["move:place:party:2"].legality is Legality.LEGAL
    assert options["move:place:party:1"].executor_ref == R.REF_PC_MOVE_PLACE


def test_another_battle_capable_companion_permits_the_swap():
    target, companion = _mon(0, 121), _mon(1, 122, hp=4)

    option = _option((target, companion), _box_mon(221, egg=True), 0)

    assert option.legality is Legality.LEGAL
    assert option.executor_ref == R.REF_PC_MOVE_PLACE


def test_positive_held_hp_is_not_admitted_without_held_read_confidence():
    # The reader validates movingMon HP consistency but exposes no held-specific read
    # confidence, so positive held HP alone cannot prove the party keeps a capable member.
    target, fainted = _mon(0, 131), _mon(1, 132, hp=0)

    option = _option((target, fainted), _mon(0, 231, hp=9), 0)

    assert option.legality is Legality.UNKNOWN
    assert option.executor_ref is None


@pytest.mark.parametrize("hp,max_hp", [(0, 20), (30, 20), (-1, 20), (5, 0)])
def test_held_party_member_without_positive_hp_proof_is_unknown(hp, max_hp):
    target, fainted = _mon(0, 141), _mon(1, 142, hp=0)

    option = _option((target, fainted), _mon(0, 241, hp=hp, max_hp=max_hp), 0)

    assert option.legality is Legality.UNKNOWN
    assert option.executor_ref is None


def test_held_box_mon_has_no_hp_and_stays_unknown():
    target, fainted = _mon(0, 151), _mon(1, 152, hp=0)
    held = _box_mon(251, egg=False)

    assert _battle_capable(held) is None
    option = _option((target, fainted), held, 0)

    assert option.legality is Legality.UNKNOWN
    assert option.executor_ref is None


@pytest.mark.parametrize("confidence", [0.0, 0.7, _UNSET])
def test_unproven_party_read_makes_all_party_placements_unknown(confidence):
    healthy, fainted = _mon(0, 161), _mon(1, 162, hp=0)

    options = _place((healthy, fainted), _box_mon(261, egg=True), party_confidence=confidence)

    assert options["move:place:party:0"].legality is Legality.UNKNOWN
    assert options["move:place:party:0"].executor_ref is None
    assert options["move:place:party:1"].legality is Legality.UNKNOWN


def test_unknown_target_or_held_evidence_never_counts_as_healthy():
    unknown_target, fainted = _mon(0, 171, hp=30, max_hp=20), _mon(1, 172, hp=0)
    known_target = _mon(0, 181)
    unknown_held = SimpleNamespace(personality=271, is_egg=None, hp=10, max_hp=10)

    assert _battle_capable(SimpleNamespace(hp=10, max_hp=10, is_egg=None)) is None
    unknown_target_option = _option((unknown_target, fainted), _box_mon(281, egg=True), 0)
    assert unknown_target_option.legality is Legality.UNKNOWN
    assert _option((known_target, fainted), unknown_held, 0).legality is Legality.UNKNOWN


def test_box_destinations_remain_unaffected_by_party_health():
    healthy, fainted = _mon(0, 191), _mon(1, 192, hp=0)

    options = _place((healthy, fainted), _box_mon(291, egg=True))

    assert options["move:place:box:0:0"].legality is Legality.LEGAL
    assert options["move:place:box:0:0"].executor_ref == R.REF_PC_MOVE_PLACE


def test_fresh_rebuild_revalidates_a_changed_party():
    healthy, fainted = _mon(0, 201), _mon(1, 202, hp=0)
    held = _box_mon(301, egg=True)

    stale = _option((healthy, fainted), held, 0, seq=1)
    assert stale.legality is Legality.UNAVAILABLE

    healed = _mon(0, 201), _mon(1, 202, hp=1)
    fresh = _option(healed, held, 0, seq=2)
    assert fresh.legality is Legality.LEGAL
