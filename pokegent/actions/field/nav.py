"""Local navigation on a :class:`MapState`: the game's collision rules as a graph, and Dijkstra/A*.

``Navigator`` is a fast re-statement of
:func:`pokegent.games.gen3.firered.maps.map_motion.predict_step` for
planning (one call there costs far too much for a flood fill over a whole map), plus the three
things a planner needs and a single step prediction does not:

* the player's **elevation travels with the node** (bridges, cave floors): a node is ``(x, y,
  elevation)``;
* **object blockers** are classified (wandering NPC, static NPC/item ball, cut tree / smash rock /
  boulder / Snorlax) so a path can say *why* a target is not reachable;
* tiles that start something are **terminal**: a warp event tile ends the path (stepping on it
warps),
  forced-movement tiles (ice, slide, current, conveyor, spinner) and water are not walked.

Historical local validation compared this module with ``predict_step`` over real maps:
whenever this module said a step moved, ``predict_step`` agreed.

Costs are heuristics (distance, encounter risk, script tiles) and exchangeable; nothing here decides
strategy.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from pokegent.actions.field.surf import nav as surf_nav
from pokegent.games.gen3.firered.maps import map_constants as K
from pokegent.games.gen3.firered.maps.map_behaviors import behavior_info, blocks_side
from pokegent.games.gen3.firered.maps.map_motion import _walk_into_signpost, traverse_forced
from pokegent.games.gen3.firered.maps.map_schema import Facing, MapObject, MapState, WarpInfo
from pokegent.games.gen3.firered.maps.obstacles import KINDS, ROLES

DIRECTIONS: tuple[Facing, ...] = ("N", "S", "W", "E")
VECTOR = K.VECTOR
OPPOSITE = K.OPPOSITE

# Encounter rolls can seize control of a route macro for a whole battle.  Make a safe detour
# preferable to every practical same-map shortcut, while still allowing the route when the risky
# terrain is the only way through.  The ROM attribute covers both land and water encounters.
ENCOUNTER_COST = 10_000
GRASS_COST = ENCOUNTER_COST  # compatibility name for callers/tests that describe land encounters
SCRIPT_TILE_COST = 12  # an active coordinate event takes the controls
LEDGE_COST = 2

#: movement types of objects that walk (their tile changes); everything else stays where it is
MOBILE_MOVEMENT = frozenset(
    {
        "wander_around",
        "wander_up_and_down",
        "wander_down_and_up",
        "wander_left_and_right",
        "wander_right_and_left",
        "walk_up_and_down",
        "walk_down_and_up",
        "walk_left_and_right",
        "walk_right_and_left",
        "copy_player",
    }
)
OBSTACLE_ROLES = ROLES
#: obstacle role -> the field capability that would clear it (reported, never exercised here)
OBSTACLE_CAPABILITY = {role: kind.requirement_key for role, kind in KINDS.items()}


def is_mobile(obj: MapObject) -> bool:
    """An object that may leave its tile (wandering/walking NPC)."""
    if obj.movement_type_name in MOBILE_MOVEMENT:
        return True
    return obj.movement_type_name is None and (obj.range_x > 0 or obj.range_y > 0)


@dataclass(frozen=True)
class Constraints:
    """Which obstacles stop a path. The default is the world as it is right now."""

    wanderers: bool = True  # walking NPCs block (at their current and previous tile)
    statics: bool = True  # standing NPCs / item balls block (they never leave their tile)
    obstacles: bool = True  # cut trees, smash rocks, boulders, Snorlax block
    water: bool = False  # surfable water / waterfall tiles are walkable (never true in reality)
    forced: bool = False  # ice, slide, current, conveyor, spinner tiles are walkable


@dataclass(frozen=True)
class Move:
    direction: Facing
    src: tuple[int, int]
    dst: tuple[int, int]  # where the player stands after (the landing tile of a ledge jump)
    kind: str  # "walk" | "ledge" | "forced"
    cost: int
    grass: bool = False
    encounter_tiles: int = 0
    script: bool = False  # an active coordinate event sits on the destination
    connection: str | None = None  # the destination is in the ring of this connection (N/S/W/E)
    warp_index: int | None = None  # the destination tile carries this warp event
    behavior_name: str = ""
    forced_tiles: tuple[tuple[int, int], ...] = ()
    forced_landing: tuple[int, int] | None = None


Node = tuple[int, int, int]  # x, y, elevation


@dataclass
class Search:
    """Result of one Dijkstra run from the player."""

    start: Node
    cost: dict[Node, int] = field(default_factory=dict)
    parent: dict[Node, tuple[Node, Move] | None] = field(default_factory=dict)
    terminal: set[Node] = field(default_factory=set)

    def _index(self) -> dict[tuple[int, int], list[Node]]:
        index = self.__dict__.get("_by_xy")
        if index is None or self.__dict__.get("_indexed") != len(self.cost):
            index = {}
            for node in self.cost:
                index.setdefault((node[0], node[1]), []).append(node)
            self.__dict__["_by_xy"] = index
            self.__dict__["_indexed"] = len(self.cost)
        return index

    def nodes_at(self, x: int, y: int) -> list[Node]:
        return self._index().get((x, y), [])

    def best_at(self, x: int, y: int) -> Node | None:
        nodes = self.nodes_at(x, y)
        return min(nodes, key=lambda n: self.cost[n]) if nodes else None

    def path_to(self, node: Node) -> tuple[Move, ...]:
        moves: list[Move] = []
        cur: Node | None = node
        while cur is not None:
            entry = self.parent.get(cur)
            if entry is None:
                break
            cur, move = entry
            moves.append(move)
        moves.reverse()
        return tuple(moves)


def _compatible(a: int, b: int) -> bool:
    return a == 0 or b == 0 or a == b


class Navigator:
    """Passability and shortest paths for one map state under :class:`Constraints`."""

    def __init__(self, state: MapState, constraints: Constraints | None = None) -> None:
        if state.player is None:
            raise ValueError("navigation needs a live map state (a player placement)")
        self.state = state
        self.constraints = constraints or Constraints()
        self._blockers = self._classify_blockers(state)
        self._warps: dict[tuple[int, int], list[WarpInfo]] = {}
        for warp in state.warps:
            self._warps.setdefault((warp.x, warp.y), []).append(warp)
        self._script_tiles = {(c.x, c.y) for c in state.coord_events if c.active}
        self._tile_cache: dict[tuple[int, int], tuple] = {}

    # -- static per-tile facts (cached) ---------------------------------------------------------

    def _tile(self, x: int, y: int) -> tuple:
        cached = self._tile_cache.get((x, y))
        if cached is None:
            st = self.state
            undefined = st.raw_block(x, y) == K.MAPGRID_UNDEFINED
            outside = undefined or st.border_id(x, y) == -1
            beh = st.behavior(x, y)
            cached = (outside, st.collision(x, y), st.elevation(x, y), behavior_info(beh))
            self._tile_cache[(x, y)] = cached
        return cached

    def encounter_at(self, x: int, y: int) -> bool:
        """Whether the ROM marks this tile for a land or water encounter roll."""
        attrs = self.state.attributes(x, y)
        value = (attrs & K.ATTR_ENCOUNTER_MASK) >> K.ATTR_ENCOUNTER_SHIFT
        return value in K.ENCOUNTER_NAMES and K.ENCOUNTER_NAMES[value] != "none"

    def encounter_risk_at(self, x: int, y: int) -> bool:
        """Encounter-risk terrain, retaining tall grass as a conservative fallback."""
        return self.encounter_at(x, y) or self._tile(x, y)[3].tall_grass

    def _classify_blockers(self, state: MapState) -> dict[tuple[int, int], list[tuple[str, int]]]:
        """tile -> [(category, elevation)] with category ``npc`` / ``wanderer`` / ``obstacle``."""
        out: dict[tuple[int, int], list[tuple[str, int]]] = {}
        for tile, objs in state.blocker_positions().items():
            for obj in objs:
                if obj.role in OBSTACLE_ROLES:
                    category = "obstacle"
                elif is_mobile(obj):
                    category = "wanderer"
                else:
                    category = "npc"
                elevation = obj.live.elevation if obj.live is not None else obj.elevation
                out.setdefault(tile, []).append((category, elevation))
        return out

    def blocker_category(self, x: int, y: int, elevation: int) -> str | None:
        """Why an object stops a mover of ``elevation`` on this tile (``None``: free)."""
        for category, obj_elevation in self._blockers.get((x, y), ()):
            if not _compatible(elevation, obj_elevation):
                continue
            if category == "obstacle" and not self.constraints.obstacles:
                continue
            if category == "npc" and not self.constraints.statics:
                continue
            if category == "wanderer" and not self.constraints.wanderers:
                continue
            return category
        return None

    def warp_at(self, x: int, y: int, elevation: int) -> WarpInfo | None:
        for warp in self._warps.get((x, y), ()):
            if warp.elevation in (0, elevation):
                return warp
        return None

    # -- one step -------------------------------------------------------------------------------

    def step(self, node: Node, d: Facing) -> tuple[Node, Move] | None:
        """The move from ``node`` in direction ``d``, or ``None`` if the player cannot make it."""
        x, y, e = node
        if surf_nav.is_swimmer(self, node):  # the surf rules (actions/field/surf/nav.py)
            return surf_nav.surf_step(self, node, d)
        vx, vy = VECTOR[d]
        nx, ny = x + vx, y + vy
        outside, collision, map_elev, info = self._tile(nx, ny)
        if outside:
            return None
        if (
            self.constraints.water
            and info.surfable
            and (mounted := surf_nav.mount_step(self, node, d))
        ):
            return mounted  # relaxation only: "Surf would unlock this" (never an executable move)
        if info.ledge == d:
            return self._ledge(node, d, (nx, ny))
        if d == "N" and info.kind == "door" and self.warp_at(nx, ny, e) is not None:
            return None  # TryDoorWarp: the press warps, it never walks onto the door tile
        if _walk_into_signpost(self.state, (nx, ny), d):
            return None  # TrySetUpWalkIntoSignpostScript runs before a north/south walk
        if collision:
            return None
        if self._edge_blocked(x, y, nx, ny, d):
            return None
        if e != 0 and map_elev not in (0, 15) and map_elev != e:
            return None
        category = self.blocker_category(nx, ny, e)
        if category is not None:
            return None
        if not self.constraints.water and (info.surfable or info.waterfall):
            return None
        forced = info.forced is not None or info.ice or info.kind in ("slide", "spin", "conveyor")
        if forced and not self.constraints.forced:
            return None
        ne = e if map_elev == 15 else map_elev
        warp = self.warp_at(nx, ny, e)
        traversal = None
        destination = (nx, ny)
        if forced:
            traversal = traverse_forced(self.state, d, from_xy=(x, y))
            if traversal is None or traversal.loop:
                return None
            destination = traversal.landing_xy
            map_elev = self.state.elevation(*destination)
            ne = e if map_elev == 15 else map_elev
            warp = self.warp_at(*destination, e)
        encounter_tiles = sum(
            self.encounter_risk_at(*tile)
            for tile in (traversal.tiles if traversal is not None else ((nx, ny),))
        )
        move = Move(
            direction=d,
            src=(x, y),
            dst=destination,
            kind="forced" if forced else "walk",
            cost=1
            + (len(traversal.tiles) - 1 if traversal is not None else 0)
            + ENCOUNTER_COST * encounter_tiles
            + (SCRIPT_TILE_COST if destination in self._script_tiles else 0),
            grass=info.tall_grass
            or (
                traversal is not None
                and any(self._tile(*tile)[3].tall_grass for tile in traversal.tiles)
            ),
            encounter_tiles=encounter_tiles,
            script=destination in self._script_tiles,
            connection=self._connection_of(*destination),
            warp_index=warp.index if warp is not None else None,
            behavior_name=info.name,
            forced_tiles=traversal.tiles if traversal is not None else (),
            forced_landing=destination if traversal is not None else None,
        )
        return (destination[0], destination[1], ne), move

    def _ledge(self, node: Node, d: Facing, over: tuple[int, int]) -> tuple[Node, Move] | None:
        x, y, e = node
        vx, vy = VECTOR[d]
        lx, ly = over[0] + vx, over[1] + vy
        outside, collision, map_elev, info = self._tile(lx, ly)
        if outside or collision or info.surfable or info.waterfall:
            return None
        if self.blocker_category(lx, ly, e) is not None or self.warp_at(lx, ly, e) is not None:
            return None
        if self.blocker_category(over[0], over[1], e) is not None:
            return None
        ne = e if map_elev == 15 else map_elev
        move = Move(
            direction=d,
            src=(x, y),
            dst=(lx, ly),
            kind="ledge",
            cost=LEDGE_COST,
            grass=info.tall_grass,
            behavior_name=self._tile(*over)[3].name,
        )
        return (lx, ly, ne), move

    def _edge_blocked(self, sx: int, sy: int, dx: int, dy: int, d: Facing) -> bool:
        st = self.state
        return blocks_side(st.behavior(sx, sy), d) or blocks_side(st.behavior(dx, dy), OPPOSITE[d])

    def _connection_of(self, x: int, y: int) -> str | None:
        lay = self.state.layout
        if 0 <= x < lay.width and 0 <= y < lay.height:
            return None
        return {1: "S", 2: "N", 3: "W", 4: "E"}.get(self.state.border_id(x, y))

    # -- search ---------------------------------------------------------------------------------

    def start_node(self) -> Node:
        p = self.state.player
        assert p is not None
        return (p.avatar_x, p.avatar_y, p.elevation)

    def search(self, *, limit: int = 20000) -> Search:
        """Dijkstra from the player. Terminal nodes (a warp tile, a connection ring tile) are
        recorded with their cost but not expanded."""
        start = self.start_node()
        result = Search(start=start)
        result.cost[start] = 0
        result.parent[start] = None
        heap: list[tuple[int, int, Node]] = [(0, 0, start)]
        counter = 0
        done: set[Node] = set()
        while heap and len(done) < limit:
            cost, _, node = heapq.heappop(heap)
            if node in done:
                continue
            done.add(node)
            if node in result.terminal:
                continue
            for d in DIRECTIONS:
                stepped = self.step(node, d)
                if stepped is None:
                    continue
                nxt, move = stepped
                new_cost = cost + move.cost
                if new_cost < result.cost.get(nxt, 1 << 30):
                    result.cost[nxt] = new_cost
                    result.parent[nxt] = (node, move)
                    if move.warp_index is not None or move.connection is not None:
                        result.terminal.add(nxt)
                    counter += 1
                    heapq.heappush(heap, (new_cost, counter, nxt))
        return result

    # -- interaction geometry ---------------------------------------------------------------------

    def is_counter(self, x: int, y: int) -> bool:
        return self._tile(x, y)[3].kind == "counter"

    def interaction_approaches(
        self, tile: tuple[int, int], *, face_only: Facing | None = None, counter_ok: bool = True
    ) -> list[tuple[tuple[int, int], Facing]]:
        """Standing tiles from which pressing A talks to / reads the thing on ``tile``.

        ``(stand, facing)`` pairs: the neighbour tile facing the object, and (``counter_ok``) the
        tile behind a counter between the player and the object (the game looks one tile further
        over a counter: the nurse, the clerk). ``face_only`` restricts the facing (signs).
        """
        out: list[tuple[tuple[int, int], Facing]] = []
        for d in DIRECTIONS:
            if face_only is not None and d != face_only:
                continue
            vx, vy = VECTOR[d]
            near = (tile[0] - vx, tile[1] - vy)
            out.append((near, d))
            if counter_ok and self.is_counter(*near):
                out.append(((tile[0] - 2 * vx, tile[1] - 2 * vy), d))
        return out


def relaxation_levels() -> tuple[tuple[str, Constraints], ...]:
    """Cumulative relaxations used to explain WHY a target is unreachable (first level that makes
    it reachable names the reason)."""
    return (
        ("blocked_by_npc", Constraints(wanderers=False)),
        ("blocked_by_static_npc", Constraints(wanderers=False, statics=False)),
        ("needs_field_move", Constraints(wanderers=False, statics=False, obstacles=False)),
        (
            "needs_surf",
            Constraints(wanderers=False, statics=False, obstacles=False, water=True),
        ),
        (
            "forced_movement",
            Constraints(wanderers=False, statics=False, obstacles=False, water=True, forced=True),
        ),
    )
