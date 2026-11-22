"""Pure SHOP feasibility assessment over trusted, caller-bound facts.

This contract is intentionally unwired: callers must supply a fresh usable bag
quantity and ROM-proven inventory for the requested map. Raw objectives cannot
attest to those facts; no ROM fact producer or admission integration exists yet.
"""

from dataclasses import dataclass
from enum import Enum
from math import isfinite


class Feasibility(str, Enum):
    SATISFIABLE = "satisfiable"
    UNSATISFIABLE = "unsatisfiable"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class InventoryCoverage(str, Enum):
    UNKNOWN = "unknown"
    PARTIAL = "partial"
    COMPLETE = "complete"


class Access(str, Enum):
    UNKNOWN = "unknown"
    ALLOWED = "allowed"
    DENIED = "denied"


@dataclass(frozen=True)
class ShopRequest:
    map_key: str | None
    item_id: int
    quantity: int = 1
    owned_quantity: int | None = None


@dataclass(frozen=True)
class ShopFacts:
    """Trusted map facts; item_price is for query_item_id."""

    map_key: str
    query_item_id: int
    coverage: InventoryCoverage
    item_ids: frozenset[int]
    money: int | None
    access: Access
    item_price: int | None = None


@dataclass(frozen=True)
class Assessment:
    verdict: Feasibility
    reason: str


def _valid_count(value: object, *, positive: bool = False) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and (value > 0 if positive else value >= 0)
    )


def _valid_amount(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    return isinstance(value, float) and isfinite(value) and value >= 0


def assess_shop(request: ShopRequest, facts: ShopFacts | None) -> Assessment:
    """Assess a map-bound SHOP request without inferring unsupported ROM facts."""
    if not _valid_count(request.item_id, positive=True) or not _valid_count(
        request.quantity, positive=True
    ):
        return Assessment(Feasibility.UNKNOWN, "request quantity or item is invalid")
    if request.owned_quantity is not None and not _valid_count(request.owned_quantity):
        return Assessment(Feasibility.UNKNOWN, "current bag quantity is invalid")
    if request.owned_quantity is not None and request.owned_quantity >= request.quantity:
        return Assessment(Feasibility.SATISFIABLE, "requested quantity is already owned")
    if request.owned_quantity is None:
        return Assessment(Feasibility.UNKNOWN, "current bag quantity is unknown")
    if request.map_key is None or facts is None:
        return Assessment(Feasibility.UNKNOWN, "target merchant facts are unbound")
    if facts.map_key != request.map_key:
        return Assessment(Feasibility.UNKNOWN, "inventory belongs to a different map")
    if facts.coverage is not InventoryCoverage.COMPLETE:
        return Assessment(Feasibility.UNKNOWN, "merchant inventory is not proven complete")
    if not isinstance(facts.item_ids, frozenset) or any(
        not _valid_count(item_id, positive=True) for item_id in facts.item_ids
    ):
        return Assessment(Feasibility.UNKNOWN, "merchant item facts are invalid")
    if not _valid_count(facts.query_item_id, positive=True):
        return Assessment(Feasibility.UNKNOWN, "priced item identity is invalid")
    if facts.query_item_id != request.item_id:
        return Assessment(Feasibility.UNKNOWN, "merchant facts price a different item")
    if request.item_id not in facts.item_ids:
        return Assessment(
            Feasibility.UNSATISFIABLE, "item is absent from proven merchant inventory"
        )
    if facts.access is Access.DENIED:
        return Assessment(Feasibility.BLOCKED, "required merchant access is denied")
    if facts.money is not None and not _valid_amount(facts.money):
        return Assessment(Feasibility.UNKNOWN, "money fact is invalid")
    if facts.item_price is not None and not _valid_amount(facts.item_price):
        return Assessment(Feasibility.UNKNOWN, "item price fact is invalid")
    if facts.money is not None and facts.item_price is not None:
        needed = facts.item_price * max(request.quantity - request.owned_quantity, 0)
        if facts.money < needed:
            return Assessment(Feasibility.BLOCKED, "known money is insufficient")
    if facts.access is not Access.ALLOWED:
        return Assessment(Feasibility.UNKNOWN, "merchant access is unknown or invalid")
    if facts.money is None or facts.item_price is None:
        return Assessment(Feasibility.UNKNOWN, "required money facts are unknown")
    return Assessment(Feasibility.SATISFIABLE, "merchant, access, and money facts support request")
