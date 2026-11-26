"""Game-owned composition of FireRed's RAM/ROM menu sources.

The default runner previously constructed :class:`FireRedMenuResolver`, loaded
:class:`GameTables` and owned the ferry-aware multichoice closure inline.  This module keeps
those exact behaviors next to the FireRed reader/tables that implement them; it is not a
second registry and adds no fallback semantics.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

MenuEntry = tuple[str, str]
MenuEntryProvider = Callable[..., tuple[MenuEntry, ...] | None]


@dataclass(frozen=True)
class FireRedMenuServices:
    """The live resolver, optional decoded tables and the resolved-menu closure."""

    menus: Any
    tables: Any
    menu_entries: MenuEntryProvider


def make_menu_services(
    actor: Any,
    profile: Any,
    rom_path: Any = None,
    *,
    navigation: Any = None,
    session: Any = None,
    logger: Any = None,
) -> FireRedMenuServices:
    """Compose the menu sources exactly as the default FireRed runner did."""
    from pokegent.games.gen3.firered.reader.menu_rom import FireRedMenuResolver

    menus = FireRedMenuResolver(actor, profile)
    tables = None
    if rom_path is not None:
        try:
            from pokegent.games.gen3.firered.rom.rom_tables import GameTables

            tables = GameTables.from_rom_file(rom_path)
        except Exception as exc:
            if logger is not None:
                logger.warning("ROM tables unavailable for %s: %s", rom_path, exc)

    def menu_entries(snapshot, menu_id, count):
        entries = menus.multichoice(snapshot, menu_id, count)
        resolved_entries = (
            tuple((entry.stable_key, entry.label) for entry in entries) if entries else None
        )
        if navigation is not None and session is not None:
            resolved_entries = navigation.ferry_menu_entries(
                snapshot,
                session.store.resolved(),
                count,
                resolved_entries,
                menu_id=menu_id,
            )
        return resolved_entries

    return FireRedMenuServices(menus=menus, tables=tables, menu_entries=menu_entries)
