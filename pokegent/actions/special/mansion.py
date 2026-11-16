"""ROM-derived Pokemon Mansion switch facts.

Navigation owns route planning. This module only describes statue locations, the global flag, and
the metatiles affected by each switch.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from pokegent.games.gen3.firered.maps.map_schema import MapState
from pokegent.local_data import read_data


def _data() -> dict[str, Any]:
    return read_data("puzzles.yaml")["mansion"]


_LOCAL_PUZZLES = read_data("puzzles.yaml", optional=True)
SWITCH_FLAG = _LOCAL_PUZZLES["mansion"]["switch_flag"] if _LOCAL_PUZZLES else 0x26C
SECRET_KEY_TILE = (
    tuple(_LOCAL_PUZZLES["mansion"]["secret_key"]["tile"]) if _LOCAL_PUZZLES else None
)


@dataclass(frozen=True)
class MansionStatue:
    id: str
    floor: str
    map_key: tuple[int, int]
    statue: tuple[int, int]
    stand: tuple[int, int]
    facing: str
    opens: tuple[tuple[tuple[int, int], ...], ...]
    closes: tuple[tuple[tuple[int, int], ...], ...]

    def facts(self, state_bit: bool | None, route_cost: int) -> dict[str, Any]:
        return {
            "switch": "set" if state_bit else "clear",
            "state_bit": SWITCH_FLAG,
            "opens": [list(gate) for gate in self.opens],
            "closes": [list(gate) for gate in self.closes],
            "route_cost": route_cost,
            "stand_tile": list(self.stand),
        }


def _statue(row: dict[str, Any]) -> MansionStatue:
    xy = lambda value: tuple(int(v) for v in value)  # noqa: E731
    groups = lambda value: tuple(tuple(xy(tile) for tile in gate) for gate in value)  # noqa: E731
    return MansionStatue(
        id=str(row["id"]),
        floor=str(row["floor"]),
        map_key=xy(row["map"]),
        statue=xy(row["statue"]),
        stand=xy(row["stand"]),
        facing=str(row["facing"]),
        opens=groups(row["opens"]),
        closes=groups(row["closes"]),
    )


@lru_cache(maxsize=1)
def statues() -> tuple[MansionStatue, ...]:
    return tuple(_statue(row) for row in _data()["statues"])


def switch_state(snapshot) -> bool | None:
    """Read the global switch bit, returning None when flags are unavailable."""
    if snapshot.flags is None:
        return None
    return snapshot.flags.is_set(SWITCH_FLAG)


def statues_on_map(state: MapState) -> tuple[MansionStatue, ...]:
    if _LOCAL_PUZZLES is None:
        return ()
    key = (state.identity.group, state.identity.number)
    return tuple(item for item in statues() if item.map_key == key)


def switch_facts(item: MansionStatue, switch_set: bool) -> dict[str, Any]:
    """Return only facts read from the ROM or the current RAM flag state."""
    return item.facts(switch_set, 6)


__all__ = [
    "MansionStatue",
    "SECRET_KEY_TILE",
    "SWITCH_FLAG",
    "statues",
    "statues_on_map",
    "switch_facts",
    "switch_state",
]
