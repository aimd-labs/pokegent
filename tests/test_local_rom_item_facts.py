"""Synthetic acceptance tests for R07 local item facts; no ROM or shipped data."""

import pytest

from pokegent.games.gen3.firered.knowledge import FireRedKnowledge, LocalRomRecord
from pokegent.games.gen3.firered.rom.rom_tables import GameTables, ItemData, TypeChart
from pokegent.games.plugin import (
    GamePluginDescriptor,
    PluginRefusal,
    PluginSelection,
    RefusalCode,
)

SHA = "0123456789abcdef0123456789abcdef01234567"
ITEM_FIELDS = frozenset(
    {"id", "name", "price", "pocket", "hold_effect", "hold_effect_param", "battle_usage"}
)


def selection(*, game="firered", profile_id="synthetic-profile"):
    descriptor = GamePluginDescriptor(
        plugin_id="synthetic-item-knowledge",
        game=game,
        profile_id=profile_id,
        contract_version=1,
        rom_sha1=SHA,
    )
    return PluginSelection(descriptor=descriptor, services=None)


def item(
    item_id,
    *,
    name="Synthetic Item",
    price=100,
    pocket=1,
    hold_effect=0,
    hold_effect_param=0,
    battle_usage=0,
):
    return ItemData(
        item_id=item_id,
        name=name,
        price=price,
        hold_effect=hold_effect,
        hold_effect_param=hold_effect_param,
        importance=0,
        pocket=pocket,
        item_type=0,
        field_use_func=0,
        battle_usage=battle_usage,
        battle_use_func=0,
        secondary_id=0,
        effect=None,
    )


def tables(items=(), *, profile_id="synthetic-profile"):
    return GameTables(
        moves=(),
        species=(),
        items=tuple(items),
        type_chart=TypeChart(bytes([0xFE, 0, 0, 0xFF, 0, 0])),
        ability_names=(),
        type_names=(),
        profile_id=profile_id,
    )


def knowledge(items=(), *, profile_id="synthetic-profile"):
    return FireRedKnowledge(
        selection(profile_id=profile_id), tables(items, profile_id=profile_id), enabled=True
    )


def test_item_facts_come_from_the_supplied_table():
    first = knowledge(
        (
            item(0),
            item(
                1,
                name="Synthetic Berry",
                price=20,
                pocket=5,
                hold_effect=3,
                hold_effect_param=7,
                battle_usage=2,
            ),
        )
    )
    second = knowledge((item(0), item(1, price=99)))

    fact = first.item(1)
    values = dict(fact.values)

    assert isinstance(fact, LocalRomRecord)
    assert fact.known is True
    assert fact.record_kind == "item"
    assert fact.record_id == 1
    assert fact.source_class == "local_rom"
    assert fact.game == "firered"
    assert fact.profile_id == "synthetic-profile"
    assert fact.rom_sha1 == SHA
    assert fact.namespace == "native"
    assert values == {
        "id": 1,
        "name": "Synthetic Berry",
        "price": 20,
        "pocket": 5,
        "hold_effect": 3,
        "hold_effect_param": 7,
        "battle_usage": 2,
    }
    assert set(values) == ITEM_FIELDS
    assert all(type(value) in (int, str) for value in values.values())
    assert dict(second.item(1).values)["price"] == 99


def test_item_unknown_beyond_table_is_typed_unknown():
    fact = knowledge((item(0), item(1))).item(2)
    assert fact.known is False
    assert fact.values == ()
    assert fact.record_kind == "item"
    assert fact.record_id == 2


@pytest.mark.parametrize("bad", ["1", 1.0, True, False, 0, -1])
def test_malformed_item_id_rejects(bad):
    with pytest.raises(ValueError):
        knowledge((item(1),)).item(bad)


def test_item_name_is_bounded():
    longest = "n" * 64
    assert dict(knowledge((item(0), item(1, name=longest))).item(1).values)["name"] == longest
    for bad in ("n" * 65, 7):
        with pytest.raises(ValueError):
            knowledge((item(0), item(1, name=bad))).item(1)


def test_item_requires_explicit_enable_and_matching_profile():
    with pytest.raises(PluginRefusal) as refused:
        FireRedKnowledge(selection(), tables((item(1),)))
    assert refused.value.code is RefusalCode.DISABLED

    with pytest.raises(PluginRefusal) as refused:
        FireRedKnowledge(
            selection(), tables((item(1),), profile_id="another-profile"), enabled=True
        )
    assert refused.value.code is RefusalCode.PROFILE_MISMATCH
