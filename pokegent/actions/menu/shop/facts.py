"""Shop fact rendering registered through the common options renderer."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pokegent.options.render import register_fact_renderer


def _render(facts: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    if isinstance(facts.get("price"), int):
        out.append(f"price {facts['price']} money")
    if isinstance(facts.get("sell_price"), int):
        out.append(f"sells for {facts['sell_price']} each")
    if isinstance(facts.get("total"), int):
        out.append(f"{facts['total']} total")
    if isinstance(facts.get("you_keep"), int):
        out.append(f"you keep {facts['you_keep']}")
    if isinstance(facts.get("teaches"), str):
        out.append(f"teaches {facts['teaches']}")
    if facts.get("evolution_item") is True:
        out.append("evolution item")
    if isinstance(facts.get("protected_reason"), str) and facts["protected_reason"]:
        out.append(f"unavailable: {facts['protected_reason']}")
    if isinstance(facts.get("owned"), int):
        out.append(f"you have {facts['owned']}")
    if isinstance(facts.get("money"), int) and facts.get("money_sufficient") is False:
        out.append(f"money available {facts['money']} (not enough)")
    if isinstance(facts.get("total_cost"), int):
        out.append(f"total cost {facts['total_cost']}")
    if isinstance(facts.get("money_after"), int):
        out.append(f"money after purchase {facts['money_after']}")
    effect = facts.get("battle_effect")
    if isinstance(effect, str) and effect:
        out.append(f"battle effect: {effect}")
    if isinstance(facts.get("unavailable_reason"), str):
        out.append(f"unavailable: {facts['unavailable_reason']}")
    if facts.get("shop_serves_goal") is True:
        out.append("serves the current objective")
    elif facts.get("shop_serves_goal") is False:
        out.append("does not serve the current objective")
    owned = facts.get("objective_owned")
    after = facts.get("objective_after")
    target = facts.get("objective_target")
    if all(isinstance(value, int) for value in (owned, after, target)):
        if after >= target:
            out.append("reaches the objective target")
        else:
            out.append(f"toward objective: {owned} -> {after} of {target}")
    if isinstance(facts.get("objective_limit_reason"), str):
        out.append(facts["objective_limit_reason"])
    if isinstance(facts.get("bag_after"), int):
        out.append(f"bag count after purchase {facts['bag_after']}")
    return out


def register_renderer() -> None:
    if (
        "shop"
        not in __import__("pokegent.options.render", fromlist=["renderer_names"]).renderer_names()
    ):
        register_fact_renderer("shop", _render)
