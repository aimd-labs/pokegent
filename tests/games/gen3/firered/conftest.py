"""Tiny authored mon fixtures; no ROM, charmap clone or campaign RAM.

Packing/test patterns adapted from tests/firered/{encoder,test_mon}.py at
pokegent-archive 492b71b1019bc92db299a20eab6ca7b22724c5c6 (AIMD Labs MIT).
The corresponding pokevon-agent source is 7ccde7d21551f07cd5fc6cd7dba368864666a050.
Existing AIMD Labs / Nous Research MIT notices remain in LICENSE.
Technical layout: pret/pokefirered 037335f4c725d7c9aecdac87066f2002b4bd7e14,
include/pokemon.h and src/pokemon.c GetSubstruct: each indexed tuple gives the
memory slot of G/A/E/M. These literal memory orders independently invert that
description; they are not generated from the decoder's slot table.
"""

import struct

import pytest

ORDERS = (
    "GAEM", "GAME", "GEAM", "GEMA", "GMAE", "GMEA",
    "AGEM", "AGME", "AEGM", "AEMG", "AMGE", "AMEG",
    "EGAM", "EGMA", "EAGM", "EAMG", "EMGA", "EMAG",
    "MGAE", "MGEA", "MAGE", "MAEG", "MEGA", "MEAG",
)


@pytest.fixture
def layout_orders():
    return ORDERS


@pytest.fixture
def encode_mon():
    def encode(
        *, personality=0x12345678, ot_id=0xABCDEF42, species=137, held_item=23,
        experience=424242, moves=(17, 29, 43, 0), pps=(13, 27, 64, 0),
        evs=(1, 23, 45, 67, 89, 255), ivs=(31, 0, 1, 30, 17, 9),
        is_egg=False, ability_num=1, flags=None, level=37, hp=71, max_hp=113,
        party=True,
    ):
        subs = {
            "G": struct.pack("<HHIBBH", species, held_item, experience, 0xE4, 219, 0),
            "A": struct.pack("<4H4B", *moves, *pps),
            "E": bytes((*evs, 0, 0, 0, 0, 0, 0)),
            "M": struct.pack(
                "<BBHII", 0x91, 203, 37 | (9 << 7) | (11 << 11) | (1 << 15),
                sum(iv << (5 * i) for i, iv in enumerate(ivs))
                | (int(is_egg) << 30) | (ability_num << 31), 0,
            ),
        }
        plain = b"".join(subs[letter] for letter in ORDERS[personality % 24])
        # Deliberately avoid all production encoding/checksum/order helpers.
        checksum = sum(int.from_bytes(plain[i:i + 2], "little") for i in range(0, 48, 2))
        key = (personality ^ ot_id).to_bytes(4, "little")
        secure = bytes(value ^ key[i % 4] for i, value in enumerate(plain))
        header = bytearray(32)
        struct.pack_into("<II", header, 0, personality, ot_id)
        header[8:18] = bytes((0xCF, 0xC8, 0xC3, 0xCE)) + b"\xff" * 6  # UNIT
        header[18:20] = bytes((2, (2 | (int(is_egg) << 2)) if flags is None else flags))
        header[20:27] = bytes((0xCE, 0xBF, 0xCD, 0xCE)) + b"\xff" * 3  # TEST
        header[27] = 0xA5
        struct.pack_into("<H", header, 28, checksum % 65536)
        box = bytes(header) + secure
        extra = struct.pack("<IBBHH5H", 0x10203040, level, 0xFF, hp, max_hp,
                            47, 59, 61, 73, 83)
        return box + extra if party else box

    return encode
