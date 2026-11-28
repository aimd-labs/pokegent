from typing import Any

from pokegent.games.gen3 import mechanics_ids as I

PREFIXES = ("IndigoPlateau", "PokemonLeague")
DEFAULT_TARGET_LEVEL = 55  # Champion team is about 59-63; an objective level goal overrides
_HEALS = (
    I.ITEM_POTION,
    I.ITEM_SUPER_POTION,
    I.ITEM_HYPER_POTION,
    I.ITEM_MAX_POTION,
    I.ITEM_ORAN_BERRY,
    I.ITEM_SITRUS_BERRY,
)


def heal_stock(snap: Any) -> tuple[int, int, int]:
    stock: dict[int, int] = {}
    for pocket in getattr(snap, "bag", ()):
        for item in pocket.items:
            stock[item.item_id] = stock.get(item.item_id, 0) + item.quantity
    full = stock.get(I.ITEM_FULL_RESTORE, 0)
    revives = stock.get(I.ITEM_REVIVE, 0) + stock.get(I.ITEM_MAX_REVIVE, 0)
    return full + sum(stock.get(i, 0) for i in _HEALS), full, revives


def _items_hp(snap: Any, tables: Any, missing: int) -> int | None:
    """HP the bag's heal items could restore, capped at ``missing``; None if unreadable."""
    stock: dict[int, int] = {}
    for pocket in getattr(snap, "bag", ()):
        for item in pocket.items:
            stock[item.item_id] = stock.get(item.item_id, 0) + item.quantity
    total = 0
    try:
        for item_id in (I.ITEM_FULL_RESTORE, *_HEALS):
            if stock.get(item_id, 0) <= 0:
                continue
            effect = tables.item(item_id).effect
            if effect.heal_hp_full:
                return missing
            if not isinstance(effect.heal_hp, int):
                return None
            total += effect.heal_hp * stock[item_id]
    except (AttributeError, IndexError, KeyError, TypeError):
        return None
    return min(total, missing)


def heal_advice(snap: Any, tables: Any = None):
    mons = [
        m
        for m in getattr(snap, "party", ())
        if not getattr(m, "is_egg", False)
        and isinstance(getattr(m, "hp", None), int)
        and getattr(m, "max_hp", 0) > 0  # unreadable HP stays unknown: skip the member
    ]
    if not mons:
        return None
    pcts = [100 * m.hp // m.max_hp for m in mons]
    fainted = sum(m.hp <= 0 for m in mons)
    items, _, revives = heal_stock(snap)
    lowest, average = min(pcts), sum(pcts) // len(pcts)
    need = lowest < 40 or fainted > 0
    if need and not (revives >= fainted and items > 0):
        suggest = "heal_at_center"
    elif (need or average < 60) and items + revives > 0:
        suggest = "use_items"
    else:
        return None
    missing = sum(m.max_hp - m.hp for m in mons if m.hp < m.max_hp)
    items_hp = _items_hp(snap, tables, missing) if tables is not None else None
    if items_hp is not None and suggest == "use_items" and items_hp < missing and lowest < 40:
        suggest = "heal_at_center"
    amounts = {} if items_hp is None else {"items_hp": items_hp, "missing_hp": missing}
    return {
        **amounts,
        "party_hp_pct": {"lowest": lowest, "average": average},
        "fainted": fainted,
        "status_afflicted": sum(1 for m in mons if getattr(m, "status", 0)),
        "heal_items": items,
        "revives": revives,
        "suggest": suggest,
    }


def league_readiness(snap: Any, name: str | None, goals: Any, tables: Any = None):
    if not name or not name.startswith(PREFIXES) or not snap.party:
        return None
    wanted = [g for _, _, g in goals]
    target = max(wanted) if wanted else DEFAULT_TARGET_LEVEL
    items, full, revives = heal_stock(snap)

    def species(mon: Any) -> str:
        try:
            return str(tables.species_name(int(mon.species)))
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            return f"species {int(mon.species)}"

    return {
        "min_level": min(m.level for m in snap.party),
        "avg_level": round(sum(m.level for m in snap.party) / len(snap.party), 1),
        "target_level": target,
        "target_source": "objective" if wanted else "default",
        "below_target": [species(m) for m in snap.party if m.level < target],
        "heal_items": items,
        "full_restores": full,
        "revives": revives,
    }
