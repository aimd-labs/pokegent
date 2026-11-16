"""Invented RAM bytes through the actual profile, reader and public telemetry path."""

import struct

import pytest

from pokegent.dashboard.game import state_block
from pokegent.dashboard.names import NameBook
from pokegent.dashboard.public import public_telemetry
from pokegent.dashboard.state import ViewerState
from pokegent.dashboard.telemetry import build_telemetry
from pokegent.games.gen3.firered.reader import ui_symbols as U
from pokegent.games.gen3.firered.reader.memory_port import SnapshotMemoryReader
from pokegent.games.gen3.firered.reader.reader import FireRedReader
from pokegent.games.gen3.firered.rom import constants as C
from pokegent.games.gen3.firered.rom.rom_data import load_profiles


@pytest.fixture
def ram(monkeypatch):
    monkeypatch.setattr("pokegent.dashboard.game.map_names.map_name", lambda *_: None)
    profile = load_profiles().get("firered-us-rev0")
    port = SnapshotMemoryReader({0x02000000: bytes(0x40000), 0x03000000: bytes(0x8000)})
    # Deliberately relocated: never read a fixed save-block address.
    sb1, sb2 = 0x0201007C, 0x0201803C
    port.poke_u32(profile.symbol("gSaveBlock1Ptr").address, sb1)
    port.poke_u32(profile.symbol("gSaveBlock2Ptr").address, sb2)
    callback = profile.symbol(U.PRE_FIELD_AVATAR_CALLBACKS[0]).address | 1
    port.poke_u32(profile.symbol("gMain").address + U.MAIN_CALLBACK2, callback)

    def write(block, field, raw):
        address = sb1 if block == "SaveBlock1" else sb2
        port.poke(address + profile.field(block, field).offset, raw)

    # Gen-3 charset: invented name AéZ, followed by EOS and ignored bytes.
    write("SaveBlock2", "playerName", bytes([0xBB, 0x1B, 0xD4, 0xFF, 0, 0, 0, 0]))
    write("SaveBlock2", "playerTrainerId", struct.pack("<HH", 12345, 54321))
    write("SaveBlock2", "playTimeHours", struct.pack("<H", 321))
    write("SaveBlock2", "playTimeMinutes", b"\x2d")
    write("SaveBlock2", "playTimeSeconds", b"\x38")
    key = 0xA1B2C3D4
    write("SaveBlock2", "encryptionKey", struct.pack("<I", key))
    write("SaveBlock1", "money", struct.pack("<I", 765432 ^ key))
    write("SaveBlock1", "coins", struct.pack("<H", key & 0xFFFF))
    for _, field, capacity in C.BAG_POCKETS:
        write("SaveBlock1", field, struct.pack("<HH", 0, key & 0xFFFF) * capacity)
    owned, seen = bytearray(52), bytearray(52)
    for raw, numbers in ((owned, (1, 9, 387, 416)), (seen, (1, 8, 9, 386, 411, 416))):
        for number in numbers:
            raw[(number - 1) // 8] |= 1 << ((number - 1) % 8)
    write("SaveBlock2", "pokedexOwned", owned)
    write("SaveBlock2", "pokedexSeen", seen)
    return profile, port, write


def project(snapshot):
    viewer = ViewerState(run_id="synthetic", decider="agent", max_steps=1, realtime=False)
    view = viewer.view()
    view["state"] = state_block(snapshot, NameBook())
    return public_telemetry(build_telemetry(view))["state"]


def read(ram):
    profile, port, _ = ram
    return FireRedReader(port, profile).read_snapshot()


def test_ram_trainer_card(ram):
    snapshot = read(ram)
    assert snapshot.usable, snapshot.health.read_errors
    state = project(snapshot)
    assert state["player"] == {
        "name": "AéZ", "trainer_id": 12345, "money": 765432,
        "play_time": {"hours": 321, "minutes": 45, "seconds": 56},
    }
    assert state["pokedex"] == {"seen": 4, "caught": 2}
    assert "sid" not in state["player"]
    assert state["party"] == []
    assert state["badges"] == []


@pytest.mark.parametrize("pointer", [0, 0x02000001, 0x03000000, 0x0203FFFC])
def test_invalid_save_blocks_are_null(ram, pointer):
    profile, port, _ = ram
    port.poke_u32(profile.symbol("gSaveBlock2Ptr").address, pointer)
    snapshot = read(ram)
    assert not snapshot.usable
    state = project(snapshot)
    assert state["player"] == dict.fromkeys(("name", "trainer_id", "play_time", "money"))
    assert state["pokedex"] == {"seen": None, "caught": None}


def test_unreadable_save_block_is_null(ram):
    profile, port, _ = ram
    pointers = port.read_range(0x03000000, 0x8000)
    snapshot = FireRedReader(SnapshotMemoryReader({0x03000000: pointers}), profile).read_snapshot()
    assert not snapshot.usable
    assert all(value is None for value in project(snapshot)["player"].values())


@pytest.mark.parametrize("name", [bytes(8), b"\xff" * 8])
def test_intro_without_name_has_no_trainer_card(ram, name):
    ram[2]("SaveBlock2", "playerName", name)
    snapshot = read(ram)
    assert snapshot.usable, snapshot.health.read_errors
    state = project(snapshot)
    assert all(value is None for value in state["player"].values())
    assert state["pokedex"] == {"seen": None, "caught": None}


@pytest.mark.parametrize("field,raw", [
    ("playTimeHours", struct.pack("<H", 1000)),
    ("playTimeMinutes", b"\x3c"), ("playTimeSeconds", b"\xff"),
])
def test_invalid_clock_only_hides_clock(ram, field, raw):
    ram[2]("SaveBlock2", field, raw)
    snapshot = read(ram)
    assert snapshot.usable, snapshot.health.read_errors
    state = project(snapshot)
    assert state["player"]["play_time"] is None
    assert state["player"]["money"] == 765432


def test_zero_is_readable_and_clock_refreshes(ram):
    profile, port, write = ram
    reader = FireRedReader(port, profile)
    assert project(reader.read_snapshot())["player"]["play_time"]["seconds"] == 56
    write("SaveBlock2", "playTimeSeconds", b"\x00")
    write("SaveBlock2", "playerTrainerId", bytes(4))
    write("SaveBlock1", "money", struct.pack("<I", 0xA1B2C3D4))
    state = project(reader.read_snapshot())
    assert state["player"]["play_time"]["seconds"] == 0
    assert state["player"]["trainer_id"] == 0
    assert state["player"]["money"] == 0


def test_public_projection_does_not_forward_private_fields(ram):
    view = ViewerState(run_id="synthetic", decider="agent", max_steps=1, realtime=False).view()
    view["state"] = state_block(read(ram), NameBook())
    view["state"]["player"]["sid"] = 54321
    view["state"]["player"]["play_time"]["private"] = "hidden"
    view["state"]["pokedex"]["owned"] = [1, 9]
    public = public_telemetry(build_telemetry(view))["state"]
    assert set(public["player"]) == {"name", "trainer_id", "money", "play_time"}
    assert set(public["player"]["play_time"]) == {"hours", "minutes", "seconds"}
    assert set(public["pokedex"]) == {"seen", "caught"}


def test_absent_optional_clock_profile_is_null(ram):
    profile, port, _ = ram
    block = profile.structs["SaveBlock2"]
    fields = {name: value for name, value in block.fields.items() if name != "playTimeSeconds"}
    block = block.model_copy(update={"fields": fields})
    profile = profile.model_copy(update={"structs": {**profile.structs, "SaveBlock2": block}})
    snapshot = FireRedReader(port, profile).read_snapshot()
    assert snapshot.usable, snapshot.health.read_errors
    assert project(snapshot)["player"]["play_time"] is None
