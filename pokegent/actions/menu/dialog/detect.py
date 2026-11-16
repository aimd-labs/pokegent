"""Detectors of the dialog family (spec 6.0, 6.C) and of the generic busy / transition scenes.

One detector per scene id, all of them views of :func:`~pokegent.actions.menu.dialog.evidence.
classify`. A detector never presses, waits or reads the emulator; the optional text sidecar is a
lookup of text that the family's snapshot source already read from the same atomic snapshot.

Priority classes (spec 6.0): prompts (yes/no, multichoice, list, whiteout, quest log) are MODAL; the
message box scenes are FIELD_DIALOG; the pure waits (map transition, scripted movement) are BUSY.
``dialog.unknown`` has specificity 0 so any specific claim of anyone beats it.
"""

from __future__ import annotations

from dataclasses import dataclass

from pokegent.actions.menu.dialog import evidence as E
from pokegent.actions.menu.dialog.text import TextSidecar
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.state.scenes import PriorityClass, SceneResult


@dataclass(frozen=True)
class ClaimSpec:
    scene_id: str
    priority: PriorityClass
    specificity: int


CLAIMS: tuple[ClaimSpec, ...] = (
    ClaimSpec(E.YES_NO, PriorityClass.MODAL, 10),
    ClaimSpec(E.HEAL_OFFER, PriorityClass.MODAL, 20),
    ClaimSpec(E.MULTICHOICE, PriorityClass.MODAL, 10),
    ClaimSpec(E.GRID_CHOICE, PriorityClass.MODAL, 10),
    ClaimSpec(E.LIST, PriorityClass.MODAL, 10),
    ClaimSpec(E.WHITEOUT, PriorityClass.MODAL, 30),
    ClaimSpec(E.QUEST_LOG, PriorityClass.MODAL, 10),
    ClaimSpec(E.PRINTING, PriorityClass.FIELD_DIALOG, 10),
    ClaimSpec(E.ADVANCE, PriorityClass.FIELD_DIALOG, 10),
    ClaimSpec(E.END, PriorityClass.FIELD_DIALOG, 10),
    ClaimSpec(E.ITEM_RECEIVE, PriorityClass.FIELD_DIALOG, 20),
    ClaimSpec(E.UNKNOWN_DIALOG, PriorityClass.FIELD_DIALOG, 0),
    ClaimSpec(E.MAP_TRANSITION, PriorityClass.BUSY, 10),
    ClaimSpec(E.SCRIPTED_MOVEMENT, PriorityClass.BUSY, 10),
)

#: every scene id this family can claim
CLAIMED_SCENES = frozenset(c.scene_id for c in CLAIMS)


class DialogDetector:
    """Claims ``scene_id`` iff the family classifier says so. Satisfies ``SceneDetector``."""

    def __init__(self, spec: ClaimSpec, sidecar: TextSidecar | None = None) -> None:
        self.name = f"dialog-family:{spec.scene_id}"
        self.priority = spec.priority
        self.specificity = spec.specificity
        self.scene_id = spec.scene_id
        self.claimed_scenes = frozenset({spec.scene_id})
        self._sidecar = sidecar

    def detect(self, snapshot: StateSnapshot) -> SceneResult | None:
        text = self._sidecar.get(snapshot) if self._sidecar is not None else None
        verdict = E.classify(snapshot, text)
        if verdict is None or verdict.scene_id != self.scene_id:
            return None
        return SceneResult(
            scene_id=verdict.scene_id,
            confidence=verdict.confidence,
            busy=verdict.busy,
            evidence=verdict.evidence,
        )


def build_detectors(sidecar: TextSidecar | None = None) -> tuple[DialogDetector, ...]:
    return tuple(DialogDetector(spec, sidecar) for spec in CLAIMS)
