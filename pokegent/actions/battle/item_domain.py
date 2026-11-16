"""Thin seam to the generic ITEM domain's battle capabilities.

The generic item branch can implement :class:`BattleItemDomain` without changing the battle
builder.  Until it lands, the default delegates to the ROM-handler/effect classification already
proved by this family.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pokegent.actions.battle.legality import Verdict, item_target_verdicts
from pokegent.actions.battle.view import BattleView


@runtime_checkable
class BattleItemDomain(Protocol):
    def targets(self, view: BattleView, item_id: int) -> list[tuple[int, Verdict]]: ...


class RomBattleItemDomain:
    """Compatibility adapter backed by ``gItems`` handlers and ``gItemEffectTable`` rows."""

    def targets(self, view: BattleView, item_id: int) -> list[tuple[int, Verdict]]:
        return item_target_verdicts(view, item_id)


__all__ = ["BattleItemDomain", "RomBattleItemDomain"]
