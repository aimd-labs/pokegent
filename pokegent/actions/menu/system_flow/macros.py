"""Executor specs of the system_flow family: waits and single taps.

Every function only *yields steps*; the executor re-validated the screen on a fresh snapshot before
the first step. Rules (each has a test):

* a wait presses nothing and ends when the screen moved on or its chunk of frames is spent, so a
long
  phase (the credits are ~15,000 frames) is many short, verifiable actions;
* A, START and B are atomic taps with a postcondition; the only multi-tap macros are the main menu's
  CONTINUE (cursor read back from the task data), the evolution yes/no (UP/DOWN is idempotent in
  ``MVSTATE_HANDLE_YES_NO``, cursor read back when the battle block is there) and the forgotten-move
  choice (see :func:`pokegent.games.gen3.moves.learn.replace_move`);
* B appears in: ``cancel_evolution`` (only reachable from ``evolution.start``), ``press_b`` (PC,
  summary give-up, foreign menu) and ``skip_quest_log`` (one CONDITIONAL tap, outcome verified). No
  other spec taps B.
"""

from __future__ import annotations

from pokegent.actions._exec.menu_executor import grid_cursor_button, select_menu_entry
from pokegent.actions._exec.models import Advance, ExecutionKind, Tap, WaitUntil
from pokegent.actions._exec.registry import Arg, ExecutorSpec, StepContext
from pokegent.actions.menu.dialog import evidence as dialog_evidence
from pokegent.actions.menu.dialog.macros import (
    _navigate,
    answer_yes_no_from,
    menu_cursor,
    quantity_bounds,
)
from pokegent.actions.menu.system_flow import evidence as E
from pokegent.actions.menu.system_flow import evidence_endgame as EG
from pokegent.actions.menu.system_flow import moves as M
from pokegent.actions.menu.system_flow import refs as R
from pokegent.actions.menu.system_flow import symbols as S
from pokegent.games.gen3.firered.reader.menu_decoders import (
    decode_cursor_menu,
    decode_storage_menu,
)
from pokegent.games.gen3.moves.learn import replace_move

HOLD, RELEASE = 4, 4  # a real press: title, menus and credits poll the keys once per frame
CHECK_EVERY = 4
#: the long phases (the credits) have nothing to catch within a few frames: look less often
LONG_CHECK_EVERY = 16
LONG_WAIT_FRAMES = 2000
SUMMARY_READY_FRAMES = 240
SUMMARY_SETTLE_FRAMES = 600
SUMMARY_INPUT_WAIT = 30
SUMMARY_MOVE_ROWS = 4  # last known slot index; the appended move-to-learn row is index 4
SUMMARY_MAX_INPUTS = SUMMARY_MOVE_ROWS + 2
SUMMARY_MAX_FRAMES = (
    SUMMARY_READY_FRAMES
    + SUMMARY_SETTLE_FRAMES
    + SUMMARY_MAX_INPUTS * (HOLD + SUMMARY_INPUT_WAIT)
    + 900
)
#: after the A of the Hall of Fame the game fades, warps and plays the walk-out scene (input locked,
#: ~2,000 frames) before the credits: the post-step busy wait has to cover all of it
HOF_EXIT_SETTLE = 6000
HOF_EXIT_SETTLE_WALL_S = 300.0


class CursorMismatch(RuntimeError):
    """The cursor is not where the macro needs it. The confirming A is NOT pressed."""


class StorageDestinationFull(RuntimeError):
    """FireRed left the deposit popup open because its selected box became full."""

    failure_reason = "destination box full"


def _tap(button: str) -> Tap:
    return Tap(button, hold_frames=HOLD, wait_frames=RELEASE)


def _press(button: str):
    def fn(ctx: StepContext):
        return _tap(button)

    return fn


def _advance_intro(ctx: StepContext):
    """Press A only at a detected input task and prove it entered a transition."""
    before = E.phase_key(E.classify(ctx.snapshot))
    obs = yield _tap("a")
    after = E.phase_key(E.classify(obs.snapshot))
    if after == before and obs.scene.scene_id != R.SCRIPT_MOVEMENT:
        raise RuntimeError("the introduction input did not enter a detected transition")
    yield WaitUntil(
        lambda current: not current.scene.busy,
        max_frames=2400,
        max_wall_s=180.0,
        check_every=4,
    )


def _pc_exit_key(observation) -> tuple[str, object]:
    verdict = E.classify(observation.snapshot)
    return (observation.scene.scene_id, E.phase_key(verdict))


def _leave_pc(ctx: StepContext):
    """Back out of storage/menu layers until the PC choice box is visible."""
    obs = ctx
    for _ in range(8):
        if obs.scene.scene_id == "dialog.multichoice":
            return
        if (
            obs.scene.scene_id == "dialog.yes_no"
            and obs.snapshot.ui is not None
            and dialog_evidence.pc_storage_yes_no_open(obs.snapshot.ui)
        ):
            # The full-exit option has already committed to leaving storage.  Decline
            # "Continue BOX operations?" through the same RAM-verified cursor adapter as
            # the standalone dialog options, then keep unwinding to the PC choice box.
            yield from answer_yes_no_from(ctx, obs.snapshot, 1)
            obs = yield WaitUntil(
                lambda current: current.scene.scene_id != "dialog.yes_no",
                max_frames=150,
                max_wall_s=8.0,
                check_every=2,
            )
            continue
        if obs.scene.scene_id == "dialog.printing":
            obs = yield WaitUntil(
                lambda current: current.scene.scene_id != "dialog.printing",
                max_frames=900,
                max_wall_s=60.0,
                check_every=4,
            )
            continue
        before = _pc_exit_key(obs)
        obs = yield _tap("b")
        if obs.scene.scene_id == "dialog.multichoice":
            return
        obs = yield WaitUntil(
            lambda current: (
                current.scene.scene_id == "dialog.multichoice" or _pc_exit_key(current) != before
            ),
            max_frames=900,
            max_wall_s=60.0,
            check_every=4,
        )
    raise CursorMismatch("PC did not reach the choice box within 8 B presses")


def _close_menu(ctx: StepContext):
    """Close a foreign menu and any START surface left beneath it."""
    yield Tap("b", hold_frames=HOLD, wait_frames=60)
    yield Tap("b", hold_frames=HOLD, wait_frames=60)


_SUPPORTED_FOREIGN_CALLBACKS = frozenset(
    {
        "CB2_Overworld",
        "CB2_OverworldBasic",
        "CB2_InitPartyMenu",
        "CB2_UpdatePartyMenu",
        "CB2_BagMenuRun",
        "CB2_PokeStorage",
    }
)


def _foreign_screen_back(ctx: StepContext):
    """Unwind a screen not owned by a family, checking RAM after every B."""
    obs = ctx
    for _ in range(4):
        obs = yield _tap("b")
        ui = obs.snapshot.ui
        callback = (
            ui.main.callback2.name
            if ui is not None and ui.valid and ui.main.callback2 is not None
            else None
        )
        if callback in _SUPPORTED_FOREIGN_CALLBACKS:
            return obs
        previous = callback
        obs = yield WaitUntil(
            lambda current: (
                current.snapshot.ui is not None
                and current.snapshot.ui.valid
                and current.snapshot.ui.main.callback2 is not None
                and (
                    current.snapshot.ui.main.callback2.name in _SUPPORTED_FOREIGN_CALLBACKS
                    or current.snapshot.ui.main.callback2.name != previous
                )
            ),
            max_frames=900,
            max_wall_s=60.0,
            check_every=4,
        )
        ui = obs.snapshot.ui
        if (
            ui is not None
            and ui.valid
            and ui.main.callback2 is not None
            and ui.main.callback2.name in _SUPPORTED_FOREIGN_CALLBACKS
        ):
            return obs
    raise CursorMismatch("foreign screen did not reach a supported callback within 4 B presses")


def _wait_for(foreign: frozenset[str]):
    def fn(ctx: StepContext):
        chunk = int(ctx.args["chunk"])
        every = LONG_CHECK_EVERY if chunk >= LONG_WAIT_FRAMES else CHECK_EVERY
        start = E.phase_key(E.classify(ctx.snapshot, foreign))
        checks = 0

        def done(obs) -> bool:
            nonlocal checks
            checks += 1
            moved = E.phase_key(E.classify(obs.snapshot, foreign)) != start
            return moved or checks * every >= chunk

        yield WaitUntil(
            done,
            max_frames=chunk + 4 * every,
            max_wall_s=max(30.0, chunk / 20.0),
            check_every=every,
        )

    return fn


def _continue_game(ctx: StepContext):
    view = EG.main_menu_view(ctx.snapshot.ui)
    if view is None:
        raise CursorMismatch("the main menu cursor is not readable; nothing pressed")
    cursor = int(view["cursor"])  # type: ignore[arg-type]
    while cursor > 0:
        obs = yield _tap("up")
        seen = EG.main_menu_view(obs.snapshot.ui)
        if seen is None or int(seen["cursor"]) != cursor - 1:  # type: ignore[arg-type]
            raise CursorMismatch("the main menu cursor did not move up by one; A not pressed")
        cursor -= 1
    yield _tap("a")


def _new_game(ctx: StepContext):
    view = EG.main_menu_view(ctx.snapshot.ui)
    if view is None:
        raise CursorMismatch("the main menu cursor is not readable; nothing pressed")
    cursor = int(view["cursor"])  # type: ignore[arg-type]
    # A cold boot starts on NEW GAME; with an existing save, move down from CONTINUE and verify the
    # cursor before confirming. The explicit mode must never silently continue an old save.
    if len(view["entries"]) > 1 and cursor != 1:  # type: ignore[arg-type]
        obs = yield _tap("down")
        seen = EG.main_menu_view(obs.snapshot.ui)
        if seen is None or int(seen["cursor"]) != 1:  # type: ignore[arg-type]
            raise CursorMismatch("the new-game cursor did not move to NEW GAME; A not pressed")
    yield _tap("a")


def _select_name_preset(ctx: StepContext):
    want = int(ctx.args["index"])
    if want != 0:
        raise CursorMismatch("only the first preset name is supported")
    # Oak's list starts on NEW NAME (cursor 0); the first preset is cursor 1.
    obs = yield _tap("down")
    menu = obs.snapshot.ui.menu.menu if obs.snapshot.ui is not None else None
    if menu is None or not menu.consistent or menu.cursor_pos != 1:
        raise CursorMismatch("the first preset cursor was not read back at row 1; A not pressed")
    yield _tap("a")


def _accept_name_preset(ctx: StepContext):
    """Move the naming keyboard cursor to OK, then accept its prefilled game preset."""
    yield _tap("start")
    yield WaitUntil(
        lambda current: bool(
            current.scene.scene_id == R.NAMING
            and current.snapshot.ui is not None
            and current.snapshot.ui.main.naming_cursor_x
            == (6 if current.snapshot.ui.main.naming_screen_page == 0 else 8)
            and current.snapshot.ui.main.naming_cursor_y == 2
        ),
        max_frames=300,
        max_wall_s=30.0,
        check_every=2,
    )
    yield _tap("a")
    yield WaitUntil(
        lambda current: current.scene.scene_id != R.NAMING,
        max_frames=900,
        max_wall_s=60.0,
        check_every=4,
    )


def _menu_answer(ctx: StepContext):
    want = int(ctx.args["entry"])
    obs = yield _tap("up" if want == 0 else "down")
    menu = obs.snapshot.ui.menu.menu if obs.snapshot.ui is not None else None
    if menu is None or not menu.consistent or menu.cursor_pos != want:
        raise CursorMismatch(f"menu cursor is not the requested row {want}; A not pressed")
    yield _tap("a")


def _evolution_answer(ctx: StepContext):
    want = int(ctx.args["entry"])  # 0 = YES (cursor on YES), 1 = NO
    obs = yield _tap("up" if want == 0 else "down")
    # gBattleCommunication[1] is the cursor of this yes/no (evolution_scene.c sEvoCursorPos); the
    # battle block carries it when the scene sits behind a battle. Without the block the cursor is
    # not readable: UP/DOWN land on YES/NO whatever the start, so the answer is still determined.
    block = M.battle_block(obs.snapshot)
    if block is not None and len(block.comm) > 1 and int(block.comm[1]) != want:
        raise CursorMismatch(f"yes/no cursor is {block.comm[1]}, wanted {want}; A not pressed")
    yield _tap("a")


def _forget_move(ctx: StepContext):
    """Compatibility entry point for the registered system-flow executor."""
    yield from replace_move(ctx)


def _storage_locations(storage, pid: int) -> list[tuple[int, int]]:
    return [
        (box, slot)
        for box, mons in enumerate(storage.boxes)
        for slot, mon in enumerate(mons)
        if mon is not None and int(mon.personality) == pid
    ]


def _pc_storage_error_dialog(snapshot) -> bool:
    storage = snapshot.storage
    return storage is not None and int(storage.ui_state) == 2


def _cursor_identity(snapshot, operation: int) -> int | None:
    storage = snapshot.storage
    if storage is None or not storage.cursor_settled or storage.cursor_position is None:
        return None
    if operation in (0, 2) and storage.cursor_area == 0:
        mon = storage.get(storage.current_box, storage.cursor_position)
    elif operation == 1 and storage.cursor_area == 1:
        mon = next(
            (member for member in snapshot.party if member.slot == storage.cursor_position), None
        )
    else:
        return None
    return int(mon.personality) if mon is not None else None


def _ready_storage_cursor(snapshot, operation: int):
    """Decode a PC cursor only while pret's input-owning task can call HandleInput."""
    if _ready_move_cursor_state(snapshot, operation) is None:
        return None
    return decode_storage_menu(snapshot, operation=operation)


def _ready_move_cursor_state(snapshot, operation: int):
    """Return settled input RAM even on title/buttons, which have no decoded slot menu."""
    storage = snapshot.storage
    verdict = E.classify(snapshot)
    if (
        storage is None
        or storage.ui_state != 0
        or storage.box_option != operation
        or not storage.cursor_settled
        or storage.cursor_position is None
        or verdict is None
        or verdict.evidence.get("phase") not in {"storage", "storage_other"}
    ):
        return None
    return storage


def _is_holding_mon(storage) -> bool | None:
    """Return only pret's carried flag; stale movingMon bytes are never a fallback."""
    held = getattr(storage, "is_holding_mon", None)
    return held if isinstance(held, bool) else None


def _move_target(snapshot, kind: str, box: int, slot: int):
    storage = snapshot.storage
    if storage is None:
        return None
    if kind == "box":
        return storage.get(box, slot)
    return next((member for member in snapshot.party if member.slot == slot), None)


def _move_identity_count(snapshot, pid: int) -> int:
    storage = snapshot.storage
    boxed = (
        sum(int(mon.personality) == pid for box in storage.boxes for mon in box if mon is not None)
        if storage is not None
        else 0
    )
    return boxed + sum(int(mon.personality) == pid for mon in snapshot.party)


def _held_origin_is(storage, kind: str, box: int, slot: int) -> bool:
    return bool(
        storage.held_origin_kind == kind
        and storage.held_origin_slot == slot
        and (kind == "party" or storage.held_origin_box == box)
    )


def _move_menu_action(snapshot) -> str | None:
    """Return the first selected-mon action only at its RAM-proven input boundary.

    ``Task_OnSelectedMon`` state 2 calls ``HandleMenuInput``. ``AddMenu`` initializes row 0,
    and ``SetMenuTextsForMon`` puts MOVE, PLACE, or SHIFT there according to the carried flag and
    the fresh occupant under the settled cursor.
    """
    storage = snapshot.storage
    ui = snapshot.ui
    menu = ui.menu.menu if ui is not None and ui.valid else None
    if (
        storage is None
        or storage.box_option != 2
        or storage.ui_state != 2
        or not storage.cursor_settled
        or ui is None
        or E.task_named(ui, S.PC_STORAGE_SELECTED_MON_TASK) is None
        or menu is None
        or not menu.window_in_use
        or not menu.consistent
        or menu.cursor_pos != 0
    ):
        return None
    if storage.cursor_area == 0:
        occupant = storage.get(storage.current_box, storage.cursor_position)
    elif storage.cursor_area == 1:
        occupant = next(
            (member for member in snapshot.party if member.slot == storage.cursor_position), None
        )
    else:
        return None
    holding = _is_holding_mon(storage)
    if holding is None:
        return None
    if holding:
        return "shift" if occupant is not None else "place"
    return "move" if occupant is not None else None


def _move_to_box(obs, target_box: int):
    """Navigate MOVE mode to a box, proving every press from the resulting RAM frame."""
    storage = obs.snapshot.storage
    if storage is None:
        raise CursorMismatch("MOVE box navigation requires readable storage")
    if int(storage.cursor_area) == 1:
        current_box = int(storage.current_box)
        yield Tap("b", hold_frames=HOLD, wait_frames=12)
        obs = yield WaitUntil(
            lambda current: bool(
                _ready_move_cursor_state(current.snapshot, 2) is not None
                and int(current.snapshot.storage.current_box) == current_box
                and int(current.snapshot.storage.cursor_area) == 0
            ),
            max_frames=120,
            max_wall_s=8.0,
            check_every=2,
            failure_reason="MOVE party panel did not close to the RAM-verified box grid",
        )
    while (
        int(getattr(obs.snapshot.storage, "options_button_mode", 0)) != 1
        and int(obs.snapshot.storage.cursor_area) != 2
    ):
        old_cursor = (
            int(obs.snapshot.storage.cursor_area),
            int(obs.snapshot.storage.cursor_position),
        )
        yield Tap("up", hold_frames=HOLD, wait_frames=12)
        obs = yield WaitUntil(
            lambda current: bool(
                _ready_move_cursor_state(current.snapshot, 2) is not None
                and (
                    int(current.snapshot.storage.cursor_area),
                    int(current.snapshot.storage.cursor_position),
                )
                != old_cursor
            ),
            max_frames=120,
            max_wall_s=8.0,
            check_every=2,
            failure_reason="MOVE cursor did not reach the next RAM-verified title step",
        )
    while int(obs.snapshot.storage.current_box) != target_box:
        current_box = int(obs.snapshot.storage.current_box)
        box_count = len(obs.snapshot.storage.boxes)
        button_mode = int(getattr(obs.snapshot.storage, "options_button_mode", 0))
        right = (target_box - current_box) % box_count <= (current_box - target_box) % box_count
        button = ("r" if right else "l") if button_mode == 1 else ("right" if right else "left")
        yield Tap(button, hold_frames=HOLD, wait_frames=12)
        obs = yield WaitUntil(
            lambda current: bool(
                _ready_move_cursor_state(current.snapshot, 2) is not None
                and int(current.snapshot.storage.current_box) != current_box
                and (button_mode == 1 or int(current.snapshot.storage.cursor_area) == 2)
            ),
            max_frames=120,
            max_wall_s=8.0,
            check_every=2,
            failure_reason="MOVE box switch did not reach a RAM-verified input boundary",
        )
    if int(obs.snapshot.storage.cursor_area) == 2:
        yield Tap("down", hold_frames=HOLD, wait_frames=12)
        obs = yield WaitUntil(
            lambda current: bool(
                _ready_move_cursor_state(current.snapshot, 2) is not None
                and int(current.snapshot.storage.current_box) == target_box
                and int(current.snapshot.storage.cursor_area) == 0
            ),
            max_frames=120,
            max_wall_s=8.0,
            check_every=2,
            failure_reason="MOVE cursor did not return to the RAM-verified box grid",
        )
    return obs


def _move_to_party(obs):
    """Open the party panel from the box grid, verifying each cursor transition in RAM."""
    storage = obs.snapshot.storage
    if storage is None:
        raise CursorMismatch("MOVE party navigation requires readable storage")
    if int(storage.cursor_area) == 1:
        return obs
    if int(storage.cursor_area) != 0:
        raise CursorMismatch("MOVE party navigation must start on the box grid")
    while int(obs.snapshot.storage.cursor_area) == 0:
        old_cursor = (
            int(obs.snapshot.storage.cursor_area),
            int(obs.snapshot.storage.cursor_position),
        )
        yield Tap("down", hold_frames=HOLD, wait_frames=12)
        obs = yield WaitUntil(
            lambda current: bool(
                _ready_move_cursor_state(current.snapshot, 2) is not None
                and (
                    int(current.snapshot.storage.cursor_area),
                    int(current.snapshot.storage.cursor_position),
                )
                != old_cursor
            ),
            max_frames=120,
            max_wall_s=8.0,
            check_every=2,
            failure_reason="MOVE cursor did not reach the next RAM-verified party-button step",
        )
    if int(obs.snapshot.storage.cursor_area) != 3 or int(
        obs.snapshot.storage.cursor_position
    ) not in (0, 1):
        raise CursorMismatch("MOVE cursor did not reach the storage buttons")
    if int(obs.snapshot.storage.cursor_position) == 1:
        yield Tap("left", hold_frames=HOLD, wait_frames=12)
        obs = yield WaitUntil(
            lambda current: bool(
                _ready_move_cursor_state(current.snapshot, 2) is not None
                and int(current.snapshot.storage.cursor_area) == 3
                and int(current.snapshot.storage.cursor_position) == 0
            ),
            max_frames=120,
            max_wall_s=8.0,
            check_every=2,
            failure_reason="MOVE cursor did not select the RAM-verified party button",
        )
    yield Tap("a", hold_frames=HOLD, wait_frames=12)
    return (
        yield WaitUntil(
            lambda current: bool(
                _ready_storage_cursor(current.snapshot, 2) is not None
                and int(current.snapshot.storage.cursor_area) == 1
            ),
            max_frames=120,
            max_wall_s=8.0,
            check_every=2,
            failure_reason="MOVE party panel did not reach a RAM-verified input boundary",
        )
    )


def _pc_move(ctx: StepContext):
    """Select a RAM-identified Pokémon in MOVE mode and hand the next prompt to the decider."""
    target_pid = int(ctx.args["pid"])
    obs = yield WaitUntil(
        lambda current: _ready_storage_cursor(current.snapshot, 2) is not None,
        max_frames=120,
        max_wall_s=8.0,
        check_every=2,
        failure_reason="move source cursor not input-ready",
    )
    requested_box = ctx.args.get("box")
    if requested_box is not None and int(requested_box) != int(obs.snapshot.storage.current_box):
        obs = yield from _move_to_box(obs, int(requested_box))
    obs = yield from select_menu_entry(
        obs,
        f"pokemon:{target_pid}",
        lambda snapshot: _ready_storage_cursor(snapshot, 2),
        tap=lambda button: Tap(button, hold_frames=HOLD, wait_frames=12),
        max_moves=128,
        readback_retries=2,
        next_button=lambda current, target: grid_cursor_button(current, target, columns=6),
    )
    obs = yield WaitUntil(
        lambda current: _move_menu_action(current.snapshot) == "move",
        max_frames=30,
        max_wall_s=3.0,
        check_every=2,
        failure_reason="move context menu did not become input-ready on MOVE",
    )
    yield Tap("a", hold_frames=HOLD, wait_frames=RELEASE)
    source_box = int(
        requested_box if requested_box is not None else obs.snapshot.storage.current_box
    )
    source_slot = int(ctx.args.get("slot", obs.snapshot.storage.cursor_position))
    yield WaitUntil(
        lambda current: bool(
            current.snapshot.storage is not None
            and current.snapshot.storage.box_option == 2
            and current.snapshot.storage.get(source_box, source_slot) is None
            and _is_holding_mon(current.snapshot.storage) is True
            and current.snapshot.storage.held_mon is not None
            and int(current.snapshot.storage.held_mon.personality) == target_pid
            and current.snapshot.storage.held_origin_kind == "box"
            and current.snapshot.storage.held_origin_box == source_box
            and current.snapshot.storage.held_origin_slot == source_slot
        ),
        max_frames=120,
        max_wall_s=8.0,
        check_every=2,
        failure_reason="move pick RAM postcondition not observed",
    )
    yield WaitUntil(
        lambda current: _ready_storage_cursor(current.snapshot, 2) is not None,
        max_frames=120,
        max_wall_s=8.0,
        check_every=2,
        failure_reason="move cursor did not become ready after pick",
    )


def _pc_move_place(ctx: StepContext):
    before = ctx.snapshot.storage
    if before is None or before.box_option != 2 or not before.cursor_settled:
        raise CursorMismatch("MOVE place requires a settled RAM cursor")
    held = before.held_mon
    if held is None or _is_holding_mon(before) is not True:
        raise CursorMismatch("MOVE place requires the RAM held-mon state")
    source_kind = before.held_origin_kind
    source_box = before.held_origin_box
    source_slot = before.held_origin_slot
    if source_kind not in {"box", "party"} or source_slot is None:
        raise CursorMismatch("MOVE place requires the RAM held-mon origin")
    if source_kind == "box" and source_box is None:
        raise CursorMismatch("MOVE place requires the RAM held-mon box origin")
    target_kind = str(ctx.args.get("target_kind", "box"))
    target_box = int(ctx.args.get("target_box", before.current_box))
    target_slot = int(ctx.args.get("target_slot", before.cursor_position))
    obs = ctx
    if target_kind == "box":
        obs = yield from _move_to_box(obs, target_box)
        obs = yield from select_menu_entry(
            obs,
            f"empty:box:{target_box}:{target_slot}"
            if before.get(target_box, target_slot) is None
            else f"pokemon:{int(before.get(target_box, target_slot).personality)}",
            lambda snapshot: _ready_storage_cursor(snapshot, 2),
            tap=lambda button: Tap(button, hold_frames=HOLD, wait_frames=12),
            max_moves=128,
            readback_retries=2,
            next_button=lambda current, target: grid_cursor_button(current, target, columns=6),
        )
    else:
        obs = yield from _move_to_party(obs)
        obs = yield from select_menu_entry(
            obs,
            f"party:{target_slot}",
            lambda snapshot: _ready_storage_cursor(snapshot, 2),
            tap=lambda button: Tap(button, hold_frames=HOLD, wait_frames=12),
            max_moves=16,
            readback_retries=2,
        )
    obs = yield WaitUntil(
        lambda current: _move_menu_action(current.snapshot) in {"place", "shift"},
        max_frames=30,
        max_wall_s=3.0,
        check_every=2,
        failure_reason="move context menu did not become input-ready on PLACE or SHIFT",
    )
    menu_action = _move_menu_action(obs.snapshot)
    destination_was_empty = menu_action == "place"
    displaced = _move_target(obs.snapshot, target_kind, target_box, target_slot)
    displaced_pid = None if displaced is None else int(displaced.personality)
    yield Tap("a", hold_frames=HOLD, wait_frames=RELEASE)

    def move_complete(snapshot) -> bool:
        storage = snapshot.storage
        target = _move_target(snapshot, target_kind, target_box, target_slot)
        held_pid = int(held.personality)
        if (
            storage is None
            or storage.box_option != 2
            or target is None
            or int(target.personality) != held_pid
            or _move_identity_count(snapshot, held_pid) != 1
        ):
            return False
        if destination_was_empty:
            return _is_holding_mon(storage) is False
        return bool(
            displaced_pid is not None
            and _is_holding_mon(storage) is True
            and storage.held_mon is not None
            and int(storage.held_mon.personality) == displaced_pid
            and _held_origin_is(storage, target_kind, target_box, target_slot)
            and _move_identity_count(snapshot, displaced_pid) == 0
        )

    yield WaitUntil(
        lambda current: move_complete(current.snapshot),
        max_frames=120,
        max_wall_s=8.0,
        check_every=2,
        failure_reason="move place RAM postcondition not observed",
    )
    yield WaitUntil(
        lambda current: _ready_storage_cursor(current.snapshot, 2) is not None,
        max_frames=120,
        max_wall_s=8.0,
        check_every=2,
        failure_reason="move cursor did not become ready after place",
    )


def _pc_move_cancel(ctx: StepContext):
    before = ctx.snapshot.storage
    if before is None or before.box_option != 2 or _is_holding_mon(before) is not False:
        raise CursorMismatch("MOVE cancel requires MOVE mode with no held Pokémon")
    yield Tap("b", hold_frames=HOLD, wait_frames=RELEASE)
    yield WaitUntil(
        lambda current: bool(
            current.snapshot.storage is not None
            and _is_holding_mon(current.snapshot.storage) is False
            and current.snapshot.ui is not None
            and dialog_evidence.pc_storage_yes_no_open(current.snapshot.ui)
        ),
        max_frames=120,
        max_wall_s=8.0,
        check_every=2,
        failure_reason="MOVE cancel did not open the Continue BOX prompt",
    )


def _pc_storage_back(ctx: StepContext):
    """Back out of storage, answering Continue BOX operations? with NO."""
    yield _tap("b")
    obs = yield WaitUntil(
        lambda current: (
            current.snapshot.ui is not None
            and dialog_evidence.pc_storage_yes_no_open(current.snapshot.ui)
        ),
        max_frames=120,
        max_wall_s=8.0,
        check_every=2,
    )
    if obs.snapshot.ui is None or not dialog_evidence.pc_storage_yes_no_open(obs.snapshot.ui):
        raise CursorMismatch("Continue BOX operations prompt is not owned by PC storage")
    yield from answer_yes_no_from(ctx, obs.snapshot, 1)
    yield WaitUntil(
        lambda current: (
            E.classify(current.snapshot) is not None
            and E.classify(current.snapshot).evidence.get("menu") == "pc_menu"
        ),
        max_frames=1200,
        max_wall_s=60.0,
        check_every=4,
    )


def _pc_storage_error(ctx: StepContext):
    """Acknowledge FireRed's storage-local error and return to storage input."""
    # The message's down-arrow escape is B; unlike A it reliably clears the storage
    # message and restores the box cursor on the live FireRed build.
    yield _tap("b")
    yield WaitUntil(
        lambda current: (
            current.snapshot.storage is not None
            and current.snapshot.storage.ui_state == 0
            and E.classify(current.snapshot) is not None
            and E.classify(current.snapshot).evidence.get("phase") in {"storage", "storage_other"}
        ),
        max_frames=240,
        max_wall_s=16.0,
        check_every=2,
    )


def _pc_storage_failure_cleanup(actor, perceiver, ctx: StepContext) -> None:
    """Cancel a refused deposit popup so fresh RAM can select another destination."""
    if int(ctx.args.get("operation", -1)) != 1 or ctx.args.get("box") is None:
        return
    target_pid = int(ctx.args["pid"])
    target_box = int(ctx.args["box"])
    obs = perceiver.observe()
    storage = obs.snapshot.storage
    if not (
        storage is not None
        and storage.deposit_box_cursor is not None
        and all(mon is not None for mon in storage.boxes[target_box])
        and any(int(mon.personality) == target_pid for mon in obs.snapshot.party)
        and not _storage_locations(storage, target_pid)
    ):
        return
    # The first B dismisses FireRed's refusal notice (ui_state 4) back to the still-open
    # destination popup (ui_state 1). A second, separately RAM-gated B cancels that popup.
    actor.tap("b", hold_frames=HOLD, wait_frames=RELEASE)
    for _ in range(60):
        obs = perceiver.observe()
        if (
            obs.snapshot.storage is not None
            and obs.snapshot.storage.deposit_box_cursor is None
            and _ready_storage_cursor(obs.snapshot, 1) is not None
        ):
            return
        if (
            obs.snapshot.storage is not None
            and obs.snapshot.storage.deposit_box_cursor is not None
            and obs.snapshot.storage.ui_state != 4
        ):
            actor.tap("b", hold_frames=HOLD, wait_frames=RELEASE)
            break
        actor.advance_frames(2)
    for _ in range(60):
        obs = perceiver.observe()
        if (
            obs.snapshot.storage is not None
            and obs.snapshot.storage.deposit_box_cursor is None
            and _ready_storage_cursor(obs.snapshot, 1) is not None
        ):
            return
        actor.advance_frames(2)
    raise RuntimeError("deposit popup did not close after destination-box refusal")


def _deposit_refusal_signature(snapshot, target_pid: int, requested_box: int | None):
    """Return the live popup signature for FireRed refusing a newly-full destination."""
    storage = snapshot.storage
    popup_box = storage.deposit_box_cursor if storage is not None else None
    if not (
        storage is not None
        and popup_box is not None
        and (requested_box is None or popup_box == requested_box)
        and all(mon is not None for mon in storage.boxes[popup_box])
        and any(int(mon.personality) == target_pid for mon in snapshot.party)
        and not _storage_locations(storage, target_pid)
    ):
        return None
    return {
        "deposit_box_cursor": popup_box,
        "ui_state": int(storage.ui_state),
        "popup_open": True,
    }


def _pc_storage(ctx: StepContext):
    operation = int(ctx.args["operation"])
    target_pid = int(ctx.args["pid"])
    target_key = f"pokemon:{target_pid}"
    obs = ctx
    obs = yield WaitUntil(
        lambda current: bool(
            (
                (verdict := E.classify(current.snapshot)) is not None
                and verdict.evidence.get("phase") in {"storage", "storage_other", "storage_error"}
                and _ready_storage_cursor(current.snapshot, operation) is not None
            )
            or (operation == 1 and _pc_storage_error_dialog(current.snapshot))
        ),
        max_frames=900,
        max_wall_s=60.0,
    )
    if (
        obs.snapshot.storage is not None
        and operation == 1
        and _pc_storage_error_dialog(obs.snapshot)
    ):
        return
    before_storage = obs.snapshot.storage
    if before_storage is None:
        raise CursorMismatch("PC storage contents are unreadable")
    requested_box = ctx.args.get("box")
    if (
        operation != 1
        and requested_box is not None
        and int(requested_box) != int(before_storage.current_box)
    ):
        target_box = int(requested_box)
        box_count = len(before_storage.boxes)
        button_mode = int(getattr(before_storage, "options_button_mode", 0))
        if button_mode != 1 and int(before_storage.cursor_area) != 2:
            for _ in range(40):
                if int(obs.snapshot.storage.cursor_area) == 2:
                    break
                old_cursor = (
                    int(obs.snapshot.storage.cursor_area),
                    int(obs.snapshot.storage.cursor_position),
                )
                obs = yield Tap("up", hold_frames=HOLD, wait_frames=12)
                obs = yield WaitUntil(
                    lambda current: (
                        current.snapshot.storage is not None
                        and (
                            int(current.snapshot.storage.cursor_area),
                            int(current.snapshot.storage.cursor_position),
                        )
                        != old_cursor
                    ),
                    max_frames=600,
                    max_wall_s=30.0,
                    check_every=2,
                )
            if int(obs.snapshot.storage.cursor_area) != 2:
                raise CursorMismatch("PC cursor did not reach the box-title area")
        for _ in range(box_count):
            current_box = int(obs.snapshot.storage.current_box)
            if current_box == target_box:
                break
            distance_right = (target_box - current_box) % box_count
            distance_left = (current_box - target_box) % box_count
            right = distance_right <= distance_left
            button = ("r" if right else "l") if button_mode == 1 else ("right" if right else "left")
            cursor_area = int(obs.snapshot.storage.cursor_area)
            obs = yield Tap(button, hold_frames=HOLD, wait_frames=12)
            obs = yield WaitUntil(
                lambda current: (
                    current.snapshot.storage is not None
                    and int(current.snapshot.storage.current_box) != current_box
                    and int(current.snapshot.storage.cursor_area) == cursor_area
                ),
                max_frames=600,
                max_wall_s=30.0,
                check_every=2,
            )
        if int(obs.snapshot.storage.current_box) != target_box:
            raise CursorMismatch(f"PC box did not reach target {target_box}")
        if int(obs.snapshot.storage.cursor_area) == 2:
            obs = yield Tap("down", hold_frames=HOLD, wait_frames=12)
            obs = yield WaitUntil(
                lambda current: (
                    current.snapshot.storage is not None
                    and int(current.snapshot.storage.cursor_area) == 0
                ),
                max_frames=600,
                max_wall_s=30.0,
                check_every=2,
            )
        obs = yield WaitUntil(
            lambda current: (
                current.snapshot.storage is not None
                and int(current.snapshot.storage.current_box) == target_box
            ),
            max_frames=600,
            max_wall_s=30.0,
            check_every=4,
        )
        before_storage = obs.snapshot.storage
    before_party_count = sum(mon is not None for mon in obs.snapshot.party)
    source_locations = _storage_locations(before_storage, target_pid)
    source = None
    if operation == 0:
        active = [
            location for location in source_locations if location[0] == before_storage.current_box
        ]
        if len(active) != 1:
            raise CursorMismatch(
                f"withdraw identity {target_pid} is not unique in active box: {active}"
            )
        source = active[0]
    obs = yield from select_menu_entry(
        obs,
        target_key,
        lambda snapshot: _ready_storage_cursor(snapshot, operation),
        tap=lambda button: Tap(button, hold_frames=HOLD, wait_frames=12),
        max_moves=128,
        readback_retries=2,
        next_button=(
            (lambda current, target: grid_cursor_button(current, target, columns=6))
            if operation == 0
            else (
                lambda current, target: (
                    "down" if target > current else "up" if target < current else None
                )
            )
        ),
    )
    # FireRed inserts the operation (WITHDRAW or STORE) as the first action for the selected
    # identity. Validate the stable storage cursor before confirming it; pressing A on an
    # unreadable or unexpectedly positioned action menu is unsafe.
    obs = yield Advance(frames=30)
    action = "WITHDRAW" if operation == 0 else "STORE"
    # The storage reader's cursor identity is the stable precondition for the modal. The modal's
    # private task is not always present in the generic UI evidence, so do not synthesize a menu
    # cursor from stale sMenu bytes.
    selected = obs.snapshot.storage
    if (
        selected is None
        or selected.box_option != operation
        or _cursor_identity(obs.snapshot, operation) != target_pid
    ):
        raise CursorMismatch(f"PC {action} action modal precondition is unreadable")
    obs = yield Tap("a", hold_frames=HOLD, wait_frames=RELEASE)
    # A generic confirmation is a new decision frame.  Return control to the decider instead of
    # answering it inside this PC macro; this is especially important for "Continue BOX
    # operations?", whose NO answer is the verified escape from the storage phase.
    obs = yield Advance(frames=30)
    if _generic_yes_no(obs.snapshot):
        return
    # The first confirmation must have left a readable storage result screen; otherwise the
    # second A is never sent.
    after_storage = obs.snapshot.storage
    if (
        after_storage is None
        or after_storage.box_option != operation
        or after_storage.cursor_area != (0 if operation == 0 else 1)
    ):
        raise CursorMismatch(f"PC {action} result screen is unreadable before second confirmation")
    obs = yield Tap("a", hold_frames=HOLD, wait_frames=RELEASE)
    if operation == 1:
        # The choose-box popup uses a private task in this ROM profile, so its function pointer is
        # intentionally not a scene claim. Give it a bounded redraw window before navigating it.
        obs = yield Advance(frames=60)
        storage = obs.snapshot.storage
        if storage is None or storage.deposit_box_cursor is None:
            raise RuntimeError("deposit box popup cursor is unreadable; destination not confirmed")
        seen_boxes: list[int] = []
        box_count = len(storage.boxes)
        if box_count <= 0:
            raise RuntimeError("PC box list is unreadable; destination not confirmed")
        for _ in range(box_count):
            current_box = storage.deposit_box_cursor
            seen_boxes.append(current_box)
            if requested_box is not None and current_box == int(requested_box):
                break
            if requested_box is None and any(mon is None for mon in storage.boxes[current_box]):
                break
            target = int(requested_box) if requested_box is not None else current_box
            right = (target - current_box) % box_count
            left = (current_box - target) % box_count
            button = "right" if right <= left else "left"
            obs = yield Tap(button, hold_frames=HOLD, wait_frames=12)
            storage = obs.snapshot.storage
            if storage is None or storage.deposit_box_cursor is None:
                raise RuntimeError(
                    "deposit box popup cursor became unreadable; destination not confirmed"
                )
        else:
            raise RuntimeError(f"deposit box popup found no box with space; read {seen_boxes}")
        obs = yield Tap("a", hold_frames=HOLD, wait_frames=12)

        def deposited(snapshot) -> bool:
            locations = (
                _storage_locations(snapshot.storage, target_pid)
                if snapshot.storage is not None
                else []
            )
            return bool(
                snapshot.usable
                and snapshot.storage is not None
                and not any(int(mon.personality) == target_pid for mon in snapshot.party)
                and sum(mon is not None for mon in snapshot.party) == before_party_count - 1
                and len(locations) == 1
                and (requested_box is None or locations[0][0] == int(requested_box))
            )

        target_box = int(requested_box) if requested_box is not None else None

        def destination_refused(snapshot) -> bool:
            return _deposit_refusal_signature(snapshot, target_pid, target_box) is not None

        # A refusal is visible immediately as the still-open destination popup. Poll that exact
        # RAM signature briefly without turning an ordinary in-flight deposit into an error.
        for _ in range(15):
            if destination_refused(obs.snapshot):
                raise StorageDestinationFull("destination box full")
            obs = yield Advance(frames=2)
        if destination_refused(obs.snapshot):
            raise StorageDestinationFull("destination box full")

        # The box and party writes can take substantially longer than the refusal signal. Preserve
        # the original success budget and give its timeout a mutation-specific reason.
        obs = yield WaitUntil(
            lambda current: deposited(current.snapshot) or destination_refused(current.snapshot),
            max_frames=900,
            max_wall_s=60.0,
            check_every=4,
            failure_reason="deposit mutation not observed",
        )
        if destination_refused(obs.snapshot):
            raise StorageDestinationFull("destination box full")

        locations = _storage_locations(obs.snapshot.storage, target_pid)
        completed_box = locations[0][0]
        if all(mon is not None for mon in obs.snapshot.storage.boxes[completed_box]):
            # The 30th deposit mutation can precede its informational notice. Keep ownership until
            # either that notice arrives or the storage cursor is ready again.
            if not (
                obs.snapshot.storage.ui_state == 2
                or _ready_storage_cursor(obs.snapshot, operation) is not None
            ):
                obs = yield WaitUntil(
                    lambda current: bool(
                        current.snapshot.storage is not None
                        and (
                            current.snapshot.storage.ui_state == 2
                            or _ready_storage_cursor(current.snapshot, operation) is not None
                        )
                    ),
                    max_frames=240,
                    max_wall_s=16.0,
                    check_every=2,
                    failure_reason="full-box deposit notice did not settle",
                )
        if obs.snapshot.storage.ui_state == 2:
            yield Tap("b", hold_frames=HOLD, wait_frames=RELEASE)
            yield WaitUntil(
                lambda current: _ready_storage_cursor(current.snapshot, operation) is not None,
                max_frames=240,
                max_wall_s=16.0,
                check_every=2,
            )
    else:
        # The box write and party-slot write are separate updates. Wait for both fresh RAM facts;
        # the next frame after the box change can still expose the old party.
        assert source is not None
        source_box, source_slot = source
        yield WaitUntil(
            lambda current: bool(
                current.snapshot.usable
                and current.snapshot.storage is not None
                and current.snapshot.storage.box_option == 0
                and current.snapshot.storage.get(source_box, source_slot) is None
                and not _storage_locations(current.snapshot.storage, target_pid)
                and any(int(mon.personality) == target_pid for mon in current.snapshot.party)
                and sum(mon is not None for mon in current.snapshot.party) == before_party_count + 1
                and current.snapshot.storage.cursor_area == 0
                and current.snapshot.storage.cursor_position == source_slot
                and current.snapshot.storage.cursor_settled
            ),
            max_frames=900,
            max_wall_s=45.0,
            check_every=4,
        )
        # FireRed exposes the completed party/box writes before the withdraw-list slide finishes.
        # ui_state 2 in that window has no error text and must remain owned by this successful
        # action; wait for fresh RAM to prove that storage input is accepted again.
        yield WaitUntil(
            lambda current: _ready_storage_cursor(current.snapshot, operation) is not None,
            max_frames=240,
            max_wall_s=16.0,
            check_every=2,
            failure_reason="withdraw cursor did not become ready after mutation",
        )


def _cursor_model(snapshot, entries, surface: str):
    if entries is None:
        return None
    return decode_cursor_menu(
        snapshot,
        surface=surface,
        entries=tuple((entry.stable_key, entry.label) for entry in entries),
    )


def _pc_menu_choice_for(menus):
    def choose(ctx: StepContext):
        if menus is None:
            raise CursorMismatch("ROM-backed PC menu resolver is unavailable")
        key = str(
            ctx.args.get("stable_key")
            or (ctx.option.action_contract.cursor_key if ctx.option.action_contract else "")
        )
        yield from select_menu_entry(
            ctx,
            key,
            lambda snapshot: _cursor_model(
                snapshot,
                menus.pokemon_storage(
                    snapshot.ui.menu.menu.max_cursor_pos - snapshot.ui.menu.menu.min_cursor_pos + 1
                )
                if snapshot.ui and snapshot.ui.menu.menu
                else None,
                "pc.storage.menu",
            ),
            tap=lambda button: Tap(button, hold_frames=HOLD, wait_frames=RELEASE),
            max_moves=8,
        )
        operation = {
            "pc_storage:withdraw": 0,
            "pc_storage:deposit": 1,
            "pc_storage:move": 2,
            "pc_storage:move_items": 3,
        }.get(key)
        if operation is None:
            yield WaitUntil(
                lambda current: current.scene.scene_id == "dialog.multichoice",
                max_frames=1200,
                max_wall_s=60.0,
                check_every=4,
            )
            return
        yield WaitUntil(
            lambda current: bool(
                current.snapshot.storage is not None
                and current.snapshot.storage.box_option == operation
                and current.snapshot.storage.ui_state == 0
                and (verdict := E.classify(current.snapshot)) is not None
                and verdict.evidence.get("phase") == "storage"
            ),
            max_frames=1200,
            max_wall_s=60.0,
            check_every=4,
        )

    return choose


def _generic_yes_no(snapshot) -> bool:
    ui = snapshot.ui
    return bool(
        ui is not None
        and ui.valid
        and (
            dialog_evidence.yes_no_open(ui)
            or dialog_evidence.yes_no_task_menu_open(ui)
            or dialog_evidence.pc_storage_yes_no_open(ui)
            or dialog_evidence.yes_no_multichoice(ui, snapshot.rom_profile)
        )
    )


def _pc_player_for(menus):
    def choose(ctx: StepContext):
        if menus is None:
            raise CursorMismatch("ROM-backed Player PC menu resolver is unavailable")
        target = str(ctx.args["stable_key"])
        top_key = (
            "player_pc:item_storage"
            if target in {"player_pc:withdraw_item", "player_pc:deposit_item"}
            else target
        )

        def top_model(snapshot):
            menu = snapshot.ui.menu.menu if snapshot.ui and snapshot.ui.menu.menu else None
            count = menu.max_cursor_pos - menu.min_cursor_pos + 1 if menu else 0
            return _cursor_model(snapshot, menus.player_pc_top(count), "player.pc")

        obs = yield from select_menu_entry(
            ctx,
            top_key,
            top_model,
            tap=lambda button: Tap(button, hold_frames=HOLD, wait_frames=RELEASE),
            max_moves=6,
        )
        if top_key != "player_pc:item_storage":
            return
        obs = yield WaitUntil(
            lambda current: bool(
                (verdict := E.classify(current.snapshot)) is not None
                and verdict.evidence.get("menu") == "player_pc_items"
            ),
            max_frames=600,
            max_wall_s=30.0,
            check_every=4,
        )

        def item_model(snapshot):
            menu = snapshot.ui.menu.menu if snapshot.ui and snapshot.ui.menu.menu else None
            count = menu.max_cursor_pos - menu.min_cursor_pos + 1 if menu else 0
            return _cursor_model(snapshot, menus.player_pc_items(count), "player.pc.items")

        obs = yield from select_menu_entry(
            obs,
            target,
            item_model,
            tap=lambda button: Tap(button, hold_frames=HOLD, wait_frames=RELEASE),
            max_moves=6,
        )
        direction = str(ctx.args["direction"])
        item_id = int(ctx.args["item_id"])
        decoder = menus.pc_item_list if direction == "withdraw" else menus.bag
        obs = yield WaitUntil(
            lambda current: decoder(current.snapshot) is not None,
            max_frames=1200,
            max_wall_s=60.0,
            check_every=4,
        )
        item_key = f"pc_item:{item_id}" if direction == "withdraw" else f"bag:items:{item_id}"
        obs = yield from select_menu_entry(
            obs,
            item_key,
            decoder,
            tap=lambda button: Tap(button, hold_frames=HOLD, wait_frames=12),
            max_moves=64,
            readback_retries=2,
        )
        # The generic adapter established the item identity.  Continue only while RAM shows an
        # input surface.  A confirmation is answered through the shared yes/no adapter; there is
        # no blind A-loop and no fixed quantity assumption.
        for _ in range(6):
            if _pc_item_result(obs.snapshot, ctx):
                return
            if _generic_yes_no(obs.snapshot):
                yield from answer_yes_no_from(ctx, obs.snapshot, 0)
                obs = yield Advance(frames=30)
                continue
            menu = obs.snapshot.ui.menu.menu if obs.snapshot.ui and obs.snapshot.ui.valid else None
            if menu is None or not menu.window_in_use or not menu.consistent:
                obs = yield WaitUntil(
                    lambda current: (
                        _pc_item_result(current.snapshot, ctx)
                        or _generic_yes_no(current.snapshot)
                        or bool(
                            current.snapshot.ui
                            and current.snapshot.ui.valid
                            and current.snapshot.ui.menu.menu
                            and current.snapshot.ui.menu.menu.window_in_use
                            and current.snapshot.ui.menu.menu.consistent
                        )
                    ),
                    max_frames=300,
                    max_wall_s=30.0,
                    check_every=4,
                )
                continue
            bounds = quantity_bounds(obs.snapshot)
            if bounds is None:
                raise CursorMismatch("Player PC quantity prompt is not RAM-readable")
            minimum, maximum = bounds
            if not (minimum <= maximum):
                raise CursorMismatch("Player PC quantity range is invalid")
            arrived = yield from _navigate(
                menu_cursor(obs.snapshot), minimum, menu_cursor, limit=max(1, maximum - minimum)
            )
            if not arrived:
                raise CursorMismatch("Player PC quantity cursor did not reach one")
            obs = yield Tap("a", hold_frames=HOLD, wait_frames=30)
        if not _pc_item_result(obs.snapshot, ctx):
            raise CursorMismatch("Player PC item transfer produced no Bag/PC-item RAM change")

    return choose


def _pc_item_result(snapshot, ctx: StepContext) -> bool:
    item_id = int(ctx.args["item_id"])
    direction = str(ctx.args["direction"])

    def quantity(value, pc: bool) -> int:
        if pc:
            storage = value.storage
            items = storage.pc_items if storage is not None else ()
        else:
            items = (item for pocket in value.bag for item in pocket.items)
        return sum(int(item.quantity) for item in items if int(item.item_id) == item_id)

    bag_before, bag_after = quantity(ctx.snapshot, False), quantity(snapshot, False)
    pc_before, pc_after = quantity(ctx.snapshot, True), quantity(snapshot, True)
    if direction == "withdraw":
        return bag_after > bag_before and pc_after < pc_before
    return bag_after < bag_before and pc_after > pc_before


def executor_specs(foreign: frozenset[str] = frozenset(), menus=None) -> tuple[ExecutorSpec, ...]:
    return (
        ExecutorSpec(
            R.REF_WAIT,
            ExecutionKind.DETERMINISTIC,
            _wait_for(foreign),
            args={"chunk": Arg(int, min=30, max=20000)},
            allowed_scenes=None,
            max_inputs=1,
            max_frames=20000 + 64,
            settle_frames=1500,
            settle_wall_s=90.0,
        ),
        ExecutorSpec(
            R.REF_ACK_CLEAR,
            ExecutionKind.ATOMIC,
            _press("a"),
            settle_frames=HOF_EXIT_SETTLE,
            settle_wall_s=HOF_EXIT_SETTLE_WALL_S,
        ),
        ExecutorSpec(
            R.REF_PRESS_A, ExecutionKind.ATOMIC, _press("a"), settle_frames=900, settle_wall_s=60.0
        ),
        ExecutorSpec(
            R.REF_ADVANCE_INTRO,
            ExecutionKind.MACRO,
            _advance_intro,
            allowed_scenes=frozenset(
                {R.PROFESSOR_TEXT, R.SCRIPT_MOVEMENT, R.GENDER, R.NAMING, R.RIVAL_NAME}
            ),
            max_inputs=1,
            max_frames=2408,
            settle_frames=2400,
            settle_wall_s=180.0,
            macro_manages_busy=True,
        ),
        ExecutorSpec(
            R.REF_PRESS_START,
            ExecutionKind.ATOMIC,
            _press("start"),
            settle_frames=1500,
            settle_wall_s=90.0,
        ),
        ExecutorSpec(
            R.REF_PRESS_B, ExecutionKind.ATOMIC, _press("b"), settle_frames=900, settle_wall_s=60.0
        ),
        ExecutorSpec(
            R.REF_BACK_OUT_FOREIGN,
            ExecutionKind.MACRO,
            _foreign_screen_back,
            allowed_scenes=frozenset(
                {
                    "pokedex.list",
                    "party.summary",
                    "menu.trainer_card",
                    "menu.options",
                    "field.map_transition",
                    "menu.start",
                    "party.list",
                    "menu.bag",
                    "pc.main",
                }
            ),
            max_inputs=4,
            max_frames=3600,
            settle_frames=120,
            settle_wall_s=120.0,
            macro_manages_busy=True,
        ),
        ExecutorSpec(
            R.REF_PC_STORAGE_BACK,
            ExecutionKind.MACRO,
            _pc_storage_back,
            allowed_scenes=frozenset({R.PC_MAIN, "dialog.yes_no"}),
            max_inputs=3,
            max_frames=1400,
            settle_frames=120,
            settle_wall_s=8.0,
            macro_manages_busy=True,
        ),
        ExecutorSpec(
            R.REF_PC_STORAGE_ERROR,
            ExecutionKind.DETERMINISTIC,
            _pc_storage_error,
            allowed_scenes=frozenset({R.PC_MAIN}),
            max_inputs=1,
            max_frames=256,
            settle_frames=120,
            settle_wall_s=16.0,
        ),
        ExecutorSpec(
            R.REF_CLOSE_MENU,
            ExecutionKind.DETERMINISTIC,
            _close_menu,
            settle_frames=900,
            settle_wall_s=60.0,
        ),
        ExecutorSpec(
            R.REF_SKIP_QUEST_LOG,
            ExecutionKind.ATOMIC,
            _press("b"),
            allowed_scenes=frozenset({R.QUEST_LOG}),
            settle_frames=900,
            settle_wall_s=60.0,
        ),
        ExecutorSpec(
            R.REF_CANCEL_EVOLUTION,
            ExecutionKind.ATOMIC,
            _press("b"),
            settle_frames=900,
            settle_wall_s=60.0,
        ),
        ExecutorSpec(
            R.REF_CONTINUE,
            ExecutionKind.MACRO,
            _continue_game,
            max_inputs=4,
            max_frames=600,
            settle_frames=2400,
            settle_wall_s=120.0,
        ),
        ExecutorSpec(
            R.REF_NEW_GAME,
            ExecutionKind.MACRO,
            _new_game,
            allowed_scenes=frozenset({R.TITLE_MAIN}),
            max_inputs=8,
            max_frames=500,
            settle_frames=2400,
            settle_wall_s=120.0,
        ),
        ExecutorSpec(
            R.REF_SELECT_PRESET,
            ExecutionKind.MACRO,
            _select_name_preset,
            args={"index": Arg(int, min=0, max=0)},
            allowed_scenes=frozenset({R.RIVAL_NAME}),
            max_inputs=2,
            max_frames=60,
            settle_frames=2400,
            settle_wall_s=180.0,
        ),
        ExecutorSpec(
            R.REF_ACCEPT_PRESET,
            ExecutionKind.MACRO,
            _accept_name_preset,
            allowed_scenes=frozenset({R.NAMING, R.SCRIPT_MOVEMENT}),
            max_inputs=2,
            max_frames=1208,
            settle_frames=2400,
            settle_wall_s=180.0,
            macro_manages_busy=True,
        ),
        ExecutorSpec(
            R.REF_CONFIRM_NAME,
            ExecutionKind.MACRO,
            _menu_answer,
            args={"entry": Arg(int, min=0, max=1)},
            allowed_scenes=frozenset({R.NAMING}),
            max_inputs=2,
            max_frames=60,
            settle_frames=2400,
            settle_wall_s=180.0,
        ),
        ExecutorSpec(
            R.REF_CHOOSE_GENDER,
            ExecutionKind.MACRO,
            _menu_answer,
            args={"entry": Arg(int, min=0, max=1)},
            allowed_scenes=frozenset({R.GENDER}),
            max_inputs=2,
            max_frames=60,
            settle_frames=2400,
            settle_wall_s=180.0,
        ),
        ExecutorSpec(
            R.REF_EVO_ANSWER,
            ExecutionKind.MACRO,
            _evolution_answer,
            args={"entry": Arg(int, min=0, max=1)},
            allowed_scenes=frozenset({R.EVO_MOVE}),
            max_inputs=2,
            max_frames=240,
            settle_frames=900,
            settle_wall_s=60.0,
        ),
        ExecutorSpec(
            R.REF_FORGET_MOVE,
            ExecutionKind.MACRO,
            _forget_move,
            args={
                "slot": Arg(int, min=0, max=3),
                "old_move": Arg(int, min=1, max=65535),
                "party_id": Arg(int, min=0, max=5),
            },
            allowed_scenes=frozenset({R.EVO_MOVE}),
            max_inputs=SUMMARY_MAX_INPUTS,
            max_frames=SUMMARY_MAX_FRAMES,
            settle_frames=900,
            settle_wall_s=60.0,
        ),
        ExecutorSpec(
            R.REF_PC_MENU_CHOICE,
            ExecutionKind.MACRO,
            _pc_menu_choice_for(menus),
            args={"stable_key": Arg(str, required=False)},
            allowed_scenes=frozenset({R.PC_MAIN, "dialog.multichoice", "dialog.printing"}),
            max_inputs=8,
            max_frames=2400,
            settle_frames=2400,
            settle_wall_s=120.0,
            macro_manages_busy=True,
        ),
        ExecutorSpec(
            R.REF_PC_PLAYER,
            ExecutionKind.MACRO,
            _pc_player_for(menus),
            args={
                "stable_key": Arg(str),
                "item_id": Arg(int, required=False, min=1, max=65535),
                "direction": Arg(str, required=False, choices=("withdraw", "deposit")),
            },
            allowed_scenes=frozenset(
                {R.PC_MAIN, "dialog.multichoice", "dialog.printing", "menu.open", "bag.items"}
            ),
            max_inputs=32,
            max_frames=4800,
            settle_frames=4800,
            settle_wall_s=240.0,
            macro_manages_busy=True,
        ),
        ExecutorSpec(
            R.REF_PC_STORAGE,
            ExecutionKind.MACRO,
            _pc_storage,
            args={
                "pid": Arg(int, min=0, max=0xFFFF_FFFF),
                "operation": Arg(int, min=0, max=1),
                "box": Arg(int, min=0, max=13, required=False),
                "slot": Arg(int, min=0, max=29, required=False),
            },
            allowed_scenes=frozenset({R.PC_MAIN}),
            max_inputs=160,
            max_frames=9000,
            settle_frames=10000,
            settle_wall_s=240.0,
            failure_cleanup=_pc_storage_failure_cleanup,
        ),
        ExecutorSpec(
            R.REF_PC_MOVE,
            ExecutionKind.MACRO,
            _pc_move,
            args={
                "pid": Arg(int, min=0, max=0xFFFF_FFFF),
                "box": Arg(int, min=0, max=13, required=False),
                "slot": Arg(int, min=0, max=29),
            },
            allowed_scenes=frozenset({R.PC_MAIN}),
            max_inputs=160,
            max_frames=600,
            settle_frames=120,
            settle_wall_s=8.0,
        ),
        ExecutorSpec(
            R.REF_PC_MOVE_PLACE,
            ExecutionKind.MACRO,
            _pc_move_place,
            allowed_scenes=frozenset({R.PC_MAIN}),
            args={
                "pid": Arg(int, min=0, max=0xFFFF_FFFF),
                "target_kind": Arg(str, choices=("box", "party")),
                "target_box": Arg(int, min=0, max=13, required=False),
                "target_slot": Arg(int, min=0, max=29),
            },
            max_inputs=129,
            max_frames=600,
            settle_frames=120,
            settle_wall_s=8.0,
        ),
        ExecutorSpec(
            R.REF_PC_MOVE_CANCEL,
            ExecutionKind.MACRO,
            _pc_move_cancel,
            allowed_scenes=frozenset({R.PC_MAIN}),
            max_inputs=1,
            max_frames=180,
            settle_frames=120,
            settle_wall_s=8.0,
        ),
        ExecutorSpec(
            R.REF_LEAVE_PC,
            ExecutionKind.MACRO,
            _leave_pc,
            allowed_scenes=frozenset(
                {R.PC_MAIN, "dialog.multichoice", "dialog.printing", "dialog.yes_no"}
            ),
            max_inputs=8,
            max_frames=8 * 904,
            settle_frames=10000,
            settle_wall_s=240.0,
        ),
    )
