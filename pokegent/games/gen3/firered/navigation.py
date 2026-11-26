"""FireRed-owned long-distance navigation construction (#139).

The factory forwards caller-supplied admitted bytes/profile to the existing graph, adapter and
optional directory constructors in the app's original order; it reads no path or actor and
catches nothing. It is not an admission or digest gate. ``make_objective_directory`` is the
standalone lazy constructor for the ROM-only directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pokegent.navigation import MapGraph, NavigationAdapter, NavigationGraph


@dataclass(frozen=True, slots=True)
class FireRedNavigationServices:
    """The exact navigation objects one offline run needs, built or omitted."""

    graph: Any
    navigation: Any
    directory: Any


def make_objective_directory(rom_bytes: bytes, profile: Any) -> Any:
    """Build the ROM-only objective directory from already-admitted bytes/profile."""
    from pokegent.goals.directory import RomMapDirectory

    return RomMapDirectory(rom_bytes, profile)


def make_navigation_services(
    rom_bytes: bytes,
    profile: Any,
    *,
    events: Any,
    needs_directory: bool = False,
) -> FireRedNavigationServices:
    """Build FireRed's map graph, navigation adapter and optional objective directory."""
    graph = MapGraph.from_rom(rom_bytes, profile)
    navigation = NavigationAdapter(
        NavigationGraph.from_rom(rom_bytes, profile), events=events
    )
    directory = None
    if needs_directory:
        directory = make_objective_directory(rom_bytes, profile)
    return FireRedNavigationServices(graph, navigation, directory)
