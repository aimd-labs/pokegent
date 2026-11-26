"""Synthetic acceptance tests for R07 local evolution metadata; no ROM or shipped data."""

import pytest

from pokegent.games.gen3 import mechanics_constants as K
from pokegent.games.gen3.firered.knowledge import FireRedKnowledge
from pokegent.games.gen3.firered.rom.rom_tables import (
    EvolutionData,
    GameTables,
    SpeciesData,
    TypeChart,
)
from pokegent.games.plugin import (
    GamePluginDescriptor,
    PluginRefusal,
    PluginSelection,
    RefusalCode,
)

SHA = "0123456789abcdef0123456789abcdef01234567"
ROW = (
    EvolutionData(4, 16, 2),
    EvolutionData(7, 21, 3),
    EvolutionData(0, 0, 0),
    EvolutionData(0, 0, 0),
    EvolutionData(0, 0, 0),
)


def selection(*, game="firered", profile_id="synthetic-profile"):
    descriptor = GamePluginDescriptor(
        plugin_id="synthetic-evolution-knowledge",
        game=game,
        profile_id=profile_id,
        contract_version=1,
        rom_sha1=SHA,
    )
    return PluginSelection(descriptor=descriptor, services=None)


def species(species_id):
    return SpeciesData(
        species_id=species_id,
        name="Synthetic Mon",
        base_hp=10, base_atk=20, base_def=30, base_spe=40, base_spa=50, base_spd=60,
        types=(K.TYPE_NORMAL, K.TYPE_NORMAL),
        catch_rate=0, exp_yield=0, ev_yield=(0, 0, 0, 0, 0, 0),
        item_common=0, item_rare=0, gender_ratio=0, egg_cycles=0,
        friendship=0, growth_rate=0, egg_groups=(0, 0), abilities=(0, 0),
        safari_flee_rate=0,
    )


def tables(*, species_ids=(1, 2), evolutions=(), profile_id="synthetic-profile"):
    return GameTables(
        moves=(),
        species=tuple(species(index) for index in range(max(species_ids) + 1)),
        items=(),
        type_chart=TypeChart(bytes([0xFE, 0, 0, 0xFF, 0, 0])),
        ability_names=(),
        type_names=(),
        profile_id=profile_id,
        evolutions=tuple(evolutions),
    )


def knowledge(*, species_ids=(1, 2), evolutions=(), profile_id="synthetic-profile"):
    return FireRedKnowledge(
        selection(profile_id=profile_id),
        tables(species_ids=species_ids, evolutions=evolutions, profile_id=profile_id),
        enabled=True,
    )


def test_evolutions_keep_native_row_order_and_padding():
    fact = knowledge(species_ids=(1,), evolutions=((), ROW)).evolutions(1)

    assert fact.known is True
    assert fact.record_kind == "evolution"
    assert fact.record_id == 1
    assert fact.values == (
        ("evolutions", ((4, 16, 2), (7, 21, 3), (0, 0, 0), (0, 0, 0), (0, 0, 0))),
    )
    assert fact.source_class == "local_rom"
    assert fact.game == "firered"
    assert fact.profile_id == "synthetic-profile"
    assert fact.rom_sha1 == SHA
    assert fact.namespace == "native"


def test_present_empty_row_is_known_with_no_entries():
    fact = knowledge(species_ids=(1,), evolutions=((), ())).evolutions(1)

    assert fact.known is True
    assert fact.values == (("evolutions", ()),)


def test_absent_evolution_row_with_known_species_is_unknown():
    fact = knowledge(species_ids=(1, 2), evolutions=((),)).evolutions(2)

    assert fact.known is False
    assert fact.values == ()
    assert fact.record_kind == "evolution"
    assert fact.record_id == 2


def test_absent_species_is_unknown_even_with_a_row():
    fact = knowledge(species_ids=(1,), evolutions=((), ROW, ROW)).evolutions(2)

    assert fact.known is False
    assert fact.values == ()


def test_evolutions_for_keeps_its_existing_empty_answer():
    decoded = tables(species_ids=(0,), evolutions=((),))
    absent = tables(species_ids=(0, 1), evolutions=((),))

    assert decoded.evolutions_for(0) == ()
    assert absent.evolutions_for(1) == ()
    assert decoded.has_evolution_data(0) is True
    assert absent.has_evolution_data(1) is False


def test_oversize_row_is_refused_not_truncated():
    oversize = tuple(EvolutionData(0, 0, 0) for _ in range(6))
    with pytest.raises(ValueError):
        knowledge(evolutions=((), oversize)).evolutions(1)


@pytest.mark.parametrize("bad", ["1", 1.0, True, False, 0, -1])
def test_malformed_species_id_rejects(bad):
    with pytest.raises(ValueError):
        knowledge(evolutions=((), ROW)).evolutions(bad)


def test_evolution_query_requires_explicit_enable_and_matching_profile():
    with pytest.raises(PluginRefusal) as refused:
        FireRedKnowledge(selection(), tables(evolutions=((), ROW)))
    assert refused.value.code is RefusalCode.DISABLED

    with pytest.raises(PluginRefusal) as refused:
        FireRedKnowledge(
            selection(), tables(evolutions=((), ROW), profile_id="another-profile"), enabled=True
        )
    assert refused.value.code is RefusalCode.PROFILE_MISMATCH
