"""Translate one live snapshot into navigation prerequisites."""

from __future__ import annotations

from pokegent.actions.field import fly
from pokegent.actions.field.capabilities import field_capability
from pokegent.actions.field.surf.rules import surf_capability
from pokegent.games.gen3 import mechanics_constants as K
from pokegent.games.gen3 import mechanics_ids as I
from pokegent.games.gen3.firered.maps.obstacles import FIELD_MOVE_KINDS, KINDS
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.navigation.models import CapabilityState, NavigationRequirement

_MOVE_CAPABILITIES = {
    "waterfall": (I.MOVE_WATERFALL, K.BADGE_VOLCANO, "Waterfall and Volcano Badge"),
    "flash": (I.MOVE_FLASH, K.BADGE_BOULDER, "Flash and Boulder Badge"),
}

#: Obstacle requirement key -> capability owner kind (Rock Smash key is ``rock_smash``).
_FIELD_MOVE_KINDS = {
    "cut": "cut",
    "rock_smash": "smash",
    "strength": "strength",
    "flash": "flash",
}


def _section_readable(snapshot: StateSnapshot, section: str) -> bool:
    """Positive, configured read confidence for one section (absent means unknown)."""
    health = getattr(snapshot, "health", None)
    confidence = getattr(health, "confidence", None) if health is not None else None
    if not confidence:
        return False
    value = confidence.get(section)
    if value is None:
        return False
    try:
        return float(value) == 1.0
    except (TypeError, ValueError):
        return False


def missing_field_move_fact(snapshot: StateSnapshot, key: str) -> dict[str, object] | None:
    """Structured proven gap of an unmet field move; ``None`` when absence is not proved."""
    if key not in _FIELD_MOVE_KINDS:
        return None
    capability = field_capability(snapshot, _FIELD_MOVE_KINDS[key])
    if not capability.known:
        return None
    if not _section_readable(snapshot, "party") or not _section_readable(snapshot, "player"):
        return None
    badges = frozenset(snapshot.player.badges) if snapshot.player is not None else frozenset()
    has_badge = capability.badge is not None and capability.badge in badges
    has_carrier = bool(capability.carrier_species)
    if has_carrier and has_badge:
        return None
    return {
        "move": capability.name,
        "badge": capability.badge_name,
        "carrier_missing": not has_carrier,
        "badge_missing": not has_badge,
    }


def from_snapshot(
    snapshot: StateSnapshot,
    *,
    visited_maps: frozenset[tuple[int, int]] = frozenset(),
) -> CapabilityState:
    caps: set[str] = set()
    for obstacle in (*KINDS.values(), *FIELD_MOVE_KINDS.values()):
        if field_capability(snapshot, obstacle.capability_kind).available:
            caps.add(obstacle.requirement_key)
    if surf_capability(snapshot).available:
        caps.add("surf")
    badges = frozenset(snapshot.player.badges if snapshot.player else ())
    moves = frozenset(move.move_id for mon in snapshot.party for move in mon.moves)
    for key, (move, badge, _label) in _MOVE_CAPABILITIES.items():
        if move in moves and badge in badges:
            caps.add(key)
    if snapshot.player is not None and fly.THUNDER_BADGE in badges and fly.carrier_slots(snapshot):
        caps.add("fly")
    items = frozenset(
        item.item_id for pocket in snapshot.bag for item in pocket.items if item.quantity > 0
    )
    if 360 in items:
        caps.add("bicycle")
    flags = frozenset(snapshot.flags.set_ids if snapshot.flags else ())
    if 355 in items or 0x192 in flags:
        caps.add("card_key")
    visited_maps = frozenset(visited_maps).union(
        destination.map_key for destination in fly.DESTINATIONS if destination.visited_flag in flags
    )
    variables = tuple(snapshot.vars.nonzero if snapshot.vars else ())
    quest_log_state = (
        snapshot.ui.menu.quest_log_state
        if snapshot.ui is not None and snapshot.ui.menu is not None
        else None
    )
    state = snapshot.map
    return CapabilityState(
        capabilities=frozenset(caps),
        items=items,
        flags=flags,
        flags_known=snapshot.flags is not None,
        vars=variables,
        vars_known=snapshot.vars is not None,
        quest_log_state=quest_log_state,
        visited_maps=visited_maps,
        biking=bool(state and state.player and state.player.biking),
        surfing=bool(state and state.player and state.player.surfing),
        strength_active=bool(state and state.strength_active),
        live_objects=tuple(state.objects if state else ()),
    )


def requirement(
    key: str,
    capabilities: CapabilityState,
    *,
    label: str | None = None,
    equivalent_keys: tuple[str, ...] = (),
) -> NavigationRequirement:
    met = capabilities.has(key) or any(capabilities.has(row) for row in equivalent_keys)
    if "&" in key:
        parts = tuple(requirement(part, capabilities) for part in key.split("&"))
        return NavigationRequirement(
            key,
            label or " and ".join(part.label for part in parts),
            met,
            "rom",
        )
    if key == "poke_flute":
        return NavigationRequirement(key, "Poke Flute", capabilities.has(key), "bag")
    if key.startswith("item:"):
        item_id = int(key.split(":", 1)[1], 0)
        return NavigationRequirement(key, label or f"item {item_id}", met, "bag")
    if key.startswith("flag:"):
        flag_id = int(key.split(":", 1)[1], 0)
        return NavigationRequirement(key, label or f"flag 0x{flag_id:X}", met, "flag")
    if key.startswith("var:"):
        return NavigationRequirement(
            key, label or key.replace(":", " "), capabilities.has(key), "var"
        )
    labels = {
        "cut": "Cut and Cascade Badge",
        "rock_smash": "Rock Smash and Thunder Badge",
        "strength": "Strength and Rainbow Badge",
        "surf": "Surf and Soul Badge",
        "waterfall": "Waterfall and Volcano Badge",
        "flash": "Flash and Boulder Badge",
        "poke_flute": "Poke Flute",
        "fly": "Fly and Thunder Badge",
        "bicycle": "Bicycle",
        "card_key": "Card Key",
    }
    return NavigationRequirement(
        key,
        labels.get(key, key.replace("_", " ")),
        capabilities.has(key),
        "party",
    )
