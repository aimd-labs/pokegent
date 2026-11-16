"""Poke Mart buy flow (WP-C4).

The field family reaches the clerk. This family owns only the native shop screens and the
transaction verification; selling and every special shop remain diagnostics.
"""

from pokegent.actions.menu.shop.detect import CLAIMED_SCENES
from pokegent.actions.menu.shop.register import CONFIRM_SCENE_IDS, ShopFamily, register

BUSY_SCENES = frozenset()
UNSUPPORTED_SCENES = frozenset()
KNOWN_GAPS = {
    "shop.vending_machine": "vending machines are outside WP-C4",
    "shop.game_corner_exchange": "prize exchange is outside WP-C4",
    "shop.special_service": "special services are outside WP-C4",
}

__all__ = ["CLAIMED_SCENES", "CONFIRM_SCENE_IDS", "KNOWN_GAPS", "register", "ShopFamily"]
