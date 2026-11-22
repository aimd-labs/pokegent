"""Admission adapter from fresh runtime bag facts to bounded ROM merchant evidence.

The objective JSON cannot attest either input. A live caller supplies one fresh core
snapshot observation (same actor/profile) and the directory supplies the merged
``POSSIBLE_CLOSED`` union for the bound map. Only a proven *new* unmet quantity plus an
item omitted from that closed union rejects a target; this is absence at the bound map,
never global unobtainability and never effective inventory or price.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pokegent.goals.shop_stock import ShopStockFacts, StockCoverage


@dataclass(frozen=True)
class ShopObservation:
    """Trusted bag quantities from one fresh, usable core snapshot."""

    profile_id: str
    rom_sha1: str
    bag: tuple[tuple[int, int], ...]
    usable: bool = True

    @classmethod
    def from_snapshot(cls, snapshot: Any, *, rom_sha1: str) -> ShopObservation:
        if not getattr(snapshot, "usable", False):
            return cls(snapshot.rom_profile, rom_sha1, (), False)
        counts: dict[int, int] = {}
        for pocket in snapshot.bag:
            for item in pocket.items:
                item_id, quantity = item.item_id, item.quantity
                if (
                    not isinstance(item_id, int)
                    or isinstance(item_id, bool)
                    or item_id <= 0
                    or not isinstance(quantity, int)
                    or isinstance(quantity, bool)
                    or quantity < 0
                ):
                    return cls(snapshot.rom_profile, rom_sha1, (), False)
                counts[item_id] = counts.get(item_id, 0) + quantity
        return cls(snapshot.rom_profile, rom_sha1, tuple(sorted(counts.items())))

    def quantity(self, item_id: int) -> int:
        return dict(self.bag).get(item_id, 0)


def absent_from_possible_stock(
    *,
    map_key: tuple[int, int],
    item_id: int,
    quantity: int,
    observation: ShopObservation | None,
    facts: ShopStockFacts | None,
) -> str | None:
    """Why the bound merchant's closed possible stock omits an unmet request, or ``None``."""
    if (
        not isinstance(item_id, int)
        or isinstance(item_id, bool)
        or item_id <= 0
        or not isinstance(quantity, int)
        or isinstance(quantity, bool)
        or quantity <= 0
        or observation is None
        or not observation.usable
        or facts is None
        or facts.coverage is not StockCoverage.POSSIBLE_CLOSED
        or facts.map_key != map_key
        or facts.profile_id != observation.profile_id
        or facts.rom_sha1 != observation.rom_sha1
    ):
        return None
    if observation.quantity(item_id) >= quantity:
        return None
    if item_id in facts.possible_item_ids:
        return None
    group, number = map_key
    return (
        f"item {item_id} is absent from the closed possible merchant stock of map "
        f"{group}:{number}; this is not a claim of global unavailability"
    )
