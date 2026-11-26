"""Synthetic acceptance tests for R07 local evolution metadata; no ROM or shipped data."""

import pytest
from test_local_rom_knowledge import SHA, selection, species

from pokegent.games.gen3.firered.knowledge import FireRedKnowledge
from pokegent.games.gen3.firered.rom.rom_tables import EvolutionData, GameTables, TypeChart
from pokegent.games.plugin import PluginRefusal, RefusalCode

ROW = (
    EvolutionData(4, 16, 2),
    EvolutionData(7, 21, 3),
    EvolutionData(0, 0, 0),
    EvolutionData(0, 0, 0),
    EvolutionData(0, 0, 0),
)


def tables(*, last_species=2, evolutions=(), profile_id="synthetic-profile"):
    return GameTables(
        moves=(),
        species=tuple(species(index) for index in range(last_species + 1)),
        items=(),
        type_chart=TypeChart(bytes([0xFE, 0, 0, 0xFF, 0, 0])),
        ability_names=(),
        type_names=(),
        profile_id=profile_id,
        evolutions=tuple(evolutions),
    )


def knowledge(*, last_species=2, evolutions=(), profile_id="synthetic-profile"):
    return FireRedKnowledge(
        selection(profile_id=profile_id),
        tables(last_species=last_species, evolutions=evolutions, profile_id=profile_id),
        enabled=True,
    )


def test_evolutions_keep_native_row_order_and_padding():
    fact = knowledge(last_species=1, evolutions=((), ROW)).evolutions(1)

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
    fact = knowledge(last_species=1, evolutions=((), ())).evolutions(1)

    assert fact.known is True
    assert fact.values == (("evolutions", ()),)


def test_absent_evolution_row_with_known_species_is_unknown():
    fact = knowledge(last_species=2, evolutions=((),)).evolutions(2)

    assert fact.known is False
    assert fact.values == ()
    assert fact.record_kind == "evolution"
    assert fact.record_id == 2


def test_absent_species_is_unknown_even_with_a_row():
    fact = knowledge(last_species=1, evolutions=((), ROW, ROW)).evolutions(2)

    assert fact.known is False
    assert fact.values == ()


def test_evolutions_for_keeps_its_existing_empty_answer():
    decoded = tables(last_species=0, evolutions=((),))
    absent = tables(last_species=1, evolutions=((),))

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
