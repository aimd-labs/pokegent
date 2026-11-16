"""Surf on a :class:`MapState`: when Surf can start, when the surfer can step ashore (map layer).

The movement of a player who is ALREADY surfing is part of
:func:`pokegent.games.gen3.firered.maps.map_motion.predict_step`
(water to water, ``stop_surfing`` onto elevation-3 land). This module adds the two rules that
``predict_step`` cannot express because they are not a step:

* **starting Surf** (:func:`start_surf`): the player stands on land at elevation 3, faces a tile
that
  the grid does not block but whose elevation differs (``COLLISION_ELEVATION_MISMATCH``), and that
  tile is surfable, fishable water (``IsPlayerFacingSurfableFishableWater``,
  ``field_control_avatar.c``).
  The game then asks the field-move question and, after YES, moves the player ONTO that water tile;
* **the water classes** the rules are verified for (``references/field_surf.yaml`` has the
  provenance of every entry; this module is the machine-readable copy the tests compare).

The capability side (a party member that knows Surf, the Soul Badge) is NOT here: it is a fact
of the
snapshot, owned by ``actions/field/surf``. Nothing here reads RAM beyond the map block.
"""

from __future__ import annotations

from dataclasses import dataclass

from pokegent.games.gen3.firered.maps import map_constants as K
from pokegent.games.gen3.firered.maps.map_behaviors import behavior_info
from pokegent.games.gen3.firered.maps.map_motion import _collision, _object_at
from pokegent.games.gen3.firered.maps.map_schema import Facing, MapState

#: the elevation a player must have to start Surf, and of every land tile a surfer may step onto
#: (``IsPlayerFacingSurfableFishableWater``: ``PlayerGetElevation() == 3``; ``CanStopSurfing``:
#: ``MapGridGetElevationAt == 3``). Measured: Route 23 water tiles are elevation 1, land 3.
LAND_ELEVATION = 3

#: behaviors the Surf prompt appears for. MB_OCEAN_WATER is measured on the real ROM (Route 23,
#: Route 21); MB_POND_WATER follows the decompile (``MetatileBehavior_IsSurfableFishableWater``) and
#: has not been measured with a Surf-capable party (no fixture has one).
START_OK = frozenset({K.MB["MB_OCEAN_WATER"], K.MB["MB_POND_WATER"]})
#: water the game refuses with "The current is much too fast!" (Sevii Islands only in this ROM)
START_TOO_FAST = frozenset({K.MB["MB_FAST_WATER"]})
#: surfable water the player can swim in but the Surf prompt is not known to appear for
START_UNTESTED = frozenset(
    {
        K.MB["MB_DEEP_WATER"],
        K.MB["MB_UNUSED_WATER"],
        K.MB["MB_CYCLING_ROAD_WATER"],
    }
)


@dataclass(frozen=True)
class SurfStart:
    """Whether Surf can be started from a tile in a direction, per the map alone.

    ``ok`` is True when the game would show the Surf prompt (ignoring the party and the badge);
    otherwise ``reason`` names the first failed condition: ``not_standing_on_land`` (elevation),
    ``blocked`` (wall, edge, map border), ``no_elevation_change`` (walkable in front),
    ``not_surfable`` (a land tile at another elevation), ``current_too_fast``, ``water_untested``,
    ``object_on_water``.
    """

    ok: bool
    reason: str | None
    stand: tuple[int, int]
    direction: Facing
    water: tuple[int, int]
    behavior_name: str | None = None


def start_surf(
    state: MapState,
    direction: Facing,
    *,
    from_xy: tuple[int, int] | None = None,
    elevation: int | None = None,
) -> SurfStart:
    """The start rule for the tile in front of ``from_xy`` (default: the player) facing
    ``direction``.

    ``elevation`` is the standing elevation; by default the player's own, or for a hypothetical
    ``from_xy`` the elevation of that tile (which must then be exactly 3 and free of walls)."""
    player = state.player
    assert player is not None
    sx, sy = from_xy or (player.avatar_x, player.avatar_y)
    vx, vy = K.VECTOR[direction]
    water = (sx + vx, sy + vy)
    behavior = state.behavior(*water)
    info = behavior_info(behavior)

    def verdict(ok: bool, reason: str | None) -> SurfStart:
        return SurfStart(ok, reason, (sx, sy), direction, water, info.name)

    if player.surfing or player.biking or player.underwater:
        return verdict(False, "already_mounted")
    if elevation is None:
        elevation = player.elevation if from_xy is None else state.elevation(sx, sy)
    if (
        elevation != LAND_ELEVATION
        or behavior_info(state.behavior(sx, sy)).surfable
        or (from_xy is not None and state.collision(sx, sy))
    ):
        return verdict(False, "not_standing_on_land")
    camera_xy = (player.save_x, player.save_y) if from_xy is None else (sx, sy)
    reason, _ = _collision(
        state, sx, sy, direction, elevation=elevation, camera=True, camera_xy=camera_xy
    )
    if reason in ("outside", "collision", "edge"):
        return verdict(False, "blocked")
    if reason == "none" or reason == "object":
        return verdict(False, "no_elevation_change")
    # reason == "elevation": the tile is a different level; is it water the prompt appears for?
    if behavior in START_TOO_FAST:
        return verdict(False, "current_too_fast")
    if behavior in START_UNTESTED:
        return verdict(False, "water_untested")
    if behavior not in START_OK:
        return verdict(False, "not_surfable")
    if _object_at(state, water[0], water[1], state.elevation(*water)):
        return verdict(False, "object_on_water")
    return verdict(True, None)


def can_dismount(
    state: MapState, direction: Facing, *, from_xy: tuple[int, int] | None = None
) -> bool:
    """A surfer's step in ``direction`` ends the Surf (``CanStopSurfing``): the prediction
    says so."""
    from pokegent.games.gen3.firered.maps.map_motion import predict_step

    return predict_step(state, direction, from_xy=from_xy).outcome == "stop_surfing"
