"""Bicycle facts and the deliberately narrow mount/dismount policy."""

from __future__ import annotations

from pokegent.games.gen3.firered.maps.map_schema import MapState
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.goals.validate import ResolvedObjective
from pokegent.options.models import Legality, Option

BICYCLE_ITEM_ID = 360
BIKE_VOUCHER_FLAG = 0x241
BICYCLE_FLAG = 0x271


def has_bicycle(snapshot: StateSnapshot) -> bool:
    return snapshot.usable and any(
        item.item_id == BICYCLE_ITEM_ID and item.quantity > 0
        for pocket in snapshot.bag
        for item in pocket.items
    )


def mounted(state: MapState) -> bool:
    return bool(state.player and state.player.biking)


def option_allowed(
    snapshot: StateSnapshot, state: MapState, *, mounting: bool, serves: bool
) -> bool:
    if not has_bicycle(snapshot) or state.player is None:
        return False
    if mounting:
        return serves and state.identity.biking_allowed and not state.player.biking
    return state.player.biking and (not state.identity.biking_allowed or not serves)


def options(
    snapshot: StateSnapshot, state: MapState, resolved: ResolvedObjective | None
) -> list[Option]:
    """Bicycle transport edges; execution remains the verified field toggle macro."""
    if not has_bicycle(snapshot) or state.player is None:
        return []
    here = (state.identity.group, state.identity.number)
    goal = resolved.target.map_key if resolved is not None else None
    registered = snapshot.registered_item
    if state.player.biking:
        # Keep a verified dismount fallback available while an objective still points at
        # another map.  The mounted route may leave us on an intermediate map (for example
        # Route 23 while the next objective is Route 24), and the caller may need to switch
        # back to the walking graph before taking that transition.
        needed = (
            not state.identity.biking_allowed
            or bool(state.identity.cave)
            or (resolved is not None and goal != here)
        )
        if not needed:
            return []
        action = (
            "Press SELECT to dismount the registered Bicycle."
            if registered == BICYCLE_ITEM_ID
            else "Use Bicycle from Key Items to walk on foot for this map or objective."
        )
        return [
            Option(
                id="bike_off",
                label="Dismount the Bicycle",
                description=action,
                legality=Legality.LEGAL,
                facts={
                    "mode": "biking",
                    "serves_goal": needed,
                    "item_id": BICYCLE_ITEM_ID,
                    "registered_item": registered,
                    "uses_select": registered == BICYCLE_ITEM_ID,
                },
                executor_ref="field.toggle_bike",
                postcondition_ref="field.bike_off",
                verb="DISMOUNT_BICYCLE",
            )
        ]
    if goal is None or goal == here or not state.identity.biking_allowed:
        return []
    if registered == BICYCLE_ITEM_ID:
        action = "Press SELECT to ride the registered Bicycle."
    elif registered == 0:
        action = "Register the Bicycle through Key Items once, then press SELECT to ride it."
    else:
        action = "The registered item is in use; use Bicycle from Key Items as a safe fallback."
    return [
        Option(
            id="bike_on",
            label="Ride the Bicycle",
            description=action,
            legality=Legality.LEGAL,
            facts={
                "mode": "on_foot",
                "serves_goal": True,
                "item_id": BICYCLE_ITEM_ID,
                "registered_item": registered,
                "uses_select": registered == BICYCLE_ITEM_ID,
            },
            executor_ref="field.toggle_bike",
            postcondition_ref="field.bike_on",
            verb="RIDE_BICYCLE",
        )
    ]
