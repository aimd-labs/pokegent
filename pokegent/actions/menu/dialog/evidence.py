"""Which dialog / busy scene does this snapshot show? One classifier, many thin detectors.

The detectors of this family all call :func:`classify` and keep the verdict that names their own
scene, so within the family a snapshot yields at most ONE claim by construction (a busy claim can
never sit next to a ready dialog claim of the same family and wrongly mark it busy).

Everything comes from ``snapshot.ui`` (M2.1, the runtime contracts section 3) plus, optionally,
the proven-current message text (``text.py``). Rules that follow the trap list there:

* ``in_battle`` is the gate: nothing here claims during a battle (the battle family owns it);
* only field callbacks (``CB2_Overworld``/``CB2_OverworldBasic``) carry field dialogs; the map-load
  callbacks and ``CB2_WhiteOut`` are busy phases;
* a window id is never proof: yes/no needs the task AND the window AND ``sMenu`` on that window with
  range 0..1; multichoice needs its task, its window and ``sMenu`` on it;
* a printer that is no longer active is no dialog; the box that stays up after the text ended is the
  script's native wait ``WaitForAorBPress``;
* a UI owned by another family (START menu, shop, PC, bag, party ...) is none of our business and
  yields no claim, so the registry reports unknown (never a guessed dialog).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pokegent.actions.menu.dialog.prompts import PromptKind, classify_prompt
from pokegent.actions.menu.dialog.text import DialogText
from pokegent.games.gen3.firered.reader import ui_symbols as U
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.games.gen3.firered.reader.ui_schemas import (
    PrinterInfo,
    TaskInfo,
    UiEvidence,
)

# scene ids (references/scenes.yaml)
PRINTING = "dialog.printing"
ADVANCE = "dialog.advance"
END = "dialog.end"
YES_NO = "dialog.yes_no"
PARTY_EXIT_TASK = "Task_HandleFieldMoveExitAreaYesNoInput"
PARTY_REPLACE_TASK = "Task_HandleReplaceMoveYesNoInput"
PARTY_STOP_TASK = "Task_HandleStopLearningMoveYesNoInput"
PARTY_CLOSE_TASK = "Task_ClosePartyMenuAfterText"
MULTICHOICE = "dialog.multichoice"
GRID_CHOICE = "dialog.grid_choice"
LIST = "dialog.list"
HEAL_OFFER = "dialog.heal_offer"
ITEM_RECEIVE = "dialog.item_receive"
UNKNOWN_DIALOG = "dialog.unknown"
MAP_TRANSITION = "field.map_transition"
SCRIPTED_MOVEMENT = "field.scripted_movement"
WHITEOUT = "system.whiteout"
QUEST_LOG = "system.quest_log_playback"

#: opaque task signature of the post-whiteout "scurried to a POKeMON CENTER" box (profile-bound,
#: unnamed in the profile). Seen in `pre-system1-live-trace.state` and one more corpus state, with
#: that message in a window-1 printer waiting on A. Treated as a signature, not as a symbol.
WHITEOUT_RUSH_TASK = {"firered-us-rev0": "fn:0x0807F45C"}

#: multichoice ids that are a plain two-entry YES / NO menu (entry 0 = YES, entry 1 = NO; B = NO).
#: The Pokemon Center nurse asks through `MULTI_YESNO`, not through the script yes/no task. Found on
#: `incident-12_5-1791328157.state` (mcId 0, two entries, text "... heal your POKeMON ...") and
#: confirmed live: entry 0 + A -> "Okay, I'll take your POKeMON ...", DOWN + A and B -> "We hope to
#: see you again!". The content of a multichoice is fixed by its id, so one proof covers all uses.
YES_NO_MULTICHOICE_IDS = {"firered-us-rev0": frozenset({0})}

#: native script waits of the field message box
NATIVE_WAIT_A_B = "WaitForAorBPress"
NATIVE_MSG_HIDDEN = "IsFieldMessageBoxHidden"

#: task kinds (ui_symbols.TASK_KINDS) that belong to this family; every other named kind is a UI
#: owned by someone else.
_OWN_TASK_KINDS = frozenset(
    {"yes_no", "multichoice", "list_menu", "field_message_box", "script_picture", "warp"}
)
_QUEST_LOG_PLAYBACK = (2, 3)  # gQuestLogState: 2 playback, 3 last playback (src/quest_log.c)


@dataclass(frozen=True)
class Verdict:
    scene_id: str
    confidence: float
    busy: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)


# -- small views ------------------------------------------------------------------------------


def callback_name(ui: UiEvidence) -> str | None:
    cb = ui.main.callback2
    return cb.name if cb is not None else None


def native_name(ui: UiEvidence) -> str | None:
    n = ui.script.native
    return n.name if n is not None else None


def task_kind(task: TaskInfo) -> str | None:
    return U.TASK_KINDS.get(task.func.name) if task.func.name else None


def foreign_ui_tasks(ui: UiEvidence) -> tuple[str, ...]:
    return tuple(
        t.func.label
        for t in ui.tasks.active
        if (k := task_kind(t)) is not None and k not in _OWN_TASK_KINDS
    )


def task_of_kind(ui: UiEvidence, kind: str) -> TaskInfo | None:
    return next((t for t in ui.tasks.active if task_kind(t) == kind), None)


def window0_printer(ui: UiEvidence) -> PrinterInfo | None:
    return next((p for p in ui.text.printers if p.window_id == 0), None)


def yes_no_open(ui: UiEvidence) -> bool:
    """A script yes/no prompt that is provably open: task + window + sMenu on that window."""
    yn, sm = ui.menu.yes_no, ui.menu.menu
    return bool(
        yn is not None
        and yn.field_prompt_open
        and sm is not None
        and sm.window_in_use
        and sm.consistent
        and sm.window_id == yn.window_id
        and sm.min_cursor_pos == 0
        and sm.max_cursor_pos == 1
    )


def yes_no_task_menu_open(ui: UiEvidence) -> bool:
    """A yes/no task with a live two-entry menu, even if its window id briefly lags."""
    yn, sm = ui.menu.yes_no, ui.menu.menu
    return bool(
        yn is not None
        and yn.task_active
        and sm is not None
        and sm.window_in_use
        and sm.consistent
        and sm.min_cursor_pos == 0
        and sm.max_cursor_pos == 1
    )


def pc_storage_yes_no_open(ui: UiEvidence) -> bool:
    """Pret's storage-local Continue BOX prompt: owning task plus its live two-row menu."""
    yn, sm = ui.menu.yes_no, ui.menu.menu
    return bool(
        callback_name(ui) == "CB2_PokeStorage"
        and any(task.func.name == "Task_OnBPressed" for task in ui.tasks.active)
        and yn is not None
        and yn.window_in_use
        and sm is not None
        and sm.window_in_use
        and sm.consistent
        and sm.window_id == yn.window_id
        and sm.min_cursor_pos == 0
        and sm.max_cursor_pos == 1
    )


def party_exit_yes_no_open(ui: UiEvidence) -> bool:
    """Dig/Teleport's input task owns this menu, not the field-script yes/no task."""
    return _party_yes_no_open(ui, PARTY_EXIT_TASK)


def party_learning_yes_no_task(ui: UiEvidence) -> str | None:
    """Return the source-bound learning prompt owner, only with a live input menu."""
    for task in (PARTY_REPLACE_TASK, PARTY_STOP_TASK):
        if _party_yes_no_open(ui, task):
            return task
    return None


def party_close_message_waiting(ui: UiEvidence) -> bool:
    """The party close task waits for window 6's final PAUSE_UNTIL_PRESS message."""
    party = ui.menu.party
    return bool(
        ui.valid and ui.main.valid and ui.main.in_battle is False
        and ui.tasks.valid and ui.menu.valid and ui.text.valid
        and callback_name(ui) == "CB2_UpdatePartyMenu"
        and ui.field.valid and ui.field.fade_active is False
        and party is not None and party.active_hint is True and party.menu_type == 0
        and any(t.func.name == PARTY_CLOSE_TASK and t.func.verified for t in ui.tasks.active)
        and any(w.window_id == 6 for w in ui.text.windows)
        and any(p.window_id == 6 and p.waiting_for_button for p in ui.text.printers)
    )


def _party_yes_no_open(ui: UiEvidence, task_name: str) -> bool:
    yn, sm, party = ui.menu.yes_no, ui.menu.menu, ui.menu.party
    return bool(
        ui.valid and ui.main.valid and ui.main.in_battle is False
        and ui.tasks.valid and ui.menu.valid and ui.text.valid
        and callback_name(ui) == "CB2_UpdatePartyMenu"
        and ui.field.valid and ui.field.fade_active is False
        and party is not None and party.active_hint is True and party.menu_type == 0
        and any(t.func.name == task_name and t.func.verified for t in ui.tasks.active)
        and yn is not None and yn.window_in_use
        and sm is not None and sm.window_in_use and sm.consistent
        and sm.window_id == yn.window_id
        and sm.min_cursor_pos == 0 and sm.max_cursor_pos == 1
        and sm.cursor_pos in (0, 1)
    )


def question_menu_open(ui: UiEvidence) -> bool:
    """A live two-entry script menu corroborated by its owning task or native wait."""
    sm = ui.menu.menu
    return bool(
        ui.tasks.valid
        and sm is not None
        and sm.window_in_use
        and sm.consistent
        and sm.min_cursor_pos == 0
        and sm.max_cursor_pos == 1
        and (
            any(len(task.data) > 6 and task.data[6] == sm.window_id for task in ui.tasks.active)
            or native_name(ui) == NATIVE_WAIT_A_B
        )
    )


def multichoice_open(ui: UiEvidence) -> bool:
    mc, sm = ui.menu.multichoice, ui.menu.menu
    return bool(
        mc is not None
        and mc.window_in_use
        and sm is not None
        and sm.window_in_use
        and sm.consistent
        and sm.window_id == mc.window_id
        and sm.min_cursor_pos == 0
        and sm.max_cursor_pos >= 0
    )


def yes_no_multichoice(ui: UiEvidence, rom_profile: str) -> bool:
    """A live two-entry multichoice whose id is known to mean YES / NO."""
    mc, sm = ui.menu.multichoice, ui.menu.menu
    return bool(
        multichoice_open(ui)
        and mc is not None
        and sm is not None
        and sm.max_cursor_pos == 1
        and mc.multichoice_id in YES_NO_MULTICHOICE_IDS.get(rom_profile, frozenset())
    )


def list_open(ui: UiEvidence) -> bool:
    lm = ui.menu.list_menu
    return bool(lm is not None and lm.window_in_use and lm.total_items > 0 and lm.max_showed > 0)


def elevator_script_wait(snapshot: StateSnapshot, ui: UiEvidence) -> bool:
    """Recognize the post-selection Silph elevator script handoff.

    ``Task_ListMenuHandleInput`` can remain in the task list after the field-specials
    list window has been removed.  The script is then stopped by ``waitstate`` until
    the special's task re-enables it; this is an A-advance boundary, not generic
    scripted movement.  See pokefirered ``src/scrcmd.c:127`` and
    ``src/field_specials.c:1407-1450``.
    """
    state = snapshot.map
    script = ui.script
    return bool(
        state is not None
        and state.identity.name == "SilphCo_Elevator"
        and script.status_name == "waiting"
        and script.mode_name == "bytecode"
        and script.controls_locked is True
        and not ui.text.printers
        and not ui.text.field_message_task
        and native_name(ui) is None
        and "Task_FieldListMenuHandleInput" in ui.task_signatures
        and "ListMenuDummyTask" in ui.task_signatures
        and not list_open(ui)
    )


def waiting_signature(ui: UiEvidence | None) -> frozenset[tuple[Any, ...]]:
    """What the game is currently blocked on in the message box: waiting printers and the native
    A/B wait. Used by the verifiers ("the box moved on")."""
    if ui is None or not ui.valid:
        return frozenset()
    sig: set[tuple[Any, ...]] = {
        ("printer", p.window_id, p.current_char_ptr)
        for p in ui.text.printers
        if p.waiting_for_button
    }
    if native_name(ui) == NATIVE_WAIT_A_B:
        sig.add(("native", NATIVE_WAIT_A_B))
    return frozenset(sig)


def _used_sections_confidence(ui: UiEvidence) -> float:
    return min(ui.confidence.get(s, 0.0) for s in ("main", "tasks", "text", "script", "menu"))


# -- the classifier ---------------------------------------------------------------------------


def classify(snapshot: StateSnapshot, text: DialogText | None = None) -> Verdict | None:
    """The one claim of this family for ``snapshot``, or ``None`` (not ours / not provable)."""
    ui = snapshot.ui
    if ui is None or not ui.valid or not ui.main.valid:
        return None
    cb = callback_name(ui)

    # A caught Pokemon with a full party stays in the naming screen after the name is accepted.
    # pret/pokefirered src/naming_screen.c: STATE_WAIT_SENT_TO_PC_MESSAGE runs the field message
    # printer and advances to STATE_FADE_OUT only on A.  This is a dialog even though gMain's
    # inBattle bit remains set and the naming task is still active.
    if cb == "CB2_NamingScreen" and ui.main.naming_screen_state == 7 and ui.text.valid:
        msg = window0_printer(ui)
        if msg is None:
            return Verdict(
                ADVANCE,
                0.95,
                False,
                {
                    "callback": cb,
                    "mechanism": "naming_screen_wait_sent_to_pc_message",
                    "naming_state": 7,
                    "decomp_state": "STATE_WAIT_SENT_TO_PC_MESSAGE",
                    "printer_state": None,
                    "fallback": True,
                },
            )
        evidence = {
            "callback": cb,
            "mechanism": "naming_screen_wait_sent_to_pc_message",
            "naming_state": 7,
            "decomp_state": "STATE_WAIT_SENT_TO_PC_MESSAGE",
            "printer_state": msg.state_name,
        }
        if msg.waiting_for_button:
            return Verdict(ADVANCE, 0.95, False, evidence)
        return Verdict(PRINTING, 0.95, False, evidence)
    if cb == "CB2_NamingScreen" and ui.main.naming_screen_state in (8, 9):
        # STATE_FADE_OUT / STATE_EXIT: the naming screen is handing control back to its caller.
        # This is a real script-owned transition, but it is not the new-game intro movement.
        return Verdict(
            SCRIPTED_MOVEMENT,
            0.95,
            True,
            {
                "callback": cb,
                "mechanism": "naming_screen_exit",
                "naming_state": ui.main.naming_screen_state,
            },
        )
    if cb in ("CB2_LoadNamingScreen", "CB2_ReturnFromNamingScreen"):
        return Verdict(
            SCRIPTED_MOVEMENT,
            0.95,
            True,
            {"callback": cb, "mechanism": "naming_screen_transition"},
        )
    if cb == "CB2_NamingScreen" and ui.main.naming_screen_state not in (2, 7, 8, 9):
        return Verdict(
            SCRIPTED_MOVEMENT,
            0.95,
            True,
            {
                "callback": cb,
                "mechanism": "naming_screen_transition",
                "naming_state": ui.main.naming_screen_state,
            },
        )
    if ui.main.in_battle:
        return None  # battle (incl. capture naming) belongs to the battle family

    if party_exit_yes_no_open(ui):
        return Verdict(
            YES_NO, 0.95, False,
            {"callback": cb, "mechanism": "party_exit_yes_no", "source_task": PARTY_EXIT_TASK},
        )

    learning_task = party_learning_yes_no_task(ui)
    if learning_task is not None:
        return Verdict(
            YES_NO, 0.95, False,
            {"callback": cb, "mechanism": "party_learning_yes_no",
             "source_task": learning_task},
        )

    if party_close_message_waiting(ui):
        return Verdict(
            ADVANCE, 0.95, False,
            {"callback": cb, "mechanism": "party_close_message",
             "source_task": PARTY_CLOSE_TASK},
        )

    if cb == "CB2_WhiteOut":
        return Verdict(WHITEOUT, 0.9, True, {"phase": "white_out_callback", "callback": cb})
    if cb in U.SEAGALLOP_CALLBACKS:
        return Verdict(
            MAP_TRANSITION,
            0.95,
            True,
            {"phase": "seagallop_ferry", "callback": cb},
        )
    if cb in U.MAP_LOAD_CALLBACKS:
        return Verdict(MAP_TRANSITION, 0.9, True, {"phase": "map_load", "callback": cb})
    if cb == "CB2_PokeStorage":
        if (
            ui.tasks.valid
            and ui.text.valid
            and ui.script.valid
            and ui.menu.valid
            and pc_storage_yes_no_open(ui)
        ):
            return Verdict(
                YES_NO,
                0.95,
                False,
                {"callback": cb, "mechanism": "pc_storage_yes_no", "prompt": "continue_box"},
            )
        return None
    if cb not in U.FIELD_CALLBACKS and cb != "CB2_PokeStorage":
        return None
    if not (ui.tasks.valid and ui.text.valid and ui.script.valid and ui.menu.valid):
        return None

    if elevator_script_wait(snapshot, ui):
        return Verdict(
            ADVANCE,
            0.95,
            False,
            {
                "callback": cb,
                "mechanism": "silph_elevator_script_wait",
                "fallback": True,
                "script": ui.script.status_name,
                "task_signatures": list(ui.task_signatures),
            },
        )

    # a menu we do not own wins the screen: stay silent
    foreign = foreign_ui_tasks(ui)
    if foreign and cb != "CB2_PokeStorage":
        return None

    unverified = _used_sections_confidence(ui) < 1.0
    cap = 0.7 if unverified else 1.0

    def v(scene: str, conf: float, busy: bool = False, **ev: Any) -> Verdict:
        return Verdict(scene, min(conf, cap), busy, {"callback": cb, **ev})

    ql = ui.menu.quest_log_state
    if ql in _QUEST_LOG_PLAYBACK:
        return v(QUEST_LOG, 0.8, quest_log_state=ql)

    script = ui.script
    locked = bool(script.controls_locked)
    info = classify_prompt(text) if text is not None else None

    # -- modal prompts -------------------------------------------------------------------------
    yn = ui.menu.yes_no
    if yn is not None and yn.task_active:
        if yes_no_open(ui) or yes_no_task_menu_open(ui):
            busy = yn.input_ready is not True  # buttons are read only once the task timer is 5
            scene = YES_NO
            if info is not None and info.kind is PromptKind.HEAL:
                scene = HEAL_OFFER
            return v(
                scene,
                0.95,
                busy,
                mechanism="script_yes_no",
                prompt=info.kind.value if info else "unreadable",
            )
        return v(UNKNOWN_DIALOG, 0.65, reason="yes_no_task_without_menu_corroboration")
    if ui.menu.multichoice is not None:
        if yes_no_multichoice(ui, snapshot.rom_profile):
            scene = HEAL_OFFER if info is not None and info.kind is PromptKind.HEAL else YES_NO
            return v(
                scene,
                0.95,
                False,
                mechanism="multichoice_yes_no",
                prompt=info.kind.value if info else "unreadable",
            )
        if multichoice_open(ui):
            sm = ui.menu.menu
            assert sm is not None
            scene = MULTICHOICE if sm.columns <= 1 else GRID_CHOICE
            return v(scene, 0.9, entries=sm.max_cursor_pos + 1, columns=sm.columns)
        return v(UNKNOWN_DIALOG, 0.65, reason="multichoice_task_without_menu_corroboration")
    question_menu = bool(
        info is not None
        and info.question
        and info.question.rstrip().endswith("?")
        and question_menu_open(ui)
    )
    if question_menu:
        return v(
            YES_NO,
            0.9,
            bool(ui.script.controls_locked),
            mechanism="question_menu",
            prompt=info.kind.value,
        )
    if list_open(ui):
        lm = ui.menu.list_menu
        assert lm is not None
        return v(LIST, 0.7, total=lm.total_items, shown=lm.max_showed)

    # -- text box ------------------------------------------------------------------------------
    printers = ui.text.printers
    msg = window0_printer(ui)
    if msg is not None:
        # A second stale shape survives with the native field-message wait, but without the
        # owning draw task its printer pointer no longer names a decodable message.  The source
        # sidecar is read from the same RAM snapshot; without a current text identity this is
        # residue, not an owned dialog.  Do not turn it into a high-confidence wait/advance loop:
        # let the field family recover ownership.
        if (
            text is None
            and not ui.text.field_message_task
            and script.running is True
            and script.mode_name == "native"
            and script.native is not None
            and script.native.name == "IsFieldMessageBoxHidden"
        ):
            return None
        corroborated = bool(ui.text.field_message_task) or (
            script.running is True and script.mode_name in ("bytecode", "native")
        )
        if not corroborated:
            # A map transition can leave the old printer struct readable after the script has
            # shut down and released field controls.  It is stale UI, not a dialog: the field
            # family must regain ownership instead of receiving a blind A.
            if script.status_name == "shutdown" and script.controls_locked is False:
                return None
            # Field scripts can leave the message printer and its box alive for one or more
            # frames after their owning task/script has returned.  This is still a real
            # overworld text box: A is the only safe generic input for both a printer that is
            # mid-print and one waiting for input.  The builder attaches a stricter RAM
            # postcondition and the runner escalates after two failed attempts.
            return v(
                ADVANCE,
                0.65,
                fallback=True,
                reason="message_printer_without_script_or_task",
                printer_state=msg.state_name,
            )
        if msg.waiting_for_button:
            scene = ITEM_RECEIVE if info and info.kind is PromptKind.ITEM_RECEIVE else ADVANCE
            return v(scene, 0.95, printer_state=msg.state_name)
        return v(PRINTING, 0.95, printer_state=msg.state_name)
    if printers:
        rush = WHITEOUT_RUSH_TASK.get(snapshot.rom_profile)
        rush_on = rush is not None and rush in ui.task_signatures
        waiting = any(p.waiting_for_button for p in printers)
        if rush_on and waiting and not script.running:
            conf = 0.95 if (info is not None and info.whiteout) else 0.85
            return v(WHITEOUT, conf, phase="acknowledge", windows=[p.window_id for p in printers])
        if rush_on and not waiting and not script.running:
            # M2.5 soak: the same box while its text is still being printed (a lost battle in the
            # corpus sat in `dialog.unknown` for the whole print): nothing to press, wait it out
            return v(
                WHITEOUT, 0.85, True, phase="printing", windows=[p.window_id for p in printers]
            )
        return v(
            UNKNOWN_DIALOG,
            0.65,
            reason="printer_outside_the_field_message_window",
            windows=[p.window_id for p in printers],
        )
    if native_name(ui) == NATIVE_WAIT_A_B and not ui.text.field_message_task:
        scene = ITEM_RECEIVE if info and info.kind is PromptKind.ITEM_RECEIVE else END
        return v(scene, 0.95, native=NATIVE_WAIT_A_B)

    # -- busy phases (nothing to read, nothing to press) ----------------------------------------
    fe = ui.field
    if not fe.valid:
        return None
    if task_of_kind(ui, "warp") is not None:
        return v(MAP_TRANSITION, 0.95, True, phase="warp_task", fade=fe.fade_direction)
    if locked and fe.fade_active:
        return v(MAP_TRANSITION, 0.8, True, phase="fade", fade=fe.fade_direction)
    if locked:
        conf = 0.7 if script.running else 0.65
        return v(
            SCRIPTED_MOVEMENT,
            conf,
            True,
            script=script.status_name,
            mode=script.mode_name,
            moving=fe.moving,
        )
    return None
