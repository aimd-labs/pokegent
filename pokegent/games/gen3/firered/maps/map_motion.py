"""Single-step movement prediction on a :class:`MapState` (spec 6.B ``field.free``, 11.3 verifier).

``predict_step`` re-implements the game's player collision rules over the data the map reader
extracted, so a navigator can ask "what happens if I press this direction now?" without moving.
The rules follow the decompile (pinned commit in ``source-lock.md``):

* ``src/field_player_avatar.c`` ``CheckForPlayerAvatarCollision`` / ``CheckForObjectEventCollision``
  (directional stair warps, stop-surfing, ledge jump, boulder push),
* ``src/event_object_movement.c`` ``GetCollisionAtCoords`` (grid collision, map border and
  connection ring, directional impassable edges, camera edge, elevation mismatch, object collision),
* ``src/fieldmap.c`` ``MapGridGetCollisionAt`` / ``GetMapBorderIdAt``.

Scope: a player on foot or surfing (bikes add rail/slope rules this module does not model; the
prediction is flagged ``bike=True`` and consumers must treat it as unverified). Forced movement
(ice, slide, current, spin, conveyor) is reported as a fact of the destination tile;
:func:`traverse_forced` simulates its bounded landing for the field planner and macro.

Truth test: ``historical local validation`` compares these predictions with the
real emulator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pokegent.games.gen3.firered.maps import map_constants as K
from pokegent.games.gen3.firered.maps.map_behaviors import behavior_info, blocks_side
from pokegent.games.gen3.firered.maps.map_schema import Facing, LiveObject, MapState
from pokegent.games.gen3.firered.maps.obstacles import ROLES

Outcome = Literal[
    "move",
    "blocked",
    "ledge_jump",
    "stop_surfing",
    "push_boulder",
    "stair_warp",
    "arrow_warp",
    "door_warp",
]
BlockReason = Literal["collision", "outside", "edge", "elevation", "object", "signpost"]

DIRECTIONS: tuple[Facing, ...] = ("N", "S", "W", "E")
FIELD_OBSTACLE_ROLES = ROLES | {"fossil"}


@dataclass(frozen=True)
class StepPrediction:
    """What pressing ``direction`` does, per the game's rules.

    ``outcome``: ``move`` (one tile; may still end on a warp tile, see ``warp_index``),
    ``blocked`` (turn in place only; ``reason`` says why), ``ledge_jump`` (lands two tiles away),
    ``stop_surfing`` (steps from water to land), ``push_boulder`` (Strength boulder moves one tile,
    the player follows), ``stair_warp`` (directional stair tile: the warp fires, no walk),
    ``arrow_warp`` (standing on an arrow-warp tile, the press fires the warp after a held press of
    about a second), ``door_warp`` (pressing north toward a warp door fires the warp; the door
    tile itself has collision and is never walked onto; the player must already face north).
    ``warp_index`` names the warp event for all warp outcomes.
    """

    direction: Facing
    outcome: Outcome
    from_xy: tuple[int, int]
    to_xy: tuple[int, int]
    reason: BlockReason | None = None
    landing_xy: tuple[int, int] | None = None
    behavior: int | None = None  # metatile behavior of the destination tile
    behavior_name: str | None = None
    object_local_id: int | None = None  # blocking / pushed object, if any
    warp_index: int | None = None  # warp event on the destination tile
    forced_move: Facing | None = None  # destination pushes the player on (slide, current ...)
    ice: bool = False  # destination is ice (the player keeps sliding in the move direction)
    encounter_risk: bool = False  # destination is tall grass / water-encounter tile
    # indices of coord events on the destination tile whose trigger var matches now: stepping there
    # starts a script that takes the controls (the move itself still happens)
    coord_events: tuple[int, ...] = ()
    connection: Literal["N", "S", "W", "E"] | None = None  # the step enters a connected map's ring
    consistent: bool = True  # False while SaveBlock1.pos and the avatar disagree (do not trust)
    bike: bool = False  # bike rules are not modelled

    @property
    def warps(self) -> bool:
        return self.outcome in ("stair_warp", "arrow_warp", "door_warp")

    @property
    def walks(self) -> bool:
        """True when the player's tile changes (including the ledge landing)."""
        return self.outcome in ("move", "ledge_jump", "stop_surfing", "push_boulder")

    @property
    def end_xy(self) -> tuple[int, int]:
        """Where the player stands afterwards (before any forced movement or warp)."""
        if self.outcome == "ledge_jump" and self.landing_xy:
            return self.landing_xy
        return self.to_xy if self.walks else self.from_xy


@dataclass(frozen=True)
class ForcedTraversal:
    """The bounded landing reached after stepping onto a forced-movement tile."""

    entry: StepPrediction
    landing_xy: tuple[int, int]
    tiles: tuple[tuple[int, int], ...]
    blocked: bool = False
    loop: bool = False
    warp_index: int | None = None


def traverse_forced(
    state: MapState,
    direction: Facing,
    *,
    from_xy: tuple[int, int] | None = None,
    limit: int = 64,
) -> ForcedTraversal | None:
    """Simulate one press entering ice, a spinner, conveyor, slide, or current.

    The first step is the player's input. Subsequent steps use the destination tile's forced
    direction; ice keeps the input direction. A wall ends the slide on the current tile, while a
    repeated tile/direction is rejected as a loop.
    """
    first = predict_step(state, direction, from_xy=from_xy)
    if not first.walks:
        return None
    current = first.end_xy
    tiles = [current]
    seen: set[tuple[tuple[int, int], Facing]] = set()
    warp_index = first.warp_index
    move_direction = direction
    spin_direction: Facing | None = None
    for _ in range(limit):
        info = behavior_info(state.behavior(*current))
        # FireRed remembers the most recent spin tile and keeps applying that direction across
        # ordinary floor.  Only MB_STOP_SPINNING releases the avatar; another spin tile changes
        # the remembered direction (field_player_avatar.c TryUpdatePlayerSpinDirection).
        if spin_direction is not None and info.kind == "stop_spin":
            return ForcedTraversal(first, current, tuple(tiles), warp_index=warp_index)
        if info.kind == "spin":
            spin_direction = info.forced
        forced_direction = spin_direction or info.forced or (move_direction if info.ice else None)
        if forced_direction is None:
            return ForcedTraversal(first, current, tuple(tiles), warp_index=warp_index)
        marker = (current, forced_direction)
        if marker in seen:
            return ForcedTraversal(first, current, tuple(tiles), loop=True, warp_index=warp_index)
        seen.add(marker)
        next_step = predict_step(state, forced_direction, from_xy=current)
        if next_step.warp_index is not None:
            return ForcedTraversal(first, current, tuple(tiles), warp_index=next_step.warp_index)
        if not next_step.walks:
            return ForcedTraversal(
                first, current, tuple(tiles), blocked=True, warp_index=warp_index
            )
        current = next_step.end_xy
        tiles.append(current)
        warp_index = next_step.warp_index or warp_index
        move_direction = forced_direction
    return ForcedTraversal(first, current, tuple(tiles), loop=True, warp_index=warp_index)


def _elevations_compatible(a: int, b: int) -> bool:
    """AreElevationsCompatible (src/event_object_movement.c)."""
    return a == 0 or b == 0 or a == b


def _live(state: MapState, skip: LiveObject | None = None) -> list[LiveObject]:
    return [o.live for o in state.objects if o.live is not None and o.live is not skip]


def _object_at(
    state: MapState, x: int, y: int, elevation: int, skip: LiveObject | None = None
) -> LiveObject | None:
    """``DoesObjectCollideWithObjectAt``: current or previous tile, compatible elevation."""
    for live in _live(state, skip):
        if (live.x, live.y) == (x, y) or (live.prev_x, live.prev_y) == (x, y):
            if _elevations_compatible(elevation, live.elevation):
                return live
    return None


def _edge_blocked(state: MapState, sx: int, sy: int, dx: int, dy: int, d: Facing) -> bool:
    """``IsMetatileDirectionallyImpassable``: the source tile's ``d`` side or the destination's
    opposite side blocks (MB_IMPASSABLE_*)."""
    return blocks_side(state.behavior(sx, sy), d) or blocks_side(
        state.behavior(dx, dy), K.OPPOSITE[d]
    )


def _collision(
    state: MapState,
    sx: int,
    sy: int,
    d: Facing,
    *,
    elevation: int,
    camera: bool,
    skip: LiveObject | None = None,
    camera_xy: tuple[int, int] | None = None,
) -> tuple[Literal["none"] | BlockReason, LiveObject | None]:
    """``GetCollisionAtCoords`` for a mover at ``(sx, sy)`` heading ``d``."""
    vx, vy = K.VECTOR[d]
    x, y = sx + vx, sy + vy
    # MapGridGetCollisionAt || GetMapBorderIdAt == CONNECTION_INVALID: both are COLLISION_IMPASSABLE
    # in the game; the reason keeps them apart (undefined block / no connection = the void).
    if state.raw_block(x, y) == K.MAPGRID_UNDEFINED or state.border_id(x, y) == -1:
        return "outside", None
    if state.collision(x, y):
        return "collision", None
    if _edge_blocked(state, sx, sy, x, y, d):
        return "edge", None
    if camera:
        cx, cy = camera_xy or (sx, sy)
        if state.border_id(cx + vx, cy + vy) == -1:  # CanCameraMoveInDirection
            return "outside", None
    map_elevation = state.elevation(x, y)
    if elevation != 0 and map_elevation not in (0, 15) and map_elevation != elevation:
        return "elevation", None
    blocker = _object_at(state, x, y, elevation, skip)
    if blocker is not None:
        return "object", blocker
    return "none", None


def _walk_into_signpost(state: MapState, destination: tuple[int, int], direction: Facing) -> bool:
    """Return whether walking into ``destination`` starts FireRed's signpost script.

    ``TrySetUpWalkIntoSignpostScript`` in ``src/field_control_avatar.c`` runs before movement
    and only handles north/south input.  It resolves the background event at the destination,
    while ``GetFacingSignpostType`` requires a sign-family metatile behavior.
    """
    if direction not in ("N", "S"):
        return False
    if behavior_info(state.behavior(*destination)).kind != "sign":
        return False
    elevation = state.elevation(*destination)
    return any(
        event.kind == "sign"
        and (event.x, event.y) == destination
        and event.elevation in (0, elevation)
        and (event.sign_facing is None or event.sign_facing == direction)
        for event in state.bg_events
    )


def predict_step(
    state: MapState,
    direction: Facing,
    *,
    strength_active: bool | None = None,
    from_xy: tuple[int, int] | None = None,
) -> StepPrediction:
    """Predict the result of moving the player one step in ``direction``.

    ``strength_active`` overrides ``state.strength_active`` (FLAG_SYS_USE_STRENGTH); unknown
    (None) is treated as "boulders do not move".
    """
    player = state.player
    sx, sy = from_xy or (player.avatar_x, player.avatar_y)
    camera_xy = (player.save_x, player.save_y) if from_xy is None else (sx, sy)
    vx, vy = K.VECTOR[direction]
    dest = (sx + vx, sy + vy)
    dest_behavior = state.behavior(*dest)
    info = behavior_info(dest_behavior)
    common = {
        "direction": direction,
        "from_xy": (sx, sy),
        "to_xy": dest,
        "behavior": dest_behavior,
        "behavior_name": info.name,
        "consistent": player.position_consistent,
        "bike": player.biking,
        "forced_move": info.forced,
        "ice": info.ice,
        "encounter_risk": info.tall_grass,
        "coord_events": tuple(
            c.index for c in state.coord_events if (c.x, c.y) == dest and c.active
        ),
    }

    # CheckForPlayerAvatarCollision: a directional stair tile under the player fires first
    source = behavior_info(state.behavior(sx, sy))
    if source.kind == "stairs" and source.warp_direction == direction:
        return StepPrediction(
            outcome="stair_warp", warp_index=_warp_at(state, (sx, sy), player.elevation), **common
        )
    # TryArrowWarp: standing on an arrow tile with a warp event, pressing its direction
    if source.kind == "arrow_warp" and source.warp_direction == direction:
        index = _warp_at(state, (sx, sy), player.elevation)
        if index is not None:
            return StepPrediction(outcome="arrow_warp", warp_index=index, **common)
    # TryDoorWarp: facing a warp door in front and pressing north (the door tile has collision)
    if direction == "N" and info.kind == "door":
        index = _warp_at(state, dest, player.elevation)
        if index is not None:
            return StepPrediction(outcome="door_warp", warp_index=index, **common)

    if _walk_into_signpost(state, dest, direction):
        return StepPrediction(outcome="blocked", reason="signpost", **common)

    reason, blocker = _collision(
        state,
        sx,
        sy,
        direction,
        elevation=player.elevation,
        camera=True,
        camera_xy=camera_xy,
    )
    warp_index = next((w.index for w in state.warps if (w.x, w.y) == dest), None)

    # CheckForObjectEventCollision
    if reason == "elevation" and player.surfing:
        if state.elevation(*dest) == 3 and not _object_at(state, *dest, 3):
            return StepPrediction(outcome="stop_surfing", warp_index=warp_index, **common)
    if info.ledge == direction:
        landing = (dest[0] + vx, dest[1] + vy)
        return StepPrediction(outcome="ledge_jump", landing_xy=landing, **common)
    if reason == "object" and blocker is not None:
        if _can_push_boulder(state, blocker, direction, strength_active):
            return StepPrediction(
                outcome="push_boulder", object_local_id=blocker.local_id, **common
            )
        return StepPrediction(
            outcome="blocked", reason="object", object_local_id=blocker.local_id, **common
        )
    if reason != "none":
        return StepPrediction(outcome="blocked", reason=reason, **common)  # type: ignore[arg-type]
    return StepPrediction(
        outcome="move", warp_index=warp_index, connection=_connection_of(state, dest), **common
    )


def _warp_at(state: MapState, xy: tuple[int, int], elevation: int) -> int | None:
    """``GetWarpEventAtPosition``: a warp event on the tile whose elevation is 0 or the player's."""
    for warp in state.warps:
        if (warp.x, warp.y) == xy and warp.elevation in (0, elevation):
            return warp.index
    return None


def _connection_of(state: MapState, dest: tuple[int, int]) -> Literal["N", "S", "W", "E"] | None:
    """Direction of the connection a tile outside the map rectangle belongs to, else None."""
    x, y = dest
    if 0 <= x < state.layout.width and 0 <= y < state.layout.height:
        return None
    return {1: "S", 2: "N", 3: "W", 4: "E"}.get(state.border_id(x, y))  # type: ignore[return-value]


def boulder_push_check(
    state: MapState,
    x: int,
    y: int,
    elevation: int,
    direction: Facing,
    skip: LiveObject | None = None,
) -> tuple[Literal["falls", "moves", "blocked"], str | None]:
    """The far-tile half of ``TryPushBoulder``: a boulder at ``(x, y)`` pushed toward ``direction``.

    ``falls``: the far tile is ``MB_FALL_WARP`` (always accepted, whatever else is there);
    ``moves``: ``GetCollisionAtCoords`` is ``COLLISION_NONE`` for the boulder (grid collision, the
    border, directional impassable edges on either tile, elevation mismatch, any object on the far
    tile; no camera edge) and the far tile is not a non-animated door; ``blocked`` has the reason
    (``collision`` / ``outside`` / ``edge`` / ``elevation`` / ``object`` / ``door``).
    """
    vx, vy = K.VECTOR[direction]
    nx, ny = x + vx, y + vy
    behavior = state.behavior(nx, ny)
    if behavior == K.MB["MB_FALL_WARP"]:
        return "falls", None
    reason, _ = _collision(state, x, y, direction, elevation=elevation, camera=False, skip=skip)
    if reason != "none":
        return "blocked", reason
    if behavior == K.MB["MB_CAVE_DOOR"]:
        return "blocked", "door"
    return "moves", None


def _can_push_boulder(
    state: MapState, obj: LiveObject, direction: Facing, strength_active: bool | None
) -> bool:
    """``TryPushBoulder``: Strength active, a pushable boulder, free tile behind it."""
    active = state.strength_active if strength_active is None else strength_active
    if not active or obj.graphics_id != K.OBJ_EVENT_GFX_PUSHABLE_BOULDER:
        return False
    outcome, _ = boulder_push_check(state, obj.x, obj.y, obj.elevation, direction, skip=obj)
    return outcome != "blocked"


def neighbors(state: MapState, **kwargs) -> dict[Facing, StepPrediction]:
    """``predict_step`` for the four directions."""
    return {d: predict_step(state, d, **kwargs) for d in DIRECTIONS}


def object_gone(state: MapState, local_id: int) -> bool:
    """Whether a field obstacle is no longer blocking its local-id tile."""
    obj = state.object_by_local_id(local_id)
    return obj is None or obj.hidden_flag_set is True or not obj.blocks_tile
