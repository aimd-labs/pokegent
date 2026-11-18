"""Reader-only storage policy and party context, using authored RAM."""

import struct

import pytest

from pokegent.games.gen3.firered.reader.memory_port import SnapshotMemoryReader
from pokegent.games.gen3.firered.reader.reader import FireRedReader, StorageDecodeCache
from pokegent.games.gen3.firered.reader.storage import (
    G_STORAGE_BOX_OPTION_OFFSET,
    STORAGE_FROM_SAVE_BLOCK1,
    STORAGE_SIZE,
)
from pokegent.games.gen3.firered.rom import constants as C


@pytest.mark.parametrize("failure", ["corrupt", "unmapped"])
def test_storage_failure_suppresses_sidecar_then_recovers_shared_cache(ram, encode_mon, failure):
    profile, port, sb1, _ = ram
    cache = StorageDecodeCache()
    address = sb1 + STORAGE_FROM_SAVE_BLOCK1
    raw = bytearray(STORAGE_SIZE)
    raw[4:84] = encode_mon(party=False, species=17, personality=101)
    port.poke(address, raw)
    a = FireRedReader(port, profile, storage_cache=cache).read_snapshot()
    assert a.usable and not a.health.read_errors and a.health.confidence["storage"] == 1
    assert a.storage.occupied == 1 and a.storage.get(0, 0).personality == 101
    if failure == "corrupt":
        raw[36] ^= 1
        port.poke(address, raw)
        broken = port
    else:
        end = address + STORAGE_SIZE
        ewram = port.read_range(0x02000000, 0x40000)
        broken = SnapshotMemoryReader({
            0x02000000: ewram[:end - 0x02000000 - 1],
            end: ewram[end - 0x02000000:],
            0x03000000: port.read_range(0x03000000, 0x8000),
        })
    suppressed = FireRedReader(broken, profile, storage_cache=cache).read_snapshot()
    assert suppressed.usable and suppressed.player == a.player and suppressed.party == a.party
    assert suppressed.storage is None and suppressed.health.confidence["storage"] == 0
    assert not suppressed.health.read_errors
    raw[4:84] = encode_mon(party=False, species=29, personality=202)
    port.poke(address, raw)
    b = FireRedReader(port, profile, storage_cache=cache).read_snapshot()
    assert b.usable and not b.health.read_errors and b.health.confidence["storage"] == 1
    assert (b.storage.get(0, 0).species, b.storage.get(0, 0).personality) == (29, 202)
    assert a.storage.get(0, 0).species == 17  # Earlier immutable snapshot stays intact.


def test_options_mode_refreshes_with_identical_storage_bytes(ram):
    profile, port, sb1, sb2 = ram
    # Keep SB2 outside storage so changing options changes only the intended field.
    relocated = 0x02030000
    port.poke(relocated, port.read_range(sb2, profile.struct_size("SaveBlock2")))
    port.poke_u32(profile.symbol("gSaveBlock2Ptr").address, relocated)
    address = sb1 + STORAGE_FROM_SAVE_BLOCK1
    before = port.read_range(address, STORAGE_SIZE)
    cache = StorageDecodeCache()
    snapshots = []
    for mode in (0, 2, 1):
        port.poke(relocated + profile.field("SaveBlock2", "optionsButtonMode").offset,
                  bytes([mode]))
        snapshot = FireRedReader(port, profile, storage_cache=cache).read_snapshot()
        assert snapshot.usable and snapshot.storage.options_button_mode == mode
        assert port.read_range(address, STORAGE_SIZE) == before
        snapshots.append(snapshot)
    assert [s.storage.options_button_mode for s in snapshots] == [0, 2, 1]


@pytest.mark.parametrize("item,quantity", [(375, 1), (1, 1000), (0, 1), (1, 0)])
def test_pc_items_use_xor_zero_and_malformed_slot_invalidates_core(ram, item, quantity):
    profile, port, sb1, sb2 = ram
    relocated, key = 0x02030000, 0xA1B2C3D4
    port.poke(relocated, port.read_range(sb2, profile.struct_size("SaveBlock2")))
    port.poke_u32(profile.symbol("gSaveBlock2Ptr").address, relocated)
    port.poke_u32(relocated + profile.field("SaveBlock2", "encryptionKey").offset, key)
    port.poke_u32(sb1 + profile.field("SaveBlock1", "money").offset, 321 ^ key)
    port.poke(sb1 + profile.field("SaveBlock1", "coins").offset,
              struct.pack("<H", key & 0xFFFF))
    for _, field, capacity in C.BAG_POCKETS:
        port.poke(sb1 + profile.field("SaveBlock1", field).offset,
                  struct.pack("<HH", 0, key & 0xFFFF) * capacity)
    pc = sb1 + profile.field("SaveBlock1", "pcItems").offset
    port.poke(pc, struct.pack("<HH", 23, 37))
    cache = StorageDecodeCache()
    good = FireRedReader(port, profile, storage_cache=cache).read_snapshot()
    assert good.usable, good.health.read_errors
    assert [(i.item_id, i.quantity) for i in good.storage.pc_items] == [(23, 37)]
    port.poke(pc, struct.pack("<HH", item, quantity))
    bad = FireRedReader(port, profile, storage_cache=cache).read_snapshot()
    assert not bad.usable and not bad.health.valid and bad.health.confidence["bag"] == 0
    assert [(e.subsystem, e.code) for e in bad.health.read_errors] == [
        ("bag", "pc_item_slot_invalid")
    ]
    assert bad.player is bad.flags is bad.vars is bad.pokedex is bad.storage is None
    assert bad.registered_item is None and bad.party == bad.bag == ()


@pytest.mark.parametrize("mode", [1, 2])  # Deposit and move; no UI actions are exercised.
def test_storage_party_tail_and_deposit_gap(ram, encode_mon, mode):
    profile, port, _, _ = ram
    party = profile.symbol("gPlayerParty").address
    port.poke(profile.symbol("gPlayerPartyCount").address, b"\x02")
    port.poke(party, encode_mon(species=17) + encode_mon(species=29)
              + bytes(100) + encode_mon(species=43))
    cache = StorageDecodeCache()
    ordinary = FireRedReader(port, profile, storage_cache=cache).read_snapshot()
    assert ordinary.usable and [(m.slot, m.species) for m in ordinary.party] == [(0, 17), (1, 29)]
    storage_ui = 0x02030000
    port.poke_u32(profile.symbol("gStorage").address, storage_ui)
    port.poke(storage_ui + G_STORAGE_BOX_OPTION_OFFSET, bytes([mode]))
    active = FireRedReader(port, profile, storage_cache=cache).read_snapshot()
    assert active.usable, active.health.read_errors
    assert [(m.slot, m.species) for m in active.party] == [(0, 17), (1, 29), (3, 43)]
    if mode == 1:
        port.poke(party + 100, bytes(100))  # Deposit leaves an empty slot inside count.
        gap = FireRedReader(port, profile, storage_cache=cache).read_snapshot()
        assert gap.usable and [(m.slot, m.species) for m in gap.party] == [(0, 17), (3, 43)]
    corrupt = bytearray(encode_mon(species=43))
    corrupt[32] ^= 1
    port.poke(party + 300, corrupt)
    bad = FireRedReader(port, profile, storage_cache=cache).read_snapshot()
    assert not bad.usable and bad.party == () and bad.player is None
    assert [(e.subsystem, e.code) for e in bad.health.read_errors] == [
        ("party", "mon_bad_checksum")
    ]
    port.poke_u32(profile.symbol("gStorage").address, 0)
    port.poke(party + 100, encode_mon(species=29))
    outside = FireRedReader(port, profile, storage_cache=cache).read_snapshot()
    assert outside.usable and [(m.slot, m.species) for m in outside.party] == [(0, 17), (1, 29)]
