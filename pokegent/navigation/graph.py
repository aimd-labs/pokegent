"""FireRed world graph built from ROM maps and overlaid with one live RAM snapshot."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from pokegent.actions.field import fly
from pokegent.games.gen3.firered.maps import map_constants as K
from pokegent.games.gen3.firered.maps.boulder_scan import (
    Unparsed,
    header_scripts_ptr,
    on_frame_movement,
    script_table,
)
from pokegent.games.gen3.firered.maps.map_motion import _walk_into_signpost
from pokegent.games.gen3.firered.maps.obstacles import (
    FieldObstacle,
    from_object,
    script_gate,
    script_tile,
)
from pokegent.games.gen3.firered.rom.script_effects import (
    Effect,
    Guard,
    Interpretation,
    ScriptContext,
    interpret,
)
from pokegent.games.gen3.firered.rom.script_scan import ROM_BASE, decode_stream, reachable
from pokegent.games.gen3.firered.rom.static_audit_codec import AuditCall
from pokegent.navigation.capabilities import requirement
from pokegent.navigation.models import (
    CapabilityState,
    NavigationEdge,
    NavigationNode,
    NavigationRequirement,
)

if TYPE_CHECKING:
    from pokegent.games.gen3.firered.maps.map_schema import (
        Facing,
        MapObject,
        MapState,
        WarpInfo,
    )

CARD_KEY_ITEM_ID = 355
CARD_KEY_FLAG = 0x192
_HM_REQUIREMENTS = {
    15: "cut",
    57: "surf",
    70: "strength",
    127: "waterfall",
    148: "flash",
    249: "rock_smash",
}

_SCRIPT_EFFECT_READERS = (
    "door_gates:on_load",
    "door_gates:on_transition",
    "elevators:bg",
    "flag_items:bg",
    "flag_items:coord",
    "flag_items:object",
    "ferries:coord",
    "ferries:npc",
    "on_load_barrier_labels:on_load",
    "on_load_barriers:on_load",
    "on_load_metatiles:on_load",
    "on_load_warp_flags:on_load",
    "script_gates:bg",
    "script_obstacles:coord",
    "script_obstacles:npc",
    "script_obstacles:on_transition",
    "toggles:bg",
    "toggles:object",
    "warp_gates:bg",
    "warp_gates:on_load",
    "warp_gates:on_transition",
)
VECTOR: dict[str, tuple[int, int]] = {"N": (0, -1), "S": (0, 1), "W": (-1, 0), "E": (1, 0)}
OPPOSITE = {"N": "S", "S": "N", "W": "E", "E": "W"}
DIRECTIONS: tuple[Facing, ...] = ("N", "S", "W", "E")
_ROM_GRAPH_CACHE_MAX_ENTRIES = 2
_ROM_GRAPH_CACHE: dict[tuple[str, str, str, str], "NavigationGraph"] = {}
_PUZZLES_UNLOADED = object()


def _rom_graph_cache_identity(
    rom: bytes, profile: Any, group_sizes: tuple[int, ...]
) -> tuple[str, str, str, str] | None:
    """Return the process cache identity for an implicitly provided standard graph.

    ``group_sizes`` must be the grouping already loaded for the build, and the key
    names the full loaded group order and member names that the provider consumes.
    The generator source fingerprint is fixed for the process, so replacing
    shipped Python sources takes effect after a process restart.  Any unavailable
    or inconsistent identity returns ``None``; unknown identities must bypass the
    cache, never alias another graph.
    """
    try:
        from pokegent.games.gen3.firered.maps.map_names import loaded_groups
        from pokegent.games.gen3.firered.rom.rom_cache import _generator_fingerprint

        order, groups = loaded_groups()
        if tuple(len(names) for names in groups) != tuple(group_sizes):
            return None
        grouping = json.dumps(
            [list(order), [list(names) for names in groups]], separators=(",", ":")
        )
        canonical_profile = json.dumps(
            profile.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return (
            hashlib.sha256(rom).hexdigest(),
            hashlib.sha256(canonical_profile.encode("utf-8")).hexdigest(),
            _generator_fingerprint(),
            hashlib.sha256(grouping.encode("utf-8")).hexdigest(),
        )
    except Exception:
        return None


def _admit_rom_graph(key: tuple[str, str, str, str], graph: "NavigationGraph") -> None:
    """Admit a successfully built graph, keeping the two most recent keys (FIFO)."""
    while len(_ROM_GRAPH_CACHE) >= _ROM_GRAPH_CACHE_MAX_ENTRIES:
        _ROM_GRAPH_CACHE.pop(next(iter(_ROM_GRAPH_CACHE)))
    _ROM_GRAPH_CACHE[key] = graph


@dataclass(frozen=True)
class CountedTrainer:
    flag: int
    map_key: tuple[int, int]
    local_id: int
    x: int
    y: int


@dataclass(frozen=True)
class ScriptToggle:
    x: int
    y: int
    flag: int
    facing: str | None
    event_index: int
    local_id: int | None = None


@dataclass(frozen=True)
class FerryRoute:
    source: NavigationNode
    destination: NavigationNode
    guards: tuple[Guard, ...]
    selector: int
    local_id: int | None = None
    coord: tuple[int, int] | None = None
    direction: str | None = None
    menu_index: int | None = None
    menu_id: int | None = None
    menu_pc: int | None = None
    menu_pcs: tuple[tuple[int, int], ...] = ()


@dataclass
class ScriptEffectStats:
    """Bound failures which would make a ROM gate audit incomplete."""

    step_limit: int = 0
    state_limit: int = 0
    unknown_opcode: int = 0

    def add(self, interpretation: Interpretation) -> None:
        for reason in interpretation.unknown:
            self.step_limit += reason.startswith("step limit")
            self.state_limit += reason.startswith("state limit")
            self.unknown_opcode += "unknown opcode" in reason.lower()


def _simple_path_corridor(
    adjacency: dict[tuple[int, int], frozenset[tuple[int, int]]],
    start: tuple[int, int],
    target: tuple[int, int],
) -> frozenset[tuple[int, int]] | None:
    """Vertices occurring on at least one simple ``start``-to-``target`` path.

    The block-cut forest has a unique path between the blocks containing the
    endpoints.  Exactly the vertices in blocks on that path can occur on a
    simple endpoint-to-endpoint path.
    """
    if start not in adjacency or target not in adjacency:
        return None
    if start == target:
        return frozenset({start})

    discovery: dict[tuple[int, int], int] = {}
    low: dict[tuple[int, int], int] = {}
    parent: dict[tuple[int, int], tuple[int, int]] = {}
    edge_stack: list[tuple[tuple[int, int], tuple[int, int]]] = []
    blocks: list[frozenset[tuple[int, int]]] = []
    clock = 0

    def visit(node: tuple[int, int]) -> None:
        nonlocal clock
        clock += 1
        discovery[node] = low[node] = clock
        for neighbor in adjacency[node]:
            if neighbor not in discovery:
                parent[neighbor] = node
                edge_stack.append((node, neighbor))
                visit(neighbor)
                low[node] = min(low[node], low[neighbor])
                if low[neighbor] >= discovery[node]:
                    block: set[tuple[int, int]] = set()
                    while edge_stack:
                        edge = edge_stack.pop()
                        block.update(edge)
                        if edge == (node, neighbor):
                            break
                    blocks.append(frozenset(block))
            elif parent.get(node) != neighbor and discovery[neighbor] < discovery[node]:
                low[node] = min(low[node], discovery[neighbor])
                edge_stack.append((node, neighbor))

    visit(start)
    if target not in discovery:
        return None

    membership: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, block in enumerate(blocks):
        for vertex in block:
            membership[vertex].append(index)
    if not membership.get(start) or not membership.get(target):
        return None

    tree: dict[tuple[str, object], set[tuple[str, object]]] = defaultdict(set)
    for vertex, indices in membership.items():
        if len(indices) < 2:
            continue
        vertex_node: tuple[str, object] = ("vertex", vertex)
        for index in indices:
            block_node: tuple[str, object] = ("block", index)
            tree[vertex_node].add(block_node)
            tree[block_node].add(vertex_node)

    def endpoint(vertex: tuple[int, int]) -> tuple[str, object]:
        indices = membership[vertex]
        return ("vertex", vertex) if len(indices) > 1 else ("block", indices[0])

    source_node, target_node = endpoint(start), endpoint(target)
    previous: dict[tuple[str, object], tuple[str, object] | None] = {source_node: None}
    queue = deque([source_node])
    while queue and target_node not in previous:
        current = queue.popleft()
        for neighbor in tree.get(current, ()):
            if neighbor not in previous:
                previous[neighbor] = current
                queue.append(neighbor)
    if target_node not in previous:
        # Both endpoints can belong to the same non-articulation block.
        if source_node != target_node:
            return None
        path = {source_node}
    else:
        path = set()
        current: tuple[str, object] | None = target_node
        while current is not None:
            path.add(current)
            current = previous[current]

    corridor: set[tuple[int, int]] = set()
    for kind, value in path:
        if kind == "block":
            corridor.update(blocks[int(value)])
        else:
            corridor.add(value)  # type: ignore[arg-type]
    return frozenset(corridor)


class NavigationGraph:
    """A lazy tile graph over every usable map in the ROM.

    Static geometry, warps, connections, object templates and scripts come from ``MapReader``.
    ``edges_from`` overlays the current ``MapState`` and :class:`CapabilityState`, so moved
    boulders, hidden objects, flags, party moves, badges and mount state are request-scoped.
    """

    def __init__(
        self,
        maps: dict[tuple[int, int], Any],
        *,
        rom: bytes = b"",
        provider: Any = None,
        item_names: dict[int, str] | None = None,
        profile: Any = None,
        _materialize: bool = False,
    ) -> None:
        if type(_materialize) is not bool:
            raise TypeError("_materialize must be an exact bool")
        if provider is None:
            from pokegent.games.gen3.firered.maps.map_provider import FireRedMapProvider

            provider = FireRedMapProvider()
        self.provider = provider
        self.maps = maps
        self._puzzle_table: Any = _PUZZLES_UNLOADED
        self._blocking_object_indexes: dict[
            tuple[int, int],
            tuple[tuple[MapObject, ...], dict[tuple[int, int], tuple[MapObject, ...]]],
        ] = {}
        self.rom = rom
        self._profile = profile
        self._item_names = item_names or {}
        self._materialized: (
            dict[tuple[Any, ...], tuple[Interpretation, tuple[tuple[int, int | None], ...]]] | None
        ) = {} if _materialize else None
        self._materialized_audit: list[AuditCall] | None = [] if _materialize else None
        self._seagallop_warps = self._read_seagallop_warps()
        self._effect_cache: dict[tuple[Any, ...], Interpretation] = {}
        self._water_body_ids: dict[tuple[tuple[int, int], int, int], tuple[int, int]] = {}
        self._map_transition_cache: dict[tuple[int, int], frozenset[tuple[int, int]]] | None = None
        self._stateful_route_flags_cache: dict[
            tuple[tuple[int, int], tuple[int, int]], frozenset[int]
        ] = {}
        self.script_trace_stats = ScriptEffectStats()
        self.script_incomplete_counts: Counter[str] = Counter(
            {reader: 0 for reader in _SCRIPT_EFFECT_READERS}
        )
        self._script_incomplete_seen: set[tuple[Any, ...]] = set()
        self._script_unknown_sites: dict[tuple[tuple[int, int], tuple[int, int]], set[str]] = (
            defaultdict(set)
        )
        self._reverse_warps: dict[tuple[int, int], list[tuple[tuple[int, int], WarpInfo]]] = (
            defaultdict(list)
        )
        for key, state in maps.items():
            for warp in state.warps:
                if not warp.dynamic:
                    self._reverse_warps[(warp.dest_group, warp.dest_number)].append((key, warp))
        self._flag_items = self._effect_flag_items()
        self._gates, self._gate_requirements = self._effect_script_gates()
        self._script_obstacles = self._effect_script_obstacles()
        self._on_load_warp_flags = self._effect_on_load_warp_flags()
        self._on_load_barriers = self._effect_on_load_barriers()
        self._on_load_warp_openings = self._effect_on_load_warp_openings()
        self._on_load_barrier_labels = self._effect_on_load_barrier_labels()
        self._on_load_metatiles = self._effect_on_load_metatiles()
        self._stateful_flags = frozenset(
            flag
            for rows in self._on_load_metatiles.values()
            for flags, _command in rows
            for flag in flags
        )
        self._script_toggles = self._effect_script_toggles()
        self._trainers_by_flag = self._read_trainers_by_flag()
        self._script_warp_gates = self._effect_script_warp_gates()
        self._script_door_gates = self._effect_script_door_gates()
        self._on_frame_landings = self._read_on_frame_landings(profile)
        self._elevators = self._effect_elevators()
        self._ferries = self._effect_ferries()

    def _read_seagallop_warps(
        self,
    ) -> tuple[tuple[int, tuple[int, int, int, int, int]], ...]:
        """Decode native ``sSeag`` entries from the identified ROM, never a shadow table."""
        if self._profile is None or not self._profile.has_symbol("sSeag"):
            return ()
        symbol = self._profile.symbol("sSeag")
        if symbol.size is None or symbol.size % 4 or symbol.address < ROM_BASE:
            return ()
        start = symbol.address - ROM_BASE
        raw = self.rom[start : start + symbol.size]
        if len(raw) != symbol.size:
            return ()
        rows = []
        for selector in range(symbol.size // 4):
            group, number, x, y = (
                int.from_bytes(
                    raw[selector * 4 + index : selector * 4 + index + 1],
                    "little",
                    signed=True,
                )
                for index in range(4)
            )
            if min(group, number, x, y) < 0:
                return ()
            rows.append((selector, (group, number, -1, x, y)))
        return tuple(rows)

    @classmethod
    def from_rom(cls, rom: bytes, profile, *, provider: Any = None) -> NavigationGraph:
        # Only an implicit standard provider on this exact class may reuse a cached
        # graph.  Explicit providers, provider subclasses and graph subclasses carry
        # behaviour the cache key cannot identify, so they bypass lookup and admission.
        # A hit returns the same mutable graph object; this cache makes no audit,
        # immutability or session-isolation promise.
        cacheable = provider is None and cls is NavigationGraph
        if provider is None:
            from pokegent.games.gen3.firered.maps.map_provider import FireRedMapProvider

            provider = FireRedMapProvider()
        group_sizes = tuple(provider.group_sizes())
        key = (
            _rom_graph_cache_identity(rom, profile, group_sizes) if cacheable else None
        )
        if key is not None:
            cached = _ROM_GRAPH_CACHE.get(key)
            if cached is not None:
                return cached
        maps: dict[tuple[int, int], Any] = {}
        for group, size in enumerate(group_sizes):
            for number in range(size):
                result = provider.read_static(rom, profile, group, number)
                if result.usable and result.state is not None:
                    maps[(group, number)] = result.state
        from pokegent.games.gen3.firered.rom.rom_tables import GameTables

        tables = GameTables.from_rom_image(rom, profile)
        item_names = {item.item_id: item.name.title() for item in tables.items}
        graph = cls(
            maps,
            rom=rom,
            provider=provider,
            item_names=item_names,
            profile=profile,
        )
        if key is not None:
            _admit_rom_graph(key, graph)
        return graph

    def state(self, map_key: tuple[int, int], live: MapState | None = None) -> MapState | None:
        if live is not None and (live.identity.group, live.identity.number) == map_key:
            return live
        return self.maps.get(map_key)

    @property
    def stateful_flags(self) -> frozenset[int]:
        """Flags whose ROM ON_LOAD scripts change map collision or metatiles."""
        return self._stateful_flags

    def stateful_flags_toward(
        self, start: tuple[int, int], target: tuple[int, int]
    ) -> frozenset[int]:
        """Return mutable geometry flags relevant to a start-to-target route.

        A toggle flag can only affect movement on maps named by its ROM ON_LOAD
        guards.  Its modeled toggle edge has no other side effect.  We build an
        undirected *over-approximation* of every ROM-derived map transition and
        retain a flag when either its toggle or an affected map occurs on some
        simple start-to-target path in that over-approximation.

        The initial corridor is then closed over dependencies: for each retained
        flag, paths to every map where it is observed or toggled join the
        corridor.  Repeating that step retains flags which gate those paths.
        The result is a sound over-approximation; it may retain unnecessary
        flags, but cannot omit a flag needed to reach and toggle another tracked
        flag.  Immutable guarded flags are not search dimensions; their live
        value remains in ``CapabilityState``.
        """
        cache_key = start, target
        cached = self._stateful_route_flags_cache.get(cache_key)
        if cached is not None:
            return cached
        mutable = {
            toggle.flag
            for toggles in self._script_toggles.values()
            for toggle in toggles
            if toggle.flag in self._stateful_flags
        }
        if not mutable:
            return frozenset()
        corridor = _simple_path_corridor(self._map_transition_overapprox(), start, target)
        if corridor is None:
            # An incomplete transition model must cost performance, never soundness.
            result = frozenset(mutable)
            self._stateful_route_flags_cache[cache_key] = result
            return result
        affected: dict[int, set[tuple[int, int]]] = defaultdict(set)
        for map_key, rows in self._on_load_metatiles.items():
            for flags, _effect in rows:
                for flag in flags:
                    affected[flag].add(map_key)
        for map_key, toggles in self._script_toggles.items():
            for toggle in toggles:
                affected[toggle.flag].add(map_key)
        # A flag whose geometry is on the direct corridor may only be toggled down an
        # off-corridor branch.  Reaching that toggle can itself cross geometry controlled by
        # another flag.  Grow the set of corridor targets with every tracked flag's scope and
        # toggle maps until no dependency flag is added.  This remains an over-approximation:
        # an unavailable branch costs search states, never a route.
        corridor_maps = set(corridor)
        corridor_targets = {target}
        result: set[int] = set()
        while True:
            expanded = {flag for flag in mutable if affected[flag] & corridor_maps}
            if expanded == result:
                break
            result = expanded
            new_targets = set().union(*(affected[flag] for flag in result))
            for corridor_target in new_targets - corridor_targets:
                dependency = _simple_path_corridor(
                    self._map_transition_overapprox(), start, corridor_target
                )
                if dependency is None:
                    # As above, an incomplete transition model must retain all dimensions.
                    result = set(mutable)
                    corridor_targets = new_targets
                    break
                corridor_maps.update(dependency)
            else:
                corridor_targets.update(new_targets)
                continue
            break
        result = frozenset(result)
        self._stateful_route_flags_cache[cache_key] = result
        return result

    def _map_transition_overapprox(
        self,
    ) -> dict[tuple[int, int], frozenset[tuple[int, int]]]:
        """Undirected superset of map transitions usable by navigation edges."""
        if self._map_transition_cache is not None:
            return self._map_transition_cache
        adjacent: dict[tuple[int, int], set[tuple[int, int]]] = {key: set() for key in self.maps}

        def connect(source: tuple[int, int], destination: tuple[int, int]) -> None:
            if source not in adjacent or destination not in adjacent:
                return
            adjacent[source].add(destination)
            adjacent[destination].add(source)

        for key, state in self.maps.items():
            for warp in state.warps:
                if not warp.dynamic:
                    connect(key, (warp.dest_group, warp.dest_number))
            for connection in state.connections:
                connect(key, (connection.dest_group, connection.dest_number))
        for key, rows in self._elevators.items():
            for row in rows:
                connect(key, row[4])
        for source, routes in self._ferries.items():
            for route in routes:
                connect(source.map_key, route.destination.map_key)

        # Fly only lands on ROM destination maps.  Connecting those destinations
        # to every launchable outdoor map is deliberately broader than live RAM.
        fly_destinations = {
            destination.map_key
            for destination in fly.DESTINATIONS
            if destination.map_key in adjacent
        }
        launch_maps = {
            key for key, state in self.maps.items() if state.identity.map_type in fly.FLY_MAP_TYPES
        }
        for source in launch_maps:
            for destination in fly_destinations:
                connect(source, destination)
        self._map_transition_cache = {key: frozenset(values) for key, values in adjacent.items()}
        return self._map_transition_cache

    def map_route(
        self, start: tuple[int, int], target: tuple[int, int]
    ) -> tuple[tuple[int, int], ...]:
        """Return one shortest ROM-transition corridor, ignoring within-map geometry."""
        adjacent = self._map_transition_overapprox()
        if start not in adjacent or target not in adjacent:
            return ()
        parent: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
        pending = deque((start,))
        while pending:
            current = pending.popleft()
            if current == target:
                route = []
                while current is not None:
                    route.append(current)
                    current = parent[current]
                return tuple(reversed(route))
            for destination in sorted(adjacent[current]):
                if destination not in parent:
                    parent[destination] = current
                    pending.append(destination)
        return ()

    def _state_for(
        self,
        map_key: tuple[int, int],
        capabilities: CapabilityState,
        live: MapState | None = None,
    ) -> MapState | None:
        state = self.state(map_key, live)
        if state is None or not capabilities.flags_known:
            return state
        rows = self._on_load_metatiles.get(map_key, ())
        if not rows:
            return state
        # A live current-map grid already contains the layout drawn by the last map load or
        # switch press.  Planning a hypothetical toggle must nevertheless be able to restore
        # the clear layout: use the ROM map as the baseline for every ON_LOAD tile, then overlay
        # the branch selected by the hypothetical flag state.  Keep all non-ON_LOAD live facts
        # (objects, player, dynamic map state) intact.
        baseline = self.maps.get(map_key)
        alternatives: dict[tuple[int, int], list[tuple[tuple[int, ...], int, int]]] = defaultdict(
            list
        )
        for flags, command in rows:
            if isinstance(command, Effect):
                xy = (command.value("x"), command.value("y"))
                metatile = command.value("metatile")
                collision = command.value("collision")
            else:
                xy = (command.u16(), command.u16(2))
                metatile = command.u16(4)
                collision = command.u16(6)
            alternatives[xy].append((flags, metatile, collision))
        changes = {}
        for xy, choices in alternatives.items():
            selected = next(
                (
                    (metatile, collision)
                    for flags, metatile, collision in choices
                    if all(flag in capabilities.flags for flag in flags)
                ),
                None,
            )
            if selected is not None:
                changes[xy] = selected
            elif baseline is not None and baseline.in_grid(*xy):
                block = baseline.raw_block(*xy)
                changes[xy] = (
                    block & K.MAPGRID_METATILE_ID_MASK,
                    (block & K.MAPGRID_COLLISION_MASK) >> K.MAPGRID_COLLISION_SHIFT,
                )
        if not changes:
            return state
        return self._apply_setmetatiles(state, changes)

    @staticmethod
    def _apply_setmetatiles(
        state: MapState, changes: dict[tuple[int, int], tuple[int, int]]
    ) -> MapState:
        blocks = list(state.grid.blocks)
        attributes = dict(state.grid.attributes)
        for (x, y), (metatile, impassable) in changes.items():
            if not state.in_grid(x, y):
                continue
            index = (y + K.MAP_OFFSET) * state.grid.columns + x + K.MAP_OFFSET
            elevation = blocks[index] >> K.MAPGRID_ELEVATION_SHIFT
            collision = min(impassable, K.MAPGRID_COLLISION_MASK >> K.MAPGRID_COLLISION_SHIFT)
            blocks[index] = (
                metatile
                | (collision << K.MAPGRID_COLLISION_SHIFT)
                | (elevation << K.MAPGRID_ELEVATION_SHIFT)
            )
            # ROM scripts occasionally use a metatile not present in the base layout.  The
            # replacement's behavior is stable for the map tile, so retain that behavior when
            # the full tileset attribute table is not in MapGrid.
            attributes.setdefault(metatile, state.attributes(x, y))
        grid = state.grid.model_copy(
            update={"blocks": tuple(blocks), "attributes": tuple(sorted(attributes.items()))}
        )
        return state.model_copy(update={"grid": grid})

    def counted_trainers(
        self, map_key: tuple[int, int], requirement_key: str
    ) -> tuple[CountedTrainer, ...]:
        """Return ROM objects counted by an ON_LOAD trainer barrier."""
        flags = tuple(
            int(key[5:], 0)
            for key in requirement_key.split("&")
            if key.startswith("flag:") and int(key[5:], 0) in self._trainers_by_flag
        )
        return tuple(trainer for flag in flags for trainer in self._trainers_by_flag.get(flag, ()))

    def route_script_unknown(self, plan: Any) -> str | None:
        """Return incomplete-script evidence encountered by a planned route."""
        sites: set[tuple[tuple[int, int], tuple[int, int]]] = set()
        for edge in plan.route:
            sites.add((edge.source.map_key, (edge.source.x, edge.source.y)))
            sites.add((edge.destination.map_key, (edge.destination.x, edge.destination.y)))
            x, y = edge.source.x, edge.source.y
            for dx, dy in edge.forced_path:
                x, y = x + dx, y + dy
                sites.add((edge.source.map_key, (x, y)))
        target = plan.target
        if target.local_id is not None:
            state = self.maps.get(target.map_key)
            obj = (
                next((row for row in state.objects if row.local_id == target.local_id), None)
                if state is not None
                else None
            )
            if obj is not None:
                sites.add((target.map_key, (obj.x, obj.y)))
        reasons = {reason for site in sites for reason in self._script_unknown_sites.get(site, ())}
        return "; ".join(sorted(reasons)) or None

    def node(self, map_key: tuple[int, int], x: int, y: int) -> NavigationNode:
        state = self.maps[map_key]
        return NavigationNode(map_key[0], map_key[1], x, y, state.elevation(x, y))

    def warp_destination(
        self, source_map: tuple[int, int], warp_index: int
    ) -> NavigationNode | None:
        state = self.maps.get(source_map)
        warp = (
            next((row for row in state.warps if row.index == warp_index), None) if state else None
        )
        if warp is None or warp.dynamic:
            return None
        return self._warp_landing(warp)

    def elevator_edges(
        self,
        map_key: tuple[int, int],
        capabilities: CapabilityState = CapabilityState(),
    ) -> tuple[NavigationEdge, ...]:
        out: list[NavigationEdge] = []
        facts = self._elevators.get(map_key, ())
        for (
            panel_x,
            panel_y,
            panel_index,
            entry,
            floor_key,
            x,
            y,
            required_flag,
            floor_var,
            floor_value,
            warp_index,
        ) in facts:
            floor = self.maps.get(floor_key)
            if floor is None:
                continue
            landing = self._walkable_landing(floor, x, y)
            needs = (
                (
                    requirement(
                        f"flag:{required_flag}",
                        capabilities,
                        label=f"Lift Key permission flag 0x{required_flag:X}",
                    ),
                )
                if required_flag is not None
                else ()
            )
            for direction in DIRECTIONS:
                dx, dy = VECTOR[direction]
                source_x, source_y = panel_x - dx, panel_y - dy
                if not self._plain_walkable(self.maps[map_key], source_x, source_y):
                    continue
                out.append(
                    NavigationEdge(
                        stable_key=f"elevator:{map_key[0]}:{map_key[1]}:{entry}",
                        source=self.node(map_key, source_x, source_y),
                        destination=landing,
                        kind="elevator",
                        cost=8,
                        requirements=needs,
                        direction=direction,
                        executor_target=(
                            ("kind", "elevator"),
                            ("floor_map", floor_key),
                            ("entry", entry),
                            ("panel_bg", panel_index),
                            ("panel_at", (panel_x, panel_y)),
                            ("required_flag", required_flag or 0),
                            ("floor_var", floor_var),
                            ("floor_value", floor_value),
                            ("warp", warp_index),
                        ),
                        prompt=True,
                    ),
                )
        return tuple(out)

    def gate_edges(
        self,
        map_key: tuple[int, int],
        capabilities: CapabilityState = CapabilityState(),
    ) -> tuple[NavigationEdge, ...]:
        """ROM script gates as prompt/action edges.

        FireRed's Silph doors share a script which checks the Card Key acquisition flag and then
        changes metatiles. The first checked flag is the per-door open state. When it is set the
        gate has no prerequisite; otherwise the edge reports the Card Key from live bag/flag data.
        """
        out: list[NavigationEdge] = []
        state = self.maps.get(map_key)
        if state is None:
            return ()
        for x, y, opened_flag, bg_index in self._gates.get(map_key, ()):
            requirement_key = self._gate_requirements.get((map_key, bg_index), "card_key")
            obstacle = script_tile(
                x=x,
                y=y,
                bg_index=bg_index,
                opened_flag=opened_flag,
                requirement_key=requirement_key,
                name="scripted door",
            )
            opened = obstacle.hide_flag in capabilities.flags
            # ON_LOAD replaces every tile in an opened Card Key door with the walkable
            # metatile.  Once RAM proves that flag set, the sign is no longer an action
            # in the route: ordinary step edges must own traversal through the doorway.
            if opened:
                continue
            label, equivalents = self._requirement_context(requirement_key)
            needs = (
                requirement(
                    obstacle.kind.requirement_key,
                    capabilities,
                    label=label,
                    equivalent_keys=equivalents,
                ),
            )
            for direction in DIRECTIONS:
                dx, dy = VECTOR[direction]
                sx, sy = x - dx, y - dy
                tx, ty = x + dx, y + dy
                if not (
                    self._plain_walkable(state, sx, sy) and self._plain_walkable(state, tx, ty)
                ):
                    continue
                out.append(
                    NavigationEdge(
                        stable_key=f"gate:{map_key[0]}:{map_key[1]}:{bg_index}:{direction}",
                        source=self.node(map_key, sx, sy),
                        destination=self.node(map_key, tx, ty),
                        kind="gate",
                        cost=2,
                        requirements=needs,
                        direction=direction,
                        executor_target=(
                            ("kind", "obstacle"),
                            ("role", obstacle.kind.role),
                            ("source", obstacle.source),
                            ("bg", bg_index),
                            ("at", (x, y)),
                            ("completion_flag", opened_flag),
                        ),
                        prompt=not opened,
                    )
                )
        return tuple(out)

    def edges_from(
        self,
        node: NavigationNode,
        capabilities: CapabilityState = CapabilityState(),
        *,
        live: MapState | None = None,
        include_unmet: bool = False,
    ) -> tuple[NavigationEdge, ...]:
        state = self._state_for(node.map_key, capabilities, live)
        if state is None or not self._inside(state, node.x, node.y):
            return ()
        out: list[NavigationEdge] = []
        out.extend(
            edge
            for edge in self._landing_recovery_edges(state, node, capabilities)
            if include_unmet or edge.available
        )
        out.extend(
            edge
            for edge in self._boulder_plan_edges(node, state, capabilities, live=live)
            if include_unmet or edge.available
        )
        out.extend(
            edge
            for edge in self.elevator_edges(node.map_key, capabilities)
            if edge.source == node and (include_unmet or edge.available)
        )
        out.extend(
            edge
            for route in self._ferries.get(node, ())
            if (edge := self._ferry_edge(route, capabilities)) is not None
            and (include_unmet or edge.available)
        )
        out.extend(
            edge
            for edge in self.gate_edges(node.map_key, capabilities)
            if edge.source == node and (include_unmet or edge.available)
        )
        out.extend(
            edge
            for edge in self._script_gate_edges(node, state, capabilities)
            if include_unmet or edge.available
        )
        out.extend(self._toggle_edges(node, state, capabilities))
        out.extend(self._fly_edges(node, state, capabilities))
        for direction in DIRECTIONS:
            edge = self._step_edge(state, node, direction, capabilities, live=live)
            if edge is not None and (include_unmet or edge.available):
                out.append(edge)
        return tuple(out)

    def _ferry_edge(
        self, route: FerryRoute, capabilities: CapabilityState
    ) -> NavigationEdge | None:
        requirements = []
        for guard in route.guards:
            if guard.kind == "var" and guard.subject >= 0x8000 and guard.subject != 0x800D:
                return None
            requirement_row = self._requirement_from_guard(guard, capabilities)
            if requirement_row is None:
                return None
            if guard.kind == "item" and (name := self._item_names.get(guard.subject)):
                requirement_row = replace(requirement_row, label=name)
            requirements.append(requirement_row)
        target: list[tuple[str, object]] = [
            ("kind", "ferry"),
            ("selector", route.selector),
            ("dest", route.destination.map_key),
        ]
        if route.local_id is not None:
            target.append(("local_id", route.local_id))
        if route.coord is not None:
            target.append(("coord", route.coord))
        if route.menu_index is not None:
            target.append(("menu_index", route.menu_index))
        if route.menu_id is not None:
            target.append(("menu_id", route.menu_id))
        if route.menu_pc is not None:
            target.append(("menu_pc", route.menu_pc))
        if route.menu_pcs:
            target.append(("menu_pcs", route.menu_pcs))
        return NavigationEdge(
            stable_key=(
                f"ferry:{route.source.map_group}:{route.source.map_number}:"
                f"{route.local_id if route.local_id is not None else 'coord'}:{route.selector}"
            ),
            source=route.source,
            destination=route.destination,
            kind="ferry",
            cost=2,
            requirements=tuple(requirements),
            direction=route.direction,
            executor_target=tuple(target),
            prompt=route.local_id is not None,
        )

    @staticmethod
    def _requirement_from_guard(
        guard: Guard, capabilities: CapabilityState
    ) -> NavigationRequirement | None:
        if guard.kind == "var":
            if guard.subject == 0x800D:
                actual = capabilities.quest_log_state
                if actual is None:
                    return None
            else:
                actual = dict(capabilities.vars).get(guard.subject, 0)
            expected = int(guard.value)
            met = {
                "lt": actual < expected,
                "eq": actual == expected,
                "gt": actual > expected,
                "le": actual <= expected,
                "ge": actual >= expected,
                "ne": actual != expected,
            }.get(guard.operator)
            if met is None or (guard.subject != 0x800D and not capabilities.vars_known):
                return None
            return NavigationRequirement(
                f"var:{guard.subject}:{guard.operator}:{expected}",
                (
                    f"quest log state {guard.operator} {expected} (RAM: {actual})"
                    if guard.subject == 0x800D
                    else (
                        f"story var 0x{guard.subject:X} {guard.operator} {expected} (RAM: {actual})"
                    )
                ),
                met,
                "var",
            )
        if guard.kind in {"flag", "badge", "trainer"}:
            flag = 0x500 + guard.subject if guard.kind == "trainer" else guard.subject
            if (
                guard.operator != "eq"
                or not isinstance(guard.value, bool)
                or not capabilities.flags_known
            ):
                return None
            met = (flag in capabilities.flags) is guard.value
            return NavigationRequirement(
                f"flag:{flag}:{guard.value}",
                f"flag 0x{flag:X} {'set' if guard.value else 'clear'}",
                met,
                "flag",
            )
        if guard.kind == "item" and guard.operator in {"ge", "gt"}:
            threshold = int(guard.value) + (guard.operator == "gt")
            if threshold != 1:
                return None
            return NavigationRequirement(
                f"item:{guard.subject}",
                f"item {guard.subject}",
                guard.subject in capabilities.items,
                "bag",
            )
        return None

    def _landing_recovery_edges(
        self, state: MapState, node: NavigationNode, capabilities: CapabilityState
    ) -> tuple[NavigationEdge, ...]:
        paths = self._on_frame_landings.get((state.identity.group, state.identity.number), ())
        if not paths:
            return ()
        inbound_landing_ids = {
            inbound.dest_warp_id
            for _source_map, inbound in self._reverse_warps.get(
                (state.identity.group, state.identity.number), ()
            )
        }
        if not any(
            warp.index in inbound_landing_ids and warp.x == node.x and warp.y == node.y
            for warp in state.warps
        ):
            return ()
        if self.provider.behavior_info(state.behavior(node.x, node.y)).kind == "door":
            return ()
        edges: list[NavigationEdge] = []
        for index, (var, value, path) in enumerate(paths):
            positions = tuple(
                (
                    node.x + sum(step[0] for step in path[:i]),
                    node.y + sum(step[1] for step in path[:i]),
                )
                for i in range(1, len(path) + 1)
            )
            if not all(self._inside(state, x, y) for x, y in positions):
                continue
            if (
                self._plain_walkable(state, node.x, node.y)
                and len(set(path)) == 1
                and all(self._plain_walkable(state, x, y) for x, y in positions)
            ):
                continue
            dx = sum(step[0] for step in path)
            dy = sum(step[1] for step in path)
            destination = self.node(
                (state.identity.group, state.identity.number), node.x + dx, node.y + dy
            )
            forced_path = positions
            direction = None
            if len(set(path)) == 1:
                direction = {(0, -1): "N", (0, 1): "S", (-1, 0): "W", (1, 0): "E"}[path[0]]
            edges.append(
                NavigationEdge(
                    f"landing:{state.identity.group}:{state.identity.number}:{node.x}:{node.y}:{index}",
                    node,
                    destination,
                    "forced",
                    len(path),
                    requirements=(requirement(f"var:{var}:{value}", capabilities),),
                    direction=direction,
                    forced_path=forced_path,
                )
            )
        return tuple(edges)

    def _live_open_tile(
        self,
        state: MapState,
        live: MapState | None,
        map_key: tuple[int, int],
        x: int,
        y: int,
    ) -> bool:
        """Whether RAM proves this local tile open on the actual current grid.

        ``_state_for`` may overlay a modeled ON_LOAD branch onto a copy of the live state
        for toggle planning.  Only the unmodified current snapshot is local evidence: a
        static map, a modeled copy, an unverified map header or an unreadable tile keeps
        the static barrier requirement.
        """
        if live is None or state is not live:
            return False
        if not live.identity.identity_verified:
            return False
        if (live.identity.group, live.identity.number) != map_key:
            return False
        player = live.player
        if player is None or not player.position_consistent:
            return False
        if not live.in_grid(x, y):
            return False
        block = live.raw_block(x, y)
        metatile = block & K.MAPGRID_METATILE_ID_MASK
        if metatile == K.MAPGRID_UNDEFINED or block & K.MAPGRID_COLLISION_MASK:
            return False
        if metatile not in live.grid.attribute_map:
            return False
        behavior = live.attributes(x, y) & K.ATTR_BEHAVIOR_MASK
        return self.provider.behavior_info(behavior).kind != "unknown"

    def _step_edge(
        self,
        state: MapState,
        node: NavigationNode,
        direction: Facing,
        capabilities: CapabilityState,
        *,
        live: MapState | None,
    ) -> NavigationEdge | None:
        dx, dy = VECTOR[direction]
        nx, ny = node.x + dx, node.y + dy
        connection = self._connection_edge(state, node, direction, capabilities)
        if connection is not None:
            return connection
        if not self._inside(state, nx, ny):
            return None
        # Physical objects remain authoritative even when their script is also a passage gate.
        # The separate gate edge may cross a proven removable NPC, but an ordinary walk must
        # never erase an NPC merely because its script contains a guarded mutation.
        blocker = self._blocking_object(state, nx, ny, capabilities)
        if blocker is not None:
            return self._obstacle_edge(state, node, blocker, direction, capabilities)
        # Gate-crossing edges skip over coordinate-event tiles, so ordinary paths must not enter
        # them.  A live snapshot can nevertheless start on that tile after its script interrupted
        # movement; RAM then proves that stepping away is legal.
        if self._script_gate_at(state, nx, ny):
            return None
        source_info = self.provider.behavior_info(state.behavior(node.x, node.y))
        info = self.provider.behavior_info(state.behavior(nx, ny))
        if info.waterfall and direction != "N":
            return None
        barrier = self._on_load_barriers.get(node.map_key, {}).get((nx, ny))
        if (
            barrier is not None
            and not all(capabilities.has(key) for key in barrier)
            and not self._live_open_tile(state, live, node.map_key, nx, ny)
        ):
            barrier_key = "&".join(barrier)
            need = requirement(
                barrier_key,
                capabilities,
                label=self._on_load_barrier_labels.get(
                    (node.map_key, (nx, ny)), self._barrier_label(barrier_key)
                ),
            )
            return NavigationEdge(
                stable_key=f"barrier:{node.map_group}:{node.map_number}:{nx}:{ny}",
                source=node,
                destination=self.node(node.map_key, nx, ny),
                kind="gate",
                cost=2,
                requirements=(need,),
                direction=direction,
                executor_target=(
                    ("kind", "obstacle"),
                    ("role", "script_barrier"),
                    ("at", (nx, ny)),
                    ("requirement", barrier_key),
                    ("on_load", True),
                ),
            )
        warp = self._warp_at(state, nx, ny, node.elevation)
        opening = self._warp_opening(state, warp, capabilities) if warp is not None else None
        if opening is not None:
            effect, opening_requirements = opening
            opened_info = self.provider.behavior_info(effect.value("behavior"))
            if opened_info.warp_trigger == "step":
                edge = self._warp_edge(state, node, warp, direction, capabilities)
                if edge is not None:
                    return replace(
                        edge,
                        requirements=tuple(dict.fromkeys(opening_requirements + edge.requirements)),
                    )
        fall_tile = info.kind in ("cracked_ice", "hole")
        # A warp entry on an ordinary/MB_CAVE tile is ROM metadata, not proof that walking onto
        # the tile fires a transition.  Only a behavior that starts a step warp by itself
        # (``IsWarpMetatileBehavior``: warp door, ladder, escalator, non-anim door, Lavaridge
        # pads, warp pad, fall warp, union room) may become an automatic step edge.  A missing
        # or unknown trigger is not proof, so those rows never route (the Mansion loop was the
        # cave case of this).
        #
        # The step warp fires when the player stands on the ENTERED tile: the row must match
        # that tile's elevation (warp.elevation == tile elevation or 0), never the approach
        # node's (undefined tiles report 0 → fail-closed).  See _step_warp_at.
        step_warp = self._step_warp_at(state, nx, ny)
        if step_warp is not None:
            edge = self._warp_edge(state, node, step_warp, direction, capabilities)
            if edge is not None and fall_tile:
                return NavigationEdge(
                    edge.stable_key.replace("warp:", "fall:", 1),
                    edge.source,
                    edge.destination,
                    "fall",
                    edge.cost,
                    edge.requirements,
                    edge.direction,
                    (
                        ("kind", "fall"),
                        ("warp", step_warp.index),
                        ("dest", edge.destination.map_key),
                    ),
                )
            return edge
        if info.kind == "cracked_ice":
            return None
        source_warp = self._warp_at(state, node.x, node.y, node.elevation)
        source_info = self.provider.behavior_info(state.behavior(node.x, node.y))
        if source_warp is not None and source_info.warp_trigger in ("arrow", "stairs"):
            if source_info.warp_direction == direction:
                return self._warp_edge(state, node, source_warp, direction, capabilities)
        if direction == "N" and info.kind == "door" and warp is not None:
            return self._warp_edge(state, node, warp, direction, capabilities)
        if _walk_into_signpost(state, (nx, ny), direction):
            return None
        if self.provider.blocks_side(
            state.behavior(node.x, node.y), direction
        ) or self.provider.blocks_side(state.behavior(nx, ny), OPPOSITE[direction]):
            return None
        if info.ledge == direction:
            lx, ly = nx + dx, ny + dy
            if not self._plain_walkable(state, lx, ly) or self._blocking_object(
                state, lx, ly, capabilities
            ):
                return None
            destination = self.node(node.map_key, lx, ly)
            return NavigationEdge(
                f"ledge:{node.map_group}:{node.map_number}:{node.x}:{node.y}:{direction}",
                node,
                destination,
                "ledge",
                2,
                direction=direction,
                executor_target=(("kind", "walk"), ("step_dir", direction)),
            )
        if state.collision(nx, ny):
            return None
        elevation = state.elevation(nx, ny)
        dismount = source_info.surfable and not info.surfable and elevation == 3
        if (
            not (info.surfable and capabilities.has("surf"))
            and not dismount
            and node.elevation not in (0, 15)
            and elevation not in (0, 15, node.elevation)
        ):
            return None
        requirements: list = []
        kind = "walk"
        if info.waterfall:
            kind = "waterfall"
            requirements.extend(
                (requirement("surf", capabilities), requirement("waterfall", capabilities))
            )
        elif info.surfable:
            # Mounting is the semantic Surf action. Once on water, swimming is only the
            # walking prefix of the next executor-sized action (a landing or an exit).
            if not source_info.surfable:
                kind = "surf"
                requirements.append(requirement("surf", capabilities))
        elif dismount:
            # FireRed's CanStopSurfing accepts a collision-free elevation-3 shore even
            # though the water node is elevation 1. Preserve that state change as its own
            # semantic edge so the adapter can select the matching dismount option.
            kind = "dismount"
        if capabilities.biking and kind == "walk":
            kind = "bike"
        forced = info.forced is not None or info.ice or info.kind in ("slide", "spin", "conveyor")
        forced_path: tuple[tuple[int, int], ...] = ()
        destination = self.node(node.map_key, nx, ny)
        cost = 1
        if forced:
            traversed = self._forced_landing(state, (nx, ny), direction, capabilities)
            if traversed is None:
                return None
            landing, forced_path = traversed
            destination = self.node(node.map_key, *landing)
            kind = "forced"
            cost = max(1, len(forced_path))
        risks = self._risks_at(state, destination.x, destination.y)
        executor_target: list[tuple[str, object]] = [
            ("kind", "dismount" if dismount else "walk"),
            ("step_dir", direction),
        ]
        if dismount:
            executor_target.append(("landing", (nx, ny)))
        elif kind == "surf":
            executor_target.append(("water_body", self._water_body_id(state, nx, ny)))
        return NavigationEdge(
            stable_key=f"move:{node.map_group}:{node.map_number}:{node.x}:{node.y}:{direction}",
            source=node,
            destination=destination,
            kind=kind,
            cost=cost + (4 if "wild_encounter" in risks else 0),
            requirements=tuple(dict.fromkeys(requirements)),
            direction=direction,
            executor_target=tuple(executor_target),
            forced_path=forced_path,
            risks=risks,
        )

    def _connection_edge(
        self,
        state: MapState,
        node: NavigationNode,
        direction: Facing,
        capabilities: CapabilityState,
    ) -> NavigationEdge | None:
        w, h = state.layout.width, state.layout.height
        if not (
            (direction == "N" and node.y == 0)
            or (direction == "S" and node.y == h - 1)
            or (direction == "W" and node.x == 0)
            or (direction == "E" and node.x == w - 1)
        ):
            return None
        conn = next((row for row in state.connections if row.direction == direction), None)
        if conn is None:
            return None
        dest = self._state_for((conn.dest_group, conn.dest_number), capabilities)
        if dest is None:
            return None
        if direction == "N":
            point = (node.x - conn.offset, dest.layout.height - 1)
        elif direction == "S":
            point = (node.x - conn.offset, 0)
        elif direction == "W":
            point = (dest.layout.width - 1, node.y - conn.offset)
        else:
            point = (0, node.y - conn.offset)
        if not self._inside(dest, *point):
            return None
        dest_info = self.provider.behavior_info(dest.behavior(*point))
        if dest.collision(*point):
            return None
        needs = ()
        kind = "connection"
        if dest_info.surfable:
            needs = (requirement("surf", capabilities),)
            kind = "surf"
        destination = self.node((conn.dest_group, conn.dest_number), *point)
        return NavigationEdge(
            f"edge:{direction}:{conn.dest_group}:{conn.dest_number}",
            node,
            destination,
            kind,
            2,
            needs,
            direction,
            (("kind", "edge"), ("edge_dir", direction), ("dest", destination.map_key)),
        )

    def _warp_edge(
        self,
        state: MapState,
        source: NavigationNode,
        warp: WarpInfo,
        direction: Facing,
        capabilities: CapabilityState,
    ) -> NavigationEdge | None:
        if warp.dynamic:
            return None
        destination = self._warp_landing(warp, capabilities)
        if destination is None:
            return None
        tile_kind = warp.tile_kind
        kind = "warp"
        flag = self._on_load_warp_flags.get(
            ((state.identity.group, state.identity.number), warp.index)
        )
        script_gate = self._script_warp_gates.get(
            ((state.identity.group, state.identity.number), warp.index)
        )
        door_gate = self._script_door_gates.get(
            ((state.identity.group, state.identity.number), warp.index)
        )
        if door_gate is not None:
            requirement_key, label, equivalent_keys = door_gate
            known = capabilities.flags_known if requirement_key.startswith("flag:") else True
            needs = (
                requirement(
                    requirement_key,
                    capabilities,
                    label=label if known else "blocked: unknown",
                    equivalent_keys=equivalent_keys,
                ),
            )
        elif script_gate is not None:
            requirement_key, switch_bg = script_gate
            known = (
                capabilities.flags_known
                if requirement_key.startswith("flag:")
                else capabilities.vars_known
            )
            if not known:
                needs = (
                    requirement(
                        requirement_key,
                        capabilities,
                        label="blocked: unknown",
                    ),
                )
            else:
                needs = (requirement(requirement_key, capabilities),)
        elif flag is None:
            needs = ()
        elif not capabilities.flags_known:
            needs = (requirement(f"flag:{flag}", capabilities, label=f"unknown (flag 0x{flag:X})"),)
        else:
            needs = (requirement(f"flag:{flag}", capabilities, label=f"flag 0x{flag:X}"),)
        executor_target: list[tuple[str, object]] = [
            ("kind", "warp"),
            ("warp", warp.index),
            ("dest", destination.map_key),
            ("dest_warp", warp.dest_warp_id),
            ("tile_kind", tile_kind or "unknown"),
        ]
        if script_gate is not None:
            executor_target.append(("switch_bg", switch_bg))
        return NavigationEdge(
            f"warp:{state.identity.group}:{state.identity.number}:{warp.index}",
            source,
            destination,
            kind,
            5,
            requirements=needs,
            direction=direction,
            executor_target=tuple(executor_target),
        )

    def _warp_opening(
        self,
        state: MapState,
        warp: WarpInfo,
        capabilities: CapabilityState,
    ) -> tuple[Effect, tuple[NavigationRequirement, ...]] | None:
        """Return one ROM-proven ON_LOAD path that opens ``warp`` and its live requirements."""
        effects = self._on_load_warp_openings.get(
            ((state.identity.group, state.identity.number), warp.index), ()
        )
        candidates: list[tuple[Effect, tuple[NavigationRequirement, ...]]] = []
        for effect in effects:
            requirements: list[NavigationRequirement] = []
            for guard in effect.guards:
                needed = self._requirement_from_guard(guard, capabilities)
                if needed is None:
                    break
                requirements.append(needed)
            else:
                candidates.append((effect, tuple(requirements)))
        met = [row for row in candidates if all(need.met for need in row[1])]
        if met:
            return met[0]
        # Multiple unmet alternatives are an OR gate. NavigationRequirement represents AND, so
        # leave that shape unknown rather than choosing a branch without ROM/RAM evidence.
        return candidates[0] if len(candidates) == 1 else None

    def _warp_landing(
        self, warp: WarpInfo, capabilities: CapabilityState = CapabilityState()
    ) -> NavigationNode | None:
        dest_key = (warp.dest_group, warp.dest_number)
        state = self._state_for(dest_key, capabilities)
        if state is None:
            return None
        target = next((row for row in state.warps if row.index == warp.dest_warp_id), None)
        if target is None:
            return None
        return self._walkable_landing(state, target.x, target.y)

    def scripted_warp_arrival(
        self,
        destination: tuple[int, int],
        destination_warp: int,
        variables: tuple[tuple[int, int], ...] = (),
    ) -> tuple[int, int] | None:
        """Return the ROM-derived endpoint of a pending ON_FRAME arrival script.

        The scene-variable guard is read from the live snapshot before the warp.  This keeps
        verification general: ordinary arrivals still use the destination warp, while a map
        whose ROM entry script owns the player gets the script's final tile.
        """
        state = self.maps.get(destination)
        if state is None:
            return None
        target = next((row for row in state.warps if row.index == destination_warp), None)
        if target is None:
            return None
        actual = dict(variables)
        for var, value, path in self._on_frame_landings.get(destination, ()):
            if (var, actual.get(var, 0)) != (var, value) or not path:
                continue
            x = target.x + sum(step[0] for step in path)
            y = target.y + sum(step[1] for step in path)
            if self._inside(state, x, y):
                return x, y
        return None

    def _walkable_landing(self, state: MapState, x: int, y: int) -> NavigationNode:
        if self._plain_walkable(state, x, y):
            return self.node((state.identity.group, state.identity.number), x, y)
        for direction in ("S", "N", "E", "W"):
            dx, dy = VECTOR[direction]
            if self._plain_walkable(state, x + dx, y + dy):
                return self.node((state.identity.group, state.identity.number), x + dx, y + dy)
        return self.node((state.identity.group, state.identity.number), x, y)

    def _script_interpretation(
        self,
        pointer: int,
        state: Any,
        *,
        local_id: int | None = None,
        trigger: tuple[int, int] | None = None,
        player_xy: tuple[int, int] | None = None,
        minimize_guards: bool = True,
        variables: tuple[tuple[int, int], ...] = (),
        guards: tuple[Guard, ...] = (),
        ferry_mode: bool = False,
    ) -> Interpretation:
        """Interpret one script once, with map-local metatile behavior context."""
        initial = list(variables)
        if local_id is not None:
            initial.append((0x800F, local_id))
        if trigger is not None:
            initial.append(trigger)
        initial_variables = tuple(sorted(initial))
        key = (
            pointer,
            (state.identity.group, state.identity.number),
            initial_variables,
            player_xy,
            minimize_guards,
            guards,
            ferry_mode,
        )
        cached = self._effect_cache.get(key)
        if cached is not None:
            return cached
        std_scripts = (
            self._profile.symbol("gStdScripts").address if self._profile is not None else None
        )
        capture = getattr(self, "_materialized", None)

        def _behavior(metatile: int) -> int | None:
            if metatile in state.grid.attribute_map:
                return state.grid.attribute_map.get(metatile, 0) & K.ATTR_BEHAVIOR_MASK
            return None

        reads: list[tuple[int, int | None]] | None = None
        if capture is not None:
            reads = []

            def metatile_behavior(metatile: int) -> int | None:
                value = _behavior(metatile)
                reads.append((metatile, value))
                return value
        else:
            metatile_behavior = _behavior
        result = interpret(
            self.rom,
            pointer,
            std_scripts_ptr=std_scripts,
            context=ScriptContext(
                player_xy=player_xy,
                initial_variables=initial_variables,
                seagallop_warps=self._seagallop_warps,
                preserve_seagallop_special_vars=ferry_mode,
                initial_guards=guards,
                metatile_behavior=metatile_behavior,
            ),
        )
        if not minimize_guards:
            self._effect_cache[key] = result
            self.script_trace_stats.add(result)
            if capture is not None and reads is not None:
                capture[key] = (result, tuple(reads))
            return result
        grouped: dict[tuple[Any, ...], list[Effect]] = defaultdict(list)
        for effect in result.effects:
            identity = (
                effect.kind,
                effect.source,
                effect.values,
                effect.decoded,
                effect.reason,
            )
            grouped[identity].append(effect)
        collapsed = []
        for effects in grouped.values():
            clauses = self._minimize_guard_clauses(effect.guards for effect in effects)
            collapsed.extend(
                replace(
                    effects[0],
                    guards=tuple(sorted(clause, key=repr)),
                )
                for clause in clauses
            )
        answer = replace(result, effects=tuple(collapsed))
        self._effect_cache[key] = answer
        self.script_trace_stats.add(answer)
        if capture is not None and reads is not None:
            capture[key] = (answer, tuple(reads))
        return answer

    def _script_effects(
        self,
        pointer: int,
        state: Any,
        *,
        reader: str,
        local_id: int | None = None,
        trigger: tuple[int, int] | None = None,
        player_xy: tuple[int, int] | None = None,
        site: tuple[int, int] | None = None,
    ) -> tuple[Effect, ...]:
        result = self._script_interpretation(
            pointer,
            state,
            local_id=local_id,
            trigger=trigger,
            player_xy=player_xy,
        )
        return self._complete_effects(reader, result, state=state, site=site)

    def _complete_effects(
        self,
        reader: str,
        result: Interpretation,
        *,
        state: Any,
        site: tuple[int, int] | None = None,
    ) -> tuple[Effect, ...]:
        """Return facts only from complete interpretations and audit every omission."""
        if result.complete:
            return result.effects
        map_key = (state.identity.group, state.identity.number)
        audit_key = (reader, map_key, site, result.script_ptr)
        if audit_key not in self._script_incomplete_seen:
            self._script_incomplete_seen.add(audit_key)
            self.script_incomplete_counts[reader] += 1
        reasons = tuple(
            dict.fromkeys(
                (
                    *result.unknown,
                    *(
                        effect.reason or f"undecoded {effect.kind} at {effect.source:#x}"
                        for effect in result.effects
                        if not effect.decoded
                    ),
                )
            )
        )
        audit = getattr(self, "_materialized_audit", None)
        if audit is not None:
            audit.append(AuditCall(reader, map_key, site, result.script_ptr, reasons))
        if site is not None:
            reason = "; ".join(reasons) or "incomplete interpretation"
            self._script_unknown_sites[(map_key, site)].add(
                f"script {result.script_ptr:#x}: {reason}"
            )
        return ()

    @staticmethod
    def _opposite_guards(first: Guard, second: Guard) -> bool:
        if (first.kind, first.subject) != (second.kind, second.subject):
            return False
        if (
            first.operator == second.operator == "eq"
            and isinstance(first.value, bool)
            and isinstance(second.value, bool)
        ):
            return first.value is not second.value
        if first.value != second.value:
            return False
        return (first.operator, second.operator) in {("eq", "ne"), ("ne", "eq")}

    @classmethod
    def _minimize_guard_clauses(
        cls, clauses: Iterable[tuple[Guard, ...]]
    ) -> tuple[frozenset[Guard], ...]:
        """Reduce path-expanded DNF without inventing facts or dropping alternatives."""
        terms = {frozenset(clause) for clause in clauses}
        changed = True
        while changed:
            changed = False
            terms = {term for term in terms if not any(other < term for other in terms)}
            additions: set[frozenset[Guard]] = set()
            rows = tuple(terms)
            for index, first in enumerate(rows):
                for second in rows[index + 1 :]:
                    left, right = first - second, second - first
                    if len(left) == len(right) == 1 and cls._opposite_guards(
                        next(iter(left)), next(iter(right))
                    ):
                        additions.add(first & second)
            if additions - terms:
                terms.update(additions)
                changed = True
        return tuple(sorted(terms, key=lambda term: (len(term), repr(sorted(term, key=repr)))))

    def _map_effects(
        self, key: tuple[int, int], script_type: int, *, reader: str
    ) -> tuple[Effect, ...]:
        if self._profile is None or key not in self.maps:
            return ()
        pointer = script_table(self.rom, header_scripts_ptr(self.rom, self._profile, *key)).get(
            script_type
        )
        if pointer is None:
            return ()
        return self._script_effects(pointer, self.maps[key], reader=reader)

    @staticmethod
    def _guard_requirement(
        guard: Guard, *, inverse: bool = False, allow_var: bool = False
    ) -> str | None:
        """Translate a symbolic guard into the positive fact that clears a gate."""
        positive = guard.value
        if guard.kind in {"flag", "badge"}:
            if guard.operator != "eq" or not isinstance(positive, bool):
                return None
            if inverse:
                positive = not positive
            return f"flag:{guard.subject}" if positive and guard.subject >= 0x20 else None
        if guard.kind == "trainer":
            if guard.operator != "eq" or not isinstance(positive, bool):
                return None
            if inverse:
                positive = not positive
            return f"flag:{0x500 + guard.subject}" if positive else None
        if guard.kind == "item":
            present = guard.operator in {"eq", "ge", "gt"} and bool(guard.value)
            if inverse:
                present = not present
            return f"item:{guard.subject}" if present else None
        if guard.kind == "hm":
            if guard.operator != "eq" or not isinstance(positive, bool):
                return None
            if inverse:
                positive = not positive
            return _HM_REQUIREMENTS.get(guard.subject) if positive else None
        if guard.kind == "var":
            if not allow_var:
                return None
            operator = guard.operator
            if inverse:
                operator = {"eq": "ne", "ne": "eq"}.get(operator, "")
            # Navigation requirements represent positive RAM facts.  Preserve an equality only
            # when it is the script's actual condition (or the exact inverse of ``!=``).  Other
            # comparisons cannot be represented by CapabilityState and remain unmodelled.
            if operator == "eq" and isinstance(guard.value, int):
                return f"var:{guard.subject}:{guard.value}"
            return None
        return None

    @classmethod
    def _positive_requirement(cls, guards: tuple[Guard, ...]) -> str | None:
        keys = {key for guard in guards if (key := cls._guard_requirement(guard)) is not None}
        return "&".join(sorted(keys)) if keys else None

    @classmethod
    def _clearing_requirement(cls, guards: tuple[Guard, ...]) -> str | None:
        keys = {
            key
            for guard in guards
            if (key := cls._guard_requirement(guard, inverse=True)) is not None
        }
        return "&".join(sorted(keys)) if keys else None

    def _effect_on_load_metatiles(
        self,
    ) -> dict[tuple[int, int], tuple[tuple[tuple[int, ...], Effect], ...]]:
        result: dict[tuple[int, int], tuple[tuple[tuple[int, ...], Effect], ...]] = {}
        for key in self.maps:
            rows = {
                (
                    tuple(
                        sorted(
                            0x500 + guard.subject if guard.kind == "trainer" else guard.subject
                            for guard in effect.guards
                            if guard.kind in {"flag", "badge", "trainer"}
                            and guard.operator == "eq"
                            and guard.value is True
                        )
                    ),
                    effect,
                )
                for effect in self._map_effects(key, 1, reader="on_load_metatiles:on_load")
                if effect.kind == "setmetatile"
                and any(
                    guard.kind in {"flag", "badge", "trainer"}
                    and guard.operator == "eq"
                    and guard.value is True
                    for guard in effect.guards
                )
            }
            if rows:
                result[key] = tuple(sorted(rows, key=lambda row: (row[0], row[1].source)))
        return result

    def _effect_on_load_barriers(
        self,
    ) -> dict[tuple[int, int], dict[tuple[int, int], tuple[str, ...]]]:
        result: dict[tuple[int, int], dict[tuple[int, int], tuple[str, ...]]] = {}
        for key, state in self.maps.items():
            options: dict[tuple[int, int], set[str]] = defaultdict(set)
            for effect in self._map_effects(key, 1, reader="on_load_barriers:on_load"):
                if effect.kind != "setmetatile":
                    continue
                xy = (effect.value("x"), effect.value("y"))
                if not state.in_grid(*xy) or effect.value("collision") <= state.collision(*xy):
                    continue
                for guard in effect.guards:
                    needed = self._guard_requirement(guard, inverse=True, allow_var=True)
                    if needed is not None:
                        options[xy].add(needed)
            barriers = {
                xy: tuple(sorted(requirements))
                for xy, requirements in options.items()
                if requirements
            }
            if barriers:
                result[key] = barriers
        return result

    def _effect_on_load_warp_openings(
        self,
    ) -> dict[tuple[tuple[int, int], int], tuple[Effect, ...]]:
        """Index guarded ON_LOAD changes that turn a blocked warp tile into a real warp."""
        result: dict[tuple[tuple[int, int], int], tuple[Effect, ...]] = {}
        for key, state in self.maps.items():
            openings: dict[tuple[int, int], list[Effect]] = defaultdict(list)
            for effect in self._map_effects(key, 1, reader="on_load_barriers:on_load"):
                if effect.kind != "setmetatile":
                    continue
                xy = effect.value("x"), effect.value("y")
                behavior = effect.value("behavior")
                if (
                    not state.in_grid(*xy)
                    or not isinstance(behavior, int)
                    or effect.value("collision") >= state.collision(*xy)
                    or self.provider.behavior_info(behavior).warp_trigger is None
                ):
                    continue
                openings[xy].append(effect)
            for warp in state.warps:
                effects = openings.get((warp.x, warp.y))
                if not warp.dynamic and effects:
                    result[(key, warp.index)] = tuple(effects)
        return result

    def _effect_on_load_barrier_labels(
        self,
    ) -> dict[tuple[tuple[int, int], tuple[int, int]], str]:
        """Explain trainer-count barriers solely from interpreted ON_LOAD effects."""
        result: dict[tuple[tuple[int, int], tuple[int, int]], str] = {}
        for map_key, barriers in self._on_load_barriers.items():
            effects = self._map_effects(map_key, 1, reader="on_load_barrier_labels:on_load")
            for xy, requirements in barriers.items():
                flags = tuple(
                    sorted(
                        int(key[5:])
                        for key in requirements
                        if key.startswith("flag:") and int(key[5:]) >= 0x500
                    )
                )
                trainers = {flag - 0x500 for flag in flags}
                if not trainers or len(trainers) != len(requirements):
                    continue
                for initial in effects:
                    if (
                        initial.kind != "setvar"
                        or initial.guards
                        or initial.value("value") != 0
                        or not isinstance(initial.value("var"), int)
                    ):
                        continue
                    var = initial.value("var")
                    increments = tuple(
                        effect
                        for effect in effects
                        if effect.kind == "addvar"
                        and effect.value("var") == var
                        and effect.value("operand") == 1
                        and isinstance(effect.value("value"), int)
                    )
                    counted = {
                        guard.subject
                        for effect in increments
                        for guard in effect.guards
                        if guard.kind == "trainer"
                        and guard.operator == "eq"
                        and guard.value is True
                    }
                    values = {effect.value("value") for effect in increments}
                    target = len(trainers)
                    if counted != trainers or values != set(range(1, target + 1)):
                        continue
                    flag_text = ", ".join(f"0x{flag:X}" for flag in flags)
                    result[(map_key, xy)] = (
                        f"ON_LOAD barrier requires {target} counted trainers defeated "
                        f"(RAM var 0x{var:X} = {target}; trainer flags {flag_text})"
                    )
                    break
        return result

    @staticmethod
    def _barrier_label(key: str) -> str:
        if "&" in key:
            return " and ".join(NavigationGraph._barrier_label(part) for part in key.split("&"))
        if key.startswith("flag:"):
            return f"required flag 0x{int(key[5:]):X}"
        return key.replace(":", " ")

    @staticmethod
    def _coordinate_completion_requirement(
        effects: tuple[Effect, ...], trigger_var: int | None, trigger_value: int | None
    ) -> str | None:
        """Name a battle event's durable state after it disables its own trigger."""
        if trigger_var is None or trigger_value is None:
            return None
        battle_paths = Counter(
            effect.guards for effect in effects if effect.kind == "trainerbattle"
        )
        battle_guards = {guards for guards, count in battle_paths.items() if count >= 2}
        removal_guards = {
            effect.guards
            for effect in effects
            if effect.kind in {"removeobject", "removeobjectat", "hideobjectat"}
        }
        values = {
            effect.value("value")
            for effect in effects
            if effect.kind == "setvar"
            and effect.value("var") == trigger_var
            and effect.value("value") != trigger_value
            and effect.guards in battle_guards
            and effect.guards in removal_guards
            and isinstance(effect.value("value"), int)
        }
        # Scene variables are monotonic progress counters.  The event disables itself once the
        # completion value is reached, and later story states must keep the passage open.
        return f"var:{trigger_var}:ge:{next(iter(values))}" if len(values) == 1 else None

    @staticmethod
    def _coordinate_battle_is_declinable(effects: tuple[Effect, ...]) -> bool:
        """Recognize the YES/NO split from its durable VAR_RESULT control flow."""
        accepted = any(
            effect.kind == "trainerbattle"
            and any(
                guard.kind == "var"
                and guard.subject == 0x800D
                and guard.operator == "ne"
                and guard.value == 0
                for guard in effect.guards
            )
            for effect in effects
        )
        declined = any(
            any(
                guard.kind == "var"
                and guard.subject == 0x800D
                and guard.operator == "eq"
                and guard.value == 0
                for guard in effect.guards
            )
            for effect in effects
        )
        return accepted and declined

    def _effect_on_load_warp_flags(self) -> dict[tuple[tuple[int, int], int], int]:
        result: dict[tuple[tuple[int, int], int], int] = {}
        for key, state in self.maps.items():
            effects = self._map_effects(key, 1, reader="on_load_warp_flags:on_load")
            for warp in state.warps:
                if warp.dynamic or not warp.dest_name:
                    continue
                for effect in effects:
                    if effect.kind == "setmetatile" and (effect.value("x"), effect.value("y")) == (
                        warp.x,
                        warp.y,
                    ):
                        guard = next(
                            (
                                row
                                for row in effect.guards
                                if row.kind in {"flag", "badge", "trainer"}
                                and row.operator == "eq"
                                and row.value is True
                            ),
                            None,
                        )
                        if guard is not None:
                            result[(key, warp.index)] = (
                                0x500 + guard.subject if guard.kind == "trainer" else guard.subject
                            )
        return result

    @staticmethod
    def _effect_metatile_open(state: Any, effect: Effect) -> bool:
        metatile = effect.value("metatile")
        attributes = state.grid.attribute_map.get(metatile)
        if attributes is None:
            return False
        return (
            effect.value("collision") == 0
            and state.collision(effect.value("x"), effect.value("y")) == 0
            and attributes & K.ATTR_BEHAVIOR_MASK
            == state.behavior(effect.value("x"), effect.value("y"))
        )

    @staticmethod
    def _matching_set_flags(effects: tuple[Effect, ...], tile_effect: Effect) -> tuple[int, ...]:
        """Flags set on the exact same symbolic path as one metatile effect."""
        return tuple(
            effect.value("flag")
            for effect in effects
            if effect.kind == "setflag"
            and effect.value("set") is True
            and effect.guards == tile_effect.guards
        )

    def _effect_script_warp_gates(
        self,
    ) -> dict[tuple[tuple[int, int], int], tuple[str, int]]:
        gates: dict[tuple[tuple[int, int], int], tuple[str, int]] = {}
        for key, state in self.maps.items():
            closed: dict[tuple[int, int], set[str]] = defaultdict(set)
            for kind in (1, 3):
                for effect in self._map_effects(
                    key,
                    kind,
                    reader=f"warp_gates:{'on_load' if kind == 1 else 'on_transition'}",
                ):
                    if effect.kind != "setmetatile" or self._effect_metatile_open(state, effect):
                        continue
                    needed = self._clearing_requirement(effect.guards)
                    if needed is not None:
                        closed[(effect.value("x"), effect.value("y"))].add(needed)
            opened: dict[tuple[int, int], list[tuple[str, int]]] = defaultdict(list)
            for event in state.bg_events:
                if event.kind != "sign" or event.script_ptr is None:
                    continue
                effects = self._script_effects(
                    event.script_ptr,
                    state,
                    reader="warp_gates:bg",
                    site=(event.x, event.y),
                )
                for effect in effects:
                    if effect.kind != "setmetatile" or not self._effect_metatile_open(
                        state, effect
                    ):
                        continue
                    for flag in self._matching_set_flags(effects, effect):
                        key_name = f"flag:{flag}"
                        if Guard("flag", flag, "eq", False) in effect.guards:
                            opened[(effect.value("x"), effect.value("y"))].append(
                                (key_name, event.index)
                            )
            for warp in state.warps:
                links = {
                    (requirement_key, bg_index)
                    for requirement_key in closed.get((warp.x, warp.y), ())
                    for opened_key, bg_index in opened.get((warp.x, warp.y), ())
                    if opened_key == requirement_key
                }
                if links:
                    gates[(key, warp.index)] = sorted(links)[0]
        return gates

    def _effect_script_door_gates(
        self,
    ) -> dict[tuple[tuple[int, int], int], tuple[str, str, tuple[str, ...]]]:
        result = {}
        for key, state in self.maps.items():
            guarded_flags: set[int] = set()
            door_requirements: set[str] = set()
            for kind in (1, 3):
                for effect in self._map_effects(
                    key,
                    kind,
                    reader=f"door_gates:{'on_load' if kind == 1 else 'on_transition'}",
                ):
                    if effect.kind != "setvar" or not isinstance(effect.value("var"), int):
                        continue
                    needed = self._positive_requirement(effect.guards)
                    if needed is not None:
                        door_requirements.add(needed)
                    if not 0x4000 <= effect.value("var") < 0x4010:
                        continue
                    for guard in effect.guards:
                        if guard.kind == "flag" and guard.operator == "eq" and guard.value is True:
                            guarded_flags.add(guard.subject)
            guarded_warps = {
                effect.value("value"): guard.subject
                for kind in (1, 3)
                for effect in self._map_effects(
                    key,
                    kind,
                    reader=f"door_gates:{'on_load' if kind == 1 else 'on_transition'}",
                )
                if effect.kind == "setvar" and effect.value("var") == 0x4001
                for guard in effect.guards
                if guard.kind == "flag" and guard.operator == "eq" and guard.value is True
            }
            if len(guarded_flags) == 1 and guarded_warps:
                for warp in state.warps:
                    if warp.dynamic or warp.index not in guarded_warps:
                        continue
                    flag = guarded_warps[warp.index]
                    item_id, source = self._hidden_item_source(flag)
                    item_name = self._item_names.get(item_id) if item_id is not None else None
                    label = (
                        f"{item_name.upper() if item_name else f'flag 0x{flag:X}'} (flag {flag})"
                    )
                    if source:
                        label += f"; source: {source}"
                    equivalents = (f"item:{item_id}",) if item_id is not None else ()
                    result[(key, warp.index)] = (f"flag:{flag}", label, equivalents)
            for obstacle in self._script_obstacles.get(key, ()):
                requirement_key = obstacle.kind.requirement_key
                if "&" not in requirement_key or requirement_key not in door_requirements:
                    continue
                for direction in DIRECTIONS:
                    dx, dy = VECTOR[direction]
                    warp_position = (obstacle.x + dx, obstacle.y + dy)
                    warp = next(
                        (
                            candidate
                            for candidate in state.warps
                            if not candidate.dynamic and (candidate.x, candidate.y) == warp_position
                        ),
                        None,
                    )
                    if warp is None:
                        continue
                    result.setdefault(
                        (key, warp.index),
                        (
                            requirement_key,
                            obstacle.requirement_label or requirement_key,
                            obstacle.equivalent_requirements,
                        ),
                    )
        return result

    def _effect_elevators(self) -> dict[tuple[int, int], tuple[tuple[Any, ...], ...]]:
        result = {}
        for key, state in self.maps.items():
            if not any(warp.dynamic for warp in state.warps):
                continue
            rows = []
            for event in state.bg_events:
                if event.kind != "sign" or event.script_ptr is None:
                    continue
                effects = self._script_effects(
                    event.script_ptr,
                    state,
                    reader="elevators:bg",
                    site=(event.x, event.y),
                )
                for effect in effects:
                    if effect.kind != "setdynamicwarp":
                        continue
                    selector = next(
                        (
                            guard
                            for guard in effect.guards
                            if guard.kind == "var"
                            and guard.subject == 0x8000
                            and guard.operator == "eq"
                        ),
                        None,
                    )
                    if selector is None:
                        continue
                    required = next(
                        (
                            guard.subject
                            for guard in effect.guards
                            if guard.kind == "flag"
                            and guard.operator == "eq"
                            and guard.value is True
                        ),
                        None,
                    )
                    floor_set = next(
                        (
                            candidate
                            for candidate in effects
                            if candidate.kind == "setvar"
                            and candidate.value("var") == 0x403A
                            and selector in candidate.guards
                        ),
                        None,
                    )
                    floor_key = effect.value("group"), effect.value("map")
                    if floor_key not in self.maps:
                        continue
                    warp_index = next(
                        (
                            warp.index
                            for warp in state.warps
                            if warp.dynamic and warp.tile_kind == "arrow_warp"
                        ),
                        next((warp.index for warp in state.warps if warp.dynamic), 0),
                    )
                    rows.append(
                        (
                            event.x,
                            event.y,
                            event.index,
                            selector.value,
                            floor_key,
                            effect.value("x"),
                            effect.value("y"),
                            required,
                            floor_set.value("var") if floor_set is not None else None,
                            floor_set.value("value") if floor_set is not None else None,
                            warp_index,
                        )
                    )
            if rows:
                result[key] = tuple(dict.fromkeys(rows))
        return result

    def _effect_script_gates(
        self,
    ) -> tuple[
        dict[tuple[int, int], tuple[tuple[int, int, int, int], ...]],
        dict[tuple[tuple[int, int], int], str],
    ]:
        gates: dict[tuple[int, int], tuple[tuple[int, int, int, int], ...]] = {}
        requirements: dict[tuple[tuple[int, int], int], str] = {}
        for key, state in self.maps.items():
            rows = []
            for event in state.bg_events:
                if event.kind != "sign" or event.script_ptr is None:
                    continue
                effects = self._script_effects(
                    event.script_ptr,
                    state,
                    reader="script_gates:bg",
                    site=(event.x, event.y),
                )
                for effect in effects:
                    if (
                        effect.kind != "setmetatile"
                        or effect.value("collision") != 0
                        or (effect.value("x"), effect.value("y")) != (event.x, event.y)
                    ):
                        continue
                    opened = next(
                        (
                            guard.subject
                            for guard in effect.guards
                            if guard.kind == "flag"
                            and guard.operator == "eq"
                            and guard.value is False
                        ),
                        None,
                    )
                    if opened is None:
                        continue
                    requirement_key = self._positive_requirement(
                        tuple(
                            guard
                            for guard in effect.guards
                            if not (guard.kind == "flag" and guard.subject == opened)
                        )
                    )
                    if requirement_key is not None:
                        rows.append((event.x, event.y, opened, event.index))
                        requirements[(key, event.index)] = requirement_key
            if rows:
                gates[key] = tuple(dict.fromkeys(rows))
        return gates, requirements

    def _effect_flag_items(self) -> dict[int, int]:
        pairs: dict[int, int] = {}
        for state in self.maps.values():
            for kind, events in (
                ("object", state.objects),
                ("coord", state.coord_events),
                ("bg", state.bg_events),
            ):
                for event in events:
                    if event.script_ptr is None:
                        continue
                    effects = self._script_effects(
                        event.script_ptr,
                        state,
                        reader=f"flag_items:{kind}",
                        local_id=getattr(event, "local_id", None),
                        site=(event.x, event.y),
                    )
                    gifts = tuple(
                        effect
                        for effect in effects
                        if effect.kind == "additem" and isinstance(effect.value("item"), int)
                    )
                    flags = tuple(
                        effect
                        for effect in effects
                        if effect.kind == "setflag"
                        and effect.value("set") is True
                        and isinstance(effect.value("flag"), int)
                    )
                    hidden_flag = getattr(event, "hidden_flag_id", 0)
                    if hidden_flag and len({gift.value("item") for gift in gifts}) == 1:
                        pairs.setdefault(hidden_flag, gifts[0].value("item"))
                    for flag in flags:
                        matching = {
                            gift.value("item")
                            for gift in gifts
                            if set(gift.guards) <= set(flag.guards)
                        }
                        if len(matching) == 1:
                            pairs.setdefault(flag.value("flag"), next(iter(matching)))
        return pairs

    def _effect_script_toggles(self) -> dict[tuple[int, int], tuple[ScriptToggle, ...]]:
        discovered: dict[tuple[int, int], list[ScriptToggle]] = defaultdict(list)
        for key, state in self.maps.items():
            for event in state.bg_events:
                if event.script_ptr is None:
                    continue
                flags = {
                    effect.value("flag")
                    for effect in self._script_effects(
                        event.script_ptr,
                        state,
                        reader="toggles:bg",
                        site=(event.x, event.y),
                    )
                    if effect.kind == "setflag" and effect.value("set") is True
                }
                for flag in sorted(flags & self._stateful_flags):
                    discovered[key].append(
                        ScriptToggle(event.x, event.y, flag, event.sign_facing, event.index)
                    )
            for obj in state.objects:
                if obj.script_ptr is None:
                    continue
                flags = {
                    effect.value("flag")
                    for effect in self._script_effects(
                        obj.script_ptr,
                        state,
                        reader="toggles:object",
                        local_id=obj.local_id,
                        site=(obj.x, obj.y),
                    )
                    if effect.kind == "setflag" and effect.value("set") is True
                }
                for flag in sorted(flags & self._stateful_flags):
                    discovered[key].append(
                        ScriptToggle(obj.x, obj.y, flag, None, obj.local_id, obj.local_id)
                    )
        return {key: tuple(dict.fromkeys(rows)) for key, rows in discovered.items()}

    def _effect_ferries(self) -> dict[NavigationNode, tuple[FerryRoute, ...]]:
        """Turn ROM-scripted Seagallop calls into guarded interaction/trigger edges."""
        discovered: dict[NavigationNode, list[FerryRoute]] = defaultdict(list)
        for map_key, state in self.maps.items():
            candidates = [
                (obj.script_ptr, obj.local_id, None, None, (obj.x, obj.y), "npc")
                for obj in state.objects
                if obj.role == "npc" and obj.script_ptr is not None
            ]
            candidates.extend(
                (
                    event.script_ptr,
                    None,
                    event.var_id,
                    event.var_value,
                    (event.x, event.y),
                    "coord",
                )
                for event in state.coord_events
                if not event.weather_only and event.script_ptr is not None
            )
            for pointer, local_id, trigger_var, trigger_value, at, kind in candidates:
                result = self._script_interpretation(
                    pointer,
                    state,
                    local_id=local_id,
                    trigger=(trigger_var, trigger_value)
                    if trigger_var is not None and trigger_value is not None
                    else None,
                    player_xy=at if local_id is None else None,
                    minimize_guards=False,
                    ferry_mode=True,
                )
                effects = list(
                    self._complete_effects(f"ferries:{kind}", result, state=state, site=at)
                )
                # The Rainbow Pass menu loops between two native-built pages.  Script ``switch``
                # uses VAR_0x8000 as scratch on both pages, so interpreting the loop as one SSA
                # path would make page-two choices contradict page one's historical guard.  The
                # page-two entrypoint and the starting-port selector are themselves ROM script
                # effects; restart there with that decoded selector and retain no guessed rows.
                page_two = {
                    effect
                    for effect in effects
                    if effect.kind == "setvar"
                    and effect.value("var") == 0x8005
                    and effect.value("value") == 1
                }
                origins = {
                    effect.value("value")
                    for effect in effects
                    if effect.kind == "setvar"
                    and effect.value("var") == 0x8004
                    and isinstance(effect.value("value"), int)
                }
                for page_entry in page_two:
                    for origin in origins:
                        entry_guards = tuple(
                            guard
                            for guard in page_entry.guards
                            if not (guard.kind == "var" and guard.subject == 0x8000)
                        )
                        page_result = self._script_interpretation(
                            page_entry.source,
                            state,
                            local_id=local_id,
                            minimize_guards=False,
                            variables=((0x8004, origin),),
                            guards=entry_guards,
                            ferry_mode=True,
                        )
                        effects.extend(
                            self._complete_effects(
                                f"ferries:{kind}", page_result, state=state, site=at
                            )
                        )
                for effect in effects:
                    selector = effect.value("selector")
                    destination_state = self.maps.get((effect.value("group"), effect.value("map")))
                    if (
                        effect.kind != "warp"
                        or effect.value("special") != 379
                        or not isinstance(selector, int)
                        or destination_state is None
                        or not isinstance(effect.value("x"), int)
                        or not isinstance(effect.value("y"), int)
                    ):
                        continue
                    destination = self._walkable_landing(
                        destination_state, effect.value("x"), effect.value("y")
                    )
                    menu_index = next(
                        (
                            int(guard.value)
                            for guard in effect.guards
                            if guard.kind == "var"
                            and guard.subject == 0x8000
                            and guard.operator == "eq"
                            and isinstance(guard.value, int)
                            and any(
                                condition.kind == "var"
                                and condition.subject == 0x4076
                                and condition.operator == "lt"
                                and condition.value == 5
                                for condition in effect.guards
                            )
                        ),
                        None,
                    )
                    scratch_guards = tuple(
                        guard
                        for guard in effect.guards
                        if guard.kind == "var" and guard.subject >= 0x8000
                    )
                    # VAR_0x8000 is the switch scratch used to select a menu row.  VAR_RESULT's
                    # quest-log comparison is retained and checked against live UI RAM.  Any
                    # other transient condition has no live capability fact and suppresses the
                    # edge instead of being silently assumed true.
                    if any(
                        guard.subject != 0x8000
                        and not (
                            guard.subject == 0x800D and guard.operator == "ne" and guard.value == 2
                        )
                        for guard in scratch_guards
                    ):
                        continue
                    guards = tuple(
                        guard
                        for guard in effect.guards
                        if not (guard.kind == "var" and guard.subject == 0x8000)
                    )
                    if trigger_var is not None and trigger_value is not None:
                        guards = (*guards, Guard("var", trigger_var, "eq", trigger_value))
                    menu_id = menu_pc = None
                    menu_pcs: tuple[tuple[int, int], ...] = ()
                    if menu_index is not None:
                        durable = frozenset(
                            guard
                            for guard in guards
                            if not (guard.kind == "var" and guard.subject >= 0x8000)
                        )
                        menu_effect = next(
                            (
                                candidate
                                for candidate in effects
                                if candidate.kind
                                in {"multichoice", "multichoicedefault", "multichoicegrid"}
                                and isinstance(candidate.value("menu_id"), int)
                                and frozenset(
                                    guard
                                    for guard in candidate.guards
                                    if not (guard.kind == "var" and guard.subject >= 0x8000)
                                )
                                == durable
                            ),
                            None,
                        )
                        if menu_effect is not None:
                            menu_id = menu_effect.value("menu_id")
                            menu_pc = ROM_BASE + menu_effect.source + 5
                    elif any(
                        guard.kind == "var"
                        and guard.subject == 0x4076
                        and guard.operator == "ge"
                        and guard.value == 5
                        for guard in guards
                    ):
                        # DrawSeagallopDestinationMenu creates the generic native menu task.
                        menu_id = 0xFF
                        origin = next(iter(origins), None)
                        page = (
                            1
                            if selector >= 5
                            or (selector == 4 and isinstance(origin, int) and origin >= 5)
                            else 0
                        )
                        durable = frozenset(
                            guard
                            for guard in guards
                            if not (guard.kind == "var" and guard.subject >= 0x8000)
                        )
                        menu_pcs = tuple(
                            sorted(
                                {
                                    (candidate.value("page"), ROM_BASE + candidate.source + 4)
                                    for candidate in effects
                                    if candidate.value("special") == 423
                                    and candidate.value("page") in {0, 1}
                                    and frozenset(
                                        guard
                                        for guard in candidate.guards
                                        if not (guard.kind == "var" and guard.subject >= 0x8000)
                                    )
                                    == durable
                                }
                            )
                        )
                        menu_pc = dict(menu_pcs).get(page)
                    for direction in DIRECTIONS:
                        dx, dy = VECTOR[direction]
                        source_xy = (at[0] - dx, at[1] - dy)
                        if not self._plain_walkable(state, *source_xy):
                            continue
                        source = self.node(map_key, *source_xy)
                        discovered[source].append(
                            FerryRoute(
                                source,
                                destination,
                                tuple(dict.fromkeys(guards)),
                                selector,
                                local_id=local_id,
                                coord=at if local_id is None else None,
                                direction=direction,
                                menu_index=menu_index,
                                menu_id=menu_id,
                                menu_pc=menu_pc,
                                menu_pcs=menu_pcs,
                            )
                        )
        return {node: tuple(dict.fromkeys(rows)) for node, rows in discovered.items()}

    @staticmethod
    def _moves_player(effect: Effect) -> bool:
        if not effect.decoded:
            return False
        if effect.kind in {
            "warp",
            "warpsilent",
            "warpdoor",
            "warphole",
            "warpteleport",
            "warpspinenter",
        }:
            return True
        return (
            effect.kind == "applymovement"
            and effect.value("local_id") == 0xFF
            and effect.value("delta") not in {None, (0, 0)}
        )

    @classmethod
    def _prevents_step(cls, effect: Effect) -> bool:
        return cls._moves_player(effect) or (effect.decoded and effect.kind == "battle_start")

    @staticmethod
    def _opens_npc(effect: Effect, local_id: int, origin: tuple[int, int]) -> bool:
        if not effect.decoded or effect.value("local_id") not in {local_id, 0xFF}:
            return False
        if effect.kind in {"removeobject", "removeobjectat", "hideobjectat"}:
            return True
        return effect.kind == "setobjectxyperm" and (effect.value("x"), effect.value("y")) != origin

    @classmethod
    def _passage_requirement(
        cls,
        effects: tuple[Effect, ...],
        *,
        local_id: int | None,
        origin: tuple[int, int],
        trigger_var: int | None,
        trigger_value: int | None,
        transition: dict[tuple[int, int], str],
    ) -> str | None:
        """Return one fact-proven passage requirement, never a guessed selector."""
        unmet_moves = {
            needed
            for effect in effects
            if cls._moves_player(effect)
            and (needed := cls._clearing_requirement(effect.guards)) is not None
        }
        met_moves = {
            needed
            for effect in effects
            if cls._moves_player(effect)
            and (needed := cls._positive_requirement(effect.guards)) is not None
        }
        if local_id is None:
            completion = cls._coordinate_completion_requirement(effects, trigger_var, trigger_value)
            if completion is not None:
                return completion
            disables_trigger = {
                needed
                for effect in effects
                if effect.kind == "setvar"
                and effect.value("var") == trigger_var
                and effect.value("value") != trigger_value
                and (needed := cls._positive_requirement(effect.guards)) is not None
            }
            paired = unmet_moves & disables_trigger
            transition_options = (
                {
                    needed
                    for (var, value), needed in transition.items()
                    if var == trigger_var and value != trigger_value
                }
                if any(cls._prevents_step(effect) for effect in effects)
                else set()
            )
            if paired:
                options = paired
            elif transition_options:
                options = transition_options
            else:
                options = unmet_moves - met_moves
        else:
            opens = {
                needed
                for effect in effects
                if cls._opens_npc(effect, local_id, origin)
                and (needed := cls._positive_requirement(effect.guards)) is not None
            }
            options = unmet_moves & opens
            if not options:
                battles = {
                    effect.value("trainer"): frozenset(effect.guards)
                    for effect in effects
                    if effect.kind == "trainerbattle" and isinstance(effect.value("trainer"), int)
                }
                battle_openings = set()
                for effect in effects:
                    if not cls._opens_npc(effect, local_id, origin):
                        continue
                    matched_battle = False
                    keys = {
                        f"flag:{0x500 + guard.subject}"
                        for guard in effect.guards
                        if guard.kind == "trainer"
                        and guard.operator == "eq"
                        and guard.value is True
                    }
                    for guard in effect.guards:
                        if (
                            guard.kind == "trainer"
                            and guard.operator == "eq"
                            and guard.value is False
                            and battles.get(guard.subject, frozenset()) <= frozenset(effect.guards)
                        ):
                            keys.add(f"flag:{0x500 + guard.subject}")
                            matched_battle = True
                    if matched_battle and keys:
                        ordered = sorted(keys, key=lambda key: (not key.startswith("var:"), key))
                        battle_openings.add("&".join(ordered))
                options = battle_openings
        return next(iter(options)) if len(options) == 1 else None

    def _effect_script_obstacles(
        self,
    ) -> dict[tuple[int, int], tuple[FieldObstacle, ...]]:
        discovered: dict[tuple[int, int], list[FieldObstacle]] = defaultdict(list)
        for key, state in self.maps.items():
            transition: dict[tuple[int, int], str] = {}
            if self._profile is not None:
                pointer = script_table(
                    self.rom, header_scripts_ptr(self.rom, self._profile, *key)
                ).get(3)
                transition_result = (
                    self._script_interpretation(pointer, state) if pointer is not None else None
                )
                if transition_result is not None:
                    for effect in self._complete_effects(
                        "script_obstacles:on_transition", transition_result, state=state
                    ):
                        if (
                            effect.decoded
                            and effect.kind == "setvar"
                            and isinstance(effect.value("var"), int)
                            and isinstance(effect.value("value"), int)
                        ):
                            needed = self._positive_requirement(effect.guards)
                            if needed is not None:
                                transition[(effect.value("var"), effect.value("value"))] = needed
            candidates = [
                (
                    obj.x,
                    obj.y,
                    obj.script_ptr,
                    obj.local_id,
                    None,
                    None,
                    f"NPC {obj.local_id}",
                    "npc",
                )
                for obj in state.objects
                if obj.role == "npc" and obj.script_ptr is not None
            ]
            candidates.extend(
                (
                    event.x,
                    event.y,
                    event.script_ptr,
                    None,
                    event.var_id,
                    event.var_value,
                    f"coordinate event {event.index}",
                    "coord",
                )
                for event in state.coord_events
                if not event.weather_only and event.script_ptr
            )
            for x, y, pointer, local_id, trigger_var, trigger_value, name, kind in candidates:
                result = self._script_interpretation(
                    pointer,
                    state,
                    local_id=local_id,
                    trigger=(trigger_var, trigger_value)
                    if trigger_var is not None and trigger_value is not None
                    else None,
                    player_xy=(x, y) if local_id is None else None,
                )
                effects = self._complete_effects(
                    f"script_obstacles:{kind}", result, state=state, site=(x, y)
                )
                if not effects:
                    continue
                needed = self._passage_requirement(
                    effects,
                    local_id=local_id,
                    origin=(x, y),
                    trigger_var=trigger_var,
                    trigger_value=trigger_value,
                    transition=transition,
                )
                if needed is None:
                    continue
                label, equivalents = (
                    self._requirement_context(needed) if "&" not in needed else (None, ())
                )
                triggerable = (
                    local_id is None
                    and self._coordinate_completion_requirement(effects, trigger_var, trigger_value)
                    == needed
                )
                discovered[key].append(
                    script_gate(
                        x=x,
                        y=y,
                        requirement_key=needed,
                        name=name,
                        local_id=local_id,
                        requirement_label=label,
                        equivalent_requirements=equivalents,
                        triggerable=triggerable,
                        declinable=triggerable and self._coordinate_battle_is_declinable(effects),
                    )
                )
                if triggerable:
                    completion_guards = {
                        effect.guards
                        for effect in effects
                        if effect.kind == "setvar"
                        and f"var:{effect.value('var')}:ge:{effect.value('value')}" == needed
                    }
                    removed = {
                        effect.value("local_id")
                        for effect in effects
                        if effect.kind in {"removeobject", "removeobjectat", "hideobjectat"}
                        and effect.guards in completion_guards
                        and isinstance(effect.value("local_id"), int)
                    }
                    for obj in state.objects:
                        if obj.local_id not in removed:
                            continue
                        discovered[key].append(
                            script_gate(
                                x=obj.x,
                                y=obj.y,
                                requirement_key=needed,
                                name=f"NPC {obj.local_id}",
                                local_id=obj.local_id,
                                requirement_label=label,
                                equivalent_requirements=equivalents,
                            )
                        )
        return {key: tuple(dict.fromkeys(rows)) for key, rows in discovered.items()}

    def _hidden_item_source(self, flag: int) -> tuple[int | None, str | None]:
        """Find the ROM item-ball script that owns a hidden acquisition flag."""
        for state in self.maps.values():
            for obj in state.objects:
                if obj.role != "item_ball" or obj.hidden_flag_id != flag or obj.script_ptr is None:
                    continue
                item_id = next(
                    (
                        command.u16(2)
                        for command in self._stream(obj.script_ptr)
                        if command.name == "setorcopyvar"
                        and len(command.operands) >= 4
                        and command.u16() == 0x8000
                        and command.u16(2) < 0x4000
                    ),
                    None,
                )
                if item_id is not None:
                    return item_id, f"{state.identity.name or 'map'} item ball"
        return None, None

    def _stream(self, pointer: int) -> tuple[Any, ...]:
        try:
            offset = pointer - ROM_BASE if pointer >= ROM_BASE else pointer
            return tuple(decode_stream(self.rom, offset))
        except (ValueError, IndexError):
            return ()

    def _read_trainers_by_flag(self) -> dict[int, tuple[CountedTrainer, ...]]:
        """Index trainer flags to their ROM map objects without using labels or heuristics."""
        rows: dict[int, list[CountedTrainer]] = defaultdict(list)
        if not self.rom:
            return {}
        for map_key, state in self.maps.items():
            for obj in state.objects:
                if obj.script_ptr is None:
                    continue
                try:
                    commands = reachable(self.rom, obj.script_ptr)
                except (ValueError, IndexError):
                    continue
                battle = next(
                    (
                        command
                        for command in commands
                        if command.name == "trainerbattle" and len(command.operands) >= 3
                    ),
                    None,
                )
                if battle is None:
                    continue
                flag = 0x500 + battle.u16(1)
                rows[flag].append(CountedTrainer(flag, map_key, obj.local_id, obj.x, obj.y))
        return {flag: tuple(values) for flag, values in rows.items()}

    def _toggle_edges(
        self, node: NavigationNode, state: MapState, capabilities: CapabilityState
    ) -> tuple[NavigationEdge, ...]:
        if not capabilities.flags_known:
            return ()
        map_key = (state.identity.group, state.identity.number)
        out: list[NavigationEdge] = []
        for toggle in self._script_toggles.get(map_key, ()):
            directions = (toggle.facing,) if toggle.facing in DIRECTIONS else DIRECTIONS
            for direction in directions:
                dx, dy = VECTOR[direction]
                stand = (toggle.x - dx, toggle.y - dy)
                if (node.x, node.y) != stand or not self._plain_walkable(state, *stand):
                    continue
                out.append(
                    NavigationEdge(
                        stable_key=(
                            f"toggle:{map_key[0]}:{map_key[1]}:{toggle.event_index}:{toggle.flag}"
                        ),
                        source=node,
                        destination=node,
                        kind="toggle",
                        cost=6,
                        direction=direction,
                        executor_target=(
                            ("kind", "toggle"),
                            ("flag", toggle.flag),
                            ("at", (toggle.x, toggle.y)),
                            ("stand", stand),
                            ("event", toggle.event_index),
                            ("local_id", toggle.local_id or 0),
                            ("toggle_to", toggle.flag not in capabilities.flags),
                        ),
                        prompt=True,
                    )
                )
        return tuple(out)

    def _read_on_frame_landings(
        self, profile: Any
    ) -> dict[tuple[int, int], tuple[tuple[int, int, tuple[tuple[int, int], ...]], ...]]:
        if not self.rom or profile is None:
            return {}
        result: dict[tuple[int, int], tuple[tuple[tuple[int, int], ...], ...]] = {}
        for key in self.maps:
            try:
                rows = on_frame_movement(self.rom, profile, *key)
            except (IndexError, KeyError, ValueError, Unparsed):
                continue
            if rows:
                result[key] = rows
        return result

    def _obstacle_edge(
        self,
        state: MapState,
        node: NavigationNode,
        obj: MapObject,
        direction: Facing,
        capabilities: CapabilityState,
    ) -> NavigationEdge | None:
        if obj.role == "item_ball" and obj.hidden_flag_id:
            key = f"flag:{obj.hidden_flag_id}"
            return NavigationEdge(
                f"item:{obj.local_id}",
                node,
                self.node(node.map_key, obj.x, obj.y),
                "field_move",
                2,
                (requirement(key, capabilities, label="collect the blocking item"),),
                direction,
                (
                    ("kind", "obstacle"),
                    ("role", "item_ball"),
                    ("local_id", obj.local_id),
                    ("hide_flag", obj.hidden_flag_id),
                ),
            )
        obstacle = from_object(obj, self.rom)
        if obstacle is None and obj.role == "fossil":
            # Fossils are interaction obstacles rather than field-move obstacles.  They
            # still occupy their live RAM tile until one choice is taken, so retain the
            # blocked crossing as an unmet semantic edge for the adapter to match to the
            # existing fossil option.
            return NavigationEdge(
                f"fossil:{obj.local_id}",
                node,
                self.node(node.map_key, obj.x, obj.y),
                "field_move",
                6,
                (requirement("fossil", capabilities, label="fossil"),),
                direction,
                (("kind", "obstacle"), ("role", "fossil"), ("local_id", obj.local_id)),
            )
        if obstacle is None:
            return None
        if obj.role == "boulder" and self._puzzle_for(state) is not None:
            return None
        kind = "boulder_push" if obj.role == "boulder" else "field_move"
        needs = [requirement(obstacle.kind.requirement_key, capabilities)]
        if obj.role == "boulder" and not capabilities.strength_active:
            needs.append(requirement("strength", capabilities))
        destination = self.node(node.map_key, obj.x, obj.y)
        return NavigationEdge(
            f"{obstacle.kind.requirement_key}:{obj.local_id}",
            node,
            destination,
            kind,
            6,
            tuple(needs),
            direction,
            (
                ("kind", "obstacle"),
                ("role", obj.role),
                ("local_id", obj.local_id),
                ("hide_flag", obstacle.hide_flag or 0),
                ("completion", obstacle.kind.completion),
                ("script_starts_battle", obstacle.script_starts_battle),
            ),
        )

    def _boulder_plan_edges(
        self,
        node: NavigationNode,
        state: MapState,
        capabilities: CapabilityState,
        *,
        live: MapState | None,
    ) -> tuple[NavigationEdge, ...]:
        """Stateful puzzle macros as graph edges from the current live position.

        The edge crosses the unlocked exit conceptually so world search can continue. The existing
        executor stops after moving the boulders; its next frame replans from RAM and takes that
        now-open exit.
        """
        player = state.player
        puzzle = self._puzzle_for(state)
        if (
            live is None
            or player is None
            or puzzle is None
            or (player.avatar_x, player.avatar_y) != (node.x, node.y)
        ):
            return ()
        world = self.provider.boulder_world(state)
        goal_type = self.provider.goal_type()
        goals = tuple(
            [goal_type("warp", (warp.index,)) for warp in state.warps if not warp.dynamic]
            + [goal_type("edge", (connection.direction,)) for connection in state.connections]
        )
        strength = requirement("strength", capabilities)
        out: list[NavigationEdge] = []
        for goal, result in world.plan_all(goals).items():
            if (
                goal.kind not in ("hole", "warp", "edge")
                or not self.provider.is_boulder_plan(result)
                or not result.pushes
            ):
                continue
            # A fall-warp is the boulder's destination, not a usable player exit while the
            # boulder occupies it.  Its executable action is the explicit ``hole`` goal below.
            if goal.kind == "warp" and any(
                hole.warp == int(goal.key[0]) for hole in puzzle.holes
            ):
                continue
            # A boulder falling through a warp does not carry the player with it.  Exit plans
            # which become reachable by dropping a boulder therefore cannot be represented as
            # a player edge through that warp.  Publish the drop itself as the state-changing
            # boulder action; after RAM verifies it, navigation replans from the player's real
            # position and the revealed lower-floor boulder.
            if goal.kind in ("warp", "edge") and result.drops:
                continue
            if goal.kind == "hole":
                stand_x, stand_y = result.pushes[-1].stand
                destination = self.node(node.map_key, stand_x, stand_y)
            else:
                destination = self._boulder_exit_destination(state, goal, capabilities)
            if destination is None:
                continue
            kind_index = {"hole": 1, "warp": 2, "edge": 3}[goal.kind]
            if goal.kind == "edge":
                x, y = "NSWE".index(str(goal.key[0])), 0
            elif goal.kind == "warp":
                x, y = int(goal.key[0]), 0
            else:
                x, y = int(goal.key[0]), int(goal.key[1])
            out.append(
                NavigationEdge(
                    stable_key=f"boulder_plan:{state.identity.name}:{goal.goal_id}",
                    source=node,
                    destination=destination,
                    kind="boulder_push",
                    cost=result.cost + 5,
                    requirements=(strength,),
                    executor_target=(
                        ("kind", "boulder_plan"),
                        ("goal_kind", kind_index),
                        ("x", x),
                        ("y", y),
                    ),
                )
            )
        return tuple(out)

    def _boulder_exit_destination(
        self, state: MapState, goal: Any, capabilities: CapabilityState
    ) -> NavigationNode | None:
        if goal.kind == "warp":
            warp = next((row for row in state.warps if row.index == int(goal.key[0])), None)
            return self._warp_landing(warp) if warp is not None else None
        direction = str(goal.key[0])
        width, height = state.layout.width, state.layout.height
        boundary = {
            "N": ((x, 0) for x in range(width)),
            "S": ((x, height - 1) for x in range(width)),
            "W": ((0, y) for y in range(height)),
            "E": ((width - 1, y) for y in range(height)),
        }[direction]
        map_key = (state.identity.group, state.identity.number)
        for x, y in boundary:
            edge = self._connection_edge(state, self.node(map_key, x, y), direction, capabilities)
            if edge is not None:
                return edge.destination
        return None

    def _forced_landing(
        self,
        state: MapState,
        start: tuple[int, int],
        direction: Facing,
        capabilities: CapabilityState,
    ) -> tuple[tuple[int, int], tuple[tuple[int, int], ...]] | None:
        current = start
        path = [current]
        seen: set[tuple[tuple[int, int], Facing]] = set()
        moving = direction
        spin_direction: Facing | None = None
        for _ in range(128):
            info = self.provider.behavior_info(state.behavior(*current))
            if spin_direction is not None and info.kind == "stop_spin":
                return current, tuple(path)
            if info.kind == "spin":
                spin_direction = info.forced
            forced = spin_direction or info.forced or (moving if info.ice else None)
            if forced is None:
                return current, tuple(path)
            marker = current, forced
            if marker in seen:
                return None
            seen.add(marker)
            dx, dy = VECTOR[forced]
            nxt = current[0] + dx, current[1] + dy
            if not self._plain_walkable(state, *nxt) or self._blocking_object(
                state, *nxt, capabilities
            ):
                return current, tuple(path)
            current = nxt
            path.append(current)
            moving = forced
        return None

    def _blocking_object(
        self, state: MapState, x: int, y: int, capabilities: CapabilityState
    ) -> MapObject | None:
        objects = self._blocking_object_tiles(state).get((x, y), ())
        for obj in objects:
            if obj.hidden_flag_id and obj.hidden_flag_id in capabilities.flags:
                continue
            return obj
        return None

    def _puzzle_for(self, state: MapState):
        """Resolve puzzle data once per graph, outside repeated search expansions."""
        table = getattr(self, "_puzzle_table", _PUZZLES_UNLOADED)
        if table is _PUZZLES_UNLOADED:
            load = getattr(self.provider, "puzzles", None)
            if load is None:
                return self.provider.puzzle_for(state)
            table = load(allow_empty=True)
            self._puzzle_table = table
        if table is None:
            return None
        return table.get((state.identity.group, state.identity.number))

    def _blocking_object_tiles(
        self, state: MapState
    ) -> dict[tuple[int, int], tuple[MapObject, ...]]:
        map_key = state.identity.group, state.identity.number
        cached = self._blocking_object_indexes.get(map_key)
        if cached is not None and cached[0] is state.objects:
            return cached[1]
        index = self._build_blocking_object_index(state)
        self._blocking_object_indexes[map_key] = state.objects, index
        return index

    def _build_blocking_object_index(
        self, state: MapState
    ) -> dict[tuple[int, int], tuple[MapObject, ...]]:
        positions: dict[tuple[int, int], list[MapObject]] = {}
        for obj in state.objects:
            if not obj.blocks_navigation:
                continue
            occupied = {(obj.x, obj.y)}
            if obj.live is not None:
                occupied.add((obj.live.prev_x, obj.live.prev_y))
            for tile in occupied:
                positions.setdefault(tile, []).append(obj)
        index = {tile: tuple(objects) for tile, objects in positions.items()}
        return index

    def _risks_at(self, state: MapState, x: int, y: int) -> tuple[str, ...]:
        risks: list[str] = []
        info = self.provider.behavior_info(state.behavior(x, y))
        if info.tall_grass or info.surfable:
            risks.append("wild_encounter")
        for obj in state.objects:
            if obj.trainer_type not in (1, 2) or obj.trainer_range <= 0:
                continue
            if x == obj.x and abs(y - obj.y) <= obj.trainer_range:
                risks.append(f"trainer_sight:{obj.local_id}")
            elif y == obj.y and abs(x - obj.x) <= obj.trainer_range:
                risks.append(f"trainer_sight:{obj.local_id}")
        return tuple(dict.fromkeys(risks))

    def _fly_edges(
        self, node: NavigationNode, state: MapState, capabilities: CapabilityState
    ) -> Iterable[NavigationEdge]:
        if (
            "fly" not in capabilities.capabilities
            or state.identity.map_type not in fly.FLY_MAP_TYPES
            or not fly.can_launch(state)
        ):
            return ()
        out = []
        for destination in fly.DESTINATIONS:
            if (
                destination.map_key not in capabilities.visited_maps
                or destination.map_key == node.map_key
            ):
                continue
            target = self.node(destination.map_key, *destination.landing)
            out.append(
                NavigationEdge(
                    f"fly:{destination.name}",
                    node,
                    target,
                    "fly",
                    fly.FLY_OVERHEAD,
                    (requirement("fly", capabilities),),
                    executor_target=(("kind", "fly"), ("dest", destination.map_key)),
                )
            )
        return tuple(out)

    def _requirement_context(self, key: str) -> tuple[str | None, tuple[str, ...]]:
        if key.startswith("item:"):
            item_id = int(key.split(":", 1)[1], 0)
            flags = tuple(
                f"flag:{flag_id}"
                for flag_id, paired_item in sorted(self._flag_items.items())
                if paired_item == item_id
            )
            return self._item_names.get(item_id), flags
        if key.startswith("flag:"):
            flag_id = int(key.split(":", 1)[1], 0)
            item_id = self._flag_items.get(flag_id)
            if item_id is not None:
                return self._item_names.get(item_id), (f"item:{item_id}",)
        if key.startswith("var:"):
            parts = key.split(":")
            if len(parts) == 3:
                _kind, ident, value = parts
                return f"story var 0x{int(ident, 0):X} = {int(value, 0)}", ()
            if len(parts) == 4:
                _kind, ident, operator, value = parts
                return f"story var 0x{int(ident, 0):X} {operator} {int(value, 0)}", ()
        return None, ()

    def _script_gate_at(self, state: MapState, x: int, y: int) -> FieldObstacle | None:
        return next(
            (
                obstacle
                for obstacle in self._script_obstacles.get(
                    (state.identity.group, state.identity.number), ()
                )
                if (obstacle.x, obstacle.y) == (x, y)
            ),
            None,
        )

    def _script_gate_edges(
        self, node: NavigationNode, state: MapState, capabilities: CapabilityState
    ) -> tuple[NavigationEdge, ...]:
        out: list[NavigationEdge] = []
        map_key = (state.identity.group, state.identity.number)
        for obstacle in self._script_obstacles.get(map_key, ()):
            on_obstacle = (node.x, node.y) == (obstacle.x, obstacle.y)
            if "&" in obstacle.kind.requirement_key:
                obstacle_requirements = tuple(
                    requirement(
                        key,
                        capabilities,
                        label=(
                            obstacle.requirement_label
                            if "&" not in obstacle.kind.requirement_key
                            else self._requirement_context(key)[0]
                        ),
                        equivalent_keys=(
                            obstacle.equivalent_requirements
                            if "&" not in obstacle.kind.requirement_key
                            else self._requirement_context(key)[1]
                        ),
                    )
                    for key in obstacle.kind.requirement_key.split("&")
                )
                door_adjacent = any(
                    not warp.dynamic
                    and abs(warp.x - obstacle.x) + abs(warp.y - obstacle.y) == 1
                    and (warp.x == obstacle.x or warp.y == obstacle.y)
                    and self._script_door_gates.get((map_key, warp.index), (None, None, ()))[0]
                    == obstacle.kind.requirement_key
                    for warp in state.warps
                )
                if (
                    obstacle.source == "tile"
                    and obstacle.local_id is None
                    and door_adjacent
                    and not on_obstacle
                ):
                    for direction in DIRECTIONS:
                        dx, dy = VECTOR[direction]
                        if (node.x + dx, node.y + dy) != (obstacle.x, obstacle.y):
                            continue
                        if not self._plain_walkable(state, node.x, node.y):
                            continue
                        if not self._same_mode_step(
                            state, (node.x, node.y), (obstacle.x, obstacle.y)
                        ):
                            continue
                        out.append(
                            NavigationEdge(
                                stable_key=(
                                    f"script_gate_entry:{map_key[0]}:{map_key[1]}:"
                                    f"{obstacle.x}:{obstacle.y}:{direction}"
                                ),
                                source=node,
                                destination=self.node(map_key, obstacle.x, obstacle.y),
                                kind="gate",
                                cost=1,
                                requirements=obstacle_requirements,
                                direction=direction,
                                executor_target=(
                                    ("kind", "obstacle"),
                                    ("role", obstacle.kind.role),
                                    ("local_id", obstacle.local_id or 0),
                                    ("at", (obstacle.x, obstacle.y)),
                                    ("requirement", obstacle.kind.requirement_key),
                                    ("triggerable", obstacle.triggerable),
                                    ("declinable", obstacle.declinable),
                                ),
                                prompt=not all(need.met for need in obstacle_requirements),
                            )
                        )
            for direction in DIRECTIONS:
                dx, dy = VECTOR[direction]
                if on_obstacle:
                    source_x, source_y = obstacle.x, obstacle.y
                    tx, ty = obstacle.x + dx, obstacle.y + dy
                else:
                    if (node.x + dx, node.y + dy) != (obstacle.x, obstacle.y):
                        continue
                    source_x, source_y = node.x, node.y
                    tx, ty = obstacle.x + dx, obstacle.y + dy
                if not self._plain_walkable(state, source_x, source_y):
                    continue
                # A gate edge is one ordinary movement across the trigger tile.  Source, trigger
                # and landing tile must share a movement mode (same elevation, or a 0/15
                # transition tile): a land tile next to a water trigger is not a crossing, the
                # player has to mount Surf first and cross the trigger on the water.
                if not on_obstacle and not self._same_mode_step(
                    state, (source_x, source_y), (obstacle.x, obstacle.y)
                ):
                    continue
                if self._inside(state, tx, ty) and not self._same_mode_step(
                    state, (obstacle.x, obstacle.y), (tx, ty)
                ):
                    continue
                source = self.node(map_key, source_x, source_y)
                destination = self.node(map_key, tx, ty)
                warp_edge = None
                opening_requirements: tuple[NavigationRequirement, ...] = ()
                if not self._plain_walkable(state, tx, ty):
                    warp = self._warp_at(state, tx, ty, node.elevation)
                    opening = (
                        self._warp_opening(state, warp, capabilities) if warp is not None else None
                    )
                    if opening is None:
                        continue
                    effect, opening_requirements = opening
                    if self.provider.behavior_info(effect.value("behavior")).warp_trigger != "step":
                        continue
                    warp_edge = self._warp_edge(state, source, warp, direction, capabilities)
                    if warp_edge is None:
                        continue
                    destination = warp_edge.destination
                    opening_requirements += warp_edge.requirements
                obstacle_requirements = tuple(
                    requirement(
                        key,
                        capabilities,
                        label=(
                            obstacle.requirement_label
                            if "&" not in obstacle.kind.requirement_key
                            else self._requirement_context(key)[0]
                        ),
                        equivalent_keys=(
                            obstacle.equivalent_requirements
                            if "&" not in obstacle.kind.requirement_key
                            else self._requirement_context(key)[1]
                        ),
                    )
                    for key in obstacle.kind.requirement_key.split("&")
                )
                needs = tuple(dict.fromkeys(opening_requirements + obstacle_requirements))
                if warp_edge is not None and all(need.met for need in needs):
                    out.append(replace(warp_edge, requirements=needs))
                    continue
                out.append(
                    NavigationEdge(
                        stable_key=(
                            f"script_gate:{map_key[0]}:{map_key[1]}:"
                            f"{obstacle.x}:{obstacle.y}:{direction}"
                        ),
                        source=source,
                        destination=destination,
                        kind="gate",
                        cost=2,
                        requirements=needs,
                        direction=direction,
                        executor_target=(
                            ("kind", "obstacle"),
                            ("role", obstacle.kind.role),
                            ("local_id", obstacle.local_id or 0),
                            ("at", (obstacle.x, obstacle.y)),
                            ("requirement", obstacle.kind.requirement_key),
                            ("triggerable", obstacle.triggerable),
                            ("declinable", obstacle.declinable),
                        ),
                        prompt=not all(need.met for need in needs),
                    )
                )
        return tuple(out)

    @staticmethod
    def _inside(state: MapState, x: int, y: int) -> bool:
        return 0 <= x < state.layout.width and 0 <= y < state.layout.height

    def _water_body_id(self, state: MapState, x: int, y: int) -> tuple[int, int]:
        """Smallest tile in a ROM water component, matching the Surf option identity."""
        map_key = (state.identity.group, state.identity.number)
        cached = self._water_body_ids.get((map_key, x, y))
        if cached is not None:
            return cached
        todo = [(x, y)]
        members = {(x, y)}
        while todo:
            cx, cy = todo.pop()
            for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                if (nx, ny) in members or not self._inside(state, nx, ny):
                    continue
                info = self.provider.behavior_info(state.behavior(nx, ny))
                if state.collision(nx, ny) or not info.surfable or info.waterfall:
                    continue
                members.add((nx, ny))
                todo.append((nx, ny))
        body_id = min(members)
        for member_x, member_y in members:
            self._water_body_ids[(map_key, member_x, member_y)] = body_id
        return body_id

    @staticmethod
    def _same_mode_step(state: MapState, a: tuple[int, int], b: tuple[int, int]) -> bool:
        """Whether one step from ``a`` to ``b`` keeps the movement mode (walk or surf).

        Mirrors the elevation rule of ordinary steps in ``_step_edge``: equal elevations, or a
        0/15 transition tile on either side.  Mounting or leaving Surf is a separate edge.
        """
        ea, eb = state.elevation(*a), state.elevation(*b)
        return ea in (0, 15) or eb in (0, 15) or ea == eb

    @staticmethod
    def _plain_walkable(state: MapState, x: int, y: int) -> bool:
        return NavigationGraph._inside(state, x, y) and state.collision(x, y) == 0

    @staticmethod
    def _warp_at(state: MapState, x: int, y: int, elevation: int) -> WarpInfo | None:
        return next(
            (
                warp
                for warp in state.warps
                if (warp.x, warp.y) == (x, y) and warp.elevation in (0, elevation)
            ),
            None,
        )

    @staticmethod
    def _step_warp_at(state: MapState, x: int, y: int) -> WarpInfo | None:
        """A step-triggered warp row matching the ENTERED tile's elevation.

        The row fires when the player stands on the entered tile, so it must match that
        tile's elevation (warp.elevation == tile elevation or 0), never the approach node's
        (Seafoam B3F ladders beside an elevation-0 walk).  Undefined tiles report 0, so
        only elevation-0 rows match there (fail-closed).
        """
        elevation = state.elevation(x, y)
        return next(
            (
                warp
                for warp in state.warps
                if (warp.x, warp.y) == (x, y)
                and warp.trigger == "step"
                and warp.elevation in (0, elevation)
            ),
            None,
        )
