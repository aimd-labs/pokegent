#!/usr/bin/env python3
"""Generate ``references/regions.json`` from the BPRE Rev0 ROM.

This is a ROM reader, not a copy of the pre-fork generator.  It uses the clean-base MapReader's
layout, attributes, directional collision and event tables, then writes deterministic region data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import deque
from pathlib import Path

from pokegent.games.gen3.firered.maps import map_names
from pokegent.games.gen3.firered.maps.map_behaviors import blocks_side
from pokegent.games.gen3.firered.maps.map_boulders import PuzzleData, on_load_closed, puzzles
from pokegent.games.gen3.firered.maps.map_constants import OPPOSITE, VECTOR
from pokegent.games.gen3.firered.maps.map_data import MapReader, RomMapCache
from pokegent.games.gen3.firered.maps.map_schema import MapState
from pokegent.games.gen3.firered.reader.memory_port import SnapshotMemoryReader
from pokegent.games.gen3.firered.rom.rom_data import load_profiles

ROOT = Path(__file__).resolve().parents[5]
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
ROM_PROFILE = "firered-us-rev0"


def _gate_id(name: str, kind: str, key: tuple[int, int]) -> str:
    return f"boulder_plan:{name}:{kind}:{key[0]}:{key[1]}"


def _port(rom: bytes) -> SnapshotMemoryReader:
    return SnapshotMemoryReader(
        {0x08000000: rom, 0x03000000: bytes(0x8000), 0x02000000: bytes(0x40000)}
    )


def _medium(state: MapState, x: int, y: int) -> str | None:
    tile = state.tile(x, y)
    if tile.undefined or tile.collision != 0:
        return None
    if tile.kind == "waterfall":
        return None
    if tile.surfable:
        return "water"
    return "land" if tile.walkable else None


def _neighbour(state: MapState, x: int, y: int, direction: str) -> tuple[int, int]:
    dx, dy = VECTOR[direction]
    return x + dx, y + dy


def _can_join(
    state: MapState, x: int, y: int, nx: int, ny: int, direction: str, medium: str
) -> bool:
    if _medium(state, x, y) != medium or _medium(state, nx, ny) != medium:
        return False
    if blocks_side(state.behavior(x, y), direction) or blocks_side(
        state.behavior(nx, ny), OPPOSITE[direction]
    ):
        return False
    a, b = state.elevation(x, y), state.elevation(nx, ny)
    return a == 0 or b == 0 or a == 15 or b == 15 or a == b


def _regions(state: MapState) -> tuple[list[str], dict[tuple[int, int], int], list[dict]]:
    width, height = state.layout.width, state.layout.height
    labels: dict[tuple[int, int], int] = {}
    details: list[dict] = []
    for y in range(height):
        for x in range(width):
            if (x, y) in labels:
                continue
            medium = _medium(state, x, y)
            if medium is None:
                continue
            region = len(details)
            labels[(x, y)] = region
            queue = deque([(x, y)])
            while queue:
                cx, cy = queue.popleft()
                for direction in ("N", "S", "W", "E"):
                    nx, ny = _neighbour(state, cx, cy, direction)
                    if not (0 <= nx < width and 0 <= ny < height) or (nx, ny) in labels:
                        continue
                    if _can_join(state, cx, cy, nx, ny, direction, medium):
                        labels[(nx, ny)] = region
                        queue.append((nx, ny))
            details.append({"id": region, "medium": medium, "point": [x, y]})
    rows = [
        "".join("." if (x, y) not in labels else ALPHABET[labels[(x, y)]] for x in range(width))
        for y in range(height)
    ]
    return rows, labels, details


def _near(labels: dict[tuple[int, int], int], x: int, y: int) -> int | None:
    if (x, y) in labels:
        return labels[(x, y)]
    for radius in range(1, 4):
        for point in ((x - radius, y), (x + radius, y), (x, y - radius), (x, y + radius)):
            if point in labels:
                return labels[point]
    return None


def _connections(
    state: MapState, labels: dict[tuple[int, int], int], all_maps: dict[str, dict]
) -> list[dict]:
    out = []
    src_info = all_maps[f"{state.identity.group}:{state.identity.number}"]
    w, h = state.layout.width, state.layout.height
    for conn in state.connections:
        dest_key = f"{conn.dest_group}:{conn.dest_number}"
        dest = all_maps.get(dest_key)
        if dest is None:
            continue
        dw, dh = dest["size"]
        pairs: set[tuple[int, int]] = set()
        if conn.direction in ("N", "S"):
            sy, dy = (0, dh - 1) if conn.direction == "N" else (h - 1, 0)
            for x in range(w):
                dx = x - conn.offset
                if 0 <= dx < dw and (x, sy) in labels:
                    dr = dest["_labels"].get(f"{dx},{dy}")
                    if dr is not None:
                        src_medium = next(
                            r["medium"] for r in src_info["regions"] if r["id"] == labels[(x, sy)]
                        )
                        dest_medium = next(r["medium"] for r in dest["regions"] if r["id"] == dr)
                        pairs.add(
                            (
                                labels[(x, sy)],
                                dr,
                                ("surf",)
                                if src_medium == "land" and dest_medium == "water"
                                else (),
                            )
                        )
        elif conn.direction in ("W", "E"):
            sx, dx = (0, dw - 1) if conn.direction == "W" else (w - 1, 0)
            for y in range(h):
                dy = y - conn.offset
                if 0 <= dy < dh and (sx, y) in labels:
                    dr = dest["_labels"].get(f"{dx},{dy}")
                    if dr is not None:
                        src_medium = next(
                            r["medium"] for r in src_info["regions"] if r["id"] == labels[(sx, y)]
                        )
                        dest_medium = next(r["medium"] for r in dest["regions"] if r["id"] == dr)
                        pairs.add(
                            (
                                labels[(sx, y)],
                                dr,
                                ("surf",)
                                if src_medium == "land" and dest_medium == "water"
                                else (),
                            )
                        )
        if pairs:
            out.append(
                {
                    "direction": conn.direction,
                    "dest": dest_key,
                    "pairs": [
                        {"from": a, "to": b, "needs": list(needs)} for a, b, needs in sorted(pairs)
                    ],
                }
            )
    return out


def _transitions(state: MapState, labels: dict[tuple[int, int], int]) -> list[dict]:
    out: set[tuple[int, int, str, tuple[str, ...]]] = set()
    w, h = state.layout.width, state.layout.height
    for y in range(h):
        for x in range(w):
            src = labels.get((x, y))
            if src is None:
                continue
            for direction in ("N", "S", "W", "E"):
                nx, ny = _neighbour(state, x, y, direction)
                dst = labels.get((nx, ny))
                if dst is None or dst == src:
                    continue
                a, b = _medium(state, x, y), _medium(state, nx, ny)
                if {a, b} == {"land", "water"}:
                    needs = ("surf",) if a == "land" else ()
                    out.add((src, dst, "surf" if needs else "dismount", needs))
    for y in range(h):
        for x in range(w):
            src = labels.get((x, y))
            if src is None:
                continue
            for direction in ("N", "S", "W", "E"):
                over = _neighbour(state, x, y, direction)
                land = _neighbour(state, *over, direction)
                dst = labels.get(land)
                if (
                    dst is not None
                    and state.tile(*over).ledge == direction
                    and _medium(state, *land) == "land"
                ):
                    out.add((src, dst, "ledge", ()))
    return [
        {"from": a, "to": b, "kind": kind, "needs": list(needs), "cost": 4 if needs else 1}
        for a, b, kind, needs in sorted(out)
    ]


def _script_gates(
    state: MapState,
    labels: dict[tuple[int, int], int],
    puzzle: PuzzleData | None,
) -> tuple[list[dict], list[dict]]:
    """Add ROM-known switch crossings to the closed-at-load region graph.

    The clean map reader sees the static map, while Victory Road's ON_LOAD script puts rock
    metatiles over these crossings until a boulder switch fires.  The boulder planner is the
    authority that can satisfy the gate; the graph only records the conditional transition.
    """
    if puzzle is None:
        return [], []
    transitions: set[tuple[int, int, str]] = set()
    gates: list[dict] = []
    for switch in puzzle.switches:
        gate = _gate_id(
            state.identity.name or f"{state.identity.group}:{state.identity.number}",
            "switch",
            switch.tile,
        )
        sides: set[int] = set()
        for opened in switch.opens:
            x, y = opened.x, opened.y
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                region = labels.get((nx, ny))
                if region is not None:
                    sides.add(region)
        for left in sides:
            for right in sides:
                if left != right:
                    transitions.add((left, right, gate))
        gates.append({"id": gate, "kind": "barrier", "tile": list(switch.tile), "var": switch.var})
    for hole in puzzle.holes:
        if hole.warp is None:
            continue
        gate = _gate_id(
            state.identity.name or f"{state.identity.group}:{state.identity.number}",
            "hole",
            hole.tile,
        )
        gates.append({"id": gate, "kind": "hole", "tile": list(hole.tile), "warp": hole.warp})
    out = [
        {"from": left, "to": right, "kind": "barrier", "needs": [gate], "cost": 1}
        for left, right, gate in sorted(transitions)
    ]
    return out, gates


def generate(rom: bytes, *, puzzles_path: str | os.PathLike[str] | None = None) -> dict:
    """Generate regions using an explicit boulder-data input when one is supplied."""
    profile = load_profiles().get(ROM_PROFILE)
    reader = MapReader(_port(rom), profile, cache=RomMapCache())
    puzzle_map = puzzles(puzzles_path)
    states: dict[str, MapState] = {}
    for group, size in enumerate(map_names.group_sizes()):
        for number in range(size):
            result = reader.read_static(group, number)
            if result.state is not None:
                states[f"{group}:{number}"] = result.state
    maps: dict[str, dict] = {}
    for key in sorted(states):
        state = states[key]
        puzzle = puzzle_map.get((state.identity.group, state.identity.number))
        # Build the graph from the map as it is loaded.  Script barriers are not present in the
        # static layout, but their closed tiles are deterministic ROM facts.
        graph_state = on_load_closed(state, puzzle=puzzle)
        rows, labels, details = _regions(graph_state)
        script_trans, gates = _script_gates(state, labels, puzzle)
        maps[key] = {
            "size": [state.layout.width, state.layout.height],
            "grid": rows,
            "regions": details,
            "_labels": {f"{x},{y}": r for (x, y), r in labels.items()},
            "warps": [],
            "connections": [],
            "trans": _transitions(graph_state, labels) + script_trans,
        }
        if gates:
            maps[key]["gates"] = gates
    for key in sorted(states):
        state, info = states[key], maps[key]
        labels = {tuple(map(int, xy.split(","))): r for xy, r in info["_labels"].items()}
        puzzle = puzzle_map.get((state.identity.group, state.identity.number))
        hole_needs = {
            hole.warp: _gate_id(state.identity.name or key, "hole", hole.tile)
            for hole in (puzzle.holes if puzzle else ())
            if hole.warp is not None
        }
        for warp in state.warps:
            region = _near(labels, warp.x, warp.y)
            if region is None or warp.dynamic:
                continue
            row = {
                "index": warp.index,
                "x": warp.x,
                "y": warp.y,
                "region": region,
                "dest": f"{warp.dest_group}:{warp.dest_number}",
                "dest_warp": warp.dest_warp_id,
                "dynamic": warp.dynamic,
                "exit": warp.trigger is not None,
            }
            if warp.index in hole_needs:
                row["needs"] = [hole_needs[warp.index]]
            info["warps"].append(row)
        info["connections"] = _connections(state, labels, maps)
    for info in maps.values():
        info.pop("_labels")
    return {
        "version": 1,
        "source": "pokegent.games.gen3.firered.maps.map_data.MapReader",
        "rom_profile": ROM_PROFILE,
        "ids": ALPHABET,
        "maps": maps,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rom", default=os.environ.get("FIRERED_ROM_PATH"))
    parser.add_argument("--out", type=Path, default=ROOT / "references" / "regions.json")
    args = parser.parse_args()
    if not args.rom:
        parser.error("set FIRERED_ROM_PATH or pass --rom")
    rom = Path(args.rom).read_bytes()
    data = generate(rom)
    args.out.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sha = hashlib.sha256(rom).hexdigest()
    args.out.with_name("regions.PROVENANCE.txt").write_text(
        f"generated_at_source: {ROM_PROFILE}\nrom_sha256: {sha}\nproducer: tools/regiongen.py\n",
        encoding="utf-8",
    )
    print(f"wrote {args.out} ({len(data['maps'])} maps)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
