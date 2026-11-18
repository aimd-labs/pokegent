"""Storage boundaries using invented bytes and the shared authored mon encoder."""

import pytest

from pokegent.games.gen3.firered.reader.memory_port import MemoryReadError, SnapshotMemoryReader
from pokegent.games.gen3.firered.reader.mon import MonDecodeError, MonErrorKind
from pokegent.games.gen3.firered.reader.storage import (
    STORAGE_FROM_SAVE_BLOCK1,
    STORAGE_SIZE,
    decode_storage,
    read_storage,
)


@pytest.mark.parametrize("delta", [-1, 1])
def test_storage_requires_exact_length(delta):
    assert decode_storage(bytes(STORAGE_SIZE)).occupied == 0
    with pytest.raises(ValueError, match="PokemonStorage is"):
        decode_storage(bytes(STORAGE_SIZE + delta))


@pytest.mark.parametrize("current_box", [0, 13, 14])
def test_current_box_bounds_and_empty_shape(current_box):
    raw = bytearray(STORAGE_SIZE)
    raw[0] = current_box
    if current_box == 14:
        with pytest.raises(ValueError, match="current box 14 outside 0..13"):
            decode_storage(raw)
        return
    storage = decode_storage(raw)
    assert storage.current_box == current_box and storage.occupied == 0
    assert isinstance(storage.boxes, tuple) and len(storage.boxes) == 14
    assert all(isinstance(box, tuple) and box == (None,) * 30 for box in storage.boxes)
    assert len(storage.names) == len(storage.wallpapers) == 14
    assert storage.get(0, 0) is storage.get(13, 29) is None


def test_first_last_placement_names_and_wallpapers(encode_mon):
    raw = bytearray(STORAGE_SIZE)
    raw[4:84] = encode_mon(party=False, species=17, personality=101)
    last = 4 + 419 * 80
    raw[last:last + 80] = encode_mon(party=False, species=29, personality=202)
    names = 4 + 420 * 80
    raw[names:names + 9] = bytes((0xCF, 0xC8, 0xC3, 0xCE)) + b"\xff" * 5  # UNIT
    raw[names + 13 * 9:names + 14 * 9] = (
        bytes((0xCE, 0xBF, 0xCD, 0xCE)) + b"\xff" * 5  # TEST
    )
    raw[-14:] = bytes(range(31, 45))
    storage = decode_storage(raw)
    assert storage.occupied == 2
    assert (storage.get(0, 0).species, storage.get(0, 0).personality) == (17, 101)
    assert (storage.get(13, 29).species, storage.get(13, 29).personality) == (29, 202)
    assert all(mon is None for i, box in enumerate(storage.boxes)
               for j, mon in enumerate(box) if (i, j) not in ((0, 0), (13, 29)))
    assert storage.names[0] == "UNIT" and storage.names[13] == "TEST"
    assert storage.wallpapers == tuple(range(31, 45))


@pytest.mark.parametrize("box,slot", [(-1, 0), (14, 0), (0, -1), (0, 30), (13, 30)])
def test_get_rejects_out_of_bounds_with_coordinates(box, slot):
    storage = decode_storage(bytes(STORAGE_SIZE))
    with pytest.raises(IndexError) as caught:
        storage.get(box, slot)
    assert caught.value.args == ((box, slot),)


def test_corrupt_final_slot_keeps_typed_cause(encode_mon):
    raw = bytearray(STORAGE_SIZE)
    last = 4 + 419 * 80
    raw[last:last + 80] = encode_mon(party=False)
    assert decode_storage(raw).get(13, 29).species == 137
    raw[last + 32] ^= 1
    with pytest.raises(ValueError, match="box 13 slot 29") as caught:
        decode_storage(raw)
    assert type(caught.value) is ValueError
    assert isinstance(caught.value.__cause__, MonDecodeError)
    assert caught.value.__cause__.kind is MonErrorKind.BAD_CHECKSUM


def test_read_storage_uses_relocated_exact_range(encode_mon):
    sb1, old_sb1 = 0x0201007C, 0x02001000
    raw = bytearray(STORAGE_SIZE)
    raw[4:84] = encode_mon(party=False, species=29)
    port = SnapshotMemoryReader({sb1 + STORAGE_FROM_SAVE_BLOCK1: raw,
                                 old_sb1 + STORAGE_FROM_SAVE_BLOCK1: b"\x0e"})
    storage = read_storage(port, sb1)
    assert storage.get(0, 0).species == 29 and storage.occupied == 1
    assert port.read_log == [("range", sb1 + STORAGE_FROM_SAVE_BLOCK1, STORAGE_SIZE)]
    short = SnapshotMemoryReader({sb1 + STORAGE_FROM_SAVE_BLOCK1: raw[:-1]})
    with pytest.raises(MemoryReadError) as caught:
        read_storage(short, sb1)
    assert (caught.value.addr, caught.value.size) == (sb1 + STORAGE_FROM_SAVE_BLOCK1,
                                                    STORAGE_SIZE)
