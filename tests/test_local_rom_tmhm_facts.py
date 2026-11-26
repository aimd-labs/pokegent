"""Synthetic acceptance tests for R07 local TM/HM facts; no ROM or shipped data."""

import pytest
from test_local_rom_knowledge import SHA, move, selection, species

from pokegent.games.gen3.firered.knowledge import FireRedKnowledge
from pokegent.games.gen3.firered.rom.rom_tables import GameTables, TypeChart
from pokegent.games.plugin import PluginRefusal, RefusalCode

TM01 = 289
MOVE = 33


def tables(
    *,
    learnsets=(b"\x01", b"\x01"),
    tmhm_moves=(MOVE,),
    last_move=MOVE,
    last_species=1,
    profile_id="synthetic-profile",
):
    return GameTables(
        moves=tuple(move(index) for index in range(last_move + 1)),
        species=tuple(species(index) for index in range(last_species + 1)),
        items=(),
        type_chart=TypeChart(bytes([0xFE, 0, 0, 0xFF, 0, 0])),
        ability_names=(),
        type_names=(),
        profile_id=profile_id,
        tmhm_learnsets=tuple(learnsets),
        tmhm_moves=tuple(tmhm_moves),
    )


def knowledge(*, profile_id="synthetic-profile", **kwargs):
    return FireRedKnowledge(
        selection(profile_id=profile_id),
        tables(profile_id=profile_id, **kwargs),
        enabled=True,
    )


def unknown(fact, species_id):
    assert fact.known is False
    assert fact.values == ()
    assert fact.record_kind == "tmhm"
    assert fact.record_id == species_id
    assert fact.source_class == "local_rom"
    assert fact.game == "firered"
    assert fact.profile_id == "synthetic-profile"
    assert fact.rom_sha1 == SHA
    assert fact.namespace == "native"


def test_compatible_and_incompatible_bits_are_known():
    compatible = knowledge().tmhm(1, TM01)
    incompatible = knowledge(learnsets=(b"\x01", b"\x00")).tmhm(1, TM01)

    assert compatible.known is True
    assert dict(compatible.values) == {"item_id": TM01, "move_id": MOVE, "can_learn": True}
    assert incompatible.known is True
    assert dict(incompatible.values)["can_learn"] is False


def test_two_tables_report_different_bits():
    first = knowledge(learnsets=(b"\x01", b"\x01")).tmhm(1, TM01)
    second = knowledge(learnsets=(b"\x01", b"\x00")).tmhm(1, TM01)

    assert dict(first.values)["can_learn"] is True
    assert dict(second.values)["can_learn"] is False


def test_missing_species_is_unknown():
    unknown(knowledge(last_species=1, learnsets=(b"\x01",) * 3).tmhm(2, TM01), 2)


def test_missing_item_mapping_is_unknown():
    unknown(knowledge(tmhm_moves=(MOVE,)).tmhm(1, TM01 + 1), 1)


@pytest.mark.parametrize("mapped", [0, -1, True, "33"])
def test_invalid_mapped_move_is_unknown(mapped):
    unknown(knowledge(tmhm_moves=(mapped,)).tmhm(1, TM01), 1)


def test_missing_move_table_row_is_unknown():
    unknown(knowledge(last_move=0).tmhm(1, TM01), 1)


def test_missing_learnset_row_is_unknown():
    unknown(knowledge(last_species=1, learnsets=(b"\x01",)).tmhm(1, TM01), 1)


def test_short_bit_row_is_unknown():
    unknown(knowledge(learnsets=(b"\x01", b"")).tmhm(1, TM01), 1)


@pytest.mark.parametrize("bad", ["1", 1.0, True, False, 0, -1])
def test_malformed_species_or_item_id_rejects(bad):
    reader = knowledge()
    with pytest.raises(ValueError):
        reader.tmhm(bad, TM01)
    with pytest.raises(ValueError):
        reader.tmhm(1, bad)


def test_tmhm_requires_explicit_enable_and_matching_profile():
    with pytest.raises(PluginRefusal) as refused:
        FireRedKnowledge(selection(), tables())
    assert refused.value.code is RefusalCode.DISABLED

    with pytest.raises(PluginRefusal) as refused:
        FireRedKnowledge(selection(), tables(profile_id="another-profile"), enabled=True)
    assert refused.value.code is RefusalCode.PROFILE_MISMATCH
