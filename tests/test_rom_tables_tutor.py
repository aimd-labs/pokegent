"""Synthetic tests for the ROM move tutor readers; no ROM or shipped data."""

from test_local_rom_knowledge import move, species

from pokegent.games.gen3.firered.rom.rom_data import default_profiles_path
from pokegent.games.gen3.firered.rom.rom_tables import GameTables, TypeChart, tutor_facts


def tables(*, tutor_moves=(10, 20), tutor_learnsets=(0b00, 0b01, 0b10)):
    return GameTables(
        moves=tuple(move(index) for index in range(21)),
        species=tuple(species(index) for index in range(3)),
        items=(),
        type_chart=TypeChart(bytes([0xFE, 0, 0, 0xFF, 0, 0])),
        ability_names=(),
        type_names=(),
        tutor_moves=tutor_moves,
        tutor_learnsets=tutor_learnsets,
    )


def test_tutor_move_by_index():
    assert tables().tutor_move(1) == 20
    assert tables().tutor_move(2) is None
    assert tables().tutor_move(-1) is None


def test_compatibility_bit_set_unset_and_out_of_range():
    t = tables()
    assert t.can_learn_tutor(1, 10) is True
    assert t.can_learn_tutor(1, 20) is False
    assert t.can_learn_tutor(2, 20) is True
    assert t.can_learn_tutor(3, 10) is None
    assert t.can_learn_tutor(1, 99) is None


def test_unreadable_tables_fail_closed():
    t = tables(tutor_moves=(), tutor_learnsets=())
    assert t.tutor_move(0) is None
    assert t.can_learn_tutor(0, 10) is None


def test_tutor_facts_lists_party_indexes_of_learners():
    assert tutor_facts(tables(), 10, [1, 0, 1, 2]) == {"move": 10, "learners": [0, 2]}
    assert tutor_facts(tables(), 20, [1, 2]) == {"move": 20, "learners": [1]}
    assert tutor_facts(tables(), 99, [1]) == {"move": 99, "learners": []}


def test_firered_rev0_profile_declares_tutor_tables():
    text = default_profiles_path().read_text(encoding="utf-8")
    assert "sTutorMoves:" in text and "sTutorLearnsets:" in text
