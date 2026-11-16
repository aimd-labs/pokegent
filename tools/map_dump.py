#!/usr/bin/env python3
"""Dump a FireRed map as text or JSON (M2.2).

Sources (exactly one):

* ``--map GROUP NUMBER`` or ``--name PalletTown``: any map straight from the ROM (static: no player,
  no live objects, no connection fill).
* ``--ram-image X.state``: the active map of a raw mGBA savestate (RAM image only, offline) plus the
  ROM, like the reader sees it.
* ``--state X.state [--advance N]``: load the savestate into the real emulator through the
  single-writer actor and read an atomic snapshot (wrap in ``flock``/``nice`` on a shared host).

The ROM is ``$FIRERED_ROM_PATH`` (read-only). Output: a character map (``--ascii``, default), a
summary (``--summary``) or the full ``MapState`` as JSON (``--json``).

Character map legend: ``@`` player, ``N`` npc/trainer object, ``i`` item ball, ``T`` cut tree,
``R`` rock smash, ``B`` boulder, ``W`` warp, ``S`` sign, ``h`` hidden item, ``c`` coord event,
``.`` walkable, ``#`` blocked, ``~`` water, ``"`` tall grass, ``v^<>`` ledge (jump direction),
``=`` ice/slide/conveyor/current, ``:`` the border and the connected-map ring outside the map
(lower case when the cell shows a connected map).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pokegent.games.gen3.firered.maps.map_data import MapReader  # noqa: E402
from pokegent.games.gen3.firered.maps.map_names import find_map  # noqa: E402
from pokegent.games.gen3.firered.maps.map_schema import MapState  # noqa: E402
from pokegent.games.gen3.firered.reader.memory_port import SnapshotMemoryReader  # noqa: E402
from pokegent.games.gen3.firered.rom.rom_data import load_profiles  # noqa: E402

LEDGE_CHAR = {"S": "v", "N": "^", "W": "<", "E": ">"}
OBJECT_CHAR = {
    "item_ball": "i",
    "cut_tree": "T",
    "rock_smash": "R",
    "boulder": "B",
    "berry_tree": "b",
    "fossil": "f",
    "snorlax": "Z",
}


def render_ascii(state: MapState, *, ring: bool = False) -> str:
    """Character map of the state; ``ring`` adds the 7-tile ring around the map."""
    width, height = state.layout.width, state.layout.height
    pad = 7 if ring else 0
    warps = {(w.x, w.y) for w in state.warps}
    signs = {(b.x, b.y) for b in state.bg_events if b.kind == "sign"}
    hidden = {(b.x, b.y) for b in state.bg_events if b.kind == "hidden_item"}
    coords = {(c.x, c.y) for c in state.coord_events if not c.weather_only}
    objects: dict[tuple[int, int], str] = {}
    for obj in state.objects:
        if obj.live is not None or obj.blocks_tile:
            objects[(obj.x, obj.y)] = OBJECT_CHAR.get(obj.role, "N")
    player = (state.player.x, state.player.y) if state.player else None
    lines = []
    for y in range(-pad, height + pad):
        row = []
        for x in range(-pad, width + pad):
            tile = state.tile(x, y)
            if (x, y) == player:
                ch = "@"
            elif (x, y) in objects:
                ch = objects[(x, y)]
            elif (x, y) in warps:
                ch = "W"
            elif (x, y) in signs:
                ch = "S"
            elif (x, y) in hidden:
                ch = "h"
            elif (x, y) in coords:
                ch = "c"
            elif tile.region != "map":
                ch = "." if (tile.region == "connection" and tile.walkable) else ":"
            elif tile.ledge:
                ch = LEDGE_CHAR[tile.ledge]
            elif tile.kind in ("water", "waterfall"):
                ch = "~"
            elif tile.tall_grass:
                ch = '"'
            elif (
                tile.ice or tile.forced_move or tile.kind in ("ice", "slide", "conveyor", "current")
            ):
                ch = "="
            elif tile.walkable:
                ch = "."
            else:
                ch = "#"
            row.append(ch)
        lines.append("".join(row))
    return "\n".join(lines)


def summary(state: MapState) -> str:
    ident, lay = state.identity, state.layout
    out = [
        f"{ident.name} ({ident.group}.{ident.number}, {ident.group_name}) "
        f"{lay.width}x{lay.height} layout {ident.layout_id} type {ident.map_type_name} "
        f"verified={ident.identity_verified}",
    ]
    if state.player:
        p = state.player
        out.append(
            f"player ({p.x},{p.y}) facing {p.facing} elevation {p.elevation} "
            f"consistent={p.position_consistent} (save {p.save_x},{p.save_y} "
            f"avatar {p.avatar_x},{p.avatar_y})"
        )
    for w in state.warps:
        out.append(
            f"warp {w.index}: ({w.x},{w.y}) -> {w.dest_name} warp {w.dest_warp_id} "
            f"[{w.tile_kind}/{w.trigger}]"
        )
    for c in state.connections:
        out.append(f"connection {c.direction} offset {c.offset} -> {c.dest_name}")
    for o in state.objects:
        out.append(
            f"object {o.local_id}: {o.role} gfx {o.graphics_id} at ({o.x},{o.y}) "
            f"{o.movement_type_name or o.movement_type} range ({o.range_x},{o.range_y}) "
            f"trainer {o.trainer_type}/{o.trainer_range} flag {o.hidden_flag_id}"
            f"{' LIVE' if o.live else ''}"
        )
    for b in state.bg_events:
        extra = (
            f" item {b.hidden_item.item_id} flag {b.hidden_item.flag_id}" if b.hidden_item else ""
        )
        out.append(f"bg {b.index}: {b.kind} at ({b.x},{b.y}){extra}")
    for c in state.coord_events:
        out.append(
            f"coord {c.index}: ({c.x},{c.y}) var 0x{c.var_id:04X}=={c.var_value} active={c.active}"
        )
    return "\n".join(out)


def _rom_port(rom: bytes, ram: tuple[bytes, bytes] | None = None) -> SnapshotMemoryReader:
    iwram, ewram = ram if ram else (bytes(0x8000), bytes(0x4_0000))
    return SnapshotMemoryReader({0x0800_0000: rom, 0x0300_0000: iwram, 0x0200_0000: ewram})


def load_state(args: argparse.Namespace, rom: bytes, profile) -> MapState:
    if args.map or args.name:
        target = tuple(args.map) if args.map else find_map(args.name)
        if target is None:
            raise SystemExit(f"unknown map name {args.name!r}")
        result = MapReader(_rom_port(rom), profile).read_static(*target)
    elif args.ram_image:
        from pokegent.games.gen3.firered.rom.conformance import load_mgba_state_ram

        ram = load_mgba_state_ram(args.ram_image)
        port = _rom_port(
            rom, (ram.read_range(0x0300_0000, 0x8000), ram.read_range(0x0200_0000, 0x4_0000))
        )
        result = MapReader(port, profile).read()
    else:
        from pokegent.emulator import EmulatorActor, PyGBAEmulator
        from pokegent.games.gen3.firered.maps.map_live import LiveMapReader

        emulator = PyGBAEmulator()
        emulator.load(os.environ["FIRERED_ROM_PATH"])
        with EmulatorActor(emulator) as actor:
            actor.load_state(str(args.state))
            if args.advance:
                actor.advance_frames(args.advance)
            result = LiveMapReader(actor, profile).read().map_result
    if result.state is None:
        raise SystemExit(
            "map read failed: " + "; ".join(f"{e.code}: {e.message}" for e in result.errors)
        )
    return result.state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--map", nargs=2, type=int, metavar=("GROUP", "NUMBER"))
    source.add_argument("--name", help="pret map name, e.g. PalletTown")
    source.add_argument("--ram-image", type=Path, help="raw mGBA savestate (offline RAM image)")
    source.add_argument("--state", type=Path, help="savestate to load into the real emulator")
    parser.add_argument("--advance", type=int, default=0, help="frames to run after --state")
    parser.add_argument("--rom", default=os.environ.get("FIRERED_ROM_PATH"))
    parser.add_argument("--json", action="store_true", help="the full MapState as JSON")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--ring", action="store_true", help="include the 7-tile ring in --ascii")
    args = parser.parse_args()
    if not args.rom:
        print("set FIRERED_ROM_PATH (or --rom)", file=sys.stderr)
        return 2
    rom = Path(args.rom).read_bytes()
    profile = load_profiles().get("firered-us-rev0")
    state = load_state(args, rom, profile)
    if args.json:
        print(state.model_dump_json(indent=1))
        return 0
    if args.summary:
        print(summary(state))
        return 0
    print(summary(state).splitlines()[0])
    print(render_ascii(state, ring=args.ring))
    return 0


if __name__ == "__main__":
    sys.exit(main())
