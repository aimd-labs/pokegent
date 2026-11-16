"""FireRed provider bundle consumed by the generic navigation graph."""

from __future__ import annotations

from pokegent.games.gen3.firered.maps import (
    map_behaviors,
    map_boulders,
    map_data,
    map_names,
)
from pokegent.games.gen3.firered.reader.memory_port import SnapshotMemoryReader
from pokegent.games.gen3.firered.rom import script_scan


class FireRedMapProvider:
    """ROM map/event/warp and puzzle implementation for :class:`NavigationGraph`."""

    behavior_info = staticmethod(map_behaviors.behavior_info)
    blocks_side = staticmethod(map_behaviors.blocks_side)
    boulders = staticmethod(map_boulders.boulders)
    puzzle_for = staticmethod(map_boulders.puzzle_for)
    puzzles = staticmethod(map_boulders.puzzles)
    unknown_opcode = script_scan.UnknownOpcode
    reachable = staticmethod(script_scan.reachable)

    @staticmethod
    def group_sizes() -> tuple[int, ...]:
        return map_names.group_sizes()

    @staticmethod
    def read_static(rom: bytes, profile, group: int, number: int):
        port = SnapshotMemoryReader(
            {0x0800_0000: rom, 0x0300_0000: bytes(0x8000), 0x0200_0000: bytes(0x40000)}
        )
        reader = map_data.MapReader(port, profile, cache=map_data.RomMapCache())
        return reader.read_static(group, number)

    @staticmethod
    def is_boulder_plan(value) -> bool:
        from pokegent.games.gen3.firered.maps.boulders import Plan

        return isinstance(value, Plan)

    @staticmethod
    def boulder_world(state):
        from pokegent.games.gen3.firered.maps.boulders import BoulderWorld

        return BoulderWorld.from_state(state)

    @staticmethod
    def goal_type():
        from pokegent.games.gen3.firered.maps.boulders import Goal

        return Goal


__all__ = ["FireRedMapProvider"]
