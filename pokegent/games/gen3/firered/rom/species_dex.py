"""National numbers from the user's ROM, never a bundled species mapping."""

import struct

from pokegent.games.gen3.firered.reader.memory_port import MemoryPort, MemoryReadError
from pokegent.games.gen3.firered.rom.rom_data import ProfileError, RomProfile

TABLE = "sSpeciesToNationalPokedexNum"
COUNT = 411


def decode_species_dex(raw: bytes) -> tuple[int, ...]:
    """The ROM array is indexed by internal species minus one (pret pokemon.c:39)."""
    if len(raw) != COUNT * 2:
        return ()
    values = struct.unpack(f"<{COUNT}H", raw)
    if values[:251] != tuple(range(1, 252)):
        return ()
    # Legacy Unown slots are not real species; vanilla stores out-of-dex flags there.
    if any(value > 386 for value in values[:251] + values[276:]):
        return ()
    if any(value > COUNT for value in values[251:276]):
        return ()
    return values[:251] + (0,) * 25 + values[276:]


def read_species_dex(port: MemoryPort, profile: RomProfile) -> tuple[int, ...]:
    try:
        table = profile.rom_table(TABLE)
        if table.size != COUNT * 2:
            return ()
        return decode_species_dex(port.read_range(table.address, table.size))
    except (ProfileError, MemoryReadError, ValueError):
        return ()


def national_dex(values: tuple[int, ...], species: int) -> int | None:
    if 1 <= species <= len(values):
        return values[species - 1] or None
    return None
