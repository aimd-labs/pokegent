"""Bounded public-data smoke for the real NavigationGraph ROM factory."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

DEADLINE_SECONDS = 30

def run_with_deadline(command, timeout=DEADLINE_SECONDS, runner=subprocess.run):
    started = time.monotonic()
    try:
        result = runner(command, capture_output=True, text=True, timeout=timeout, check=False)
        return result.returncode, time.monotonic() - started, result.stdout, result.stderr
    except subprocess.TimeoutExpired as exc:
        return 124, time.monotonic() - started, exc.stdout or "", exc.stderr or ""


def _worker() -> None:
    from types import SimpleNamespace

    from pokegent.games.gen3.firered.maps import map_schema
    from pokegent.games.gen3.firered.rom.rom_data import load_profiles
    from pokegent.navigation.graph import NavigationGraph

    class SyntheticProvider:
        def group_sizes(self):
            return (1,)

        def read_static(self, rom, profile, group, number):
            state = map_schema.MapState(
                identity=map_schema.MapIdentity(
                    group=group,
                    number=number,
                    name="SyntheticRoom",
                    layout_id=1,
                    region_section_id=0,
                    map_type=0,
                    map_type_name="synthetic",
                    weather=0,
                    cave=0,
                    floor_num=0,
                    battle_type=0,
                    music=0,
                    biking_allowed=True,
                    allow_escaping=True,
                    allow_running=True,
                    identity_verified=True,
                ),
                layout=map_schema.MapLayoutInfo(
                    width=1,
                    height=1,
                    border_width=1,
                    border_height=1,
                    border=(0,),
                    primary_tileset=0,
                    secondary_tileset=0,
                    layout_ptr=0,
                ),
                grid=map_schema.MapGrid(
                    columns=15,
                    rows=15,
                    blocks=(0,) * 225,
                    attributes=((0, 0),),
                ),
            )
            return SimpleNamespace(usable=True, state=state)

    profile = load_profiles().get("firered-us-rev0")
    rom = bytearray(profile.rom.size)

    def put(address, value):
        offset = address - 0x08000000
        rom[offset : offset + len(value)] = value

    # A one-entry public map-group/header/script chain, all invented addresses and bytes.
    put(profile.symbol("gMapGroups").address, (0x08001000).to_bytes(4, "little"))
    put(0x08001000, (0x08002000).to_bytes(4, "little"))
    put(0x08002008, (0x08003000).to_bytes(4, "little"))
    chart = profile.rom_table("gTypeEffectiveness")
    put(chart.address, b"\xfe")
    put(chart.address + chart.size - 3, b"\xff")
    items = profile.rom_table("gItems")
    synthetic_item = bytearray(44)
    synthetic_item[:2] = b"\xbb\xff"  # invented one-letter item name: "A"
    synthetic_item[14:18] = (1).to_bytes(2, "little") + (123).to_bytes(2, "little")
    put(items.address + 44, bytes(synthetic_item))

    graph = NavigationGraph.from_rom(bytes(rom), profile, provider=SyntheticProvider())
    assert len(graph.maps) == 1
    assert graph.maps[(0, 0)].identity.name == "SyntheticRoom"
    assert graph._item_names[1] == "A"
    print(
        f"factory built {len(graph.maps)} synthetic map; "
        f"decoded invented item {graph._item_names[1]!r}"
    )


def main() -> int:
    if "--worker" in sys.argv:
        _worker()
        return 0
    command = [sys.executable, str(Path(__file__).resolve()), "--worker"]
    code, elapsed, stdout, stderr = run_with_deadline(command)
    print(f"public startup smoke elapsed={elapsed:.3f}s deadline={DEADLINE_SECONDS}s")
    if stdout:
        print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)
    if code:
        print(f"public startup smoke failed (exit={code})", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
