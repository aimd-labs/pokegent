"""Synthetic acceptance tests for R07 native-to-National Dex mapping; no ROM or shipped data."""

import pytest
from test_local_rom_knowledge import SHA, selection, species

from pokegent.games.gen3.firered.knowledge import FireRedKnowledge
from pokegent.games.gen3.firered.rom.rom_tables import GameTables, TypeChart
from pokegent.games.plugin import PluginRefusal, RefusalCode


def tables(*, species_rows=3, species_dex=(), profile_id="synthetic-profile"):
    return GameTables(
        moves=(),
        species=tuple(species(index) for index in range(species_rows)),
        items=(),
        type_chart=TypeChart(bytes([0xFE, 0, 0, 0xFF, 0, 0])),
        ability_names=(),
        type_names=(),
        profile_id=profile_id,
        species_dex=tuple(species_dex),
    )


def knowledge(*, species_rows=3, species_dex=(), profile_id="synthetic-profile"):
    return FireRedKnowledge(
        selection(profile_id=profile_id),
        tables(species_rows=species_rows, species_dex=species_dex, profile_id=profile_id),
        enabled=True,
    )


def test_known_species_maps_through_public_tables():
    fact = knowledge(species_dex=(0, 25)).national_dex(2)

    assert fact.known is True
    assert fact.record_kind == "national_dex"
    assert fact.record_id == 2
    assert fact.values == (("species_id", 2), ("national_dex", 25))
    assert fact.source_class == "local_rom"
    assert fact.game == "firered"
    assert fact.profile_id == "synthetic-profile"
    assert fact.rom_sha1 == SHA
    assert fact.namespace == "native"


def test_mapping_comes_from_each_supplied_table():
    first = knowledge(species_dex=(0, 25)).national_dex(2)
    second = knowledge(species_dex=(0, 151)).national_dex(2)

    assert dict(first.values)["national_dex"] == 25
    assert dict(second.values)["national_dex"] == 151


@pytest.mark.parametrize("mapping", [None, 0, -7, True, "25", 25.0])
def test_absent_or_impure_mapping_is_typed_unknown(mapping):
    fact = knowledge(species_dex=(0, mapping)).national_dex(2)

    assert fact.known is False
    assert fact.values == ()
    assert fact.record_kind == "national_dex"
    assert fact.record_id == 2


def test_missing_species_is_unknown_even_with_mapping():
    fact = knowledge(species_rows=1, species_dex=(0, 25)).national_dex(2)

    assert fact.known is False
    assert fact.values == ()
    assert fact.record_id == 2


@pytest.mark.parametrize("bad", ["1", 1.0, True, False, 0, -1])
def test_malformed_species_id_rejects(bad):
    with pytest.raises(ValueError):
        knowledge(species_dex=(0, 25)).national_dex(bad)


def test_national_dex_requires_explicit_enable_and_matching_profile():
    with pytest.raises(PluginRefusal) as refused:
        FireRedKnowledge(selection(), tables(species_dex=(0, 25)))
    assert refused.value.code is RefusalCode.DISABLED

    with pytest.raises(PluginRefusal) as refused:
        FireRedKnowledge(
            selection(),
            tables(species_dex=(0, 25), profile_id="another-profile"),
            enabled=True,
        )
    assert refused.value.code is RefusalCode.PROFILE_MISMATCH
