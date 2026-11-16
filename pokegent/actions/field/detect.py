"""Detectors of the field family: ``field.free`` (controllable / stepping) and
``field.trainer_vision``.

Both are views of :func:`~pokegent.actions.field.evidence.classify`, so within the family a
snapshot yields at most ONE claim by construction. Priority: ``field.free`` is class FIELD (6);
``field.trainer_vision`` is a BUSY claim (class 7) with specificity 20, above the dialog family's
generic ``field.scripted_movement`` (specificity 10), so the two never tie.
"""

from __future__ import annotations

from dataclasses import dataclass

from pokegent.actions.field import evidence as E
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.state.scenes import PriorityClass, SceneResult


@dataclass(frozen=True)
class ClaimSpec:
    scene_id: str
    priority: PriorityClass
    specificity: int


CLAIMS: tuple[ClaimSpec, ...] = (
    ClaimSpec("menu.fly_map", PriorityClass.MENU, 30),
    ClaimSpec(E.CURRENT, PriorityClass.FIELD, 30),
    ClaimSpec(E.DARKNESS, PriorityClass.FIELD, 11),
    ClaimSpec(E.FREE, PriorityClass.FIELD, 10),
    ClaimSpec(E.TRAINER_VISION, PriorityClass.BUSY, 20),
)

CLAIMED_SCENES = frozenset(c.scene_id for c in CLAIMS)


class FieldDetector:
    """Claims ``scene_id`` iff the family classifier says so. Satisfies ``SceneDetector``."""

    def __init__(self, spec: ClaimSpec) -> None:
        self.name = f"field-family:{spec.scene_id}"
        self.priority = spec.priority
        self.specificity = spec.specificity
        self.scene_id = spec.scene_id

    def detect(self, snapshot: StateSnapshot) -> SceneResult | None:
        if self.scene_id == "menu.fly_map":
            ui = snapshot.ui
            if ui is None or not ui.main.valid or not ui.tasks.valid:
                return None
            callback = ui.main.callback2
            if callback is None or callback.name != "CB2_RegionMap":
                return None
            task = next((task for task in ui.tasks.active if task.func.name == "Task_FlyMap"), None)
            if task is None:
                return None
            return SceneResult(
                scene_id=self.scene_id,
                # The callback/task pair is the detector's complete proof.  The callback symbol
                # is currently reference-sourced (and therefore lowers section confidence), but
                # withholding this exact recovery scene would recreate the restart deadlock.
                confidence=0.9,
                evidence={
                    "callback": callback.name,
                    "task": task.func.name,
                    "task_slot": task.slot,
                    "task_state": task.data[0] if task.data else None,
                },
            )
        verdict = E.classify(snapshot)
        if verdict is None or verdict.scene_id != self.scene_id:
            if (
                self.scene_id == E.DARKNESS
                and verdict is not None
                and verdict.scene_id == E.FREE
                and snapshot.map is not None
                and snapshot.map.identity.cave
                and (snapshot.map.flash_level or 0) > 0
            ):
                return SceneResult(
                    scene_id=self.scene_id,
                    confidence=verdict.confidence,
                    busy=verdict.busy,
                    evidence={
                        **verdict.evidence,
                        "cave": snapshot.map.identity.cave,
                        "flash_level": snapshot.map.flash_level,
                    },
                )
            return None
        return SceneResult(
            scene_id=verdict.scene_id,
            confidence=verdict.confidence,
            busy=verdict.busy,
            evidence=verdict.evidence,
        )


def build_detectors() -> tuple[FieldDetector, ...]:
    return tuple(FieldDetector(spec) for spec in CLAIMS)
