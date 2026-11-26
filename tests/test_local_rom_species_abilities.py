"""Synthetic RED-first checks for R07 native species ability IDs."""

import dataclasses

import pytest

from pokegent.games.gen3 import mechanics_constants as K
from pokegent.games.gen3.firered.knowledge import FireRedKnowledge
from pokegent.games.gen3.firered.rom.rom_tables import GameTables, SpeciesData, TypeChart
from pokegent.games.plugin import GamePluginDescriptor, PluginSelection

SHA = "0123456789abcdef0123456789abcdef01234567"


def selection(*, profile_id="synthetic-profile"):
    descriptor = GamePluginDescriptor(
        plugin_id="synthetic-abilities",
        game="firered",
        profile_id=profile_id,
        contract_version=1,
        rom_sha1=SHA,
    )
    return PluginSelection(descriptor=descriptor, services=None)


def species(species_id, abilities):
    return SpeciesData(
        species_id=species_id,
        name="Synthetic Mon",
        base_hp=1, base_atk=2, base_def=3, base_spe=4, base_spa=5, base_spd=6,
        types=(K.TYPE_NORMAL, K.TYPE_NORMAL),
        catch_rate=0,
        exp_yield=0,
        ev_yield=(0, 0, 0, 0, 0, 0),
        item_common=0,
        item_rare=0,
        gender_ratio=0,
        egg_cycles=0,
        friendship=0,
        growth_rate=0,
        egg_groups=(0, 0),
        abilities=abilities,
        safari_flee_rate=0,
    )


def tables(roster, *, profile_id="synthetic-profile"):
    filler = species(0, (0, 0))
    by_id = {row.species_id: row for row in roster}
    rows = tuple(by_id.get(index, filler) for index in range(max(by_id, default=0) + 1))
    return GameTables(
        moves=(),
        species=rows,
        items=(),
        type_chart=TypeChart(bytes([0xFE, 0, 0, 0xFF, 0, 0])),
        ability_names=(),
        type_names=(),
        profile_id=profile_id,
    )


def knowledge(roster, *, profile_id="synthetic-profile"):
    return FireRedKnowledge(
        selection(profile_id=profile_id),
        tables(roster, profile_id=profile_id),
        enabled=True,
    )


def test_caller_table_ability_pairs_are_reported():
    first = knowledge((species(5, (7, 12)),))
    second = knowledge((species(5, (31, 0)),))
    assert dict(first.species(5).values)["abilities"] == (7, 12)
    assert dict(second.species(5).values)["abilities"] == (31, 0)


def test_pair_order_and_zero_placeholder_are_preserved():
    zeros_first = dict(knowledge((species(5, (0, 44)),)).species(5).values)
    zeros_second = dict(knowledge((species(5, (44, 0)),)).species(5).values)
    assert zeros_first["abilities"] == (0, 44)
    assert zeros_second["abilities"] == (44, 0)


def test_known_record_keeps_identity_and_adds_only_abilities():
    fact = knowledge((species(5, (7, 12)),)).species(5)
    assert set(dict(fact.values)) == {"types", "base_stats", "abilities"}
    assert fact.record_kind == "species"
    assert fact.record_id == 5
    assert fact.source_class == "local_rom"
    assert fact.game == "firered"
    assert fact.namespace == "native"
    assert fact.profile_id == "synthetic-profile"
    assert fact.rom_sha1 == SHA


def test_abilities_are_native_ids_not_names_or_active_ability():
    values = dict(knowledge((species(5, (7, 12)),)).species(5).values)
    abilities = values["abilities"]
    assert isinstance(abilities, tuple)
    assert abilities == (7, 12)
    assert all(type(value) is int for value in abilities)
    assert "ability" not in values
    assert "active_ability" not in values


def test_unknown_species_stays_empty():
    fact = knowledge((species(1, (7, 12)),)).species(2)
    assert fact.known is False
    assert fact.values == ()


def test_abilities_tuple_is_detached_and_record_frozen():
    built = tables((species(5, (7, 12)),))
    fact = FireRedKnowledge(selection(), built, enabled=True).species(5)
    abilities = dict(fact.values)["abilities"]
    assert abilities == built.species(5).abilities
    assert abilities is not built.species(5).abilities
    with pytest.raises(dataclasses.FrozenInstanceError):
        fact.known = False
