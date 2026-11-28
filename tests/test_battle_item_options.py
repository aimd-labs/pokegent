"""Synthetic (no ROM) battle item options: ids, legality and executor_args of _item_options."""

from types import SimpleNamespace as NS

from pokegent.actions.battle.builder import BattleBuilder
from pokegent.actions.battle.state import ItemEffectInfo, ItemInfo

POTION, ANTIDOTE, XATK = 13, 14, 15


def _mon(slot, hp, status=(), egg=False):
    return NS(
        slot=slot,
        name=f"M{slot}",
        hp=hp,
        max_hp=20,
        status=status,
        is_egg=egg,
        fainted=hp <= 0,
        active=slot == 0,
    )


def _item(role, **effect):
    return ItemInfo(item_id=0, role=role, effect=ItemEffectInfo(**effect))


def _options(party, atk_stage=0):
    items = {
        POTION: _item("medicine", heals_hp=True),
        ANTIDOTE: _item("medicine", cures=("poison",)),
        XATK: _item("stat_booster", stat_boost=(("atk", 1),)),
    }
    me = NS(side="player", stat_stages=(0, atk_stage) + (0,) * 6)
    view = NS(
        bag={POTION: 2, ANTIDOTE: 1, XATK: 1},
        bag_pocket=dict.fromkeys((POTION, ANTIDOTE, XATK), "items"),
        block=NS(lookups=NS(items=items), player=me, battler_at=lambda b: me, sides=None),
        player=NS(battler=0, hp=party[0].hp, max_hp=20, status=(), volatile=()),
        party=tuple(party),
        tables=None,
        is_trainer=False,
        mon=lambda slot: next((m for m in party if m.slot == slot), None),
    )
    return {o.id: o for o in BattleBuilder()._item_options(view)}


def test_potion_offered_for_hurt_mon_and_unavailable_at_full_hp():
    opts = _options([_mon(0, 20), _mon(1, 5)])
    hurt, full = opts[f"item:{POTION}:1"], opts[f"item:{POTION}:0"]
    assert hurt.legality.name == "LEGAL" and hurt.executor_args["target_slot"] == 1
    assert hurt.executor_args["item_id"] == POTION
    assert full.legality.name == "UNAVAILABLE" and full.executor_ref is None
    assert full.facts["unavailable_reason"] == "full_hp"


def test_antidote_needs_poison_on_the_target():
    opts = _options([_mon(0, 20, ("poison",)), _mon(1, 20)])
    assert opts[f"item:{ANTIDOTE}:0"].legality.name == "LEGAL"
    assert opts[f"item:{ANTIDOTE}:1"].facts["unavailable_reason"] == "no_status_to_cure"


def test_x_attack_unavailable_at_plus_six_and_has_no_target_slot():
    assert _options([_mon(0, 20)], atk_stage=0)[f"item:{XATK}:-1"].legality.name == "LEGAL"
    capped = _options([_mon(0, 20)], atk_stage=6)[f"item:{XATK}:-1"]
    assert capped.legality.name == "UNAVAILABLE"
    assert capped.facts["unavailable_reason"] == "already_in_effect"


def test_fainted_and_egg_targets_are_unavailable():
    opts = _options([_mon(0, 20), _mon(1, 0), _mon(2, 20, egg=True)])
    assert opts[f"item:{POTION}:1"].facts["unavailable_reason"] == "fainted"
    assert opts[f"item:{POTION}:2"].facts["unavailable_reason"] == "egg"
    assert opts[f"item:{POTION}:2"].legality.name == "UNAVAILABLE"
