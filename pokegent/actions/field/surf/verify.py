"""Postconditions of the Surf options.

``surf:<x>:<y>`` is verified by the existing ``field.interaction_started`` (the question is on
the screen);
the Surf itself is checked by :func:`surfing_on_water` on the frames after the dialog family's
YES, which
is the position/state check the live test and the objective tests use. ``dismount:<x>:<y>`` is
verified
here: same map, no longer surfing, standing on land at elevation 3.
"""

from __future__ import annotations

from pokegent.actions._exec.registry import VerifierSpec, VerifyContext
from pokegent.actions.field.surf import options as O
from pokegent.games.gen3.firered.maps.map_behaviors import behavior_info
from pokegent.games.gen3.firered.maps.map_schema import MapState
from pokegent.games.gen3.firered.maps.map_surf import LAND_ELEVATION


def surfing_on_water(state: MapState | None) -> bool:
    """The avatar surfs and the tile under it is surfable water (position AND state, never one
    alone)."""
    p = state.player if state is not None else None
    if p is None or not p.surfing:
        return False
    return behavior_info(state.behavior(p.avatar_x, p.avatar_y)).surfable


def on_land(state: MapState | None) -> bool:
    p = state.player if state is not None else None
    if p is None or p.surfing or p.elevation != LAND_ELEVATION:
        return False
    return not behavior_info(state.behavior(p.avatar_x, p.avatar_y)).surfable


def dismounted(ctx: VerifyContext) -> bool:
    after, before = ctx.after.map, ctx.before.map
    if after is None or before is None or after.player is None:
        return False
    same_map = (after.identity.group, after.identity.number) == (
        before.identity.group,
        before.identity.number,
    )
    return same_map and on_land(after)


def verifier_specs() -> list[VerifierSpec]:
    return [VerifierSpec(O.PC_DISMOUNTED, dismounted, max_frames=60, max_wall_s=10.0)]
