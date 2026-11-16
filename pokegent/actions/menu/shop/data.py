"""ROM-derived item facts shared by the shop option builder."""

from __future__ import annotations

from typing import Any

from pokegent.games.gen3.firered.rom.rom_tables import POCKET_KEY_ITEMS, GameTables, ItemData


def sell_item(
    tables: GameTables, item_id: int, *, owned: int | None = None
) -> tuple[str, int, str | None]:
    """Return the ROM name, half-price sale value, and any game refusal reason."""
    try:
        item = tables.item(item_id)
    except KeyError:
        return f"ITEM {item_id}", 0, "item is not in the supported ROM table"
    protected = None
    if item.importance or item.pocket == POCKET_KEY_ITEMS:
        protected = "key item"
    elif item.price == 0:
        protected = "price is 0"
    return item.name, item.price // 2, protected


def item_facts(item: ItemData, *, owned: int, money: int, need: str | None) -> dict[str, Any]:
    effect = item.effect
    effects: list[str] = []
    if item.is_ball:
        effects.append("catches wild Pokemon")
    if effect is not None:
        if effect.revive:
            effects.append("revives a fainted Pokemon")
        if effect.heals_hp:
            effects.append("restores HP")
        if effect.cures:
            effects.append("cures " + ", ".join(sorted(effect.cures)))
        if effect.heal_pp is not None:
            effects.append("restores PP")
    return {
        "item_id": item.item_id,
        "name": item.name,
        "price": item.price,
        "owned": owned,
        "kind": (
            "ball"
            if item.is_ball
            else "revive"
            if effect is not None and effect.revive
            else "heal_hp"
            if effect is not None and effect.heals_hp
            else "cure_status"
            if effect is not None and effect.cures
            else "other"
        ),
        "battle_effect": "; ".join(effects),
        "need": need,
        "money": money,
        "money_sufficient": money >= item.price,
    }
