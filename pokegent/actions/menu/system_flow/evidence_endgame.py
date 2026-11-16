"""Hall of Fame, credits, boot/intro, title screen and main menu from the UI block.

Decomp facts the rules encode (pret/pokefirered, ``source-lock.md``; the b35c sprint doc has the
timings):

* The Hall of Fame screen (``src/hall_of_fame.c``) runs under ``CB2_HofIdle`` with one task chain
  ``Task_Hof_*``. Only ``Task_Hof_ExitOnKeyPressed`` reads a button (A); pressing A earlier is
  ignored.
  ``FLAG_SYS_GAME_CLEAR`` is set at the START of the screen (``EnterHallOfFame``), not at its end.
* The credits (``src/credits.c``) run under ``CB2_Credits``; A is read only at "THE END"
  (``CREDITSSCENE_WAITBUTTON``, 600 frames, then ``SoftReset``). That state lives in a heap
  struct no
  profile symbol reads, so the credits are only ever waited out.
* After the reset: copyright, intro (``CB2_Intro``; A/START/SELECT skip it), title screen
  (``Task_TitleScreenMain``, A/START in scene ``RUN``), main menu (``Task_HandleMenuInput``).
"""

from __future__ import annotations

from pokegent.actions.menu.system_flow import layouts as L
from pokegent.actions.menu.system_flow import refs as R
from pokegent.actions.menu.system_flow import symbols as S
from pokegent.actions.menu.system_flow.evidence import (
    Verdict,
    any_task,
    callback_name,
    confidence_cap,
    task_named,
    waiting_printers,
    word,
)
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.games.gen3.firered.reader.ui_schemas import UiEvidence


def wait_is_progress(snapshot: StateSnapshot) -> bool:
    """Return whether a wait ended while the ending is still running.

    The ending does not mutate durable save state every frame.  Its proof of life is the RAM
    callback/task that owns the ceremony or credits, so those waits must count as progress even
    when the scene fingerprint is unchanged.  A callback outside this set is deliberately false:
    the next perception must decide whether that known scene/title is complete.
    """
    ui = snapshot.ui
    if not snapshot.usable or ui is None or not ui.valid:
        return False
    callback = callback_name(ui)
    if callback in (*S.HOF_INIT_CALLBACKS, S.HOF_IDLE_CALLBACK, S.CREDITS_CALLBACK):
        return True
    return callback == S.HOF_IDLE_CALLBACK and any_task(ui, S.HOF_TASKS) is not None


def classify(snapshot: StateSnapshot, ui: UiEvidence) -> Verdict | None:
    cb = callback_name(ui)
    if cb is None:
        return None
    cap = confidence_cap(ui, "main", "tasks")

    def v(scene: str, conf: float, busy: bool = False, **ev) -> Verdict:
        return Verdict(scene, min(conf, cap), busy, {"callback": cb, **ev})

    if cb in S.HOF_INIT_CALLBACKS:
        return v(R.HALL_OF_FAME, 0.9, True, phase="init")
    if cb == S.HOF_IDLE_CALLBACK:
        return _hall_of_fame(ui, v)
    if cb == S.CREDITS_CALLBACK:
        return v(R.CREDITS, 0.95, False, phase="rolling")
    if cb in S.BOOT_CALLBACKS:
        return v(R.LOGO, 0.9, True, phase="copyright_or_setup")
    if cb == S.INTRO_CALLBACK:
        return v(R.LOGO, 0.9, False, phase="intro")
    if cb in (S.TITLE_INIT_CALLBACK, S.TITLE_CALLBACK):
        return _title(ui, cb, v)
    if cb in (S.MAIN_MENU_INIT_CALLBACK, S.MAIN_MENU_CALLBACK):
        return _main_menu(ui, cb, v)
    if cb == S.NEW_GAME_CALLBACK:
        return _new_game(ui, v)
    if cb in S.NAMING_CALLBACKS:
        # Naming-screen states are not intro movement.  In particular, a full-party caught-mon
        # naming screen owns the "sent to PC" field message in STATE_WAIT_SENT_TO_PC_MESSAGE (7);
        # the dialog family must see that state so it can press A.  Unknown/load/return callbacks
        # are transitions owned by the naming screen, not proof of the real intro callback.
        if cb != S.NAMING_INPUT_CALLBACK:
            return v(R.FIELD_SCRIPTED_MOVEMENT, 0.95, True, phase="naming_transition", callback=cb)
        task = task_named(ui, S.NAMING_TASK)
        state = ui.main.naming_screen_state
        if task is not None and state == 2 and not ui.field.fade_active:
            return v(
                R.NAMING,
                0.97,
                False,
                phase="keyboard",
                task=task.func.name,
                state=state,
                page=ui.main.naming_screen_page,
                cursor=(ui.main.naming_cursor_x, ui.main.naming_cursor_y),
            )
        return v(
            R.FIELD_SCRIPTED_MOVEMENT,
            0.95,
            True,
            phase="naming_transition",
            callback=cb,
            state=state,
        )
    # overworld.c CB2_NewGame initializes the save, runs DoMapLoadLoop, then installs
    # CB2_Overworld.  The save becomes coherent before the player ObjectEvent is spawned.  Keep
    # this exact callback as a recognized zero-input hand-off on a usable snapshot, but do not use
    # a generic busy wait before WarpToPlayersRoom has installed the destination: ClearSav1 zeros
    # mapLayoutId, then WarpIntoMap -> LoadCurrentMapData writes the destination's layout ID. Once
    # that verified field is nonzero, DoMapLoadLoop owns the transition and may be waited normally.
    if snapshot.usable and cb == S.NEW_GAME_MAP_CALLBACK:
        has_destination = bool(snapshot.player is not None and snapshot.player.map_layout_id != 0)
        return v(
            R.SCRIPT_MOVEMENT,
            0.95,
            has_destination,
            phase="new_game_map_load",
            state=ui.main.state,
            destination_installed=has_destination,
        )
    if not snapshot.usable and cb in S.NEW_GAME_TRANSITION_CALLBACKS:
        return v(R.SCRIPT_MOVEMENT, 0.95, True, phase="new_game_map_load")
    return None


def _new_game(ui: UiEvidence, v) -> Verdict:
    """Classify only input points proved by Oak's active task or a waiting printer."""
    task = any_task(ui, S.NEW_GAME_TASKS)
    task_name = task.func.name if task is not None else None
    fading = bool(ui.field.fade_active)
    if task_name == S.NEW_GAME_CONTROLS_TASK and not fading:
        return v(
            R.PROFESSOR_TEXT,
            0.97,
            False,
            phase="controls_guide",
            task=task_name,
            page=ui.main.oak_speech_page,
        )
    if task_name == S.NEW_GAME_PIKACHU_TASK:
        state = ui.main.state
        if state == 1 and not fading:
            return v(
                R.PROFESSOR_TEXT,
                0.97,
                False,
                phase="pikachu_guide",
                task=task_name,
                state=state,
                page=ui.main.oak_speech_page,
            )
        return v(
            R.SCRIPT_MOVEMENT, 0.95, True, phase="pikachu_transition", task=task_name, state=state
        )
    if task_name == S.NEW_GAME_GENDER_TASK:
        return v(R.GENDER, 0.97, False, phase="gender", task=task_name)
    if task_name == S.NEW_GAME_NAME_TASK:
        return v(R.RIVAL_NAME, 0.97, False, phase="preset_choices", task=task_name)
    if task_name == S.NEW_GAME_CONFIRM_TASK:
        return v(
            R.NAMING,
            0.97,
            False,
            phase="confirm",
            task=task_name,
            subject="rival" if ui.main.oak_speech_has_player_name else "player",
        )
    waiting = waiting_printers(ui)
    if waiting:
        return v(
            R.PROFESSOR_TEXT,
            0.97,
            False,
            phase="oak_text",
            task=task_name,
            text_ptrs=tuple(printer.current_char_ptr for printer in waiting),
        )
    return v(R.SCRIPT_MOVEMENT, 0.9, True, phase="oak_transition", task=task_name)


def _hall_of_fame(ui: UiEvidence, v) -> Verdict | None:
    if task_named(ui, S.HOF_EXIT_TASK) is not None:
        return v(R.HALL_OF_FAME, 0.97, False, phase="exit_ready", task=S.HOF_EXIT_TASK)
    leaving = any_task(ui, S.HOF_LEAVING_TASKS)
    if leaving is not None:
        return v(R.HALL_OF_FAME, 0.95, True, phase="leaving", task=leaving.func.label)
    saving = any_task(ui, S.HOF_SAVING_TASKS)
    if saving is not None:
        return v(R.HALL_OF_FAME, 0.95, True, phase="saving", task=saving.func.label)
    # the display chain (mons one by one, confetti, player picture): nothing to press. The task is
    # NOT part of the phase (``detail``): one wait covers the whole chain until the exit task
    # appears, so the runner's repeat guard (same scene, option and fingerprint) never sees the six
    # mons x three tasks as one stuck screen.
    shown = any_task(ui, S.HOF_TASKS)
    if shown is not None:
        return v(R.HALL_OF_FAME, 0.95, False, phase="display", detail=shown.func.label)
    # CB2_HofIdle with a task chain this profile does not name: the screen is the Hall of Fame,
    # the phase is not provable, so nothing is pressed (a bounded wait still ends)
    return v(R.HALL_OF_FAME, 0.8, False, phase="display", detail=None)


def _title(ui: UiEvidence, cb: str, v) -> Verdict | None:
    task = task_named(ui, S.TITLE_TASK)
    if cb == S.TITLE_INIT_CALLBACK or task is None:
        return v(R.TITLE_PRESS_START, 0.85, True, phase="init")
    scene = word(task, L.TITLE_D_SCENE)
    state = word(task, L.TITLE_D_STATE)
    name = L.TITLE_SCENE_NAMES.get(scene, f"scene_{scene}")
    if scene == L.TITLE_SCENE_RUN and state >= 1:
        return v(R.TITLE_PRESS_START, 0.97, False, phase="run", title_scene=name)
    return v(R.TITLE_PRESS_START, 0.9, True, phase=name, title_scene=name)


def main_menu_view(ui: UiEvidence) -> dict[str, object] | None:
    """Type and cursor of the main menu from the task that reads the buttons, else ``None``."""
    task = task_named(ui, S.MAIN_MENU_INPUT_TASK)
    if task is None:
        return None
    menu_type = word(task, L.MENU_D_TYPE)
    if menu_type not in L.MENU_ENTRIES:
        return None
    cursor = word(task, L.MENU_D_CURSOR)
    entries = L.MENU_ENTRIES[menu_type]
    if cursor >= len(entries):
        return None
    return {
        "menu_type": menu_type,
        "menu_type_name": L.MENU_TYPE_NAMES[menu_type],
        "cursor": cursor,
        "entries": entries,
        "has_save": menu_type != L.MENU_NEWGAME_ONLY,
    }


def _main_menu(ui: UiEvidence, cb: str, v) -> Verdict | None:
    view = main_menu_view(ui)
    if view is not None and cb == S.MAIN_MENU_CALLBACK:
        scene = R.TITLE_CONTINUE if view["has_save"] else R.TITLE_MAIN
        # the task reads buttons only while no palette fade runs (main_menu.c Task_HandleMenuInput)
        fading = bool(ui.field.valid and ui.field.fade_active)
        return v(
            scene,
            0.97,
            fading,
            phase="menu",
            menu_type=view["menu_type_name"],
            cursor=view["cursor"],
        )
    # set-up, fade, hand-over to the selected action: nothing to press
    task = any_task(ui, S.MAIN_MENU_TASKS)
    return v(R.TITLE_MAIN, 0.85, True, phase="menu_busy", task=task.func.label if task else None)
