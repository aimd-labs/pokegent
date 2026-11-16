"""Postconditions of the dialog family (spec 11.3 "Dialog Choice: Script-/Menu-State ...").

Each verifier is a pure predicate on ``VerifyContext(before, after, ...)`` with its own bounded wait
(the executor polls it every ``check_every`` frames until it holds or the bound ends). "Prompt
unchanged after the input" is the failure; the executor reports it as ``uncertain`` /
``E_POSTCONDITION_NOT_MET`` and the runner's no-progress counter escalates repeats.
"""

from __future__ import annotations

from pokegent.actions._exec.registry import VerifierSpec, VerifyContext
from pokegent.actions.menu.dialog import builder as B
from pokegent.actions.menu.dialog import evidence as E
from pokegent.actions.menu.dialog.macros import list_position
from pokegent.actions.menu.dialog.text import TextSidecar


def _text_settled_for(sidecar: TextSidecar | None):
    def check(v: VerifyContext) -> bool:
        """The printing we waited for is over: the scene left printing, or the box shows another
        page / message now (the next text may already be printing when this is checked)."""
        if v.args.get("_done"):  # the executor's bounded wait already saw exactly this condition
            return True
        if v.scene.scene_id != E.PRINTING:
            return True
        if sidecar is None:
            return False
        before, after = sidecar.identity(v.before), sidecar.identity(v.after)
        return before is not None and after is not None and before != after

    return check


def _box_moved_on(v: VerifyContext) -> bool:
    """The game is no longer blocked on what it was blocked on before the press."""
    before = E.waiting_signature(v.before.ui)
    after = E.waiting_signature(v.after.ui)
    if not before:
        return after != before
    return bool(before - after)


def _fallback_progressed_for(sidecar: TextSidecar | None):
    """A generic A on an unowned field box must produce observable RAM/UI progress."""

    def check(v: VerifyContext) -> bool:
        if v.scene.scene_id != E.ADVANCE:
            return True
        ui = v.after.ui
        if ui is None or not ui.valid:
            return False
        # A missing active window-0 printer proves the box is idle/closed.  Pointer movement or
        # printer-state churn alone is deliberately not progress: only a proven text-page change
        # may satisfy the fallback while the printer remains active.
        if E.window0_printer(ui) is None:
            return True
        if sidecar is None:
            return False
        before, after = sidecar.identity(v.before), sidecar.identity(v.after)
        return before is not None and after is not None and before != after

    return check


def _prompt_closed(v: VerifyContext) -> bool:
    ui = v.after.ui
    if ui is None or not ui.valid:
        return False
    if E.yes_no_open(ui) or E.pc_storage_yes_no_open(ui) or E.multichoice_open(ui):
        return False
    expected = v.option.facts.get("return_phase") if v.option is not None else None
    if expected:
        if v.scene.scene_id == "field.free":
            return expected == "field.free"
        if v.scene.scene_id == "pc.main":
            from pokegent.actions.menu.system_flow import evidence as system_evidence

            verdict = system_evidence.classify(v.after)
            return verdict is not None and verdict.evidence.get("phase") == expected
        return False
    return True


def _pc_destination(v: VerifyContext) -> bool:
    key = v.option.id
    evidence = v.scene.evidence
    if key in ("pc:someone", "pc:bill"):
        return v.scene.scene_id == "pc.main" and evidence.get("menu") == "pc_menu"
    if key == "pc:player":
        return v.scene.scene_id == "pc.main" and evidence.get("menu") == "player_pc"
    if key == "pc:oak":
        return v.scene.scene_id == E.YES_NO
    if key == "pc:hall_of_fame":
        return v.scene.scene_id == "special.hall_of_fame"
    if key in ("pc:log_off", "cancel_choice"):
        return v.scene.scene_id == "field.free"
    return False


def _whiteout_done(v: VerifyContext) -> bool:
    return v.scene.scene_id != E.WHITEOUT


def _list_closed(v: VerifyContext) -> bool:
    ui = v.after.ui
    return bool(ui is not None and ui.valid and not E.list_open(ui))


def _list_moved(v: VerifyContext) -> bool:
    now = list_position(v.after)
    return now is not None and now != list_position(v.before)


def verifier_specs(sidecar: TextSidecar | None = None) -> tuple[VerifierSpec, ...]:
    return (
        VerifierSpec(
            B.POST_TEXT_SETTLED, _text_settled_for(sidecar), max_frames=60, max_wall_s=5.0
        ),
        VerifierSpec(B.POST_BOX_MOVED_ON, _box_moved_on, max_frames=120, max_wall_s=8.0),
        VerifierSpec(
            B.POST_FALLBACK_PROGRESS,
            _fallback_progressed_for(sidecar),
            max_frames=120,
            max_wall_s=8.0,
        ),
        VerifierSpec(B.POST_PROMPT_CLOSED, _prompt_closed, max_frames=150, max_wall_s=8.0),
        VerifierSpec(B.POST_PC_DESTINATION, _pc_destination, max_frames=120, max_wall_s=8.0),
        VerifierSpec(B.POST_WHITEOUT_DONE, _whiteout_done, max_frames=60, max_wall_s=5.0),
        VerifierSpec(B.POST_LIST_CLOSED, _list_closed, max_frames=150, max_wall_s=8.0),
        VerifierSpec(B.POST_LIST_MOVED, _list_moved, max_frames=60, max_wall_s=5.0),
    )
