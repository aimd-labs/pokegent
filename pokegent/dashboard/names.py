"""Display names for species, moves and types, from the ROM's own tables (read once, off-thread).

The viewer shows "Charmander", "Fire", "Ember", not ids. The names come from the same decoded ROM
tables the battle facts use (:class:`~pokegent.games.gen3.firered.rom.rom_tables.GameTables`):
no name list is
copied into the repository. Without tables (no ROM table loaded, an id out of range) every lookup
answers ``None`` and the telemetry shows the in-game nickname or the id instead.
"""

from __future__ import annotations

from typing import Any

from pokegent.games.gen3 import mechanics_constants as K


class NameBook:
    """Species / move / type names. ``tables`` is a ``GameTables`` or ``None``."""

    def __init__(self, tables: Any | None = None) -> None:
        self._tables = tables

    @property
    def available(self) -> bool:
        return self._tables is not None

    def species_name(self, species_id: int) -> str | None:
        return self._lookup(lambda t: t.species(species_id).name)

    def species_types(self, species_id: int) -> tuple[str, ...]:
        """The species' types as upper-case names, one or two, in the game's order."""
        raw = self._lookup(lambda t: t.species(species_id).types)
        if not raw:
            return ()
        names = [type_name(i) for i in raw]
        return tuple(dict.fromkeys(n for n in names if n))

    def move_name(self, move_id: int) -> str | None:
        return self._lookup(lambda t: t.move(move_id).name)

    def _lookup(self, getter: Any) -> Any:
        if self._tables is None:
            return None
        try:
            return getter(self._tables)
        except (KeyError, IndexError, ValueError):
            return None


def type_name(type_id: int) -> str | None:
    """Upper-case English type name; ``None`` for the ``???`` slot or an unknown id."""
    if 0 <= type_id < len(K.TYPE_NAMES):
        name = K.TYPE_NAMES[type_id]
        return None if name == "???" else name.upper()
    return None
