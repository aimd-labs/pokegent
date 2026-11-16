"""FireRed semantics for otherwise generic multichoice menu entries."""

from __future__ import annotations

from typing import Any

FIRERED_MENU_ENTRY_FACTS: dict[str, dict[str, Any]] = {
    "pc:bill": {
        "menu": "pc_menu",
        "menu_goal": "team preparation",
        "menu_outcome": "opens Pokémon storage",
        "menu_kind": "team_storage",
        "does": "Open Pokémon storage for withdraw, deposit, move, and held-item operations.",
        "need": "team or storage management",
        "serves_goal": "team preparation",
        "repeat_count": 0,
    },
    "pc:someone": {
        "menu": "pc_menu",
        "menu_goal": "team preparation",
        "menu_outcome": "opens Pokémon storage",
        "menu_kind": "team_storage",
        "does": "Open Pokémon storage for withdraw, deposit, move, and held-item operations.",
        "need": "team or storage management",
        "serves_goal": "team preparation",
        "repeat_count": 0,
    },
    "pc:player": {
        "menu": "pc_menu",
        "menu_goal": "item storage",
        "menu_outcome": "opens item storage",
        "menu_kind": "item_storage",
        "does": "Open item storage and the mailbox.",
        "need": "item or mail management",
        "serves_goal": "item storage",
        "repeat_count": 0,
    },
    "pc:oak": {
        "menu": "pc_menu",
        "menu_goal": "Pokédex rating",
        "menu_outcome": "opens the Pokédex rating",
        "menu_kind": "pokedex_rating",
        "does": "Ask Professor Oak to rate Pokédex progress.",
        "need": "Pokédex progress feedback",
        "serves_goal": "Pokédex completion",
        "repeat_count": 0,
    },
    "pc:hall_of_fame": {
        "menu": "pc_menu",
        "menu_goal": "Hall of Fame records",
        "menu_outcome": "opens the Hall of Fame records",
        "menu_kind": "records",
        "does": "Review Hall of Fame teams.",
        "need": "review completed League teams",
        "serves_goal": "Hall of Fame review",
        "repeat_count": 0,
    },
    "pc:log_off": {
        "menu": "pc_menu",
        "menu_goal": "leave the current menu",
        "menu_outcome": "leaves the PC and returns to the field",
        "menu_kind": "exit",
        "does": "Log off and return to the field.",
        "need": "leave the PC",
        "serves_goal": "safe exit",
        "repeat_count": 0,
    },
    "cancel_choice": {
        "menu": "pc_menu",
        "menu_goal": "leave the current menu",
        "menu_outcome": "leaves the PC choice box",
        "menu_kind": "exit",
    },
}


def menu_entry_facts(stable_key: str) -> dict[str, Any]:
    """Return immutable-by-convention facts for a known FireRed menu entry."""
    return dict(FIRERED_MENU_ENTRY_FACTS.get(stable_key, {}))
