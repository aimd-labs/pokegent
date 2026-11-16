"""Optional family for scripted FireRed puzzles and labelled menus.

The package is intentionally small at the boundary: each work package adds a focused module,
while :mod:`register` remains the single extension point used by the family integrator.
"""

from __future__ import annotations

from pokegent.actions.special.register import register
from pokegent.local_data import read_data

CLAIMED_SCENES: frozenset[str] = (
    frozenset({"dialog.elevator", "menu.elevator"})
    if read_data("elevators.yaml", optional=True) is not None else frozenset()
)
HANDOFF_SCENES: frozenset[str] = CLAIMED_SCENES & frozenset({"dialog.elevator"})
BUSY_SCENES: frozenset[str] = frozenset()
UNSUPPORTED_SCENES: frozenset[str] = frozenset()
KNOWN_GAPS: dict[str, str] = {}

__all__ = [
    "BUSY_SCENES",
    "CLAIMED_SCENES",
    "HANDOFF_SCENES",
    "KNOWN_GAPS",
    "UNSUPPORTED_SCENES",
    "register",
]
