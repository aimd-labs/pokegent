"""Synthetic decoder regressions; fixture provenance and layout oracle in conftest.py."""

import struct

import pytest

from pokegent.games.gen3.firered.reader.mon import (
    MonDecodeError,
    MonErrorKind,
    block_checksum,
    decode_box_mon,
    decode_party_mon,
    split_substructs,
    substruct_order,
    xor_secure,
)
from pokegent.games.gen3.firered.rom import constants as C


@pytest.mark.parametrize("index", range(24))
@pytest.mark.parametrize("multiple", [0, 1000])
def test_indexed_layout_sentinels_and_full_decode(index, multiple, layout_orders, encode_mon):
    personality = 24 * multiple + index
    order = layout_orders[index]
    assert substruct_order(personality) == order
    sentinels = {letter: bytes((ord(letter),)) * 12 for letter in "GAEM"}
    assert split_substructs(b"".join(sentinels[c] for c in order), personality) == sentinels
    raw = encode_mon(personality=personality)
    mon = decode_party_mon(raw, slot=3)
    assert (mon.personality, mon.ot_id, mon.species, mon.held_item) == (
        personality, 0xABCDEF42, 137, 23,
    )
    assert (mon.nickname, mon.ot_name, mon.language, mon.markings) == ("UNIT", "TEST", 2, 0xA5)
    assert (mon.experience, mon.friendship, mon.pp_bonuses) == (424242, 219, 0xE4)
    assert [(m.move_id, m.pp) for m in mon.moves] == [(17, 13), (29, 27), (43, 64), (0, 0)]
    assert tuple(mon.evs.model_dump().values()) == (1, 23, 45, 67, 89, 255)
    assert tuple(mon.ivs.model_dump().values()) == (31, 0, 1, 30, 17, 9)
    assert (mon.is_egg, mon.ability_num, mon.pokerus, mon.met_location) == (False, 1, 0x91, 203)
    assert (mon.met_level, mon.met_game, mon.pokeball, mon.ot_gender) == (37, 9, 11, 1)
    assert (mon.slot, mon.level, mon.hp, mon.max_hp, mon.status, mon.mail) == (
        3, 37, 71, 113, 0x10203040, 0xFF,
    )
    assert tuple(mon.stats.model_dump().values()) == (113, 47, 59, 61, 73, 83)
    assert len(raw) == 100
    box_raw = encode_mon(personality=personality, party=False)
    assert len(box_raw) == 80
    box = decode_box_mon(box_raw)
    assert box.model_dump() == {key: mon.model_dump()[key] for key in box.model_dump()}
    assert (box.tid, box.sid) == (0xEF42, 0xABCD)


def test_xor_all_twelve_little_endian_words():
    # PID ^ OTID = 0x22664422, whose little-endian bytes are 22 44 66 22.
    plain = bytes(range(48))
    expected = struct.pack(
        "<12I", 0x21644522, 0x25604126, 0x296C4D2A, 0x2D68492E,
        0x31745532, 0x35705136, 0x397C5D3A, 0x3D78593E,
        0x01446502, 0x05406106, 0x094C6D0A, 0x0D48690E,
    )
    encrypted = xor_secure(0x11223344, 0x33447766, plain)
    assert encrypted == expected
    assert xor_secure(0x11223344, 0x33447766, encrypted) == plain


@pytest.mark.parametrize("size", [0, 47, 49])
def test_xor_rejects_wrong_secure_lengths(size):
    with pytest.raises(ValueError, match="secure block must be 48 bytes"):
        xor_secure(1, 2, bytes(size))


def test_checksum_little_endian_sum_and_wrap():
    assert block_checksum(b"\x01\x02" * 24) == 24 * 513
    assert block_checksum(struct.pack("<24H", *range(1, 25))) == 300
    assert block_checksum(b"\xff\xff" * 24) == 65512
    assert block_checksum(bytes(48)) == 0


@pytest.mark.parametrize("decoder,size", [(decode_box_mon, 80), (decode_party_mon, 100)])
@pytest.mark.parametrize("delta", [-80, -1, 1, 20])
def test_wrong_lengths_have_exact_typed_error(decoder, size, delta):
    with pytest.raises(MonDecodeError) as caught:
        decoder(bytes(size + delta))
    assert type(caught.value) is MonDecodeError
    assert caught.value.kind is MonErrorKind.BAD_LENGTH


@pytest.mark.parametrize("decoder,size", [(decode_box_mon, 80), (decode_party_mon, 100)])
def test_cleared_slots_never_return_partial_data(decoder, size):
    with pytest.raises(MonDecodeError) as caught:
        decoder(bytes(size))
    assert type(caught.value) is MonDecodeError
    assert caught.value.kind is MonErrorKind.EMPTY_SLOT


@pytest.mark.parametrize("offset,bit", [(0x20 + 17, 4), (0x1C, 1), (4, 1), (0, 1)])
@pytest.mark.parametrize("party", [False, True])
def test_selected_payload_checksum_and_key_corruption_fail_closed(encode_mon, offset, bit, party):
    # Chosen corruptions are detectable; additive checksums are not cryptographic integrity.
    raw = bytearray(encode_mon(party=party))
    raw[offset] ^= bit
    with pytest.raises(MonDecodeError) as caught:
        (decode_party_mon if party else decode_box_mon)(bytes(raw))
    assert type(caught.value) is MonDecodeError
    assert caught.value.kind is MonErrorKind.BAD_CHECKSUM


@pytest.mark.parametrize("overrides,kind", [
    ({"flags": 3}, MonErrorKind.BAD_EGG),
    ({"species": 0}, MonErrorKind.EMPTY_SLOT),
    ({"species": C.NUM_SPECIES}, MonErrorKind.INVALID_SPECIES),
    ({"species": 0xFFFF}, MonErrorKind.INVALID_SPECIES),
    ({"held_item": C.ITEMS_COUNT}, MonErrorKind.INVALID_FIELD),
    ({"moves": (C.MOVES_COUNT, 0, 0, 0)}, MonErrorKind.INVALID_FIELD),
    ({"pps": (0, 0, 0, 65)}, MonErrorKind.INVALID_FIELD),
])
@pytest.mark.parametrize("party", [False, True])
def test_valid_checksum_invalid_box_fields_are_typed(encode_mon, overrides, kind, party):
    raw = encode_mon(**overrides, party=party)
    with pytest.raises(MonDecodeError) as caught:
        (decode_party_mon if party else decode_box_mon)(raw)
    assert type(caught.value) is MonDecodeError
    assert caught.value.kind is kind


@pytest.mark.parametrize("overrides", [
    {"level": 0}, {"level": 101}, {"hp": 114}, {"hp": 0, "max_hp": 0},
])
def test_valid_checksum_invalid_party_extras_are_typed(encode_mon, overrides):
    with pytest.raises(MonDecodeError) as caught:
        decode_party_mon(encode_mon(**overrides))
    assert type(caught.value) is MonDecodeError
    assert caught.value.kind is MonErrorKind.INVALID_FIELD


@pytest.mark.parametrize("species,level", [(1, 1), (C.NUM_SPECIES - 1, 100)])
def test_supported_field_boundaries_and_egg(encode_mon, species, level):
    mon = decode_party_mon(encode_mon(
        species=species, level=level, hp=0, is_egg=True, ability_num=0,
        held_item=C.ITEMS_COUNT - 1, moves=(0, 1, 2, C.MOVES_COUNT - 1),
    ))
    assert (mon.species, mon.level, mon.hp, mon.is_egg, mon.ability_num) == (
        species, level, 0, True, 0,
    )
