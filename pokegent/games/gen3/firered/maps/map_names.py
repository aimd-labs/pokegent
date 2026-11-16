"""Read a user-supplied local map index; no copied map-label dataset is bundled."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pokegent.local_data import local_path, read_data

MAP_GROUPS_PATH_ENV = "POKEGENT_MAP_GROUPS"


def default_map_groups_path() -> Path | None:
    return local_path("pret_map_groups.json")


@lru_cache(maxsize=4)
def _load(path: str | Path | None) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    from pokegent.games.gen3.firered.rom.rom_cache import RomDataError
    from pokegent.local_data import LocalDataError

    try:
        document = read_data("pret_map_groups.json", path=Path(path) if path is not None else None)
    except LocalDataError as exc:
        raise RomDataError(str(exc)) from exc
    order = tuple(document["group_order"])
    groups = tuple(tuple(document["groups"][name]) for name in order)
    return order, groups


def group_names() -> tuple[str, ...]:
    return _load(default_map_groups_path())[0]


def group_sizes() -> tuple[int, ...]:
    return tuple(len(g) for g in _load(default_map_groups_path())[1])


def group_count() -> int:
    return len(group_names())


def map_name(group: int, number: int) -> str | None:
    """pret name of map ``(group, number)`` or ``None`` for an id outside the table."""
    groups = _load(default_map_groups_path())[1]
    if 0 <= group < len(groups) and 0 <= number < len(groups[group]):
        return groups[group][number]
    return None


def group_name(group: int) -> str | None:
    order = group_names()
    return order[group] if 0 <= group < len(order) else None


def find_map(name: str) -> tuple[int, int] | None:
    """Reverse lookup (case-insensitive) of a pret map name to ``(group, number)``."""
    wanted = name.lower()
    for group, names in enumerate(_load(default_map_groups_path())[1]):
        for number, candidate in enumerate(names):
            if candidate.lower() == wanted:
                return group, number
    return None
