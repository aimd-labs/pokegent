"""Synthetic acceptance tests for the R07 item restoration subset; no ROM data."""

from dataclasses import replace

import pytest
from test_local_rom_item_facts import item, knowledge

from pokegent.games.gen3.firered.rom.rom_tables import ItemEffect

RESTORATION_FIELDS = (
    "heal_hp",
    "heal_hp_full",
    "heal_hp_half",
    "revive",
    "cures",
    "heal_pp",
    "heal_pp_all_moves",
    "sacred_ash",
)


def with_effect(item_id, effect, **overrides):
    return replace(item(item_id, **overrides), effect=effect)


def test_item_restoration_exposes_only_the_decoded_subset():
    effect = ItemEffect(
        heal_hp=20,
        heal_hp_half=True,
        revive=True,
        cures=frozenset({"sleep", "burn"}),
        heal_pp=10,
        heal_pp_all_moves=True,
        sacred_ash=True,
        stat_boost=(("atk", 1),),
        ev_gains=(("hp", 4),),
        pp_up=True,
        pp_max=True,
        level_up=True,
        focus_energy=True,
        guard_spec=True,
        raw=b"\x01\x02",
    )

    fact = knowledge((item(0), with_effect(1, effect))).item_restoration(1)
    values = dict(fact.values)

    assert fact.known is True
    assert fact.record_kind == "item_restoration"
    assert fact.record_id == 1
    assert tuple(values) == RESTORATION_FIELDS
    assert values == {
        "heal_hp": 20,
        "heal_hp_full": False,
        "heal_hp_half": True,
        "revive": True,
        "cures": ("burn", "sleep"),
        "heal_pp": 10,
        "heal_pp_all_moves": True,
        "sacred_ash": True,
    }


def test_present_empty_effect_is_known_with_defaults():
    fact = knowledge((item(0), with_effect(1, ItemEffect()))).item_restoration(1)

    assert fact.known is True
    assert dict(fact.values) == {
        "heal_hp": None,
        "heal_hp_full": False,
        "heal_hp_half": False,
        "revive": False,
        "cures": (),
        "heal_pp": None,
        "heal_pp_all_moves": False,
        "sacred_ash": False,
    }


def test_item_restoration_values_come_from_the_supplied_table():
    first = knowledge((item(0), with_effect(1, ItemEffect(heal_hp=20))))
    second = knowledge((item(0), with_effect(1, ItemEffect(heal_hp=50))))

    assert dict(first.item_restoration(1).values)["heal_hp"] == 20
    assert dict(second.item_restoration(1).values)["heal_hp"] == 50


def test_absent_effect_is_typed_unknown_not_no_effect():
    fact = knowledge((item(0), item(1))).item_restoration(1)

    assert fact.known is False
    assert fact.values == ()
    assert fact.record_kind == "item_restoration"
    assert fact.record_id == 1


def test_missing_row_is_typed_unknown():
    fact = knowledge((item(0), with_effect(1, ItemEffect()))).item_restoration(2)

    assert fact.known is False
    assert fact.values == ()
    assert fact.record_kind == "item_restoration"
    assert fact.record_id == 2


@pytest.mark.parametrize("bad", ["1", 1.0, True, False, 0, -1])
def test_malformed_item_id_rejects(bad):
    with pytest.raises(ValueError):
        knowledge((item(1),)).item_restoration(bad)
