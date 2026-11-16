"""``RomMapDirectory``: the deep checks of objective validation, from the ROM alone.

Implements :class:`~pokegent.goals.validate.MapDirectory` with static map reads (no emulator,
no RAM). A target person is checked against the object events of that map (templates), a warp index
against the map's warp table, coordinates against the layout size.
"""

from __future__ import annotations

import re
import unicodedata
from types import SimpleNamespace

from pokegent.games.gen3.firered.maps import map_names
from pokegent.games.gen3.firered.maps.map_data import MapReader, RomMapCache
from pokegent.games.gen3.firered.maps.world import WorldAdapter
from pokegent.games.gen3.firered.reader.memory_port import SnapshotMemoryReader
from pokegent.games.gen3.firered.rom.rom_tables import GameTables
from pokegent.goals.validate import MapFacts


class RomTablesUnavailable(RuntimeError):
    """The ROM exists, but its static tables could not be read."""


class AmbiguousItemNameError(ValueError):
    """A normalized item name matched more than one exact ROM item name."""

    code = "unknown_item"


def _normalize_item_name(name: str) -> str:
    folded = unicodedata.normalize("NFKD", name.casefold())
    without_accents = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", without_accents).strip()


class RomMapDirectory:
    def __init__(self, rom: bytes, profile) -> None:
        port = SnapshotMemoryReader(
            {0x0800_0000: rom, 0x0300_0000: bytes(0x8000), 0x0200_0000: bytes(0x4_0000)}
        )
        self._reader = MapReader(port, profile, cache=RomMapCache())
        self._rom = rom
        self._profile = profile
        try:
            self._tables = GameTables.from_port(
                SnapshotMemoryReader({0x0800_0000: self._rom}), self._profile
            )
            self._tables_error: Exception | None = None
        except Exception as exc:
            self._tables = None
            self._tables_error = exc
        self._facts: dict[tuple[int, int], MapFacts | None] = {}
        self._leaders: dict[int, tuple[tuple[int, int], int] | None] | None = None

    def resolve_item(self, name: str) -> int | None:
        if self._tables_error is not None:
            raise RomTablesUnavailable(str(self._tables_error)) from self._tables_error
        wanted = _normalize_item_name(name)
        matches = [item for item in self._tables.items if _normalize_item_name(item.name) == wanted]
        if len(matches) > 1:
            candidates = ", ".join(repr(item.name) for item in matches)
            raise AmbiguousItemNameError(
                f"ambiguous ROM item name {name!r}; candidates: {candidates}"
            )
        return matches[0].item_id if matches else None

    def describe(self, group: int, number: int) -> MapFacts | None:
        key = (group, number)
        if key not in self._facts:
            result = self._reader.read_static(group, number)
            state = result.state if result.usable else None
            self._facts[key] = (
                None
                if state is None
                else MapFacts(
                    width=state.layout.width,
                    height=state.layout.height,
                    warp_count=len(state.warps),
                    npc_ids=frozenset(
                        o.local_id for o in state.objects if o.script_ptr is not None
                    ),
                    trainer_ids=frozenset(
                        o.local_id for o in state.objects if o.trainer_type in (1, 2)
                    ),
                )
            )
        return self._facts[key]

    def leader_for_badge(self, badge: int) -> tuple[tuple[int, int], int] | None:
        """Find the NPC whose ROM script sets this badge's flag."""
        if self._leaders is None:
            world = WorldAdapter(self._rom, self._profile, tables=self._tables)
            empty = SimpleNamespace(flags=None, party=())
            leaders: dict[int, tuple[tuple[int, int], int] | None] = {}
            for group, size in enumerate(map_names.group_sizes()):
                for number in range(size):
                    result = world.map_reader.read_static(group, number)
                    if not result.usable or result.state is None:
                        continue
                    for npc in world._npcs(empty, result.state):
                        if npc.trainer_kind == "gym_leader" and npc.leader_badge is not None:
                            leaders.setdefault(npc.leader_badge, ((group, number), npc.local_id))
            self._leaders = leaders
        return self._leaders.get(badge)
