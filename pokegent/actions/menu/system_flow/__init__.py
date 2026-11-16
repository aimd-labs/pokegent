"""Scene family ``system_flow``: the endgame, evolution and the PC.

Claims: ``special.hall_of_fame``, ``system.credits``, ``system.logo``, ``title.press_start`` /
``title.main`` / ``title.continue`` (C1), ``evolution.start`` / ``.animation`` / ``.done`` /
``.move_learning`` (C2), ``pc.main`` (C3). ``system.game_clear`` is not detected as a screen: it
is a
run-level fact (``FLAG_SYS_GAME_CLEAR``) handled by the runner and put into every frame this
family builds.
Documentation, evidence and the supported / not supported list:
``the runtime contracts``.

Public surface: :func:`register`. ``pokegent.actions.register_optional`` imports this package
and calls it.
"""

from __future__ import annotations

from pokegent.actions.menu.system_flow.detect import CLAIMED_SCENES
from pokegent.actions.menu.system_flow.foreign import FOREIGN_MENUS
from pokegent.actions.menu.system_flow.policy import SystemFlowOptions
from pokegent.actions.menu.system_flow.refs import FAMILY
from pokegent.actions.menu.system_flow.register import (
    CONFIRM_SCENE_IDS,
    SystemFlowFamily,
    register,
)

#: claimed and only waited out by the runner (no builder is asked for options while busy)
BUSY_SCENES: frozenset[str] = frozenset()

#: recognised but deliberately not acted on
UNSUPPORTED_SCENES: frozenset[str] = frozenset()

#: catalog rows whose behavior is folded into the run manager or another system_flow fact,
# rather than
#: requiring a detector/builder of their own.
FOLDED_SCENES: dict[str, str] = {
    "system.game_clear": "run-level FLAG_SYS_GAME_CLEAR fact observed by Runner [pokegent.loop:Runner._observe_game_clear]",  # noqa: E501
    "move.new_offer": "handled by evolution.move_learning [ExecutorSpec:system_flow.evolution_answer]",  # noqa: E501
    "move.no_free_slot": "handled by evolution.move_learning [ExecutorSpec:system_flow.evolution_answer]",  # noqa: E501
    "move.replace_confirm": "handled by evolution.move_learning [ExecutorSpec:system_flow.forget_move]",  # noqa: E501
}

#: scene ids the catalog lists for this family's mechanisms that the family does not claim, and why
KNOWN_GAPS: dict[str, str] = {
    "system.boot": "not part of the endgame; the reboot passes through system.logo",
    "pc.pokemon_mode": "the withdraw/deposit/move menu is folded into pc.main (one B backs out)",
    "pc.item_mode": "no item-storage macro; the PC screens are backed out of, not used",
}

__all__ = [
    "BUSY_SCENES",
    "CLAIMED_SCENES",
    "CONFIRM_SCENE_IDS",
    "FOLDED_SCENES",
    "FAMILY",
    "FOREIGN_MENUS",
    "KNOWN_GAPS",
    "UNSUPPORTED_SCENES",
    "SystemFlowFamily",
    "SystemFlowOptions",
    "register",
]
