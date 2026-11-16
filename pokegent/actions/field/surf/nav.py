"""Navigation mode ``surf``: the :class:`~pokegent.actions.field.nav.Navigator` over water.

The Navigator is ONE graph; a node is ``(x, y, elevation)``. A node on a surfable water tile is a
swimmer, and :func:`surf_step` gives the swimmer's rules (``Navigator.step`` dispatches to it):

* water to water: the same elevation, no wall, no impassable edge, no object; a waterfall is a wall
  (no Waterfall field move is modelled); a current tile is forced movement (bounded landing, only
  with ``Constraints.forced``, one-way by construction);
* water to land: ONLY a land tile at elevation 3 with no object at elevation 3 (``CanStopSurfing``),
  a **dismount** move. A swimmer never walks onto a tile of another elevation (walls, cliffs, the
  elevation-0 and 15 "any level" tiles are refused: the game would keep the surf flag on land);
* the node after a dismount is a land node: the search goes on on foot, so a plan "swim, step
ashore,
  walk to the door" is one path.

:func:`mount_step` is the other direction, used ONLY under ``Constraints(water=True)`` (the
planner's
"what would Surf unlock" relaxation, never an executable plan): a land node facing surfable water
that :func:`~pokegent.games.gen3.firered.maps.map_surf.start_surf` accepts becomes a swimmer.
Starting Surf is an
event (a prompt), so the macros never follow a ``surf_start`` move; ``actions/field/surf`` has the
option for it.

The equivalence with ``predict_step`` for a surfer is tested over synthetic and real maps
(``historical local validation``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pokegent.games.gen3.firered.maps import map_constants as K
from pokegent.games.gen3.firered.maps.map_motion import traverse_forced
from pokegent.games.gen3.firered.maps.map_schema import Facing
from pokegent.games.gen3.firered.maps.map_surf import LAND_ELEVATION, start_surf

if TYPE_CHECKING:  # pragma: no cover
    from pokegent.actions.field.nav import Move, Navigator, Node

#: cost of the prompt + mount animation in the relaxation (prefers an equal walk on land)
MOUNT_COST = 10


def is_swimmer(nav: Navigator, node: Node) -> bool:
    """The node is on surfable water at a water elevation (not the elevation-3 land level)."""
    x, y, e = node
    info = nav._tile(x, y)[3]
    return info.surfable and not info.waterfall and e != LAND_ELEVATION


def surf_step(nav: Navigator, node: Node, d: Facing) -> tuple[Node, Move] | None:
    """The swimmer's move from ``node`` in direction ``d`` (``None``: the game does not allow
    it)."""
    from pokegent.actions.field.nav import ENCOUNTER_COST, Move

    x, y, e = node
    vx, vy = K.VECTOR[d]
    nx, ny = x + vx, y + vy
    outside, collision, map_elev, info = nav._tile(nx, ny)
    if outside or collision or nav._edge_blocked(x, y, nx, ny, d):
        return None
    if info.waterfall:
        return None  # a waterfall is a wall without the Waterfall field move
    warp = nav.warp_at(nx, ny, e)
    if info.surfable:
        if map_elev not in (0, 15) and map_elev != e:
            return None
        if nav.blocker_category(nx, ny, e) is not None:
            return None
        if info.forced is not None:  # a current: forced movement with a bounded landing
            if not nav.constraints.forced:
                return None
            return _current_step(nav, node, d)
        encounter_tiles = int(nav.encounter_risk_at(nx, ny))
        move = Move(
            direction=d,
            src=(x, y),
            dst=(nx, ny),
            kind="walk",
            cost=1 + ENCOUNTER_COST * encounter_tiles,
            encounter_tiles=encounter_tiles,
            script=(nx, ny) in nav._script_tiles,
            connection=nav._connection_of(nx, ny),
            warp_index=warp.index if warp is not None else None,
            behavior_name=info.name,
        )
        return (nx, ny, e if map_elev in (0, 15) else map_elev), move
    # land: only the dismount (CanStopSurfing); the object test uses elevation 3, not the swimmer's
    if map_elev != LAND_ELEVATION or nav.blocker_category(nx, ny, LAND_ELEVATION) is not None:
        return None
    encounter_tiles = int(nav.encounter_risk_at(nx, ny))
    move = Move(
        direction=d,
        src=(x, y),
        dst=(nx, ny),
        kind="dismount",
        cost=1 + ENCOUNTER_COST * encounter_tiles,
        grass=info.tall_grass,
        encounter_tiles=encounter_tiles,
        script=(nx, ny) in nav._script_tiles,
        connection=nav._connection_of(nx, ny),
        warp_index=warp.index if warp is not None else None,
        behavior_name=info.name,
    )
    return (nx, ny, LAND_ELEVATION), move


def _current_step(nav: Navigator, node: Node, d: Facing) -> tuple[Node, Move] | None:
    from pokegent.actions.field.nav import ENCOUNTER_COST, Move

    x, y, e = node
    traversal = traverse_forced(nav.state, d, from_xy=(x, y))
    if traversal is None or traversal.loop:
        return None
    land = traversal.landing_xy
    _, _, map_elev, info = nav._tile(*land)
    if not info.surfable or info.forced is not None or info.waterfall:
        return None  # a ride that ends on land or inside the next current is not a plan
    if land == (x, y):
        return None  # pushed straight back where it came from: no move
    warp = nav.warp_at(*land, e)
    encounter_tiles = sum(nav.encounter_risk_at(*tile) for tile in traversal.tiles)
    move = Move(
        direction=d,
        src=(x, y),
        dst=land,
        kind="forced",
        cost=1 + len(traversal.tiles) - 1 + ENCOUNTER_COST * encounter_tiles,
        encounter_tiles=encounter_tiles,
        connection=nav._connection_of(*land),
        warp_index=warp.index if warp is not None else None,
        behavior_name=nav._tile(x + K.VECTOR[d][0], y + K.VECTOR[d][1])[3].name,
        forced_tiles=traversal.tiles,
        forced_landing=land,
    )
    return (land[0], land[1], e if map_elev in (0, 15) else map_elev), move


def mount_step(nav: Navigator, node: Node, d: Facing) -> tuple[Node, Move] | None:
    """A land node facing water that the Surf prompt accepts: the relaxation's ``surf_start`` move.

    Returns ``None`` when the start rule does not hold (the caller falls back to the old rules),
    so a synthetic map whose water has the player's own elevation keeps its previous behaviour."""
    from pokegent.actions.field.nav import ENCOUNTER_COST, Move

    x, y, e = node
    if e != LAND_ELEVATION:
        return None
    rule = start_surf(nav.state, d, from_xy=(x, y), elevation=e)
    if not rule.ok:
        return None
    wx, wy = rule.water
    if nav.blocker_category(wx, wy, nav.state.elevation(wx, wy)) is not None:
        return None
    water_elev = nav.state.elevation(wx, wy)
    encounter_tiles = int(nav.encounter_risk_at(wx, wy))
    move = Move(
        direction=d,
        src=(x, y),
        dst=(wx, wy),
        kind="surf_start",
        cost=MOUNT_COST + ENCOUNTER_COST * encounter_tiles,
        encounter_tiles=encounter_tiles,
        behavior_name=rule.behavior_name or "",
    )
    return (wx, wy, water_elev if water_elev not in (0, 15) else 1), move
