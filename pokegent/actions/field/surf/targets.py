"""Water bodies, shore edges, landing groups and the exits they open (targets of the Surf options).

* A **water body** is a connected set of surfable (non-waterfall) tiles of the current map. Its
id is its
  smallest tile ``(x, y)``: stable, never a position of anything that moves (the grass patches
  do the same).
* A **shore edge** is a ``(stand, facing)`` pair from which Surf starts on that body
  (:func:`~pokegent.games.gen3.firered.maps.map_surf.start_surf`); the macro walks to the
  cheapest reachable one.
* A **landing group** is the set of shore tiles from which a swimmer steps onto one connected
piece of land;
  its id is its smallest landing tile. The land behind it is flooded on foot to name the exits
  there.
* An **exit** is a warp or a map connection of the current map in the vocabulary of the field
options
  (``warp:<g>:<n>:<i>``, ``edge:<dir>:<g>:<n>``), so ``serves_goal`` can judge it with the map
  graph.
"""

from __future__ import annotations

from dataclasses import dataclass

from pokegent.actions.field import nav as N
from pokegent.actions.field import targets as T
from pokegent.actions.field.surf import nav as surf_nav
from pokegent.games.gen3.firered.maps import map_constants as K
from pokegent.games.gen3.firered.maps.map_schema import Facing, MapState
from pokegent.games.gen3.firered.maps.map_surf import LAND_ELEVATION, start_surf


@dataclass(frozen=True)
class ShoreEdge:
    stand: tuple[int, int]
    face: Facing
    water: tuple[int, int]


@dataclass(frozen=True)
class Exit:
    option_id: str
    kind: str  # warp | edge
    dest: tuple[int, int] | None
    dest_name: str
    index: int | None = None
    direction: str | None = None

    def facts(self, here: tuple[int, int]) -> dict:
        """The ``target`` mapping ``serves_goal`` reads (same keys the field builder writes)."""
        t: dict = {
            "kind": self.kind,
            "map": list(here),
            "dest": list(self.dest) if self.dest else None,
        }
        if self.kind == "warp":
            t["index"] = self.index
        else:
            t["direction"] = self.direction
        return t


@dataclass(frozen=True)
class WaterBody:
    canon: tuple[int, int]
    tiles: frozenset[tuple[int, int]]

    @property
    def option_id(self) -> str:
        return f"surf:{self.canon[0]}:{self.canon[1]}"


@dataclass(frozen=True)
class LandingGroup:
    canon: tuple[int, int]
    landings: tuple[tuple[int, int], ...]
    region_tiles: int
    exits: tuple[Exit, ...] = ()

    @property
    def option_id(self) -> str:
        return f"dismount:{self.canon[0]}:{self.canon[1]}"


class FromTile(N.Navigator):
    """The on-foot Navigator started on a given land tile (flood fill of a landing)."""

    def __init__(
        self, state: MapState, start: tuple[int, int], constraints: N.Constraints | None = None
    ) -> None:
        super().__init__(state, constraints)
        self._start = (start[0], start[1], LAND_ELEVATION)

    def start_node(self) -> N.Node:
        return self._start


def movement_tiles(state: MapState, nav: N.Navigator) -> tuple[tuple[int, int], ...]:
    """Bounded free-swimming targets; ordinary target planning still proves each path."""
    if state.player is None or not state.player.surfing:
        return ()
    targets = []
    for direction in N.DIRECTIONS:
        node = nav.start_node()
        endpoint = None
        for _ in range(max(state.layout.width, state.layout.height)):
            stepped = nav.step(node, direction)
            if stepped is None:
                break
            nxt, move = stepped
            if (
                move.kind != "walk"
                or move.warp_index is not None
                or move.connection is not None
                or move.script
                or not surf_nav.is_swimmer(nav, nxt)
            ):
                break
            endpoint = move.dst
            node = nxt
        if endpoint is not None:
            targets.append(endpoint)
    return tuple(targets)


# -- water bodies
# -----------------------------------------------------------------------------------


def _is_water(nav: N.Navigator, x: int, y: int) -> bool:
    outside, collision, _elev, info = nav._tile(x, y)
    return not outside and not collision and info.surfable and not info.waterfall


def water_bodies(state: MapState, nav: N.Navigator) -> list[WaterBody]:
    """Connected components of surfable tiles inside the map rectangle, ordered by their id."""
    lay = state.layout
    todo = {(x, y) for y in range(lay.height) for x in range(lay.width) if _is_water(nav, x, y)}
    bodies: list[WaterBody] = []
    while todo:
        start = min(todo)
        todo.discard(start)
        stack, members = [start], {start}
        while stack:
            x, y = stack.pop()
            for nxt in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if nxt in todo:
                    todo.discard(nxt)
                    members.add(nxt)
                    stack.append(nxt)
        bodies.append(WaterBody(min(members), frozenset(members)))
    return bodies


def shore_edges(
    state: MapState, body: WaterBody, search: N.Search | None = None
) -> list[ShoreEdge]:
    """Every ``(stand, facing)`` from which Surf starts on ``body``; with ``search`` only the
    ones the
    player can stand on now (a land node of the foot search), cheapest first."""
    out: list[tuple[int, ShoreEdge]] = []
    for wx, wy in body.tiles:
        for d, (vx, vy) in K.VECTOR.items():
            stand = (wx - vx, wy - vy)
            if search is not None:
                nodes = [n for n in search.nodes_at(*stand) if n[2] == LAND_ELEVATION]
                if not nodes:
                    continue
                cost = min(search.cost[n] for n in nodes)
            else:
                cost = 0
            rule = start_surf(state, d, from_xy=stand)  # type: ignore[arg-type]
            if rule.ok and rule.water == (wx, wy):
                out.append((cost, ShoreEdge(stand, d, (wx, wy))))  # type: ignore[arg-type]
    out.sort(key=lambda item: (item[0], item[1].stand, item[1].face))
    return [edge for _, edge in out]


# -- exits
# ------------------------------------------------------------------------------------------


def exit_targets(state: MapState, nav: N.Navigator) -> list[tuple[Exit, T.Target]]:
    """Warps and connections of the map as (exit, target); inert / script-driven ones are left
    out."""
    out: list[tuple[Exit, T.Target]] = []
    ident = state.identity
    for warp in state.warps:
        target = T.warp_target(state, nav, warp)
        if target.unsupported or target.unavailable:
            continue
        info = target.info
        dest = tuple(info["dest"]) if info["dest"] else None
        out.append(
            (Exit(target.option_id, "warp", dest, info["dest_name"], index=warp.index), target)
        )
    for target in T.edge_targets(state):
        info = target.info
        out.append(
            (
                Exit(
                    target.option_id,
                    "edge",
                    tuple(info["dest"]),
                    info["dest_name"],
                    direction=info["direction"],
                ),
                target,
            )
        )
    del ident
    return out


def reachable_exits(
    exits: list[tuple[Exit, T.Target]], nav: N.Navigator, search: N.Search
) -> list[tuple[Exit, T.Plan]]:
    out = []
    for exit_, target in exits:
        plan = T.plan_target(nav, search, target)
        if plan is not None:
            out.append((exit_, plan))
    return out


def unlocked_by_body(
    state: MapState,
    bodies: list[WaterBody],
    exits: list[tuple[Exit, T.Target]],
    foot_nav: N.Navigator,
    foot_search: N.Search,
) -> dict[tuple[int, int], list[Exit]]:
    """Exits that only Surf makes reachable, by the water body of the FIRST mount on their
    cheapest path
    (the relaxation search: on foot, mount where the start rule holds, swim, step ashore, walk
    on)."""
    relaxed = N.Navigator(state, N.Constraints(water=True))
    rsearch = relaxed.search()
    owner = {tile: body.canon for body in bodies for tile in body.tiles}
    out: dict[tuple[int, int], list[Exit]] = {}
    for exit_, target in exits:
        if T.plan_target(foot_nav, foot_search, target) is not None:
            continue
        plan = T.plan_target(relaxed, rsearch, target)
        if plan is None:
            continue
        mount = next((m for m in plan.moves if m.kind == "surf_start"), None)
        if mount is None or mount.dst not in owner:
            continue
        out.setdefault(owner[mount.dst], []).append(exit_)
    return out


# -- landings
# ---------------------------------------------------------------------------------------


def landing_groups(state: MapState, nav: N.Navigator, search: N.Search) -> list[LandingGroup]:
    """The shore tiles a swimmer can step onto, grouped by the piece of land they open into."""
    landings = sorted(
        {
            move.dst
            for node in search.cost
            if surf_nav.is_swimmer(nav, node)
            for direction in N.DIRECTIONS
            if (stepped := nav.step(node, direction)) is not None
            and (move := stepped[1]).kind == "dismount"
        }
    )
    exits = exit_targets(state, nav)
    groups: list[LandingGroup] = []
    covered: set[tuple[int, int]] = set()
    for landing in landings:
        if landing in covered:
            continue
        flood_nav = FromTile(state, landing)
        flood = flood_nav.search()
        region = {(n[0], n[1]) for n in flood.cost}
        members = tuple(sorted(tile for tile in landings if tile in region))
        covered.update(members)
        found = tuple(e for e, _plan in reachable_exits(exits, flood_nav, flood))
        groups.append(LandingGroup(members[0], members, len(region), found))
    return groups


def surf_mode(state: MapState) -> bool:
    p = state.player
    return p is not None and p.surfing


__all__ = [
    "Exit",
    "FromTile",
    "LandingGroup",
    "ShoreEdge",
    "WaterBody",
    "exit_targets",
    "landing_groups",
    "reachable_exits",
    "shore_edges",
    "surf_mode",
    "surf_nav",
    "unlocked_by_body",
    "water_bodies",
]
