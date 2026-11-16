"""The evolution scene and the learn-move part of it, from ``Task_EvolutionScene``'s data words.

Decomp facts (``src/evolution_scene.c``): the scene runs under ``CB2_EvolutionSceneUpdate``; its
task
keeps ``tState`` in data[0], the species in data[1]/data[2], ``tBits`` in data[3] (bit 0 = the
evolution
may be stopped, bit 7 = a move is being learned), ``tLearnMoveState`` in data[6] and, while a
yes/no is
open, the next states of YES and NO in data[7]/data[8].

* B cancels ONLY in ``EVOSTATE_WAIT_CYCLE_MON_SPRITE`` and only if bit 0 of ``tBits`` is set; that
  window is ``evolution.start``. Everywhere else B does nothing for the evolution (and in the
  learn-move
  part it means NO), so no other scene of this family ever offers B.
* Messages that end in a paragraph or ``{PAUSE_UNTIL_PRESS}`` leave the window-0 printer waiting for
  A; they are the only places in the scene that read A outside the yes/no.
* While a move is chosen the summary screen runs under its own callback with ``Task_EvolutionScene``
  still alive (``tLearnMoveState`` 5/6).
"""

from __future__ import annotations

from pokegent.actions.menu.system_flow import layouts as L
from pokegent.actions.menu.system_flow import refs as R
from pokegent.actions.menu.system_flow import symbols as S
from pokegent.actions.menu.system_flow.evidence import (
    Verdict,
    callback_name,
    confidence_cap,
    task_named,
    waiting_printers,
    word,
)
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.games.gen3.firered.reader.ui_schemas import TaskInfo, UiEvidence


def evo_view(task: TaskInfo) -> dict[str, int]:
    """The task's data words by meaning (all unsigned)."""
    return {
        "state": word(task, L.EVO_D_STATE),
        "pre": word(task, L.EVO_D_PRE_SPECIES),
        "post": word(task, L.EVO_D_POST_SPECIES),
        "bits": word(task, L.EVO_D_BITS),
        "learn_state": word(task, L.EVO_D_LEARN_STATE),
        "yes_state": word(task, L.EVO_D_YES_STATE),
        "no_state": word(task, L.EVO_D_NO_STATE),
        "was_stopped": word(task, L.EVO_D_WAS_STOPPED),
        "party_id": word(task, L.EVO_D_PARTY_ID),
    }


def classify(snapshot: StateSnapshot, ui: UiEvidence) -> Verdict | None:
    cb = callback_name(ui)
    if cb is None:
        return None
    cap = confidence_cap(ui, "main", "tasks", "text")

    def v(scene: str, conf: float, busy: bool = False, **ev) -> Verdict:
        return Verdict(scene, min(conf, cap), busy, {"callback": cb, **ev})

    if cb in S.EVOLUTION_LOAD_CALLBACKS:
        return v(R.EVO_ANIMATION, 0.9, True, phase="loading")
    task = task_named(ui, S.EVOLUTION_TASK)
    if task is None:
        return None
    if cb == S.SUMMARY_SETUP_CALLBACK:
        d = evo_view(task)
        if d["state"] == L.EVOSTATE_REPLACE_MOVE:
            return v(R.EVO_MOVE, 0.85, True, phase="summary_loading", **_evo_ev(d))
        return None
    if cb == S.SUMMARY_CALLBACK:
        # the move-to-forget screen of the evolution's learn-move: the task is alive and waits
        # for it
        d = evo_view(task)
        if d["state"] == L.EVOSTATE_REPLACE_MOVE:
            return v(R.EVO_MOVE, 0.9, False, phase="choose_slot", **_evo_ev(d))
        return None
    if cb != S.EVOLUTION_CALLBACK:
        return None
    d = evo_view(task)
    state = d["state"]
    if state not in L.EVOSTATE_NAMES:
        return None
    ev = _evo_ev(d)
    waiting = waiting_printers(ui)
    learning = bool(d["bits"] & L.EVO_BIT_LEARN_MOVE) or state in L.EVO_LEARN_STATES
    if waiting:
        # the game is provably blocked on A/B in a message (paragraph or wait-for-press)
        if learning and state >= L.EVOSTATE_TRY_LEARN_MOVE:
            return v(R.EVO_MOVE, 0.95, False, phase="message", **ev)
        if state in L.EVO_DONE_STATES:
            return v(R.EVO_DONE, 0.95, False, phase="message", **ev)
        return None  # text waiting in a state that never waits: not provable, not pressed
    if state == L.EVOSTATE_REPLACE_MOVE:
        if d["learn_state"] == L.MVSTATE_HANDLE_YES_NO:
            question = (
                "delete_a_move"
                if d["yes_state"] == L.MVSTATE_SHOW_MOVE_SELECT
                else ("stop_learning" if d["yes_state"] == L.MVSTATE_CANCEL else None)
            )
            if question is None:
                return None
            return v(R.EVO_MOVE, 0.95, False, phase="yes_no", question=question, **ev)
        return v(R.EVO_MOVE, 0.85, True, phase="move_state_" + str(d["learn_state"]), **ev)
    if state in (L.EVOSTATE_LEARNED_MOVE, L.EVOSTATE_TRY_LEARN_ANOTHER):
        return v(R.EVO_MOVE, 0.85, True, phase="learned", **ev)
    if state == L.EVOSTATE_WAIT_CYCLE:
        if d["bits"] & L.EVO_BIT_CAN_STOP and not d["was_stopped"]:
            return v(R.EVO_START, 0.97, False, phase="cancel_window", **ev)
        return v(R.EVO_ANIMATION, 0.95, False, phase="cycle_cannot_cancel", **ev)
    return v(R.EVO_ANIMATION, 0.95, state in (0, 12, 16), phase=L.EVOSTATE_NAMES[state], **ev)


def _evo_ev(d: dict[str, int]) -> dict[str, object]:
    return {
        "state": d["state"],
        "learn_state": d["learn_state"],
        "species_from": d["pre"],
        "species_to": d["post"],
        "can_cancel": bool(d["bits"] & L.EVO_BIT_CAN_STOP),
        "was_stopped": bool(d["was_stopped"]),
        "party_id": d["party_id"],
    }
