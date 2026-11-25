"""Fly capability code; destination routing is disabled without local game data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from pokegent.actions.menu.system_flow.moves import HM_MOVE_IDS
from pokegent.games.gen3 import mechanics_constants as K
from pokegent.games.gen3 import mechanics_ids as I
from pokegent.games.gen3.firered.maps import map_names
from pokegent.games.gen3.firered.maps.map_schema import MapState
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.local_data import LocalDataError, read_data

FLY_MOVE_ID = I.MOVE_FLY
HM02_ITEM_ID = 340
THUNDER_BADGE = K.BADGE_THUNDER
THUNDER_BADGE_FLAG = 0x822  # FLAG_BADGE03_GET, BPRE Rev0
FLY_MAP_TYPES = frozenset({1, 2, 3, 6})  # town/city/route/ocean route
FLY_OVERHEAD = 60
FLY_MIN_SAVING = 80
FLY_MIN_HOPS = 3
#: Maximum general (non-route) destinations exposed in one field frame.
FLY_CHOICE_LIMIT = 3


@dataclass(frozen=True)
class FlyDestination:
    mapsec: int
    name: str
    map_key: tuple[int, int]
    landing: tuple[int, int]
    visited_flag: int
    cell: tuple[int, int]


def _load_destinations() -> tuple[FlyDestination, ...]:
    data = read_data("fly_destinations.json", optional=True)
    if data is None:
        return ()
    for row in data["destinations"]:
        label = map_names.map_name(*row["map_key"])
        if label is None or label.replace("_", "").lower() != row["name"].replace("_", "").lower():
            raise LocalDataError(
                "Local Fly destination does not match the verified local map index"
            )
    return tuple(
        FlyDestination(
            row["mapsec"], row["name"], tuple(row["map_key"]), tuple(row["landing"]),
            row["visited_flag"], tuple(row["cell"]),
        )
        for row in data["destinations"]
    )


DESTINATIONS = _load_destinations()
DESTINATIONS_BY_NAME = {row.name: row for row in DESTINATIONS}
#: integer-keyed companion (the executor args and postcondition carry only group/number, never a
#: map-name string: a 6+ character string argument is a payload secret and would trip
#: assert_payload_clean, live-0089 E_PAYLOAD_LEAK)
DESTINATIONS_BY_KEY = {row.map_key: row for row in DESTINATIONS}


def has_item(snapshot: StateSnapshot, item_id: int) -> bool:
    return snapshot.usable and any(
        item.item_id == item_id and item.quantity > 0
        for pocket in snapshot.bag
        for item in pocket.items
    )


def carrier_slots(snapshot: StateSnapshot) -> tuple[int, ...]:
    return tuple(
        mon.slot for mon in snapshot.party if any(move.move_id == FLY_MOVE_ID for move in mon.moves)
    )


def hm02_carriers(snapshot: StateSnapshot, tables=None) -> tuple[int, ...]:
    """Party slots compatible with HM02, using the caller's ROM learnset adapter.

    Compatibility is deliberately UNKNOWN without a table adapter.  Move ids in the
    party are not enough: FireRed still rejects species that cannot learn HM02.
    """
    if not snapshot.usable or tables is None:
        return ()
    result: list[int] = []
    for mon in snapshot.party:
        allowed = tables.can_learn_tmhm(mon.species, FLY_MOVE_ID)
        if allowed is True:
            result.append(mon.slot)
    return tuple(result)


def forget_candidates(snapshot: StateSnapshot, slot: int, tables=None) -> tuple[int, ...]:
    """Return safe replacement candidates, weakest first, never an HM.

    Missing move-table facts are ordered after known facts; the executor still stops at the
    game's prompt, so an unknown move is never silently discarded.
    """
    member = next((mon for mon in snapshot.party if mon.slot == slot), None)
    if member is None:
        return ()
    rows = []
    for index, move in enumerate(member.moves):
        move_id = int(move.move_id)
        if not move_id or move_id in HM_MOVE_IDS:
            continue
        power = None
        if tables is not None:
            try:
                power = tables.move(move_id).power
            except Exception:
                pass
        rows.append((power is None, power if power is not None else 10**9, index, move_id))
    return tuple(row[3] for row in sorted(rows))


def can_fly_here(state: MapState) -> bool:
    """Return true only for a verified outdoor map where FireRed permits Fly."""
    return state.identity.identity_verified and state.identity.map_type in FLY_MAP_TYPES


def visited(snapshot: StateSnapshot) -> tuple[FlyDestination, ...]:
    if snapshot.flags is None:
        return ()
    return tuple(dest for dest in DESTINATIONS if snapshot.flags.is_set(dest.visited_flag))


def cursor_step(cursor: tuple[int, int], target: tuple[int, int]) -> str | None:
    """Return the one-cell direction toward a destination, or ``None`` when selected."""
    x, y = cursor
    tx, ty = target
    if x != tx:
        return "right" if tx > x else "left"
    if y != ty:
        return "down" if ty > y else "up"
    return None


def offered_destinations(
    snapshot: StateSnapshot,
    state: MapState,
    *,
    limit: int = FLY_CHOICE_LIMIT,
) -> tuple[FlyDestination, ...]:
    """Bounded visited destinations selectable from a verified outdoor map.

    These are general travel choices, not route steps: nothing here proves the destination
    serves the active objective or saves walking distance.  The current map is excluded and
    the order is stable (region-map section, then name) so a rebuilt frame exposes the same
    subset.  Opening Fly is legal on any verified outdoor map; the executor then proves the
    live cursor from RAM and refuses an unreadable cursor instead of guessing a start cell.
    """
    if limit < 1 or not capability(snapshot, state)["available"] or not can_launch(state):
        return ()
    here = (state.identity.group, state.identity.number)
    rows = sorted(
        (dest for dest in visited(snapshot) if dest.map_key != here),
        key=lambda dest: (dest.mapsec, dest.name),
    )
    return tuple(rows[:limit])


def can_launch(state: MapState) -> bool:
    """Whether FireRed permits opening Fly from this verified outdoor map.

    The executor does not assume a start cell: it waits for ``Task_FlyMap`` and then proves
    the live cursor, selected map section and visited type from RAM before any direction or
    confirmation.  A map whose cursor is unreadable is refused by that executor, not guessed.
    """
    return can_fly_here(state)


def capability(snapshot: StateSnapshot, state: MapState) -> dict[str, object]:
    slots = carrier_slots(snapshot)
    badge = snapshot.player is not None and THUNDER_BADGE in snapshot.player.badges
    allowed = can_fly_here(state)
    return {
        "known": snapshot.usable and snapshot.player is not None,
        "move_id": FLY_MOVE_ID,
        "carrier_slots": list(slots),
        "thunder_badge": badge,
        "map_allows_fly": allowed,
        "available": bool(slots) and badge and allowed,
    }


def route_plan(
    snapshot: StateSnapshot,
    state: MapState,
    target_map: tuple[int, int] | None,
    regions,
    caps: Iterable[str] = (),
) -> tuple[FlyDestination, int, int] | None:
    """Choose a destination only when it beats the committed walking route clearly."""
    if target_map is None or regions is None or not capability(snapshot, state)["available"]:
        return None
    here = (state.identity.group, state.identity.number)
    source_region = regions.region_at(here, state.player.x, state.player.y)
    if source_region is None or target_map == here:
        return None
    walking = regions.route((*here, source_region), target_map, caps)
    if walking is None or walking.cost < FLY_MIN_HOPS * 10:
        return None
    best: tuple[FlyDestination, int, int] | None = None
    for dest in visited(snapshot):
        if dest.map_key == here:
            continue
        region = regions.region_at(dest.map_key, *dest.landing)
        if region is None:
            continue
        candidate = regions.route_with_fly(
            (*here, source_region), target_map, (*caps, "fly"), (dest,), fly_cost=FLY_OVERHEAD
        )
        if candidate is None:
            continue
        tail_cost = candidate.cost - FLY_OVERHEAD
        if best is None or (candidate.cost, tail_cost) < (best[1], best[2]):
            best = (dest, candidate.cost, tail_cost)
    if best is None or walking.cost - best[1] < FLY_MIN_SAVING:
        return None
    return best
