"""ROM-derived region graph for FireRed navigation.

The cache is deliberately boring JSON.  Runtime navigation reads the cache and never scans the
425 ROM maps; ``tools/regiongen.py`` is the only producer.  A region is a same-medium connected
component of walkable map tiles.  Warps, map seams, ledges and field-move transitions are edges
between regions.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

MapKey = tuple[int, int]
RegionKey = tuple[int, int, int]
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_DEFAULT = Path(__file__).resolve().parents[2] / "references" / "regions.json"
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegionRoute:
    cost: int
    nodes: tuple[RegionKey, ...]
    first: RegionKey | None = None
    missing: tuple[str, ...] = ()


def _key(value: str | Iterable[int]) -> MapKey:
    if isinstance(value, str):
        group, number = value.split(":", 1)
        return int(group), int(number)
    group, number = value
    return int(group), int(number)


def _map_name(key: MapKey) -> str:
    return f"{key[0]}:{key[1]}"


class RegionGraph:
    """A capability-aware graph over map regions."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.version = int(data.get("version", 1))
        self.maps: dict[str, dict[str, Any]] = data.get("maps", {})
        self._decoded: dict[str, tuple[tuple[int | None, ...], ...]] = {}
        self._adj: dict[RegionKey, list[tuple[RegionKey, int, tuple[str, ...], str]]] = defaultdict(
            list
        )
        self._build_edges()

    @classmethod
    def load(cls, path: str | Path = _DEFAULT) -> "RegionGraph | None":
        if path == _DEFAULT:
            from pokegent.games.gen3.firered.rom.rom_cache import existing_cache_path

            cached = existing_cache_path()
            if cached is not None and (cached / "regions.json").is_file():
                path = cached / "regions.json"
            elif __import__("os").environ.get("FIRERED_ROM_PATH"):
                from pokegent.games.gen3.firered.rom.rom_cache import ensure

                path = ensure() / "regions.json"
        try:
            return cls(json.loads(Path(path).read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, KeyError) as exc:
            _LOG.error("invalid region cache %s: %s", path, exc)
            raise

    def _build_edges(self) -> None:
        for map_text, info in self.maps.items():
            src_map = _key(map_text)
            for warp in info.get("warps", ()):
                # ``exit`` describes whether the field builder may offer this warp as an
                # option.  It is not a connectivity fact: interior doors (including the
                # Elite Four chain) are ROM warps with ``exit: false``.
                if warp.get("dynamic") or warp.get("dest") is None:
                    continue
                dest_map = _key(warp["dest"])
                dest_info = self.maps.get(_map_name(dest_map))
                if dest_info is None:
                    continue
                dest_warp = int(warp.get("dest_warp", -1))
                land = next(
                    (w for w in dest_info.get("warps", ()) if w["index"] == dest_warp),
                    None,
                )
                dest_region = land.get("region") if land else None
                if dest_region is None:
                    dest_region = self.region_at(dest_map, *(warp.get("landing") or (0, 0)))
                if dest_region is None:
                    continue
                self._add(
                    (src_map[0], src_map[1], int(warp["region"])),
                    (dest_map[0], dest_map[1], int(dest_region)),
                    10,
                    tuple(warp.get("needs", ())),
                    "warp",
                )
            for conn in info.get("connections", info.get("conns", ())):
                dest_map = _key(conn["dest"])
                pairs = conn.get("pairs", ())
                for pair in pairs:
                    if isinstance(pair, dict):
                        src_region, dest_region = int(pair["from"]), int(pair["to"])
                        needs = tuple(pair.get("needs", ()))
                    else:
                        src_region, dest_region = (
                            int(pair[0]),
                            int(pair[1] if len(pair) == 2 else pair[3]),
                        )
                        needs = ()
                    self._add(
                        (src_map[0], src_map[1], src_region),
                        (dest_map[0], dest_map[1], dest_region),
                        10,
                        needs,
                        "connection",
                    )
            for trans in info.get("trans", ()):
                src_region, dest_region = int(trans["from"]), int(trans["to"])
                needs = tuple(trans.get("needs", ()))
                self._add(
                    (src_map[0], src_map[1], src_region),
                    (src_map[0], src_map[1], dest_region),
                    int(trans.get("cost", 1)),
                    needs,
                    str(trans.get("kind", "transition")),
                )

    def _add(
        self, src: RegionKey, dest: RegionKey, cost: int, needs: tuple[str, ...], kind: str
    ) -> None:
        edge = (dest, cost, needs, kind)
        if edge not in self._adj[src]:
            self._adj[src].append(edge)

    def _grid(self, key: MapKey) -> tuple[tuple[int | None, ...], ...]:
        name = _map_name(key)
        cached = self._decoded.get(name)
        if cached is not None:
            return cached
        info = self.maps[name]
        rows: list[tuple[int | None, ...]] = []
        for row in info.get("grid", ()):
            rows.append(tuple(None if c == "." else _ALPHABET.index(c) for c in row))
        result = tuple(rows)
        self._decoded[name] = result
        return result

    def region_at(self, map_key: MapKey, x: int, y: int) -> int | None:
        info = self.maps.get(_map_name(map_key))
        if info is None:
            return None
        grid = self._grid(map_key)
        if 0 <= y < len(grid) and 0 <= x < len(grid[y]) and grid[y][x] is not None:
            return grid[y][x]
        # Warp and door tiles can be blocked while their adjacent standing tile is in the region.
        for radius in range(1, 4):
            for nx, ny in ((x - radius, y), (x + radius, y), (x, y - radius), (x, y + radius)):
                if 0 <= ny < len(grid) and 0 <= nx < len(grid[ny]) and grid[ny][nx] is not None:
                    return grid[ny][nx]
        return None

    def route(
        self,
        source: RegionKey,
        target_map: MapKey,
        caps: Iterable[str] = (),
        *,
        target_region: int | None = None,
    ) -> RegionRoute | None:
        """Find a route to ``target_map`` (or one exact region on that map).

        A same-map route is only free when its region is the requested destination.  Keeping the
        region in the state matters for one-way puzzle gates: a ladder landing in the source side
        of a cycle must not be treated as progress merely because it has the right map id.
        """
        allowed = frozenset(caps)
        if source[0:2] == target_map and (target_region is None or source[2] == target_region):
            return RegionRoute(0, (source,))
        distances: dict[RegionKey, int] = {source: 0}
        parents: dict[RegionKey, RegionKey] = {}
        queue: list[tuple[int, int, RegionKey]] = [(0, 0, source)]
        serial = 0
        while queue:
            cost, _, node = queue.pop(0)
            if cost != distances.get(node):
                continue
            if node[0:2] == target_map and (target_region is None or node[2] == target_region):
                path = self._path(parents, node)
                return RegionRoute(cost, path, path[1] if len(path) > 1 else None)
            for dest, edge_cost, needs, _kind in self._adj.get(node, ()):
                if not set(needs).issubset(allowed):
                    continue
                new = cost + edge_cost
                if new < distances.get(dest, 1 << 60):
                    distances[dest] = new
                    parents[dest] = node
                    serial += 1
                    queue.append((new, serial, dest))
            queue.sort(key=lambda item: (item[0], item[1]))
        return None

    def route_with_fly(
        self,
        source: RegionKey,
        target_map: MapKey,
        caps: Iterable[str],
        destinations: Iterable[Any],
        *,
        fly_cost: int = 60,
    ) -> RegionRoute | None:
        """Route with verified Fly edges to visited destination heal regions.

        The edges are request-scoped because visited-town flags belong to a live snapshot, not to
        the static cache. ``fly`` is still an explicit capability gate; callers must separately
        prove that the current map is an outdoor Fly-allowed map.
        """
        base = self.route(source, target_map, caps)
        if "fly" not in set(caps):
            return base
        best = base
        for destination in destinations:
            landing = self.region_at(destination.map_key, *destination.landing)
            if landing is None:
                continue
            tail = self.route(
                (destination.map_key[0], destination.map_key[1], landing), target_map, caps
            )
            if tail is None:
                continue
            candidate = RegionRoute(
                fly_cost + tail.cost,
                (source, (destination.map_key[0], destination.map_key[1], landing), *tail.nodes),
                first=(destination.map_key[0], destination.map_key[1], landing),
            )
            if best is None or candidate.cost < best.cost:
                best = candidate
        return best

    @staticmethod
    def _path(parents: dict[RegionKey, RegionKey], node: RegionKey) -> tuple[RegionKey, ...]:
        path = [node]
        while path[-1] in parents:
            path.append(parents[path[-1]])
        path.reverse()
        return tuple(path)

    def missing_for(
        self, source: RegionKey, target_map: MapKey, caps: Iterable[str] = ()
    ) -> tuple[str, ...]:
        if self.route(source, target_map, caps) is not None:
            return ()
        all_route = self.route(source, target_map, self._all_needs())
        if all_route is None:
            return ()
        found: set[str] = set()
        for left, right in zip(all_route.nodes, all_route.nodes[1:]):
            for dest, _cost, needs, _kind in self._adj.get(left, ()):
                if dest == right:
                    found.update(needs)
        return tuple(sorted(found))

    def _all_needs(self) -> frozenset[str]:
        return frozenset(
            need
            for edges in self._adj.values()
            for _dest, _cost, needs, _kind in edges
            for need in needs
        )

    def serves(
        self,
        source: RegionKey,
        destination: RegionKey,
        target_map: MapKey,
        caps: Iterable[str] = (),
    ) -> bool | None:
        route = self.route(source, target_map, caps)
        remaining = self.route(destination, target_map, caps)
        if route is None or remaining is None:
            return False
        return remaining.cost < route.cost

    def edges_from(
        self, source: RegionKey, caps: Iterable[str] = ()
    ) -> tuple[tuple[RegionKey, int, tuple[str, ...], str], ...]:
        allowed = frozenset(caps)
        return tuple(edge for edge in self._adj.get(source, ()) if set(edge[2]).issubset(allowed))

    def warp_destinations(self, dest_map: MapKey, warp_index: int) -> tuple[int, ...]:
        return tuple(
            sorted(
                {
                    int(w["region"])
                    for w in self.maps.get(_map_name(dest_map), {}).get("warps", ())
                    if int(w.get("index", -1)) == warp_index
                }
            )
        )

    def exit_regions(self, map_key: MapKey, target: Mapping[str, object]) -> tuple[int, ...]:
        """The regions of ``map_key`` an exit option stands in: a warp's own region, or the regions
        the connection leaves from. ``target`` is the option's ``facts["target"]``."""
        info = self.maps.get(_map_name(map_key))
        if info is None:
            return ()
        if target.get("kind") == "warp":
            return tuple(
                sorted(
                    {
                        int(w["region"])
                        for w in info.get("warps", ())
                        if int(w.get("index", -1)) == target.get("index")
                    }
                )
            )
        dest = target.get("dest")
        if target.get("kind") == "edge" and isinstance(dest, (list, tuple)) and len(dest) == 2:
            return tuple(
                sorted(
                    {
                        int(p["from"])
                        for c in info.get("connections", ())
                        if c.get("dest") == _map_name((dest[0], dest[1]))
                        and c.get("direction") == target.get("direction")
                        for p in c.get("pairs", ())
                    }
                )
            )
        return ()

    def connection_destinations(
        self, source_map: MapKey, source_region: int, dest_map: MapKey, direction: str
    ) -> tuple[int, ...]:
        for conn in self.maps.get(_map_name(source_map), {}).get("connections", ()):
            if conn.get("dest") == _map_name(dest_map) and conn.get("direction") == direction:
                return tuple(
                    sorted(
                        {
                            int(p["to"])
                            for p in conn.get("pairs", ())
                            if int(p["from"]) == source_region
                        }
                    )
                )
        return ()


def load_regions(path: str | Path = _DEFAULT) -> RegionGraph | None:
    return RegionGraph.load(path)
