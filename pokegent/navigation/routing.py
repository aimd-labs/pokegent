"""``MapGraph``: which map is how many exits away from which (a technical route hint).

Built from the game's own warp and connection tables (all 425 maps, from the ROM alone), so it knows
nothing about story gates, badges, HMs or one-way ledges: it answers "which exit leads TOWARD the
target map if every door were open". That is a fact for ``serves_goal``, never a plan. agent decides
the strategy; the graph only keeps the facts honest ("this door is on a shortest technical route").

One physical fact is checked: a map connection whose seam is a wall all along (no tile pair with
collision 0 on both sides) is not an edge. Without that check the graph walked Route 6 ->
Saffron City
through a fence, and the Fan Club -> Cerulean errand found no exit that "leads toward" its target.
"""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Iterable, Mapping

MapKey = tuple[int, int]
_ROM_GRAPH_CACHE: dict[tuple[str, str], "MapGraph"] = {}


class MapGraph:
    def __init__(self, edges: Mapping[MapKey, Iterable[MapKey]], *, regions=None) -> None:
        self._out: dict[MapKey, frozenset[MapKey]] = {k: frozenset(v) for k, v in edges.items()}
        back: dict[MapKey, set[MapKey]] = {}
        for src, dests in self._out.items():
            for dest in dests:
                back.setdefault(dest, set()).add(src)
        self._in = {k: frozenset(v) for k, v in back.items()}
        self._cache: dict[MapKey, dict[MapKey, int]] = {}
        self.regions = regions

    def __contains__(self, key: object) -> bool:
        return key in self._out or key in self._in

    def distances_to(self, target: MapKey) -> dict[MapKey, int]:
        """Exits needed from every map that can reach ``target`` (BFS on the reversed graph)."""
        cached = self._cache.get(target)
        if cached is not None:
            return cached
        dist = {target: 0}
        queue = deque([target])
        while queue:
            cur = queue.popleft()
            for prev in self._in.get(cur, ()):
                if prev not in dist:
                    dist[prev] = dist[cur] + 1
                    queue.append(prev)
        self._cache[target] = dist
        return dist

    def distance(self, source: MapKey, target: MapKey) -> int | None:
        return self.distances_to(target).get(source)

    def leads_toward(self, current: MapKey, dest: MapKey, target: MapKey) -> bool | None:
        """Does leaving ``current`` for ``dest`` shorten the technical route to ``target``?
        ``None`` when the graph cannot say (an unknown map)."""
        dist = self.distances_to(target)
        here, there = dist.get(current), dist.get(dest)
        if here is None or there is None:
            return None
        return there < here

    @classmethod
    def from_rom(cls, rom: bytes, profile) -> MapGraph:
        """Read every map's warps and connections from ROM bytes (about 425 static reads)."""
        key = (hashlib.sha1(rom).hexdigest(), profile.profile_id)
        cached = _ROM_GRAPH_CACHE.get(key)
        if cached is not None:
            return cached
        from pokegent.games.gen3.firered.maps import map_names
        from pokegent.games.gen3.firered.maps.map_data import MapReader, RomMapCache
        from pokegent.games.gen3.firered.reader.memory_port import SnapshotMemoryReader

        port = SnapshotMemoryReader(
            {0x0800_0000: rom, 0x0300_0000: bytes(0x8000), 0x0200_0000: bytes(0x4_0000)}
        )
        reader = MapReader(port, profile, cache=RomMapCache())
        states = {}
        for group, size in enumerate(map_names.group_sizes()):
            for number in range(size):
                result = reader.read_static(group, number)
                if result.usable:
                    states[(group, number)] = result.state
        edges: dict[MapKey, set[MapKey]] = {}
        for key, state in states.items():
            out = edges.setdefault(key, set())
            for warp in state.warps:
                if not warp.dynamic:
                    out.add((warp.dest_group, warp.dest_number))
            for conn in state.connections:
                if conn.direction not in ("N", "S", "W", "E"):
                    continue
                dest = (conn.dest_group, conn.dest_number)
                # a seam nobody can cross is no edge: Route 6's north connection to Saffron City
                # only scrolls the map, a fence closes it; the way in is the gatehouse (a warp)
                if dest in states and seam_is_open(state, states[dest], conn):
                    out.add(dest)
        from pokegent.navigation.regions import load_regions

        graph = cls(edges, regions=load_regions())
        _ROM_GRAPH_CACHE[key] = graph
        return graph

    def region_leads_toward(
        self,
        current: MapKey,
        source_region: int,
        dest: MapKey,
        target: MapKey,
        *,
        caps: Iterable[str] = (),
        dest_region: int | None = None,
    ) -> bool | None:
        """Region-level equivalent of :meth:`leads_toward`; ``None`` preserves map fallback."""
        if self.regions is None or dest_region is None:
            return self.leads_toward(current, dest, target)
        source = (current[0], current[1], source_region)
        destination = (dest[0], dest[1], dest_region)
        return self.regions.serves(source, destination, target, caps)


def seam_is_open(here, there, conn) -> bool:
    """Is there at least one tile pair across the connection where both sides have collision 0?

    Static, ROM-only: objects, ledges, water and trees are not looked at (the graph is a technical
    hint, not a plan), only whether the border of ``here`` is a wall or fence all along.
    ``conn.offset`` shifts ``there`` along the edge (pret ``struct MapConnection``)."""
    w, h = here.layout.width, here.layout.height
    dw, dh = there.layout.width, there.layout.height
    if conn.direction in ("N", "S"):
        row, drow = (0, dh - 1) if conn.direction == "N" else (h - 1, 0)
        return any(
            0 <= x - conn.offset < dw
            and here.collision(x, row) == 0
            and there.collision(x - conn.offset, drow) == 0
            for x in range(w)
        )
    column, dcolumn = (0, dw - 1) if conn.direction == "W" else (w - 1, 0)
    return any(
        0 <= y - conn.offset < dh
        and here.collision(column, y) == 0
        and there.collision(dcolumn, y - conn.offset) == 0
        for y in range(h)
    )
