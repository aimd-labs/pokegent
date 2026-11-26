"""Game-owned Dex fallback construction for the FireRed plugin (#139).

The factory stays lazy: importing the game helper does not import the private encounter
loader or read any ROM data, and the caller invokes it only for an enabled Dex fallback.
"""

from __future__ import annotations

from typing import Any


def make_dex_fallback() -> Any:
    """Return the existing ``DexFallback`` over the privately loaded encounter table."""
    from pokegent.games.gen3.firered.rom.wild import load_wild_table
    from pokegent.goals.dex_fallback import DexFallback

    return DexFallback(load_wild_table())
