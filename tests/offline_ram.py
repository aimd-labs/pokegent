"""Tiny authored layout and zero-filled RAM containers; no game-derived fixtures."""

import builtins
import os
import struct
import subprocess
import sys
from pathlib import Path

import yaml

from pokegent.games.gen3.firered.reader import ui_symbols as U
from pokegent.games.gen3.firered.rom import constants as C
from pokegent.games.gen3.firered.rom.conformance import (
    MGBA_STATE_EWRAM_OFFSET,
    MGBA_STATE_GAME_CODE_OFFSET,
    MGBA_STATE_IWRAM_OFFSET,
    MGBA_STATE_SIZE,
)

REPO = Path(__file__).resolve().parents[1]
OVERRIDES = ("FIRERED_ROM_PATH", "POKEGENT_LOCAL_DATA_DIR", "POKEGENT_ROM_PROFILES",
             "POKEGENT_SCENES", "POKEGENT_BATTLE_ITEM_CLASSES", "PYTHONPATH", "PYTHONHOME")


def synthetic_inputs(tmp_path, monkeypatch):
    for key in OVERRIDES:
        monkeypatch.delenv(key, raising=False)

    def layout(fields):
        offset, entries = 0, {}
        for name, size in fields:
            entries[name] = dict(offset=offset, size=size, source="authored synthetic")
            offset += size
        return dict(size=offset, fields=entries, source="authored synthetic")

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
    profile = dict(display_name="Synthetic", supported=True, layout="invented",
                   rom=dict(sha1="0" * 40, game_code="TEST", version=0),
                   symbols={name: dict(address=addr, source="authored synthetic")
                            for name, addr in addresses.items()})
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(yaml.safe_dump(dict(
        schema_version=1, layouts={"invented": dict(structs=dict(SaveBlock1=sb1, SaveBlock2=sb2))},
        profiles={"synthetic": profile, "unsupported": {**profile, "supported": False}},
    )))
    monkeypatch.setenv("POKEGENT_ROM_PROFILES", str(profile_path))
    raw = bytearray(MGBA_STATE_SIZE)
    # Container marker required by the existing raw loader; all payload bytes are authored.
    raw[MGBA_STATE_GAME_CODE_OFFSET:MGBA_STATE_GAME_CODE_OFFSET + 4] = b"BPRE"
    struct.pack_into("<II", raw, MGBA_STATE_IWRAM_OFFSET + 0x100, 0x02004000, 0x02006000)
    struct.pack_into("<I", raw, MGBA_STATE_IWRAM_OFFSET + 0x200 + U.MAIN_CALLBACK2,
                     addresses[U.PRE_FIELD_AVATAR_CALLBACKS[0]] | 1)
    states = tmp_path / "states"
    states.mkdir()
    good = states / "a.state"
    good.write_bytes(raw)
    raw[MGBA_STATE_EWRAM_OFFSET + 0x1000] = 7  # reader's invalid live party-count guard
    bad = states / "b.state"
    bad.write_bytes(raw)

    original_import = builtins.__import__

    def offline_import(name, *args, **kwargs):
        forbidden = ("mgba", "pygba", "pyboy", "pokegent.emulator", "pokegent.providers",
                     "pokegent.system1", "pokegent.dashboard", "uvicorn", "fastapi")
        assert not any(name == item or name.startswith(item + ".") for item in forbidden), name
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", offline_import)
    import socket

    def no_network(*args, **kwargs):
        raise AssertionError("offline tool attempted network access")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    return states, good, bad, profile_path


def inventory(directory):
    return {str(path.relative_to(directory)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in directory.rglob("*") if path.is_file()}


def run_tool(name, args, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    before = inventory(tmp_path)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-B", str(REPO / "tools" / name), *map(str, args)],
                            cwd=outside, env=env, capture_output=True, text=True, timeout=30)
    assert inventory(tmp_path) == before
    assert "Traceback" not in result.stderr
    return result
