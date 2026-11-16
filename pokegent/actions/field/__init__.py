"""Scene family ``field`` (spec 6.B): walking, talking, warps, and the objective hand-over.

Public surface: :func:`register`. Documentation, evidence, truth numbers and the UNSUPPORTED list:
``the runtime contracts``. The objective file contract is in ``pokegent/goals`` and
``docs/objective-file.md``.
"""

from __future__ import annotations

from pokegent.actions.field.attempts import AttemptLedger, capability_signature
from pokegent.actions.field.builder import FieldBuilder, VisitedMaps, store_source
from pokegent.actions.field.detect import CLAIMED_SCENES, FieldDetector, build_detectors
from pokegent.actions.field.evidence import DARKNESS, FREE, TRAINER_VISION, Verdict, classify
from pokegent.actions.field.register import FAMILY, FieldFamily, register
from pokegent.actions.field.source import FieldSnapshotSource

# A warp-exit task can release controls in CB2_Overworld after CB1_Overworld already ran for that
# frame.  Confirming the first clean field snapshot across a later frame gives CB1's trainer and
# MAP_SCRIPT_ON_FRAME checks their next scheduled turn before any option is offered.
CONFIRM_SCENE_IDS = frozenset({FREE})

#: scene -> what stands behind it
IMPLEMENTED: dict[str, str] = {
    "field.free": "controllable overworld: navigation, field obstacles, grass training, and semantic interaction options",  # noqa: E501
    "field.darkness": "dark cave: optional Flash alongside ordinary blind navigation",
    "field.trainer_vision": "a trainer has the player in sight (BUSY wait); WAIT_FOR_SCRIPT if built",  # noqa: E501
    "field.current": "water-current movement is detected and waited out by the field macro",
    "menu.fly_map": "resumed Fly region map: verified destinations or a RAM-verified cancel",
}

#: recognised-by-nobody or explicitly not acted on: the runner escalates, nothing is guessed
UNSUPPORTED: dict[str, str] = {
    "field.bicycle": "bike menus outside field.free (field.free offers verified bike toggles)",
    "field.surf_edge": "no own scene: Surf is offered from field.free (surf:/dismount: options, actions/field/surf)",  # noqa: E501
    "field.strength_boulder": "Strength arming (strength:<id>) and boulder_plan:<map>:<goal> are offered from field.free; the catalog scene itself is not claimed",  # noqa: E501
    "field.waterfall": "Waterfall",
    "field.fishing": "fishing",
    "field.hidden_item": "hidden-item search (discovery policy)",
    "field.sweet_scent": "Sweet Scent",
    "field.dig_teleport": "Dig / Teleport (Fly is offered from field.free when its gates are proven)",  # noqa: E501
    "field.hole": "holes (one-way warps) are offered as warps, the reset risk is not modelled",
}

__all__ = [
    "CLAIMED_SCENES",
    "CONFIRM_SCENE_IDS",
    "FAMILY",
    "FREE",
    "DARKNESS",
    "IMPLEMENTED",
    "TRAINER_VISION",
    "UNSUPPORTED",
    "AttemptLedger",
    "FieldBuilder",
    "FieldDetector",
    "FieldFamily",
    "FieldSnapshotSource",
    "Verdict",
    "VisitedMaps",
    "build_detectors",
    "capability_signature",
    "classify",
    "register",
    "store_source",
]
