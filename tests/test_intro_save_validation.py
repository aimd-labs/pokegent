"""Invented RAM/profile only: validate the same save-load gate used by checkpoints."""

import struct
from types import SimpleNamespace

import pytest

from pokegent.emulator import GBA_EWRAM, GBA_IWRAM, AtomicSnapshot
from pokegent.games.gen3.firered.reader import ui_symbols as U
from pokegent.games.gen3.firered.reader.live import LiveReader
from pokegent.games.gen3.firered.rom import constants as C
from pokegent.games.gen3.firered.rom.rom_data import RomProfile


def synthetic_save(callback, *, active=False, mismatch=False, wrong_index=False,
                   index=0, bad_header=False, bad_party=False):
    def layout(fields):
        offset = 0
        entries = {}
        for name, size in fields:
            entries[name] = dict(offset=offset, size=size, source="synthetic")
            offset += size
        return dict(size=offset, fields=entries, source="synthetic")

    sb1 = layout([
        ("pos", 4), ("location", 2), ("mapLayoutId", 2), ("money", 4), ("coins", 2),
        ("playerPartyCount", 1), ("playerParty", 600), ("registeredItem", 2),
        ("dynamicWarp", 8), ("pcItems", 4),
        *((name, capacity * 4) for _, name, capacity in C.BAG_POCKETS),
        ("flags", C.NUM_FLAG_BYTES), ("vars", C.VARS_COUNT * 2),
    ])
    sb2 = layout([
        ("encryptionKey", 4), ("playerName", 8), ("playerGender", 1),
        ("playerTrainerId", 4), ("optionsButtonMode", 1),
        ("pokedexOwned", 52), ("pokedexSeen", 52),
    ])
    addresses = dict(gSaveBlock1Ptr=0x03000100, gSaveBlock2Ptr=0x03000104,
                     gPlayerPartyCount=0x02001000, gPlayerParty=0x02001004,
                     gObjectEvents=0x02002000, gPlayerAvatar=0x02002300, gMain=0x03000200)
    addresses.update({name: 0x08000100 + i * 4
                      for i, name in enumerate(U.PRE_FIELD_AVATAR_CALLBACKS)})
    profile = RomProfile.model_validate(dict(
        profile_id="synthetic", display_name="Synthetic", supported=True,
        rom=dict(sha1="0" * 40, game_code="TEST", version=0),
        structs=dict(SaveBlock1=sb1, SaveBlock2=sb2),
        symbols={name: dict(address=addr, source="synthetic")
                 for name, addr in addresses.items()},
    ))
    ewram, iwram, rom = bytearray(0x40000), bytearray(0x8000), bytearray(0x1000)
    struct.pack_into("<II", iwram, 0x100, 0x02004000, 0x02006000)
    cb = addresses.get(callback, 0x08000900) | 1
    struct.pack_into("<I", iwram, 0x200 + U.MAIN_CALLBACK2, cb)
    ewram[0x2300 + C.AVATAR_OBJECT_EVENT_ID_OFFSET] = index
    if active:
        obj = 0x2000 + (C.OBJECT_EVENT_SIZE if wrong_index else 0)
        ewram[obj] = C.OBJ_EVENT_ACTIVE_BIT
        ewram[obj + 2] = C.OBJ_EVENT_IS_PLAYER_BIT
        struct.pack_into("<hh", ewram, obj + 0x10, 8 if mismatch else 7, 7)
    if bad_party:
        ewram[0x1000] = 7
    rom[0xAC:0xB0] = b"FAIL" if bad_header else b"TEST"
    atomic = AtomicSnapshot(seq=0, frame=1, core_frame=1, load_epoch=1,
                            keys_held=frozenset(), taken_at=0,
                            regions={GBA_EWRAM.name: bytes(ewram), GBA_IWRAM.name: bytes(iwram)},
                            region_defs=(GBA_EWRAM, GBA_IWRAM), rom=bytes(rom))
    return LiveReader(SimpleNamespace(read_atomic_snapshot=lambda: atomic), profile)


@pytest.mark.parametrize("callback", U.PRE_FIELD_AVATAR_CALLBACKS)
def test_intro_checkpoint_roundtrip_without_avatar(callback):
    reader = synthetic_save(callback)
    assert reader.read().snapshot.usable
    assert reader.after_state_load("synthetic checkpoint").ok


def test_initialized_avatar_coords_are_still_checked_in_intro():
    assert synthetic_save(U.NEW_GAME_SCENE_CALLBACK, active=True).after_state_load().ok
    check = synthetic_save(U.NEW_GAME_SCENE_CALLBACK, active=True, mismatch=True).after_state_load()
    assert not check.ok
    assert "object event coords" in check.conformance["failures"][0]


@pytest.mark.parametrize("kwargs", [
    {}, {"active": True, "wrong_index": True}, {"index": C.OBJECT_EVENTS_COUNT},
    {"bad_header": True}, {"bad_party": True},
])
def test_corruption_remains_refused(kwargs):
    callback = "unknown_callback" if not kwargs else U.NEW_GAME_SCENE_CALLBACK
    assert not synthetic_save(callback, **kwargs).after_state_load().ok
