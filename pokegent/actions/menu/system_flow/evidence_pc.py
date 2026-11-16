"""The PC: the Pokemon storage screen and the PC menu (``Withdraw / Deposit / Move / See ya``).

Both are screens no macro of this agent owns; the live system froze for hours at one of them
(incident class G10, ``CB2_PokeStorage`` at a Pokemon Center). The family claims them as
``pc.main`` with
the minimal answer: one B to back out (decomp: B in the box asks "Continue BOX operations?"
whose B means
NO and leaves; B in the PC menu is "SEE YA!"). Nothing is withdrawn, deposited or moved.
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
    word,
)
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.games.gen3.firered.reader.ui_schemas import UiEvidence

_FIELD_CALLBACKS = ("CB2_Overworld", "CB2_OverworldBasic")


def classify(snapshot: StateSnapshot, ui: UiEvidence) -> Verdict | None:
    cb = callback_name(ui)
    if cb is None:
        return None
    # Confirmation boxes belong to the generic dialog adapter, even while the PC callback
    # remains installed.  This keeps PC confirmations (toss, transfer, quantity, and box
    # continuation prompts) on the same RAM-cursor yes/no path as every other prompt.
    from pokegent.actions.menu.dialog import evidence as dialog_evidence

    if (
        dialog_evidence.yes_no_open(ui)
        or dialog_evidence.yes_no_task_menu_open(ui)
        or dialog_evidence.pc_storage_yes_no_open(ui)
        or dialog_evidence.yes_no_multichoice(ui, snapshot.rom_profile)
    ):
        return None
    cap = confidence_cap(ui, "main", "tasks")

    def v(conf: float, busy: bool = False, **ev) -> Verdict:
        return Verdict(R.PC_MAIN, min(conf, cap), busy, {"callback": cb, **ev})

    if cb == S.PC_STORAGE_CALLBACK:
        # FireRed's storage-local refusal message (box full, party full, last Pokémon)
        # parks the storage UI in state 2. It has no generic printer/task, so claim it
        # explicitly instead of waiting for a task that will never appear. Successful deposit and
        # withdraw macros retain ownership of their own post-mutation state-2 redraws until the
        # cursor is ready, so a state-2 snapshot reaching this classifier has no preceding mutation
        # owned by the current action and remains the defensive error case.
        storage = snapshot.storage
        if storage is not None and int(storage.ui_state) == 2:
            return v(0.98, False, phase="storage_error", menu="pc_storage")
        # Storage-local error messages are ordinary message-box waits. Let the dialog family
        # claim them so the refused operation becomes a bounded decider frame.
        if ui.text.printers and ui.text.field_message_task:
            return None
        main = task_named(ui, S.PC_STORAGE_TASK)
        if main is not None:
            return v(0.95, False, phase="storage", menu="pc_storage")
        # another phase of the storage system (confirm, screen change, fade): the screen is the
        # same,
        # one B is still the way out (at the confirm it means NO = leave), but it is not provable
        # that the box is taking input, so the claim is weaker and the option the same B
        return v(0.8, False, phase="storage_other", menu="pc_storage")
    if cb in _FIELD_CALLBACKS:
        if task_named(ui, S.PLAYER_PC_ITEM_MENU_TASK) is not None:
            return v(0.95, False, phase="player_pc_item_menu", menu="player_pc_items")
        if task_named(ui, S.PLAYER_PC_MENU_TASK) is not None:
            return v(0.95, False, phase="player_pc_menu", menu="player_pc")
        menu = task_named(ui, S.PC_MENU_TASK)
        if menu is not None:
            state = word(menu, L.PCMENU_D_STATE)
            if state == L.PCMENU_STATE_HANDLE_INPUT:
                return v(
                    0.95,
                    False,
                    phase="pc_menu",
                    menu="pc_menu",
                    selected=word(menu, L.PCMENU_D_SELECTED),
                )
            return v(0.85, True, phase="pc_menu_busy", menu="pc_menu")
    return None
