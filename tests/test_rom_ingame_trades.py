"""Synthetic bytes for the in-game trade table reader; no ROM or shipped data."""

import pytest

from pokegent.games.gen3.firered.rom import rom_tables
from pokegent.games.gen3.firered.rom.rom_data import RomIdentity, RomProfile, RomTableEntry
from pokegent.games.gen3.firered.rom.rom_tables import RomImagePort, TablesError

BASE = 0x08000040


def image(pairs):
    data = bytearray(0x40 + 60 * len(pairs))
    for i, (offered, wanted) in enumerate(pairs):
        row = 0x40 + i * 60
        data[row : row + 11] = b"\xaa" * 11  # nickname bytes are never parsed
        data[row + 0x0C : row + 0x0E] = offered.to_bytes(2, "little")
        data[row + 0x38 : row + 0x3A] = wanted.to_bytes(2, "little")
    return RomImagePort(bytes(data))


def profile(size=120):
    entry = RomTableEntry(address=BASE, size=size, source="synthetic") if size else None
    rom = RomIdentity(sha1="0" * 40, game_code="BPRE", version=0)
    tables = {"sInGameTrades": entry} if entry else {}
    return RomProfile(profile_id="s", display_name="S", supported=False, rom=rom, rom_tables=tables)


def test_ingame_trades_reads_only_species_and_requested_species():
    port = image([(63, 122), (21, 83)])
    assert rom_tables.ingame_trades(port, profile()) == ((63, 122), (21, 83))


@pytest.mark.parametrize("size", [0, 61])
def test_ingame_trades_fails_closed_without_a_table_or_with_a_ragged_size(size):
    with pytest.raises(TablesError):
        rom_tables.ingame_trades(image([(1, 2)] * 2), profile(size))


def test_trade_facts_lists_party_indexes_holding_the_wanted_species():
    facts = rom_tables.trade_facts(((63, 122), (21, 83)), (122, 5, 122))
    assert facts == (
        {"offered": 63, "wanted": 122, "matching_members": [0, 2]},
        {"offered": 21, "wanted": 83, "matching_members": []},
    )
