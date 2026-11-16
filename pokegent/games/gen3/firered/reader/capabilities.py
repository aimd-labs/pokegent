"""Field-move capability facts used by the field option builder.

The move ids come from ``include/constants/moves.h`` and the badge gates from the
FireRed field-move checks.  This module only answers whether the current snapshot
proves a capability; it never teaches a move or changes the game.
"""

from __future__ import annotations

from dataclasses import dataclass

from pokegent.games.gen3 import mechanics_constants as MK
from pokegent.games.gen3 import mechanics_ids as I
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.games.gen3.firered.rom import constants as C


@dataclass(frozen=True)
class Capability:
    name: str
    move_id: int | None = None
    badge: int | None = None
    badge_name: str | None = None
    known: bool = False
    available: bool = False
    carrier_species: tuple[int, ...] = ()
    item_id: int | None = None


_FIELD_MOVES = {
    "cut": ("Cut", I.MOVE_CUT, MK.BADGE_CASCADE, "Cascade"),
    "smash": ("Rock Smash", I.MOVE_ROCK_SMASH, MK.BADGE_THUNDER, "Thunder"),
    "strength": ("Strength", I.MOVE_STRENGTH, MK.BADGE_RAINBOW, "Rainbow"),
    "flash": ("Flash", I.MOVE_FLASH, MK.BADGE_BOULDER, "Boulder"),
}


class FireRedCapabilities:
    """Capability predicates owned by the FireRed add-on.

    Core planners can depend on this small semantic surface without importing badge, item, or
    move ids.  The legacy module-level function remains for callers that have not migrated.
    """

    def field(self, snapshot: StateSnapshot, kind: str) -> Capability:
        return field_capability(snapshot, kind)

    def has_field_move(self, snapshot: StateSnapshot, kind: str) -> bool:
        return self.field(snapshot, kind).available


def field_capability(snapshot: StateSnapshot, kind: str) -> Capability:
    """Return a fail-closed capability fact for an obstacle kind."""
    if kind == "snorlax":
        known = snapshot.usable
        item = I.ITEM_POKE_FLUTE
        present = any(
            item_.item_id == item and item_.quantity > 0
            for pocket in snapshot.bag
            for item_ in pocket.items
        )
        return Capability("Poke Flute", known=known, available=known and present, item_id=item)
    entry = _FIELD_MOVES.get(kind)
    if entry is None:
        return Capability(kind, known=snapshot.usable, available=snapshot.usable)
    name, move_id, badge, badge_name = entry
    known = snapshot.usable and snapshot.player is not None
    carriers = tuple(
        sorted({m.species for m in snapshot.party if any(mv.move_id == move_id for mv in m.moves)})
    )
    badges = snapshot.player.badges if snapshot.player is not None else ()
    return Capability(
        name,
        move_id,
        badge,
        badge_name,
        known=known,
        available=known and bool(carriers) and badge in badges,
        carrier_species=carriers,
    )


def badge_flag(badge: int) -> int:
    """The saved flag for a badge, kept local to capability checks."""
    return C.FLAG_BADGE01_GET + badge - 1
