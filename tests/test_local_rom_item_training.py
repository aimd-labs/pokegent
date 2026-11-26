"""Synthetic RED-first checks for the R07 item training subset; no ROM data."""

from dataclasses import replace

import pytest
from test_local_rom_item_facts import item, knowledge, selection, tables

from pokegent.games.gen3.firered.knowledge import FireRedKnowledge
from pokegent.games.gen3.firered.rom.rom_tables import ItemEffect
from pokegent.games.plugin import PluginRefusal, RefusalCode

TRAINING_FIELDS = ("ev_gains", "pp_up", "pp_max", "level_up")


def with_effect(item_id, effect, **overrides):
    return replace(item(item_id, **overrides), effect=effect)


def test_training_has_exactly_the_four_ordered_pairs():
    effect = ItemEffect(
        ev_gains=(("hp", 10), ("sp_attack", 4)),
        pp_up=True,
        pp_max=True,
        level_up=True,
        heal_hp=20,
        cures=frozenset({"sleep"}),
        stat_boost=(("atk", 1),),
        raw=b"\x01\x02",
    )
    fact = knowledge((item(0), with_effect(1, effect))).item_training(1)
    values = dict(fact.values)

    assert fact.known is True
    assert fact.record_kind == "item_training"
    assert fact.record_id == 1
    assert tuple(values) == TRAINING_FIELDS
    assert values == {
        "ev_gains": (("hp", 10), ("sp_attack", 4)),
        "pp_up": True,
        "pp_max": True,
        "level_up": True,
    }


def test_present_empty_effect_is_known_and_absent_effect_is_unknown():
    reader = knowledge((item(0), with_effect(1, ItemEffect()), item(2)))

    empty = reader.item_training(1)
    assert empty.known is True
    assert dict(empty.values) == {
        "ev_gains": (),
        "pp_up": False,
        "pp_max": False,
        "level_up": False,
    }

    absent = reader.item_training(2)
    assert absent.known is False
    assert absent.values == ()
    assert absent.record_kind == "item_training"
    assert absent.record_id == 2


def test_training_values_come_from_the_supplied_table():
    first = knowledge((item(0), with_effect(1, ItemEffect(ev_gains=(("hp", 1),), pp_up=True))))
    second = knowledge(
        (item(0), with_effect(1, ItemEffect(ev_gains=(("attack", 2),), level_up=True)))
    )

    assert dict(first.item_training(1).values)["ev_gains"] == (("hp", 1),)
    assert dict(first.item_training(1).values)["pp_up"] is True
    assert dict(second.item_training(1).values)["ev_gains"] == (("attack", 2),)
    assert dict(second.item_training(1).values)["level_up"] is True


def test_ev_gains_keep_decoded_order_signed_values_and_detach_pairs():
    effect = replace(ItemEffect(), ev_gains=[("attack", -3), ("hp", 7), ("defense", 0)])
    fact = knowledge((item(0), with_effect(1, effect))).item_training(1)
    gains = dict(fact.values)["ev_gains"]

    assert isinstance(gains, tuple)
    assert all(isinstance(pair, tuple) for pair in gains)
    assert gains == (("attack", -3), ("hp", 7), ("defense", 0))
    assert [gain for _, gain in gains] == [-3, 7, 0]
    assert gains is not effect.ev_gains


def test_missing_row_is_typed_unknown():
    fact = knowledge((item(0), with_effect(1, ItemEffect()))).item_training(2)

    assert fact.known is False
    assert fact.values == ()
    assert fact.record_kind == "item_training"
    assert fact.record_id == 2
    assert fact.namespace == "native"


@pytest.mark.parametrize("bad", ["1", 1.0, True, False, 0, -1])
def test_malformed_item_id_rejects(bad):
    with pytest.raises(ValueError):
        knowledge((item(1),)).item_training(bad)


def test_neighbors_and_constructor_gates_are_unchanged():
    with pytest.raises(PluginRefusal) as refused:
        FireRedKnowledge(selection(), tables((item(1),)))
    assert refused.value.code is RefusalCode.DISABLED

    reader = knowledge((item(0), with_effect(1, ItemEffect(heal_hp=20, pp_up=True))))
    assert reader.item(1).record_kind == "item"
    assert reader.item_restoration(1).record_kind == "item_restoration"
    assert reader.item_training(1).record_kind == "item_training"
    assert "pp_up" not in dict(reader.item_restoration(1).values)
    assert "heal_hp" not in dict(reader.item_training(1).values)
