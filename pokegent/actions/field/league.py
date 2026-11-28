from typing import Any

from pokegent.games.gen3 import mechanics_ids as I

PREFIXES = ("IndigoPlateau", "PokemonLeague")
DEFAULT_TARGET_LEVEL = 55  # Champion team is about 59-63; an objective level goal overrides
_HEALS = (I.ITEM_POTION, I.ITEM_SUPER_POTION, I.ITEM_HYPER_POTION, I.ITEM_MAX_POTION)


def league_readiness(snap: Any, name: str | None, goals: Any, tables: Any = None):
    if not name or not name.startswith(PREFIXES) or not snap.party:
        return None
    wanted = [g for _, _, g in goals]
    target = max(wanted) if wanted else DEFAULT_TARGET_LEVEL
    stock: dict[int, int] = {}
    for pocket in snap.bag:
        for item in pocket.items:
            stock[item.item_id] = stock.get(item.item_id, 0) + item.quantity

    def species(mon: Any) -> str:
        try:
            return str(tables.species_name(int(mon.species)))
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            return f"species {int(mon.species)}"

    full = stock.get(I.ITEM_FULL_RESTORE, 0)
    return {
        "min_level": min(m.level for m in snap.party),
        "avg_level": round(sum(m.level for m in snap.party) / len(snap.party), 1),
        "target_level": target,
        "target_source": "objective" if wanted else "default",
        "below_target": [species(m) for m in snap.party if m.level < target],
        "heal_items": full + sum(stock.get(i, 0) for i in _HEALS),
        "full_restores": full,
        "revives": stock.get(I.ITEM_REVIVE, 0) + stock.get(I.ITEM_MAX_REVIVE, 0),
    }
