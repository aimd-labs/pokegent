"""Synthetic acceptance tests for R07 local native type names; no ROM or shipped data."""

import pytest
from test_local_rom_knowledge import SHA, selection

from pokegent.games.gen3.firered.knowledge import FireRedKnowledge, LocalRomRecord
from pokegent.games.gen3.firered.rom.rom_tables import GameTables, TypeChart
from pokegent.games.plugin import PluginRefusal, RefusalCode

CHART = TypeChart(bytes([0xFE, 0, 0, 0xFF, 0, 0]))
DEFAULT_NAMES = ("Synthetic Zero", "Synthetic Second")


def tables(type_names=DEFAULT_NAMES, *, profile_id="synthetic-profile"):
    return GameTables(
        moves=(),
        species=(),
        items=(),
        type_chart=CHART,
        ability_names=(),
        type_names=tuple(type_names),
        profile_id=profile_id,
    )


def knowledge(type_names=DEFAULT_NAMES, *, profile_id="synthetic-profile"):
    return FireRedKnowledge(
        selection(profile_id=profile_id),
        tables(type_names, profile_id=profile_id),
        enabled=True,
    )


def test_type_zero_is_a_valid_native_row():
    fact = knowledge().type(0)

    assert isinstance(fact, LocalRomRecord)
    assert fact.known is True
    assert fact.record_kind == "type"
    assert fact.record_id == 0
    assert fact.values == (("id", 0), ("name", "Synthetic Zero"))
    assert (fact.source_class, fact.game, fact.namespace) == ("local_rom", "firered", "native")
    assert (fact.profile_id, fact.rom_sha1) == ("synthetic-profile", SHA)


def test_each_supplied_table_answers_with_its_own_names():
    first = knowledge(("Alpha Zero", "Alpha Second")).type(1)
    other = knowledge(("Beta Zero", "Beta Second")).type(1)

    assert first.record_id == 1
    assert first.values == (("id", 1), ("name", "Alpha Second"))
    assert other.values == (("id", 1), ("name", "Beta Second"))


def test_blank_decoded_name_stays_known():
    fact = knowledge(("", "")).type(0)

    assert fact.known is True
    assert fact.values == (("id", 0), ("name", ""))


def test_missing_type_row_is_typed_unknown():
    fact = knowledge().type(2)

    assert fact.known is False
    assert fact.values == ()
    assert (fact.record_kind, fact.record_id, fact.rom_sha1) == ("type", 2, SHA)


@pytest.mark.parametrize("bad", ["0", 0.0, True, False, -1])
def test_malformed_type_id_rejects(bad):
    with pytest.raises(ValueError):
        knowledge().type(bad)


def test_type_name_is_bounded():
    longest = "n" * 64
    fact = knowledge((longest,)).type(0)
    assert dict(fact.values)["name"] == longest

    for bad in ("n" * 65, 7):
        with pytest.raises(ValueError):
            knowledge((bad,)).type(0)


def test_existing_defaults_still_gate_the_adapter():
    with pytest.raises(PluginRefusal) as refused:
        FireRedKnowledge(selection(), tables())
    assert refused.value.code is RefusalCode.DISABLED

    with pytest.raises(PluginRefusal) as refused:
        FireRedKnowledge(selection(), tables(profile_id="another-profile"), enabled=True)
    assert refused.value.code is RefusalCode.PROFILE_MISMATCH
