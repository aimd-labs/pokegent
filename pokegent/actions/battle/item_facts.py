"""Short, stable rendering for battle item facts.

The mechanics layer owns the numbers.  This adapter only orders the facts that are
safe to show to the decider: effect, target numbers, remaining quantity, danger,
and the turn cost.  Keeping this separate from the generic renderer prevents an
item's internal ids and handler details from becoming prompt text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pokegent.options.render import register_fact_renderer, renderer_names


def _render(facts: Mapping[str, Any]) -> Sequence[str]:
    out: list[str] = []
    if facts.get("would_have_effect") is False:
        reason = facts.get("reason")
        text = "would have no effect"
        if isinstance(reason, str) and reason:
            text += f" ({reason})"
        out.append(text)
        if facts.get("turn_still_used") is True:
            out.append("the turn is still used")
    else:
        restored = facts.get("hp_restored")
        after = facts.get("hp_after")
        if isinstance(restored, int):
            out.append(f"restores {restored} HP")
        if isinstance(facts.get("target_hp"), int) and isinstance(facts.get("target_max_hp"), int):
            hp = f"HP {facts['target_hp']}/{facts['target_max_hp']}"
            if isinstance(after, int):
                hp += f" -> {after}/{facts['target_max_hp']}"
            out.append(hp)
        current = facts.get("cures_current")
        cures = facts.get("cures")
        statuses = current if isinstance(current, (list, tuple)) and current else cures
        if isinstance(statuses, (list, tuple)) and statuses:
            out.append("heals " + ", ".join(str(x) for x in statuses))
        if facts.get("kind") == "revive":
            out.append(
                "revives to 50% HP" if facts.get("hp_restored") else "revives a fainted Pokemon"
            )
        wakes = facts.get("wakes")
        if isinstance(wakes, (list, tuple)):
            targets = [target for target in wakes if isinstance(target, str) and target]
            if targets:
                out.append(f"wakes up {' and '.join(targets)}")
        # Move facts also carry stat_changes.  This renderer is for item options only;
        # otherwise a foe-targeted move such as Tail Whip gets the false generic claim.
        if facts.get("item_id") is not None and facts.get("stat_changes"):
            out.append("raises a battle stat")
    quantity = facts.get("quantity")
    # Shop options also carry a quantity, but their price/total-cost facts are
    # rendered by the shop family.  Do not let the battle item renderer leak
    # ``N left`` into shop prompts when both families are registered.
    if isinstance(quantity, int) and "price" not in facts:
        out.append(f"{quantity} left")
    danger = facts.get("foe_can_ko_after_heal")
    if danger in (True, False, "yes", "no"):
        answer = "yes" if danger is True or danger == "yes" else "no"
        out.append(f"foe can still knock it out after the heal: {answer}")
    if facts.get("uses_turn") is True:
        out.append("uses your turn")
    return out


def register_renderer() -> None:
    """Register once; family registration is intentionally repeat-safe in tests."""
    if "item" in renderer_names():
        register_fact_renderer("item", _render, replace=True)
    else:
        register_fact_renderer("item", _render)


__all__ = ["register_renderer"]
