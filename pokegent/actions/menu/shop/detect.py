"""Shop scene detectors, ordered by the catalog's MODAL/MENU priority classes."""

from __future__ import annotations

from pokegent.actions.menu.shop import evidence
from pokegent.actions.menu.shop.refs import CLAIMED_SCENES  # noqa: F401
from pokegent.state.scenes import PriorityClass


def build_detectors():
    return (
        evidence.detector("dialog.shop_prompt", PriorityClass.MODAL),
        evidence.detector("shop.buy_quantity", PriorityClass.MODAL),
        evidence.detector("shop.buy_confirm", PriorityClass.MODAL),
        evidence.detector("shop.buy_list", PriorityClass.MENU),
        evidence.detector("shop.buy_done", PriorityClass.MENU),
        evidence.detector("shop.sell_quantity", PriorityClass.MODAL),
        evidence.detector("shop.sell_confirm", PriorityClass.MODAL),
        evidence.detector("shop.sell_list", PriorityClass.MENU),
        evidence.detector("shop.sell_done", PriorityClass.MENU),
        evidence.detector("shop.main", PriorityClass.MENU),
    )
