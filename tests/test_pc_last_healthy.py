"""PC deposit admission must never remove the last battle-capable party member.

The builder re-derives its verdict from a real :class:`StateSnapshot`; these tests call the
registered ``pc_main`` handler with validated storage/party models, not planner metadata.
"""

from __future__ import annotations

import pytest

from pokegent.actions.menu.system_flow import refs as R
from pokegent.actions.menu.system_flow.build_pc import pc_main
from pokegent.actions.menu.system_flow.evidence import Verdict
from pokegent.actions.menu.system_flow.policy import SystemFlowOptions
from pokegent.games.gen3.firered.reader.schemas import (
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

PARTY_SIZE = 6


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


def _storage(*, box_option: int = 1, full: bool = False) -> PokemonStorage:
    filler = object()
    boxes = tuple(
        tuple(filler for _ in range(BOX_CAPACITY))
        if full
        else tuple(None for _ in range(BOX_CAPACITY))
        for _ in range(BOX_COUNT)
    )
    return PokemonStorage(
        current_box=0,
        boxes=boxes,
        names=tuple(f"Box {index}" for index in range(BOX_COUNT)),
        wallpapers=tuple(range(BOX_COUNT)),
        box_option=box_option,
        ui_state=0,
    )


def _snapshot(
    party: tuple[PartyMember, ...],
    *,
    seq: int = 1,
    storage: PokemonStorage | None = None,
) -> StateSnapshot:
    return StateSnapshot.create(
        rom_profile="synthetic",
        snapshot_seq=seq,
        health=Health(valid=True),
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
        storage=storage if storage is not None else _storage(),
    )


def _deposit(party: tuple[PartyMember, ...], *, seq: int = 1, full: bool = False):
    snapshot = _snapshot(party, seq=seq, storage=_storage(full=full))
    ctx = BuildContext(
        snapshot=snapshot,
        scene=SceneResult(scene_id=R.PC_MAIN, confidence=0.95),
    )
    verdict = Verdict(R.PC_MAIN, 0.95, evidence={"menu": "pc_storage", "phase": "storage"})
    draft = pc_main(ctx, snapshot, verdict, SystemFlowOptions())
    return {option.id: option for option in draft.options}


def _option(party, pid: int, *, seq: int = 1, full: bool = False):
    return _deposit(party, seq=seq, full=full)[f"deposit:{pid}"]


def test_sole_battle_capable_is_refused_when_companion_is_fainted():
    healthy, fainted = _mon(0, 101), _mon(1, 102, hp=0)

    option = _option((healthy, fainted), 101)

    assert option.legality is Legality.UNAVAILABLE
    assert option.executor_ref is None


def test_sole_battle_capable_is_refused_when_companion_is_an_egg():
    healthy, egg = _mon(0, 111), _mon(1, 112, egg=True)

    option = _option((healthy, egg), 111)

    assert option.legality is Legality.UNAVAILABLE
    assert option.executor_ref is None


def test_two_battle_capable_members_allow_either_deposit():
    first, second = _mon(0, 121), _mon(1, 122, hp=5)

    options = _deposit((first, second))

    assert options["deposit:121"].legality is Legality.LEGAL
    assert options["deposit:122"].legality is Legality.LEGAL


def test_depositing_fainted_member_is_allowed_while_another_is_capable():
    healthy, fainted = _mon(0, 131), _mon(1, 132, hp=0)

    option = _option((healthy, fainted), 132)

    assert option.legality is Legality.LEGAL
    assert option.executor_ref == R.REF_PC_STORAGE


def test_last_party_member_and_full_box_controls_are_unchanged():
    only = _option((_mon(0, 141),), 141)
    first, second = _mon(0, 142), _mon(1, 143)
    full_box = _option((first, second), 142, full=True)

    assert only.legality is Legality.UNAVAILABLE
    assert only.facts["reason"] == "cannot deposit the last party Pokemon"
    assert full_box.legality is Legality.UNAVAILABLE
    assert full_box.facts["reason"] == "PC storage unreadable"


def test_unreadable_hp_evidence_fails_closed():
    # The validators accept the model; a party read that cannot prove HP must not be read as
    # battle-capable.  Here the target itself is unreadable and no companion is proven capable.
    unreadable, fainted = _mon(0, 151, hp=30, max_hp=20), _mon(1, 152, hp=0)

    option = _option((unreadable, fainted), 151)

    assert option.legality is Legality.UNKNOWN
    assert option.executor_ref is None


def test_unreadable_target_with_proven_capable_companion_is_allowed():
    unreadable, healthy = _mon(0, 161, hp=30, max_hp=20), _mon(1, 162)

    option = _option((unreadable, healthy), 161)

    assert option.legality is Legality.LEGAL


def test_empty_party_offers_no_deposit_legs():
    options = _deposit(())

    assert not any(option.startswith("deposit:") for option in options)


def test_changed_fresh_party_data_must_not_retain_old_admission():
    healthy, fainted = _mon(0, 171), _mon(1, 172, hp=0)
    stale = _option((healthy, fainted), 171)
    assert stale.legality is Legality.UNAVAILABLE

    healed = _mon(0, 171), _mon(1, 172, hp=1)
    fresh = _option(healed, 171, seq=2)
    assert fresh.legality is Legality.LEGAL


@pytest.mark.parametrize("hp,max_hp", [(30, 20), (-1, 20), (5, 0)])
def test_inconsistent_hp_never_counts_as_capable(hp, max_hp):
    unknown, healthy = _mon(0, 181, hp=hp, max_hp=max_hp), _mon(1, 182)

    assert _option((unknown, healthy), 181).legality is Legality.LEGAL
