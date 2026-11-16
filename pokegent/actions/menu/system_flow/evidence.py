"""Which system-flow scene does this snapshot show? One classifier, thin detectors.

Every detector of the family calls :func:`classify` and keeps the verdict naming its own scene, so a
snapshot yields at most ONE claim of this family by construction. Everything comes from
``snapshot.ui`` (M2.1): ``gMain.callback2`` and the task list with its data words. No claim is made
from a screenshot, a frame counter or a pointer that the profile does not name; an unnamed
callback/task is never a reason to claim (the registry then reports ``scene.unknown``, no input).

Per-concern classifiers: :mod:`.evidence_endgame` (Hall of Fame, credits, boot/intro, title, main
menu), :mod:`.evidence_evolution` (evolution scene and its learn-move part), :mod:`.evidence_pc`
(PC storage and the PC menu).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.games.gen3.firered.reader.ui_schemas import TaskInfo, UiEvidence

#: ``FLAG_SYS_GAME_CLEAR`` (verified constant, ``firered/constants.py``); the run-level fact,
# see facts.py
FLAG_SYS_GAME_CLEAR = 0x82C


@dataclass(frozen=True)
class Verdict:
    scene_id: str
    confidence: float
    busy: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)


# -- small views over the UI block -----------------------------------------------------------------


def callback_name(ui: UiEvidence) -> str | None:
    cb = ui.main.callback2
    return cb.label if cb is not None else None


def task_named(ui: UiEvidence, name: str) -> TaskInfo | None:
    """The first active task whose profile symbol is ``name`` (None if none, or the name is
    unknown)."""
    return next((t for t in ui.tasks.active if t.func.name == name), None)


def any_task(ui: UiEvidence, names) -> TaskInfo | None:
    wanted = set(names)
    return next((t for t in ui.tasks.active if t.func.name in wanted), None)


def word(task: TaskInfo, index: int) -> int:
    """``task.data[index]`` as an unsigned 16-bit word (the game stores s16)."""
    return task.data[index] & 0xFFFF if index < len(task.data) else 0


def waiting_printers(ui: UiEvidence) -> tuple[Any, ...]:
    return tuple(p for p in ui.text.printers if p.waiting_for_button)


def confidence_cap(ui: UiEvidence, *sections: str) -> float:
    """1.0 when every section used is verified evidence, else 0.7 (below the strong threshold)."""
    low = min((ui.confidence.get(s, 0.0) for s in sections), default=0.0)
    return 1.0 if low >= 1.0 else 0.7


def usable_ui(snapshot: StateSnapshot) -> UiEvidence | None:
    ui = snapshot.ui
    if ui is None or not ui.valid or not ui.main.valid or not ui.tasks.valid:
        return None
    return ui


def classify(snapshot: StateSnapshot, foreign: frozenset[str] = frozenset()) -> Verdict | None:
    """The one claim of this family for ``snapshot``, or ``None`` (not ours / not provable).

    ``foreign``: the scene ids of the opt-in back-out safety net that are switched on
    (:mod:`.foreign`); empty by default.
    """
    from pokegent.actions.menu.system_flow import (
        evidence_endgame,
        evidence_evolution,
        evidence_pc,
    )
    from pokegent.actions.menu.system_flow import (
        foreign as foreign_menus,
    )

    ui = usable_ui(snapshot)
    if ui is None:
        return None
    for part in (evidence_endgame, evidence_evolution, evidence_pc):
        verdict = part.classify(snapshot, ui)
        if verdict is not None:
            return verdict
    specific = foreign_menus.classify(snapshot, ui, foreign)
    if specific is not None:
        return specific
    return foreign_menus.classify_generic(snapshot, ui)


def phase_key(verdict: Verdict | None) -> tuple[Any, ...] | None:
    """What counts as "the screen moved on": scene, phase and the state words of the verdict."""
    if verdict is None:
        return None
    ev = verdict.evidence
    return (
        verdict.scene_id,
        ev.get("phase"),
        ev.get("task"),
        ev.get("state"),
        ev.get("learn_state"),
        ev.get("cursor"),
    )


def game_clear(snapshot: StateSnapshot) -> bool | None:
    """``FLAG_SYS_GAME_CLEAR`` as a run-level fact; ``None`` when the snapshot cannot say."""
    if not snapshot.usable or snapshot.flags is None:
        return None
    return bool(snapshot.flags.is_set(FLAG_SYS_GAME_CLEAR))
