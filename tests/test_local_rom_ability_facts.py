"""Synthetic acceptance tests for R07 local ability names; no ROM or shipped data."""

import pytest
from test_local_rom_knowledge import SHA, selection

from pokegent.games.gen3.firered.knowledge import FireRedKnowledge, LocalRomRecord
from pokegent.games.gen3.firered.rom.rom_tables import GameTables, TypeChart
from pokegent.games.plugin import PluginRefusal, RefusalCode


def tables(ability_names=(), *, profile_id="synthetic-profile"):
    return GameTables(
        moves=(),
        species=(),
        items=(),
        type_chart=TypeChart(bytes([0xFE, 0, 0, 0xFF, 0, 0])),
        ability_names=tuple(ability_names),
        type_names=(),
        profile_id=profile_id,
    )


def knowledge(ability_names=(), *, profile_id="synthetic-profile"):
    return FireRedKnowledge(
        selection(profile_id=profile_id),
        tables(ability_names, profile_id=profile_id),
        enabled=True,
    )


def test_ability_name_comes_from_the_supplied_table():
    first = knowledge(("", "Synthetic Alpha", "Synthetic Beta"))
    second = knowledge(("", "Other Alpha"))

    fact = first.ability(2)

    assert isinstance(fact, LocalRomRecord)
    assert fact.known is True
    assert fact.record_kind == "ability"
    assert fact.record_id == 2
    assert fact.values == (("id", 2), ("name", "Synthetic Beta"))
    assert fact.source_class == "local_rom"
    assert fact.game == "firered"
    assert fact.profile_id == "synthetic-profile"
    assert fact.rom_sha1 == SHA
    assert fact.namespace == "native"
    assert dict(second.ability(1).values)["name"] == "Other Alpha"


def test_blank_decoded_name_is_known_table_content():
    fact = knowledge(("", "")).ability(1)

    assert fact.known is True
    assert fact.values == (("id", 1), ("name", ""))


def test_missing_ability_row_is_typed_unknown():
    fact = knowledge(("", "Synthetic Alpha")).ability(2)

    assert fact.known is False
    assert fact.values == ()
    assert fact.record_kind == "ability"
    assert fact.record_id == 2


@pytest.mark.parametrize("bad", ["1", 1.0, True, False, 0, -1])
def test_malformed_ability_id_rejects(bad):
    with pytest.raises(ValueError):
        knowledge(("", "Synthetic Alpha")).ability(bad)


def test_ability_name_is_bounded():
    longest = "n" * 64
    assert dict(knowledge(("", longest)).ability(1).values)["name"] == longest
    for bad in ("n" * 65, 7):
        with pytest.raises(ValueError):
            knowledge(("", bad)).ability(1)


def test_ability_requires_explicit_enable_and_matching_profile():
    with pytest.raises(PluginRefusal) as refused:
        FireRedKnowledge(selection(), tables(("", "Synthetic Alpha")))
    assert refused.value.code is RefusalCode.DISABLED

    with pytest.raises(PluginRefusal) as refused:
        FireRedKnowledge(
            selection(),
            tables(("", "Synthetic Alpha"), profile_id="another-profile"),
            enabled=True,
        )
    assert refused.value.code is RefusalCode.PROFILE_MISMATCH
