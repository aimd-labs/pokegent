"""FireRed ``gTrainers`` and trainer-party ROM decoder."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from pokegent.games.gen3.firered.rom.rom_data import RomProfile
from pokegent.games.gen3.firered.rom.text import decode_gen3_text

ROM_BASE = 0x0800_0000
TRAINER_ENTRY_SIZE = 40
TRAINER_COUNT = 743
TRAINER_CLASS_ENTRY_SIZE = 13
TRAINER_CLASS_COUNT = 100


class TrainerRomError(ValueError):
    pass


@dataclass(frozen=True)
class TrainerMon:
    species: int
    level: int
    moves: tuple[int, ...] = ()
    held_item: int = 0


@dataclass(frozen=True)
class TrainerParty:
    trainer_id: int
    name: str
    levels: tuple[int, ...]
    members: tuple[TrainerMon, ...] = ()

    @property
    def ace_level(self) -> int:
        return max(self.levels)


class TrainerTable:
    """Guarded view over FireRed's ROM-resident trainer metadata and party pointers."""

    def __init__(self, rom: bytes, profile: RomProfile) -> None:
        entry = profile.rom_table("gTrainers")
        if entry.size < TRAINER_ENTRY_SIZE * TRAINER_COUNT:
            raise TrainerRomError("gTrainers profile entry is shorter than NUM_TRAINERS")
        class_entry = profile.rom_table("gTrainerClassNames")
        if class_entry.size < TRAINER_CLASS_ENTRY_SIZE * TRAINER_CLASS_COUNT:
            raise TrainerRomError(
                "gTrainerClassNames profile entry is shorter than NUM_TRAINER_CLASSES"
            )
        self._rom = rom
        self._base = entry.address
        self._class_base = class_entry.address

    def party(self, trainer_id: int) -> TrainerParty:
        if not 0 < trainer_id < TRAINER_COUNT:
            raise TrainerRomError(f"trainer id {trainer_id} is outside gTrainers")
        record = self._slice(self._base + trainer_id * TRAINER_ENTRY_SIZE, TRAINER_ENTRY_SIZE)
        party_flags = record[0]
        party_size = record[0x20]
        party_ptr = struct.unpack_from("<I", record, 0x24)[0]
        if party_flags & ~0x3:
            raise TrainerRomError(f"trainer {trainer_id} has unknown party flags {party_flags}")
        if not 1 <= party_size <= 6:
            raise TrainerRomError(f"trainer {trainer_id} has invalid party size {party_size}")
        custom_moves = bool(party_flags & 0x1)
        held_items = bool(party_flags & 0x2)
        stride = 16 if custom_moves else 8
        raw_party = self._slice(party_ptr, stride * party_size)
        members = []
        for index in range(party_size):
            row = raw_party[index * stride : (index + 1) * stride]
            level = row[2]
            species = struct.unpack_from("<H", row, 4)[0]
            moves = (
                tuple(
                    struct.unpack_from("<H", row, (8 if held_items else 6) + 2 * move)[0]
                    for move in range(4)
                )
                if custom_moves
                else ()
            )
            held_item = struct.unpack_from("<H", row, 6)[0] if held_items else 0
            members.append(TrainerMon(species, level, moves, held_item))
        levels = tuple(member.level for member in members)
        if any(not 1 <= level <= 100 for level in levels):
            raise TrainerRomError(f"trainer {trainer_id} has invalid levels {levels}")
        name = decode_gen3_text(record[4:16])
        return TrainerParty(trainer_id, name, levels, tuple(members))

    def metadata(self, trainer_id: int) -> tuple[int | None, str, str]:
        """Return ``(id, class name, trainer name)`` from one ROM trainer row."""
        if not 0 < trainer_id < TRAINER_COUNT:
            return None, "unknown", "unknown"
        record = self._slice(self._base + trainer_id * TRAINER_ENTRY_SIZE, TRAINER_ENTRY_SIZE)
        class_table = self._slice(self._class_base, TRAINER_CLASS_ENTRY_SIZE * TRAINER_CLASS_COUNT)
        return self.metadata_from_record(trainer_id, record, class_table)

    @staticmethod
    def metadata_from_record(
        trainer_id: int, record: bytes, class_table: bytes
    ) -> tuple[int | None, str, str]:
        """Decode metadata when the caller has already read the 40-byte ROM row."""
        if not 0 < trainer_id < TRAINER_COUNT:
            return None, "unknown", "unknown"
        if len(record) != TRAINER_ENTRY_SIZE:
            raise TrainerRomError("trainer row has the wrong size")
        if len(class_table) < TRAINER_CLASS_ENTRY_SIZE * TRAINER_CLASS_COUNT:
            raise TrainerRomError("trainer class table has the wrong size")
        trainer_class = record[1]
        if trainer_class >= TRAINER_CLASS_COUNT:
            class_name = "unknown"
        else:
            start = trainer_class * TRAINER_CLASS_ENTRY_SIZE
            class_name = decode_gen3_text(class_table[start : start + TRAINER_CLASS_ENTRY_SIZE])
        return trainer_id, class_name, decode_gen3_text(record[4:16])

    def _slice(self, address: int, size: int) -> bytes:
        offset = address - ROM_BASE
        if offset < 0 or offset + size > len(self._rom):
            raise TrainerRomError(f"ROM address 0x{address:08X}+0x{size:X} is outside the ROM")
        return self._rom[offset : offset + size]
