"""Reader regressions using authored RAM and the existing synthetic mon encoder.

Scenario references: pokegent-archive tests/firered/test_reader.py at
492b71b1019bc92db299a20eab6ca7b22724c5c6; assertions follow current public APIs.
Existing AIMD Labs / Nous Research MIT notices remain in LICENSE.
"""

import struct

import pytest

from pokegent.games.gen3.firered.reader.memory_port import EWRAM_END, SnapshotMemoryReader
from pokegent.games.gen3.firered.reader.reader import FireRedReader, StorageDecodeCache
from pokegent.games.gen3.firered.reader.schemas import ReadError
from pokegent.games.gen3.firered.rom import constants as C
from pokegent.games.gen3.firered.rom.rom_data import ProfileError


def assert_no_payload(snapshot):
    assert snapshot.player is snapshot.flags is snapshot.vars is snapshot.pokedex is None
    assert snapshot.storage is snapshot.registered_item is snapshot.dynamic_warp is None
    assert snapshot.party == snapshot.bag == ()


def assert_invalid(snapshot, subsystem, code, addr=None):
    assert not snapshot.usable and not snapshot.health.valid
    errors = snapshot.health.read_errors
    assert errors and all(isinstance(error, ReadError) for error in errors)
    assert [(error.subsystem, error.code, error.addr) for error in errors] == [
        (subsystem, code, addr)
    ]
    assert errors[0].message
    assert snapshot.health.confidence[subsystem] == 0
    assert_no_payload(snapshot)


@pytest.mark.parametrize("block", [1, 2])
@pytest.mark.parametrize("pointer,code", [
    (0, "pointer_out_of_ewram"), (0x01FFFFFC, "pointer_out_of_ewram"),
    (0x03000000, "pointer_out_of_ewram"), (0x02000001, "pointer_unaligned"),
])
def test_save_pointer_guards(ram, block, pointer, code):
    profile, port, _, _ = ram
    port.poke_u32(profile.symbol(f"gSaveBlock{block}Ptr").address, pointer)
    assert_invalid(FireRedReader(port, profile).read_snapshot(), "save_blocks", code, pointer)
    assert len(port.read_log) == 1  # Invalid pointers must never be dereferenced.


@pytest.mark.parametrize("block", [1, 2])
def test_save_pointer_exact_fit_and_overrun(ram, block):
    profile, port, sb1, sb2 = ram
    if block == 1:
        field = profile.field("SaveBlock1", "vars")
        size = field.offset + field.size
    else:
        size = profile.struct_size("SaveBlock2")
    pointer = EWRAM_END - size
    assert pointer % 4 == 0
    original = sb1 if block == 1 else sb2
    port.poke(pointer, port.read_range(original, size))
    port.poke_u32(profile.symbol(f"gSaveBlock{block}Ptr").address, pointer)
    assert FireRedReader(port, profile).read_snapshot().usable
    port.poke_u32(profile.symbol(f"gSaveBlock{block}Ptr").address, pointer + 4)
    assert_invalid(FireRedReader(port, profile).read_snapshot(), "save_blocks",
                   "pointer_out_of_ewram", pointer + 4)


@pytest.mark.parametrize("missing", ["pointers", "sb1", "sb2", "party"])
def test_unmapped_reader_ranges_are_typed_failures(ram, missing):
    profile, port, sb1, sb2 = ram
    pointer_addr = profile.symbol("gSaveBlock1Ptr").address
    span = max(profile.symbol("gSaveBlock2Ptr").address, pointer_addr) + 4 - pointer_addr
    sb1_field = profile.field("SaveBlock1", "vars")
    ranges = {
        "pointers": (pointer_addr, span),
        "sb1": (sb1, sb1_field.offset + sb1_field.size),
        "sb2": (sb2, profile.struct_size("SaveBlock2")),
        "party": (profile.symbol("gPlayerPartyCount").address,
                  profile.symbol("gPlayerParty").address + 600
                  - profile.symbol("gPlayerPartyCount").address),
    }
    addr, size = ranges[missing]
    region_base, region_size = ((0x03000000, 0x8000) if missing == "pointers" else
                                (0x02000000, 0x40000))
    raw = port.read_range(region_base, region_size)
    regions = {0x02000000: port.read_range(0x02000000, 0x40000),
               0x03000000: port.read_range(0x03000000, 0x8000)}
    del regions[region_base]
    regions[region_base] = raw[:addr - region_base + size - 1]
    regions[addr + size] = raw[addr + size - region_base:]
    broken = SnapshotMemoryReader(regions)
    snapshot = FireRedReader(broken, profile).read_snapshot()
    assert_invalid(snapshot, "party" if missing == "party" else "save_blocks", "read_failed", addr)
    assert sum(entry[1] == pointer_addr for entry in broken.read_log) <= 2


@pytest.mark.parametrize("keep_moving", [False, True])
def test_relocation_retries_one_complete_distinct_view(ram, keep_moving):
    profile, port, sb1, sb2 = ram
    money_off = profile.field("SaveBlock1", "money").offset
    key_off = profile.field("SaveBlock2", "encryptionKey").offset
    port.poke_u32(sb1 + money_off, 123)
    new_sb1, new_sb2, new_key = 0x02020000, 0x02028000, 0x12340000
    field = profile.field("SaveBlock1", "vars")
    port.poke(new_sb1, port.read_range(sb1, field.offset + field.size))
    port.poke(new_sb2, port.read_range(sb2, profile.struct_size("SaveBlock2")))
    port.poke_u32(new_sb1 + money_off, 456 ^ new_key)
    port.poke_u32(new_sb2 + key_off, new_key)
    pointer_addr = profile.symbol("gSaveBlock1Ptr").address
    pointer2_addr = profile.symbol("gSaveBlock2Ptr").address
    span = pointer2_addr + 4 - pointer_addr
    pointer_reads = []

    def relocate(index):
        if moving.read_log[index] == ("range", pointer_addr, span):
            pointer_reads.append(index)
            if len(pointer_reads) == 2:
                moving.poke_u32(pointer_addr, new_sb1)
                moving.poke_u32(pointer2_addr, new_sb2)
            elif keep_moving and len(pointer_reads) == 4:
                moving.poke_u32(pointer_addr, sb1)
                moving.poke_u32(pointer2_addr, sb2)

    moving = SnapshotMemoryReader({0x02000000: port.read_range(0x02000000, 0x40000),
                                   0x03000000: port.read_range(0x03000000, 0x8000)},
                                  on_read=relocate)
    snapshot = FireRedReader(moving, profile).read_snapshot()
    assert len(pointer_reads) == 4
    if keep_moving:
        assert_invalid(snapshot, "save_blocks", "pointer_changed", pointer_addr)
    else:
        assert snapshot.usable and not snapshot.health.read_errors
        assert snapshot.player.money == 456  # Both relocated blocks must win, not either old one.
    for address in (sb1, sb2, new_sb1, new_sb2):
        assert sum(entry[1] == address for entry in moving.read_log) == 1


@pytest.mark.parametrize("source", ["live", "saveblock"])
@pytest.mark.parametrize("count", [0, 6, 7])
def test_party_count_bounds(ram, encode_mon, source, count):
    profile, port, sb1, _ = ram
    count_addr = (profile.symbol("gPlayerPartyCount").address if source == "live" else
                  sb1 + profile.field("SaveBlock1", "playerPartyCount").offset)
    party_addr = (profile.symbol("gPlayerParty").address if source == "live" else
                  sb1 + profile.field("SaveBlock1", "playerParty").offset)
    port.poke(party_addr, b"".join(encode_mon(species=slot + 1) for slot in range(6)))
    port.poke(count_addr, bytes([count]))
    snapshot = FireRedReader(port, profile, party_source=source).read_snapshot()
    if count == 7:
        assert_invalid(snapshot, "party", "party_count_invalid")
    else:
        assert snapshot.usable, snapshot.health.read_errors
        assert [mon.species for mon in snapshot.party] == list(range(1, count + 1))
        assert [mon.slot for mon in snapshot.party] == list(range(count))


def test_default_live_and_explicit_saved_party_are_distinct(ram, encode_mon):
    profile, port, sb1, _ = ram
    port.poke(profile.symbol("gPlayerPartyCount").address, b"\x01")
    port.poke(profile.symbol("gPlayerParty").address, encode_mon(species=17))
    port.poke(sb1 + profile.field("SaveBlock1", "playerPartyCount").offset, b"\x01")
    port.poke(sb1 + profile.field("SaveBlock1", "playerParty").offset, encode_mon(species=29))
    live = FireRedReader(port, profile).read_snapshot()
    saved = FireRedReader(port, profile, party_source="saveblock").read_snapshot()
    assert live.usable and saved.usable
    assert [mon.species for mon in live.party] == [17]
    assert [mon.species for mon in saved.party] == [29]


@pytest.mark.parametrize("source", ["live", "saveblock"])
@pytest.mark.parametrize("kind", ["bad_checksum", "bad_egg", "empty_slot"])
def test_corrupt_party_drops_valid_sibling_and_all_core_data(ram, encode_mon, source, kind):
    profile, port, sb1, _ = ram
    count_addr = (profile.symbol("gPlayerPartyCount").address if source == "live" else
                  sb1 + profile.field("SaveBlock1", "playerPartyCount").offset)
    party_addr = (profile.symbol("gPlayerParty").address if source == "live" else
                  sb1 + profile.field("SaveBlock1", "playerParty").offset)
    port.poke(count_addr, b"\x02")
    port.poke(party_addr, encode_mon() * 2)
    reader = FireRedReader(port, profile, party_source=source)
    assert len(reader.read_snapshot().party) == 2
    corrupt = bytearray(encode_mon(flags=3) if kind == "bad_egg" else encode_mon())
    if kind == "bad_checksum":
        corrupt[32] ^= 1
    elif kind == "empty_slot":
        corrupt[:] = bytes(100)
    port.poke(party_addr + 100, corrupt)
    assert_invalid(reader.read_snapshot(), "party", f"mon_{kind}")


def test_leftover_party_slot_ignored_outside_storage(ram, encode_mon):
    profile, port, _, _ = ram
    port.poke(profile.symbol("gPlayerPartyCount").address, b"\x01")
    port.poke(profile.symbol("gPlayerParty").address,
              encode_mon(species=17) + encode_mon(species=29))
    snapshot = FireRedReader(port, profile).read_snapshot()
    assert snapshot.usable and [mon.species for mon in snapshot.party] == [17]


@pytest.mark.parametrize("pocket,field,capacity", C.BAG_POCKETS)
def test_bag_first_last_slots_and_inclusive_bounds(ram, pocket, field, capacity):
    profile, port, sb1, _ = ram
    offset = sb1 + profile.field("SaveBlock1", field).offset
    port.poke(offset, struct.pack("<HH", 1, 1))
    port.poke(offset + (capacity - 1) * 4, struct.pack("<HH", C.ITEMS_COUNT - 1, 999))
    snapshot = FireRedReader(port, profile).read_snapshot()
    assert snapshot.usable, snapshot.health.read_errors
    bag = next(bag for bag in snapshot.bag if bag.pocket == pocket)
    assert bag.capacity == capacity
    assert [(item.item_id, item.quantity) for item in bag.items] == [(1, 1), (374, 999)]


@pytest.mark.parametrize("pocket,field,capacity", C.BAG_POCKETS)
@pytest.mark.parametrize("item,quantity", [(375, 1), (1, 1000), (0, 1), (1, 0)])
def test_bad_bag_slot_invalidates_core(ram, pocket, field, capacity, item, quantity):
    profile, port, sb1, _ = ram
    offset = sb1 + profile.field("SaveBlock1", field).offset + (capacity - 1) * 4
    port.poke(offset, struct.pack("<HH", item, quantity))
    snapshot = FireRedReader(port, profile).read_snapshot()
    assert_invalid(snapshot, "bag", "bag_slot_invalid")
    assert f"{pocket}[{capacity - 1}]" in snapshot.health.read_errors[0].message


def test_bag_key_rotation_and_key_only_cache_invalidation(ram):
    profile, port, sb1, sb2 = ram
    cache = StorageDecodeCache()
    snapshots = []
    for key in (0xA1B2C3D4, 0x31415926):
        port.poke_u32(sb2 + profile.field("SaveBlock2", "encryptionKey").offset, key)
        port.poke_u32(sb1 + profile.field("SaveBlock1", "money").offset, 321 ^ key)
        port.poke(sb1 + profile.field("SaveBlock1", "coins").offset,
                  struct.pack("<H", key & 0xFFFF))
        for _, field, capacity in C.BAG_POCKETS:
            offset = sb1 + profile.field("SaveBlock1", field).offset
            port.poke(offset, struct.pack("<HH", 0, key & 0xFFFF) * capacity)
            port.poke(offset, struct.pack("<HH", 23, 37 ^ (key & 0xFFFF)))
        snapshot = FireRedReader(port, profile, storage_cache=cache).read_snapshot()
        assert snapshot.usable, snapshot.health.read_errors
        assert all([(item.item_id, item.quantity) for item in pocket.items] == [(23, 37)]
                   for pocket in snapshot.bag)
        snapshots.append(snapshot)
    assert snapshots[0].bag == snapshots[1].bag
    key = 0x31415927  # Leave every bag byte unchanged; keep other keyed fields valid.
    port.poke_u32(sb2 + profile.field("SaveBlock2", "encryptionKey").offset, key)
    port.poke_u32(sb1 + profile.field("SaveBlock1", "money").offset, 321 ^ key)
    port.poke(sb1 + profile.field("SaveBlock1", "coins").offset, struct.pack("<H", key & 0xFFFF))
    snapshot = FireRedReader(port, profile, storage_cache=cache).read_snapshot()
    assert not snapshot.usable and snapshot.player is None and snapshot.bag == ()
    assert_no_payload(snapshot)
    assert {error.subsystem for error in snapshot.health.read_errors} == {"bag"}
    assert {error.code for error in snapshot.health.read_errors} == {"bag_slot_invalid"}


def test_bag_errors_are_bounded_per_pocket(ram):
    profile, port, sb1, _ = ram
    pocket, field, _ = C.BAG_POCKETS[0]
    port.poke(sb1 + profile.field("SaveBlock1", field).offset, struct.pack("<HH", 375, 1) * 5)
    snapshot = FireRedReader(port, profile).read_snapshot()
    assert not snapshot.usable and snapshot.bag == () and snapshot.player is None
    assert_no_payload(snapshot)
    assert [(error.subsystem, error.code) for error in snapshot.health.read_errors] == [
        ("bag", "bag_slot_invalid")
    ] * 4
    assert [error.message for error in snapshot.health.read_errors] == [
        *(f"{pocket}[{index}]: item 375, quantity 1" for index in range(3)),
        f"{pocket}: 5 invalid slots in total",
    ]


def test_invalid_deferred_hash_still_has_typed_error_and_no_payload(ram):
    profile, port, _, _ = ram
    port.poke_u32(profile.symbol("gSaveBlock1Ptr").address, 0x02000001)
    snapshot = FireRedReader(port, profile).read_snapshot(defer_hash=True)
    assert_invalid(snapshot, "save_blocks", "pointer_unaligned", 0x02000001)
    assert snapshot.state_hash == ""


@pytest.mark.parametrize("kind", ["unsupported", "missing_symbol", "pocket_width"])
def test_configuration_errors_remain_profile_errors(ram, kind):
    profile, port, _, _ = ram
    if kind == "unsupported":
        profile = profile.model_copy(update={"supported": False})
    elif kind == "missing_symbol":
        profile = profile.model_copy(update={"symbols": {
            name: value for name, value in profile.symbols.items() if name != "gPlayerParty"
        }})
    else:
        block = profile.structs["SaveBlock1"]
        fields = dict(block.fields)
        field = C.BAG_POCKETS[0][1]
        fields[field] = fields[field].model_copy(update={"size": fields[field].size - 1})
        profile = profile.model_copy(update={"structs": {
            **profile.structs, "SaveBlock1": block.model_copy(update={"fields": fields})
        }})
    with pytest.raises(ProfileError):
        FireRedReader(port, profile).read_snapshot()


def test_negative_explicit_sequence_is_a_caller_error(ram):
    profile, port, _, _ = ram
    with pytest.raises(ValueError, match="seq must be >= 0"):
        FireRedReader(port, profile).read_snapshot(seq=-1)
    assert port.read_log == []
