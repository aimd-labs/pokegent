"""Synthetic RED-first checks for the R07 item battle-effect subset; no ROM data."""

from dataclasses import replace

import pytest
from test_local_rom_item_facts import item, knowledge, selection, tables

from pokegent.games.gen3.firered.knowledge import FireRedKnowledge
from pokegent.games.gen3.firered.rom.rom_tables import ItemEffect
from pokegent.games.plugin import PluginRefusal, RefusalCode

BATTLE_FIELDS = ("stat_boost", "focus_energy", "guard_spec")


def with_effect(item_id, effect, **overrides):
    return replace(item(item_id, **overrides), effect=effect)


def test_battle_effects_have_exactly_three_ordered_pairs():
    effect = ItemEffect(
        stat_boost=(("atk", 1), ("spe", 2)),
        focus_energy=True,
        guard_spec=True,
        heal_hp=20,
        ev_gains=(("hp", 4),),
    )
    fact = knowledge((item(0), with_effect(1, effect))).item_battle_effects(1)
    values = dict(fact.values)

    assert fact.known is True
    assert fact.record_kind == "item_battle_effects"
    assert fact.record_id == 1
    assert fact.namespace == "native"
    assert tuple(values) == BATTLE_FIELDS
    assert values == {
        "stat_boost": (("atk", 1), ("spe", 2)),
        "focus_energy": True,
        "guard_spec": True,
    }


def test_present_empty_effect_is_known_and_absent_effect_is_unknown():
    reader = knowledge((item(0), with_effect(1, ItemEffect()), item(2)))

    empty = reader.item_battle_effects(1)
    assert empty.known is True
    assert dict(empty.values) == {
        "stat_boost": (),
        "focus_energy": False,
        "guard_spec": False,
    }

    absent = reader.item_battle_effects(2)
    assert absent.known is False
    assert absent.values == ()
    assert absent.record_kind == "item_battle_effects"
    assert absent.record_id == 2


def test_values_come_from_the_supplied_table():
    first = knowledge((item(0), with_effect(1, ItemEffect(stat_boost=(("atk", 1),)))))
    second = knowledge((item(0), with_effect(1, ItemEffect(guard_spec=True))))

    assert dict(first.item_battle_effects(1).values)["stat_boost"] == (("atk", 1),)
    assert dict(second.item_battle_effects(1).values)["guard_spec"] is True
    assert dict(second.item_battle_effects(1).values)["stat_boost"] == ()


def test_stat_boost_pairs_keep_order_signs_and_detach():
    effect = replace(ItemEffect(), stat_boost=[("spe", -2), ("atk", 3), ("acc", 0)])
    fact = knowledge((item(0), with_effect(1, effect))).item_battle_effects(1)
    boost = dict(fact.values)["stat_boost"]

    assert isinstance(boost, tuple)
    assert all(isinstance(pair, tuple) for pair in boost)
    assert boost == (("spe", -2), ("atk", 3), ("acc", 0))
    assert boost is not effect.stat_boost


def test_missing_row_is_typed_unknown():
    fact = knowledge((item(0), with_effect(1, ItemEffect()))).item_battle_effects(2)

    assert fact.known is False
    assert fact.values == ()
    assert fact.record_kind == "item_battle_effects"
    assert fact.record_id == 2
    assert fact.namespace == "native"


@pytest.mark.parametrize("bad", ["1", 1.0, True, False, 0, -1])
def test_malformed_item_id_rejects(bad):
    with pytest.raises(ValueError):
        knowledge((item(1),)).item_battle_effects(bad)


def test_neighbors_and_constructor_gates_are_unchanged():
    with pytest.raises(PluginRefusal) as refused:
        FireRedKnowledge(selection(), tables((item(1),)))
    assert refused.value.code is RefusalCode.DISABLED

    reader = knowledge(
        (item(0), with_effect(1, ItemEffect(stat_boost=(("atk", 1),), guard_spec=True)))
    )
    assert reader.item(1).record_kind == "item"
    assert reader.item_restoration(1).record_kind == "item_restoration"
    assert reader.item_training(1).record_kind == "item_training"
    assert reader.item_battle_effects(1).record_kind == "item_battle_effects"
    assert "stat_boost" not in dict(reader.item_restoration(1).values)
    assert "stat_boost" not in dict(reader.item_training(1).values)
    assert "guard_spec" not in dict(reader.item_training(1).values)
