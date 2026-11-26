"""Opt-in FireRed snapshot projection for the generic observation envelope (#139).

This is internal SDK projection data only. It is not a provider payload and not a
redaction claim, so projected fields are not safe to expose by default. The caller
supplies ``restore_epoch`` explicitly; this module never infers restore authority.
"""

from __future__ import annotations

from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.games.observation import (
    GameObservation,
    ObservationRejected,
    create_observation,
)

FIRERED_GAME_ID = "firered"


def project_observation(
    snapshot: StateSnapshot,
    *,
    restore_epoch: int,
    enabled: bool = False,
) -> GameObservation:
    """Project a validated FireRed snapshot onto a frozen generic observation.

    The exact-bool opt-in and snapshot type are checked before any snapshot field is
    read. A usable snapshot contributes exactly ``map_group``, ``map_num``, ``x`` and
    ``y`` from ``snapshot.player``; an unusable snapshot contributes an empty payload.
    """
    if type(enabled) is not bool:
        raise ObservationRejected("enabled must be an exact bool")
    if not enabled:
        raise ObservationRejected("observation projection is opt-in; pass enabled=True")
    if not isinstance(snapshot, StateSnapshot):
        raise ObservationRejected("snapshot must be a StateSnapshot")

    payload: dict[str, int] = {}
    if snapshot.usable:
        payload = {
            "map_group": snapshot.player.map_group,
            "map_num": snapshot.player.map_num,
            "x": snapshot.player.x,
            "y": snapshot.player.y,
        }
    return create_observation(
        enabled=True,
        game_id=FIRERED_GAME_ID,
        profile_id=snapshot.rom_profile,
        snapshot_seq=snapshot.snapshot_seq,
        restore_epoch=restore_epoch,
        usable=snapshot.usable,
        payload=payload,
    )
