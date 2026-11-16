"""RAM conformance checks for the FireRed reader (spec WP1 DoD: real RAM is the gate).

Runs :class:`FireRedReader` over a set of memory ports (one per saved state or emulator
moment) and reports every violated invariant. Clean-room: it only uses the reader's public
API plus a few cross-checks written from the decompile's struct layouts.

Port sources:

* :func:`load_mgba_state_ram` turns a raw mGBA save state into a read-only
  :class:`SnapshotMemoryReader` (IWRAM + EWRAM only). No emulator, no ROM needed. The offsets
  (IWRAM at 0x19000, EWRAM at 0x21000 of a 0x61000-byte state) were found empirically on
  mGBA 0.10 states of this ROM; the loader refuses any other size or game code.
* A live backend is wired in by WP1c: anything implementing ``MemoryPort`` can be passed to
  :func:`check_port`.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pokegent.games.gen3.firered.reader.memory_port import MemoryPort, SnapshotMemoryReader
from pokegent.games.gen3.firered.reader.reader import FireRedReader
from pokegent.games.gen3.firered.rom import constants as C
from pokegent.games.gen3.firered.rom.rom_data import RomProfile

MGBA_STATE_SIZE = 0x61000
MGBA_STATE_GAME_CODE_OFFSET = 0x1C
MGBA_STATE_IWRAM_OFFSET = 0x19000
MGBA_STATE_EWRAM_OFFSET = 0x21000
IWRAM_SIZE = 0x8000
EWRAM_SIZE = 0x40000


class StateFormatError(ValueError):
    """The file is not a raw mGBA state of the expected shape."""


def load_mgba_state_ram(path: str | Path, *, game_code: bytes = b"BPRE") -> SnapshotMemoryReader:
    """Read a raw mGBA save state (read-only) and expose its IWRAM/EWRAM as a MemoryPort."""
    with open(path, "rb") as handle:  # read-only
        data = handle.read()
    if len(data) != MGBA_STATE_SIZE:
        raise StateFormatError(f"{path}: size {len(data):#x} != expected {MGBA_STATE_SIZE:#x}")
    found = data[MGBA_STATE_GAME_CODE_OFFSET : MGBA_STATE_GAME_CODE_OFFSET + 4]
    if found != game_code:
        raise StateFormatError(f"{path}: game code {found!r} != {game_code!r}")
    iwram = data[MGBA_STATE_IWRAM_OFFSET : MGBA_STATE_IWRAM_OFFSET + IWRAM_SIZE]
    ewram = data[MGBA_STATE_EWRAM_OFFSET : MGBA_STATE_EWRAM_OFFSET + EWRAM_SIZE]
    return SnapshotMemoryReader({0x0300_0000: iwram, 0x0200_0000: ewram})


def check_port(name: str, port: MemoryPort, profile: RomProfile) -> dict[str, Any]:
    """Check one memory image. ``ok`` is True only if every invariant holds."""
    failures: list[str] = []
    live = FireRedReader(port, profile, party_source="live").read_snapshot()
    saved = FireRedReader(port, profile, party_source="saveblock").read_snapshot()
    for label, snap in (("live", live), ("saveblock", saved)):
        if not snap.usable:
            failures.extend(
                f"{label}: {e.subsystem}/{e.code}: {e.message}" for e in snap.health.read_errors
            )
    result: dict[str, Any] = {"name": name, "ok": False, "failures": failures}
    if not live.usable:
        return result

    player = live.player
    assert player is not None
    # Cross-check: SaveBlock1.pos equals the player object event's current coords minus
    # MAP_OFFSET (7) when the avatar block is configured.
    if profile.has_symbol("gObjectEvents") and profile.has_symbol("gPlayerAvatar"):
        events = profile.symbol("gObjectEvents").address
        avatar = profile.symbol("gPlayerAvatar").address
        index = port.read_u8(avatar + C.AVATAR_OBJECT_EVENT_ID_OFFSET)
        if index < C.OBJECT_EVENTS_COUNT:
            obj = events + index * C.OBJECT_EVENT_SIZE
            # The usable reader above already enforces the exact pre-field callback allowlist
            # and absence of ANY active player before permitting an uncreated avatar. Only an
            # initialized active player has coordinates to cross-check here.
            if (
                port.read_u8(obj) & C.OBJ_EVENT_ACTIVE_BIT
                and port.read_u8(obj + 2) & C.OBJ_EVENT_IS_PLAYER_BIT
            ):
                raw = port.read_range(obj + 0x10, 4)
                cx, cy = struct.unpack("<hh", raw)
                if (cx - 7, cy - 7) != (player.x, player.y):
                    failures.append(
                        f"pos ({player.x},{player.y}) != object event coords-7 ({cx - 7},{cy - 7})"
                    )
    result["summary"] = {
        "map": [player.map_group, player.map_num],
        "pos": [player.x, player.y],
        "facing": player.facing,
        "money": player.money,
        "badges": list(player.badges),
        "party": [(m.species, m.level, m.hp, m.max_hp) for m in live.party],
        "saved_party_matches_live": live.party == saved.party,
        "state_hash": live.state_hash,
    }
    result["ok"] = not failures
    return result


def run_conformance(ports: Iterable[tuple[str, MemoryPort]], profile: RomProfile) -> dict[str, Any]:
    results = [check_port(name, port, profile) for name, port in ports]
    failed = [r for r in results if not r["ok"]]
    return {"ok": not failed, "tested": len(results), "failed": len(failed), "results": results}


def run_state_dir(
    states_dir: str | Path, profile: RomProfile, *, pattern: str = "*.state", limit: int = 0
) -> dict[str, Any]:
    paths = sorted(Path(states_dir).glob(pattern))
    if limit > 0:
        paths = paths[:limit]
    if not paths:
        raise FileNotFoundError(f"no save states matched {pattern!r} in {states_dir}")

    def ports() -> Iterable[tuple[str, MemoryPort]]:
        for path in paths:
            yield path.name, load_mgba_state_ram(path)

    return run_conformance(ports(), profile)
