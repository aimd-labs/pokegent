#!/usr/bin/env python3
"""Derive the Strength-boulder puzzle data of every map from the ROM (WP-A step A3, class G03).

For every map that has a pushable boulder object (graphics id 97) this reads, from the ROM alone:

* the boulder objects (local id, template tile, elevation, hide flag, reveal flag = the
``trainerType``
  field, which the game uses as "flag to clear when this boulder falls through a hole");
* the tiles whose metatile behavior is ``MB_STRENGTH_BUTTON`` (the switches) and
``MB_FALL_WARP`` (the
  holes);
* the script of the coordinate event on each switch (what a boulder landing there runs), decoded
  with a small script reader that knows only the commands these scripts use (anything else stops the
  decode and is reported, never guessed): ``setmetatile`` (the barrier tiles it opens),
  ``copyobjectxytoperm`` (the boulders it pins), ``setvar`` (the puzzle variable it sets);
* the ``ON_LOAD`` map script (``setmetatile`` calls guarded by ``compare var, 100`` close the
barrier
  while the variable is not 100);
* every ``ON_TRANSITION`` script that resets puzzle variables (``Route23`` resets the four
Victory Road
  variables and the 2F/3F boulder hide flags).

Run ``python -m pokegent.games.gen3.firered.maps.boulder_scan --rom <local-rom>``.
Optional ``--write <local-output>`` and ``--check <local-output>`` select an external output file.

The ROM is ``$FIRERED_ROM_PATH`` (read-only). The decoded command ids are the pinned FRLG script
command table (``data/script_cmd_table.inc``); ``historical local validation`` checks the VR
numbers
against the bytes.
"""

from __future__ import annotations

import argparse
import logging
import os
import struct
import sys
from pathlib import Path

import yaml

from pokegent.games.gen3.firered.maps import map_constants as K
from pokegent.games.gen3.firered.maps.map_data import MapReader, RomMapCache
from pokegent.games.gen3.firered.maps.map_names import map_name
from pokegent.games.gen3.firered.reader.memory_port import SnapshotMemoryReader
from pokegent.games.gen3.firered.rom.rom_data import load_profiles

ROM_BASE = 0x0800_0000
SCHEMA = "boulders/1"

# FRLG script command table (pret/pokefirered data/script_cmd_table.inc): opcode -> (name,
# argument bytes)
OPS: dict[int, tuple[str, int]] = {
    0x02: ("end", 0),
    0x03: ("return", 0),
    0x04: ("call", 4),
    0x05: ("goto", 4),
    0x06: ("goto_if", 5),
    0x07: ("call_if", 5),
    0x16: ("setvar", 4),
    0x21: ("compare_var_to_value", 4),
    0x25: ("special", 2),
    0x29: ("setflag", 2),
    0x2A: ("clearflag", 2),
    0x2B: ("checkflag", 2),
    0x68: ("map_script_2", 9),
    0x2F: ("playse", 2),
    0x30: ("waitse", 0),
    0x64: ("copyobjectxytoperm", 2),
    0x4F: ("applymovement", 6),
    0x51: ("waitmovement", 2),
    0x69: ("lockall", 0),
    0x6B: ("releaseall", 0),
    0xA2: ("setmetatile", 8),
}
COND_NAMES = {0: "lt", 1: "eq", 2: "gt", 3: "le", 4: "ge", 5: "ne"}
PUZZLE_DONE = 100
MB_STRENGTH_BUTTON = K.MB["MB_STRENGTH_BUTTON"]
MB_FALL_WARP = K.MB["MB_FALL_WARP"]
# the VAR_MAP_SCENE_* range the Victory Road scripts use (0x4064..0x4067) is read from the
# scripts, not assumed
MAP_SCRIPT_ON_LOAD, MAP_SCRIPT_ON_TRANSITION, MAP_SCRIPT_ON_FRAME_TABLE = 1, 3, 2
_LOGGER = logging.getLogger(__name__)


class Unparsed(Exception):
    pass


def read_ops(rom: bytes, ptr: int, *, limit: int = 200) -> list[dict]:
    """Decode a script linearly until ``end`` / ``return`` / ``goto``; branches are recorded,
    not followed."""
    out: list[dict] = []
    pos = ptr - ROM_BASE
    for _ in range(limit):
        opcode = rom[pos]
        if opcode not in OPS:
            raise Unparsed(f"opcode 0x{opcode:02X} at 0x{pos + ROM_BASE:08X}")
        name, size = OPS[opcode]
        args = rom[pos + 1 : pos + 1 + size]
        entry: dict = {"op": name}
        if name in ("setvar", "compare_var_to_value"):
            entry["var"], entry["value"] = struct.unpack("<HH", args)
        elif name in (
            "setflag",
            "clearflag",
            "checkflag",
            "copyobjectxytoperm",
            "playse",
            "special",
            "waitmovement",
        ):
            entry["arg"] = struct.unpack("<H", args)[0]
        elif name == "map_script_2":
            entry["var"] = struct.unpack("<H", args[:2])[0]
            entry["value"] = args[1]
            entry["target"] = struct.unpack("<I", args[3:7])[0]
        elif name == "applymovement":
            entry["object"], entry["target"] = struct.unpack("<HI", args)
        elif name == "setmetatile":
            entry["x"], entry["y"], entry["metatile"], entry["impassable"] = struct.unpack(
                "<HHHH", args
            )
        elif name in ("goto_if", "call_if"):
            entry["cond"] = COND_NAMES[args[0]]
            entry["target"] = struct.unpack("<I", args[1:])[0]
        elif name in ("call", "goto"):
            entry["target"] = struct.unpack("<I", args)[0]
        out.append(entry)
        pos += 1 + size
        if name in ("end", "return", "goto"):
            return out
    raise Unparsed(f"script at 0x{ptr:08X} longer than {limit} commands")


def script_table(rom: bytes, scripts_ptr: int) -> dict[int, int]:
    """Map script type -> script pointer of a map's script table (type byte + u32 ptr,
    0-terminated)."""
    table: dict[int, int] = {}
    pos = scripts_ptr - ROM_BASE
    while rom[pos] != 0:
        table.setdefault(rom[pos], struct.unpack_from("<I", rom, pos + 1)[0])
        pos += 5
    return table


def called_movement_paths(
    rom: bytes, pointer: int, *, seen: frozenset[int] = frozenset()
) -> tuple[tuple[tuple[int, int], ...], ...]:
    """Collect movement paths through the calls made by one event script."""
    if pointer in seen:
        return ()
    try:
        ops = read_ops(rom, pointer)
    except Unparsed:
        return ()
    paths: list[tuple[tuple[int, int], ...]] = []
    for item in ops:
        if item["op"] == "applymovement":
            pos = item["target"] - ROM_BASE
            path: list[tuple[int, int]] = []
            directions = {0x10: (0, 1), 0x11: (0, -1), 0x12: (-1, 0), 0x13: (1, 0)}
            while rom[pos] != 0xFE:
                if rom[pos] not in directions:
                    path = []
                    break
                path.append(directions[rom[pos]])
                pos += 1
            if path:
                paths.append(tuple(path))
        elif item["op"] == "call":
            paths.extend(called_movement_paths(rom, item["target"], seen=seen | {pointer}))
    return tuple(paths)


def on_frame_roots(rom: bytes, table_pointer: int) -> tuple[tuple[int, int, int], ...]:
    """Return the ROM var/value/script rows in one ON_FRAME script table."""
    pos = table_pointer - ROM_BASE
    found: list[tuple[int, int, int]] = []
    while 0 <= pos <= len(rom) - 8:
        var, value, target = struct.unpack_from("<HHI", rom, pos)
        if var == 0:
            break
        if not ROM_BASE <= target < ROM_BASE + len(rom):
            break
        found.append((var, value, target))
        pos += 8
    return tuple(found)


def on_frame_movement(
    rom: bytes, profile, group: int, number: int
) -> tuple[tuple[int, int, tuple[tuple[int, int], ...]], ...]:
    """Return scene var/value and the ROM movement path for each ON_FRAME entry."""
    table = script_table(rom, header_scripts_ptr(rom, profile, group, number))
    table_pointer = table.get(MAP_SCRIPT_ON_FRAME_TABLE)
    if table_pointer is None:
        return ()
    found: list[tuple[int, int, tuple[tuple[int, int], ...]]] = []
    for var, value, target in on_frame_roots(rom, table_pointer):
        paths = called_movement_paths(rom, target)
        if len(paths) != 1:
            _LOGGER.debug(
                "ON_FRAME movement undecodable: map=%s entry var=0x%04X value=%d script=0x%08X",
                map_name(group, number) or f"{group}:{number}",
                var,
                value,
                target,
            )
        else:
            found.append((var, value, paths[0]))
    return tuple(found)


def header_scripts_ptr(rom: bytes, profile, group: int, number: int) -> int:
    base = profile.symbol("gMapGroups").address - ROM_BASE
    group_ptr = struct.unpack_from("<I", rom, base + 4 * group)[0]
    header = struct.unpack_from("<I", rom, group_ptr - ROM_BASE + 4 * number)[0]
    return struct.unpack_from("<I", rom, header - ROM_BASE + 8)[0]


def tile_fact(state, o: dict) -> dict:
    """A setmetatile call plus the behavior / elevation the tile gets: the layout of these
    maps shows the
    OPEN barrier (ON_LOAD closes it), so the layout's own tile tells the open metatile's
    behavior; the
    elevation bits of the grid block are kept by the script (measured live: closed block 0x4F07)."""
    same = state.metatile_id(o["x"], o["y"]) == o["metatile"]
    return {
        "x": o["x"],
        "y": o["y"],
        "metatile": o["metatile"],
        "impassable": o["impassable"],
        "behavior": state.behavior(o["x"], o["y"]) if same else None,
        "elevation": state.elevation(o["x"], o["y"]) if same else None,
    }


def effects_of(ops: list[dict], state) -> dict:
    """The facts of a switch script: barrier tiles opened, boulders pinned, the variable set."""
    opens = [tile_fact(state, o) for o in ops if o["op"] == "setmetatile"]
    pins = [o["arg"] for o in ops if o["op"] == "copyobjectxytoperm"]
    sets = [(o["var"], o["value"]) for o in ops if o["op"] == "setvar"]
    guard = next((o for o in ops if o["op"] == "compare_var_to_value"), None)
    return {
        "opens": opens,
        "pins": pins,
        "set_var": {"var": sets[0][0], "value": sets[0][1]} if sets else None,
        "guard_var": guard["var"] if guard else None,
    }


def scan(rom: bytes) -> dict:
    profile = load_profiles().get("firered-us-rev0")
    port = SnapshotMemoryReader(
        {ROM_BASE: rom, 0x0300_0000: bytes(0x8000), 0x0200_0000: bytes(0x4_0000)}
    )
    reader = MapReader(port, profile, cache=RomMapCache())
    maps: dict[str, dict] = {}
    resets: dict[str, dict] = {}
    puzzle_vars: set[int] = set()
    n_groups = 43
    all_maps = [(g, n) for g in range(n_groups) for n in range(256) if map_name(g, n)]
    for group, number in all_maps:
        result = reader.read_static(group, number)
        if result.state is None:
            continue
        state = result.state
        boulders = [o for o in state.objects if o.role == "boulder"]
        if not boulders:
            continue
        name = state.identity.name
        entry: dict = {"group": group, "number": number}
        entry["boulders"] = [
            {
                "local_id": o.local_id,
                "tile": [o.x, o.y],
                "elevation": o.elevation,
                "hide_flag": o.hidden_flag_id or None,
                "reveal_flag": o.trainer_type or None,
            }
            for o in boulders
        ]
        entry["switches"], entry["holes"] = [], []
        unparsed: list[str] = []
        for y in range(state.layout.height):
            for x in range(state.layout.width):
                behavior = state.behavior(x, y)
                if behavior == MB_FALL_WARP:
                    warp = next((w for w in state.warps if (w.x, w.y) == (x, y)), None)
                    entry["holes"].append(
                        {
                            "tile": [x, y],
                            "warp": warp.index if warp else None,
                            "to": [warp.dest_group, warp.dest_number, warp.dest_warp_id]
                            if warp
                            else None,
                        }
                    )
        for coord in state.coord_events:
            if coord.weather_only or state.behavior(coord.x, coord.y) != MB_STRENGTH_BUTTON:
                continue
            switch = {
                "tile": [coord.x, coord.y],
                "trigger_var": coord.var_id,
                "trigger_value": coord.var_value,
            }
            try:
                fx = effects_of(read_ops(rom, coord.script_ptr), state)
            except Unparsed as exc:
                switch["unparsed"] = str(exc)
                unparsed.append(str(exc))
                fx = {"opens": [], "pins": [], "set_var": None, "guard_var": None}
            switch.update(fx)
            if fx["set_var"]:
                puzzle_vars.add(fx["set_var"]["var"])
            entry["switches"].append(switch)
        # ON_LOAD: setmetatile calls behind `compare var, 100; call_if ne` close the barrier tiles
        table = script_table(rom, header_scripts_ptr(rom, profile, group, number))
        closed: list[dict] = []
        if MAP_SCRIPT_ON_LOAD in table:
            try:
                head = read_ops(rom, table[MAP_SCRIPT_ON_LOAD])
                guard = None
                for op in head:
                    if op["op"] == "compare_var_to_value":
                        guard = op
                    elif (
                        op["op"] == "call_if"
                        and op["cond"] == "ne"
                        and guard
                        and guard["value"] == PUZZLE_DONE
                    ):
                        closed.append(
                            {
                                "var": guard["var"],
                                "tiles": [
                                    {k: t[k] for k in ("x", "y", "metatile", "impassable")}
                                    for t in (
                                        tile_fact(state, s)
                                        for s in read_ops(rom, op["target"])
                                        if s["op"] == "setmetatile"
                                    )
                                ],
                            }
                        )
            except Unparsed as exc:
                unparsed.append("on_load: " + str(exc))
        entry["closed_until_done"] = closed
        if unparsed:
            entry["unparsed"] = unparsed
        if entry["switches"] or entry["holes"] or len(boulders) > 0:
            maps[name] = entry
    # ON_TRANSITION scripts that reset puzzle variables
    for group, number in all_maps:
        table = script_table(rom, header_scripts_ptr(rom, profile, group, number))
        if MAP_SCRIPT_ON_TRANSITION not in table:
            continue
        try:
            ops = read_ops(rom, table[MAP_SCRIPT_ON_TRANSITION])
        except Unparsed:
            continue
        hits = [o for o in ops if o["op"] == "setvar" and o["var"] in puzzle_vars]
        if hits:
            resets[map_name(group, number)] = {
                "vars": [{"var": o["var"], "value": o["value"]} for o in hits],
                "set_flags": [o["arg"] for o in ops if o["op"] == "setflag"],
                "clear_flags": [o["arg"] for o in ops if o["op"] == "clearflag"],
            }
    return {"maps": maps, "resets_on_transition": resets}


HEADER = """\
# Strength boulder puzzles derived from the user-supplied ROM; do not edit by hand.
# The scanner's --check mode compares the file with a fresh scan of the local ROM.
#
# Rules (src/field_player_avatar.c TryPushBoulder, verified live:
# the runtime contracts):
#   * a push needs FLAG_SYS_USE_STRENGTH (0x805, cleared by every map load) and a boulder = object
#     graphics id 97; the boulder moves one tile when the far tile is MB_FALL_WARP or has no
#     collision / directional wall / elevation mismatch / object; the player stays on its tile.
#   * a boulder that stops on an MB_STRENGTH_BUTTON tile runs that tile's coordinate-event script
#     (the `switches` entries below: `opens` = the setmetatile calls, `pins` = copyobjectxytoperm).
#   * a boulder on an MB_FALL_WARP tile falls: its hide flag is set and the flag in its trainerType
#     field (`reveal_flag`) is cleared, which makes the boulder of the floor below appear.
#   * ON_LOAD closes the barrier tiles (`closed_until_done`) while the puzzle variable is not 100.
#   * the ON_TRANSITION script of the entry map resets the puzzle variables
#     (`resets_on_transition`).
"""


def render(data: dict) -> str:
    body = {
        "schema": SCHEMA,
        "source": "User-supplied ROM, decoded by pokegent.games.gen3.firered.maps.boulder_scan",  # noqa: E501
        "verified_by": [],
        **data,
    }
    return HEADER + yaml.safe_dump(body, sort_keys=False, default_flow_style=None, width=110)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--rom", default=os.environ.get("FIRERED_ROM_PATH"))
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--write", type=Path)
    group.add_argument("--check", type=Path)
    args = parser.parse_args()
    if not args.rom:
        print("FIRERED_ROM_PATH or --rom is required", file=sys.stderr)
        return 2
    text = render(scan(Path(args.rom).read_bytes()))
    if args.write:
        args.write.write_text(text)
        return 0
    if args.check:
        if args.check.read_text() != text:
            print(f"{args.check} differs from a fresh scan", file=sys.stderr)
            return 1
        return 0
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
