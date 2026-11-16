"""Surf capability facts and the reference data (``references/field_surf.yaml``).

A capability is a FACT of the snapshot: a party member that knows Surf and the Soul Badge. The field
builder offers Surf either way and says what is missing (a diagnostic); nothing here teaches a
move or
changes the game. The numbers are verified (see the yaml): the Soul Badge flag by clearing it on a
scratch copy of a real state, the move id by the party of the Route 23 fixtures.
"""

from __future__ import annotations

from functools import lru_cache

from pokegent.actions.field.capabilities import Capability
from pokegent.games.gen3 import mechanics_constants as MK
from pokegent.games.gen3 import mechanics_ids as I
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.local_data import read_data

_LOCAL_REFERENCE = read_data("field_surf.yaml", optional=True)
SURF_BADGE = MK.BADGE_SOUL
SURF_BADGE_NAME = "Soul"


@lru_cache(maxsize=1)
def reference() -> dict:
    return read_data("field_surf.yaml")


def surf_capability(snapshot: StateSnapshot) -> Capability:
    """Fail-closed: ``available`` only with a party member that knows Surf AND the Soul Badge."""
    known = snapshot.usable and snapshot.player is not None
    carriers = tuple(
        sorted(
            {m.species for m in snapshot.party if any(mv.move_id == I.MOVE_SURF for mv in m.moves)}
        )
    )
    badges = snapshot.player.badges if snapshot.player is not None else ()
    return Capability(
        "Surf",
        I.MOVE_SURF,
        SURF_BADGE,
        SURF_BADGE_NAME,
        known=known,
        available=known and bool(carriers) and SURF_BADGE in badges,
        carrier_species=carriers,
    )
