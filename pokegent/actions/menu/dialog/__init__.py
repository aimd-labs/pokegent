"""Scene family ``dialog`` (+ generic busy / transition scenes). Contracts: docs/contracts.md.

Public surface: :func:`register` (wires detectors, builder, executors, verifiers), the text sidecar
and snapshot source, the prompt policy. Documentation and the coverage numbers:
``the runtime contracts``.

What is implemented and what is deliberately not (spec 6.K.1: every scene has detect / enumerate /
guard / execute / verify / recover, or is explicitly UNSUPPORTED):
"""

from __future__ import annotations

from pokegent.actions.menu.dialog.detect import CLAIMED_SCENES, DialogDetector, build_detectors
from pokegent.actions.menu.dialog.evidence import Verdict, classify
from pokegent.actions.menu.dialog.prompts import (
    PromptInfo,
    PromptKind,
    PromptPolicy,
    classify_prompt,
)
from pokegent.actions.menu.dialog.register import (
    CONFIRM_SCENE_IDS,
    FAMILY,
    DialogFamily,
    register,
)
from pokegent.actions.menu.dialog.source import DialogSnapshotSource
from pokegent.actions.menu.dialog.text import (
    DialogText,
    TextSidecar,
    decode_message,
    read_dialog_text,
)

#: scene -> what stands behind it. "wait" scenes press nothing; "A" scenes press A only while the
#: game is provably blocked on it; "cursor" scenes move the cursor under read-back and press A last.
IMPLEMENTED: dict[str, str] = {
    "dialog.printing": "wait (deterministic, forced, no input, no model)",
    "dialog.advance": (
        "A while a message printer waits for A/B; orphaned field printers use a generic A fallback "
        "with RAM progress verification"
    ),
    "dialog.end": "A while the script waits in WaitForAorBPress with the box still up",
    "dialog.yes_no": "script yes/no task or the YES/NO multichoice (id 0); full 6.C context",
    "dialog.heal_offer": "YES/NO specialisation by the proven-current question text",
    "dialog.item_receive": "A on an item message (text-recognised); bag-full flagged, not acted on",
    "dialog.multichoice": "one option per entry from sMenu; labels only via a provider",
    "dialog.list": "rows/scroll/cancel from the list task; synthetic evidence only",
    "dialog.unknown": (
        "claimed for dialog-like screens still lacking a safe generic action; escalates"
    ),
    "field.map_transition": "BUSY wait (warp task, map-load callbacks, locked fade)",
    "field.scripted_movement": "BUSY wait (field locked by a script or a field effect)",
    "system.whiteout": "BUSY wait (CB2_WhiteOut) + ACKNOWLEDGE (post-whiteout message)",
}

#: recognised but explicitly not acted on
UNSUPPORTED_RECOGNISED: dict[str, str] = {
    "dialog.grid_choice": "multi-column navigation is not implemented",
    "system.quest_log_playback": (
        "gQuestLogState playback never occurs in the fixtures or the 1281-state corpus, so the "
        "recognition and the exit input are unverified (spec 6.A: only with a known input)"
    ),
}

#: never claimed by this package: no detector, so the registry reports unknown / dialog.unknown
#: and the runner escalates (never guessed)
UNSUPPORTED_UNCLAIMED: dict[str, str] = {
    "dialog.quantity": "shop quantity belongs to the shop family",
    "dialog.picture": "no fixture; not in the M4 scope",
    "dialog.starter_choice": "intro scene, not in the M4 scope",
    "dialog.trade_offer": "no fixture; not in the M4 scope",
    "dialog.move_tutor": "no fixture; not in the M4 scope",
    "dialog.save_prompt": "START-menu sub-prompt (foreign UI task), not a script prompt",
    "dialog.ferry": "no fixture; a generic multichoice is claimed instead",
    "dialog.elevator": "no labels; a generic multichoice is claimed instead",
    "dialog.script_puzzle": "no fixture; not in the M4 scope",
}

__all__ = [
    "CLAIMED_SCENES",
    "CONFIRM_SCENE_IDS",
    "FAMILY",
    "IMPLEMENTED",
    "UNSUPPORTED_RECOGNISED",
    "UNSUPPORTED_UNCLAIMED",
    "DialogDetector",
    "DialogFamily",
    "DialogSnapshotSource",
    "DialogText",
    "PromptInfo",
    "PromptKind",
    "PromptPolicy",
    "TextSidecar",
    "Verdict",
    "build_detectors",
    "classify",
    "classify_prompt",
    "decode_message",
    "read_dialog_text",
    "register",
]
