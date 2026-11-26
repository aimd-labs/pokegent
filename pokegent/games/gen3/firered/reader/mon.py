# coding: utf-8
"""Gen-3 Pokemon struct decoding: pure functions, no memory access.

Layouts and the substructure permutation come from the pret/pokefirered decompile (commit in
``constants.PRET_COMMIT``): ``include/pokemon.h`` (struct BoxPokemon / Pokemon, substructs 0-3)
and ``src/pokemon.c`` (``GetSubstruct``, ``CalculateBoxMonChecksum``, ``DecryptBoxMon``).
This is a clean-room implementation from the decompile; nothing is copied from another repo.

Contract: a decode either returns complete, validated data or raises :class:`MonDecodeError`.
The checksum is verified BEFORE any field of the encrypted block is used; a bad checksum never
yields partial data.
"""

from __future__ import annotations

import struct
from enum import Enum

from pokegent.games.gen3.firered.reader.schemas import BoxMon, MonMove, PartyMember, StatBlock
from pokegent.games.gen3.firered.rom import constants as C
from pokegent.games.gen3.firered.rom.text import decode_gen3_text

# -- byte layout (include/pokemon.h) -------------------------------------------------------
OFF_PERSONALITY = 0x00
OFF_OT_ID = 0x04
OFF_NICKNAME = 0x08  # u8[10]
OFF_LANGUAGE = 0x12
OFF_FLAGS = 0x13  # bit0 isBadEgg, bit1 hasSpecies, bit2 isEgg
OFF_OT_NAME = 0x14  # u8[7]
OFF_MARKINGS = 0x1B
OFF_CHECKSUM = 0x1C  # u16
OFF_SECURE = 0x20  # 48 encrypted bytes = 4 substructs x 12 bytes
SECURE_SIZE = 48
SUBSTRUCT_SIZE = 12
# struct Pokemon extras (after the 80-byte BoxPokemon)
OFF_STATUS = 0x50
OFF_LEVEL = 0x54
OFF_MAIL = 0x55
OFF_HP = 0x56
OFF_MAX_HP = 0x58
OFF_STATS = 0x5A  # attack, defense, speed, spAttack, spDefense (u16 each)

FLAG_IS_BAD_EGG = 1 << 0
FLAG_HAS_SPECIES = 1 << 1
FLAG_IS_EGG = 1 << 2

# src/pokemon.c GetSubstruct: SUBSTRUCT_CASE(n, v1, v2, v3, v4) gives, for personality % 24 == n,
# the memory slot (0..3) of substruct types 0 (Growth), 1 (Attacks), 2 (EVs/Condition), 3 (Misc).
_SLOT_OF_TYPE: tuple[tuple[int, int, int, int], ...] = (
    (0, 1, 2, 3),
    (0, 1, 3, 2),
    (0, 2, 1, 3),
    (0, 3, 1, 2),
    (0, 2, 3, 1),
    (0, 3, 2, 1),
    (1, 0, 2, 3),
    (1, 0, 3, 2),
    (2, 0, 1, 3),
    (3, 0, 1, 2),
    (2, 0, 3, 1),
    (3, 0, 2, 1),
    (1, 2, 0, 3),
    (1, 3, 0, 2),
    (2, 1, 0, 3),
    (3, 1, 0, 2),
    (2, 3, 0, 1),
    (3, 2, 0, 1),
    (1, 2, 3, 0),
    (1, 3, 2, 0),
    (2, 1, 3, 0),
    (3, 1, 2, 0),
    (2, 3, 1, 0),
    (3, 2, 1, 0),
)


def _memory_order(slots: tuple[int, int, int, int]) -> str:
    """Invert type->slot into the memory order string, e.g. (0,2,1,3) -> 'GEAM'."""
    order = [""] * 4
    for letter, slot in zip("GAEM", slots, strict=True):
        order[slot] = letter
    return "".join(order)


#: Substructure memory order by ``personality % 24`` (G=Growth, A=Attacks, E=EVs, M=Misc).
SUBSTRUCT_ORDER: tuple[str, ...] = tuple(_memory_order(s) for s in _SLOT_OF_TYPE)


class MonErrorKind(str, Enum):
    BAD_LENGTH = "bad_length"
    BAD_CHECKSUM = "bad_checksum"
    BAD_EGG = "bad_egg"
    EMPTY_SLOT = "empty_slot"
    INVALID_SPECIES = "invalid_species"
    INVALID_FIELD = "invalid_field"


class MonDecodeError(ValueError):
    """A mon struct could not be decoded into trustworthy data."""

    def __init__(self, kind: MonErrorKind, message: str) -> None:
        super().__init__(f"{kind.value}: {message}")
        self.kind = kind
        self.message = message


# -- primitives (pure) -----------------------------------------------------------------------


def xor_secure(personality: int, ot_id: int, block: bytes) -> bytes:
    """XOR the 48-byte block word-wise with ``personality ^ ot_id`` (encrypt == decrypt)."""
    if len(block) != SECURE_SIZE:
        raise ValueError(f"secure block must be {SECURE_SIZE} bytes, got {len(block)}")
    key = (personality ^ ot_id) & 0xFFFF_FFFF
    words = struct.unpack("<12I", block)
    return struct.pack("<12I", *(word ^ key for word in words))


def substruct_order(personality: int) -> str:
    return SUBSTRUCT_ORDER[personality % 24]


def block_checksum(block: bytes) -> int:
    """Sum of the 24 little-endian u16 words, mod 2**16. Independent of substruct order."""
    return sum(struct.unpack("<24H", block)) & 0xFFFF


def split_substructs(plain: bytes, personality: int) -> dict[str, bytes]:
    """Split a decrypted 48-byte block into ``{'G','A','E','M'}`` substructs."""
    order = substruct_order(personality)
    return {
        letter: plain[index * SUBSTRUCT_SIZE : (index + 1) * SUBSTRUCT_SIZE]
        for index, letter in enumerate(order)
    }


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def is_empty_slot(raw: bytes) -> bool:
    """True for an all-zero struct (the engine's cleared slot)."""
    return not any(raw)


# -- decoding --------------------------------------------------------------------------------


def decode_box_mon(raw: bytes) -> BoxMon:
    """Decode an 80-byte BoxPokemon. Raises :class:`MonDecodeError` on anything untrustworthy."""
    if len(raw) != C.BOX_MON_SIZE:
        raise MonDecodeError(
            MonErrorKind.BAD_LENGTH, f"box mon is {C.BOX_MON_SIZE} bytes, got {len(raw)}"
        )
    return _decode_box(raw)


def decode_party_mon(raw: bytes, slot: int = 0) -> PartyMember:
    """Decode a 100-byte struct Pokemon (party slot). Raises :class:`MonDecodeError`."""
    if len(raw) != C.PARTY_MON_SIZE:
        raise MonDecodeError(
            MonErrorKind.BAD_LENGTH, f"party mon is {C.PARTY_MON_SIZE} bytes, got {len(raw)}"
        )
    box = _decode_box(raw[: C.BOX_MON_SIZE])
    status = _u32(raw, OFF_STATUS)
    level = raw[OFF_LEVEL]
    mail = raw[OFF_MAIL]
    hp = _u16(raw, OFF_HP)
    max_hp = _u16(raw, OFF_MAX_HP)
    attack, defense, speed, sp_attack, sp_defense = struct.unpack_from("<5H", raw, OFF_STATS)

    problems: list[str] = []
    if not 1 <= level <= C.MAX_LEVEL:
        problems.append(f"level {level} outside 1..{C.MAX_LEVEL}")
    if max_hp == 0 or hp > max_hp:
        problems.append(f"hp {hp} / max_hp {max_hp} inconsistent")
    if problems:
        raise MonDecodeError(MonErrorKind.INVALID_FIELD, "; ".join(problems))

    return PartyMember(
        **box.model_dump(),
        slot=slot,
        status=status,
        level=level,
        hp=hp,
        max_hp=max_hp,
        stats=StatBlock(
            hp=max_hp,
            attack=attack,
            defense=defense,
            speed=speed,
            sp_attack=sp_attack,
            sp_defense=sp_defense,
        ),
        mail=mail,
    )


def _decode_box(raw: bytes) -> BoxMon:
    personality = _u32(raw, OFF_PERSONALITY)
    ot_id = _u32(raw, OFF_OT_ID)
    flags = raw[OFF_FLAGS]
    stored_checksum = _u16(raw, OFF_CHECKSUM)
    secure = raw[OFF_SECURE : OFF_SECURE + SECURE_SIZE]

    if is_empty_slot(raw):
        raise MonDecodeError(MonErrorKind.EMPTY_SLOT, "slot is all zero")

    # Verify the checksum BEFORE touching any field of the encrypted block.
    plain = xor_secure(personality, ot_id, secure)
    computed = block_checksum(plain)
    if computed != stored_checksum:
        raise MonDecodeError(
            MonErrorKind.BAD_CHECKSUM, f"stored 0x{stored_checksum:04X}, computed 0x{computed:04X}"
        )
    if flags & FLAG_IS_BAD_EGG:
        raise MonDecodeError(MonErrorKind.BAD_EGG, "the game flagged this mon as a Bad Egg")

    subs = split_substructs(plain, personality)
    growth, attacks, evs, misc = subs["G"], subs["A"], subs["E"], subs["M"]

    species = _u16(growth, 0)
    if species == 0:
        raise MonDecodeError(MonErrorKind.EMPTY_SLOT, "species is 0")
    if not 1 <= species < C.NUM_SPECIES:
        raise MonDecodeError(
            MonErrorKind.INVALID_SPECIES, f"species {species} outside 1..{C.NUM_SPECIES - 1}"
        )
    held_item = _u16(growth, 2)
    experience = _u32(growth, 4)
    pp_bonuses = growth[8]
    friendship = growth[9]

    move_ids = struct.unpack_from("<4H", attacks, 0)
    pps = attacks[8:12]

    ev_values = evs[0:6]
    pokerus = misc[0]
    met_location = misc[1]
    origins = _u16(misc, 2)
    iv_word = _u32(misc, 4)

    problems: list[str] = []
    if held_item >= C.ITEMS_COUNT:
        problems.append(f"held item {held_item} >= {C.ITEMS_COUNT}")
    for move_id in move_ids:
        if move_id >= C.MOVES_COUNT:
            problems.append(f"move id {move_id} >= {C.MOVES_COUNT}")
    if any(pp > 64 for pp in pps):
        problems.append(f"pp {list(pps)} above 64")
    if problems:
        raise MonDecodeError(MonErrorKind.INVALID_FIELD, "; ".join(problems))

    return BoxMon(
        personality=personality,
        ot_id=ot_id,
        nickname=decode_gen3_text(raw[OFF_NICKNAME : OFF_NICKNAME + C.POKEMON_NAME_LENGTH]),
        language=raw[OFF_LANGUAGE],
        ot_name=decode_gen3_text(raw[OFF_OT_NAME : OFF_OT_NAME + C.PLAYER_NAME_LENGTH]),
        markings=raw[OFF_MARKINGS],
        species=species,
        held_item=held_item,
        experience=experience,
        pp_bonuses=pp_bonuses,
        friendship=friendship,
        moves=tuple(MonMove(move_id=m, pp=p) for m, p in zip(move_ids, pps, strict=True)),
        evs=StatBlock(
            hp=ev_values[0],
            attack=ev_values[1],
            defense=ev_values[2],
            speed=ev_values[3],
            sp_attack=ev_values[4],
            sp_defense=ev_values[5],
        ),
        ivs=StatBlock(
            hp=iv_word & 31,
            attack=(iv_word >> 5) & 31,
            defense=(iv_word >> 10) & 31,
            speed=(iv_word >> 15) & 31,
            sp_attack=(iv_word >> 20) & 31,
            sp_defense=(iv_word >> 25) & 31,
        ),
        is_egg=bool((iv_word >> 30) & 1),
        ability_num=(iv_word >> 31) & 1,
        pokerus=pokerus,
        met_location=met_location,
        met_level=origins & 0x7F,
        met_game=(origins >> 7) & 0xF,
        pokeball=(origins >> 11) & 0xF,
        ot_gender=(origins >> 15) & 1,
    )
