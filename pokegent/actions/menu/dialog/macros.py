"""Executor specs of the dialog family (spec 11.1, 11.2): waits, A presses, cursor macros.

Every function only *yields steps*; it never touches the emulator. The model supplies an option id,
the builder supplied the arguments, the executor re-validated everything on a fresh snapshot before
the first step runs. Rules:

* a wait presses nothing (``dialog.wait_for_text``, ``dialog.wait_for_recovery``);
* A is pressed only by single-tap specs whose options exist where the game is provably blocked on
  A/B (the builder's job, re-checked by the executor); ``close_dialog`` is a one-step macro only so
  it can verify that the box moved on without waiting through unrelated downstream scripted work;
* cursor macros move the cursor one step at a time, READ it back from the next observation, press
  A only when the cursor is verified on the target. An unexpected cursor stops the macro without A
  (``verified_outcome`` stays ``False``/unset and the result is ``uncertain``), never a blind press;
* the budgets are small and fixed; nothing is derived from model text.
"""

from __future__ import annotations

from collections.abc import Generator

from pokegent.actions._exec.models import ExecutionKind, Step, Tap, WaitUntil
from pokegent.actions._exec.registry import Arg, ExecutorSpec, StepContext
from pokegent.actions.menu.dialog import builder as B
from pokegent.actions.menu.dialog import evidence as E
from pokegent.actions.menu.dialog.prompts import PromptPolicy
from pokegent.actions.menu.dialog.text import TextSidecar
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot

HOLD, RELEASE = 4, 2  # frames a button is held / the gap after it (a real press, not a glitch)

PRINT_WAIT_FRAMES = 2400  # ~40 s of game time: slow text speed, a long message
RECOVERY_WAIT_FRAMES = 1800
#: after a step the executor waits for a busy scene to end (bounded): a Pokemon Center healing
#: sequence or a fanfare after a message runs 300-600 frames, so the default 300 is too tight
BUSY_AFTER_A = 900
BUSY_AFTER_WAIT_FRAMES = 1500
#: wall-clock companion of the two budgets above (the host may be loaded: nice 15, shared lock)
BUSY_AFTER_WALL_S = 60.0


def _tap(button: str) -> Tap:
    return Tap(button, hold_frames=HOLD, wait_frames=RELEASE)


def menu_cursor(snapshot: StateSnapshot) -> int | None:
    """``sMenu.cursorPos`` if the menu is corroborated (window in use, consistent), else None."""
    ui = snapshot.ui
    sm = ui.menu.menu if ui is not None and ui.valid else None
    if sm is None or not sm.window_in_use or not sm.consistent:
        return None
    return sm.cursor_pos


def quantity_bounds(snapshot: StateSnapshot) -> tuple[int, int] | None:
    """Read the active quantity menu's cursor range from sMenu."""
    sm = snapshot.ui.menu.menu if snapshot.ui and snapshot.ui.valid else None
    if sm is None or not sm.window_in_use or not sm.consistent:
        return None
    return int(sm.min_cursor_pos), int(sm.max_cursor_pos)


def list_position(snapshot: StateSnapshot) -> int | None:
    """Absolute row of the list cursor (scroll offset + row), None if no list is open."""
    ui = snapshot.ui
    if ui is None or not ui.valid or not E.list_open(ui):
        return None
    lm = ui.menu.list_menu
    assert lm is not None
    return lm.scroll_offset + lm.cursor_row


class CursorMismatch(RuntimeError):
    """The cursor is not where the macro needs it. A is NOT pressed; the executor reports the
    action as ``uncertain`` (or ``rejected`` if nothing was pressed yet) with this reason."""


def _navigate(start: int | None, want: int, read, *, limit: int) -> Generator[Step, object, bool]:
    """Move a vertical cursor from ``start`` to ``want`` one verified step at a time.

    ``read(snapshot) -> position | None``. Returns True iff the cursor is verified on ``want``.
    A step that does not move the cursor by exactly one stops the walk (no further press).
    """
    cur = start
    steps = 0
    while cur is not None and cur != want:
        if steps >= limit:
            return False
        step = 1 if want > cur else -1
        obs = yield _tap("down" if step > 0 else "up")
        steps += 1
        new = read(obs.snapshot)  # type: ignore[attr-defined]
        if new != cur + step:
            return False
        cur = new
    return cur == want


def _answer_yes_no(ctx: StepContext, policy: PromptPolicy | None = None):
    want = int(ctx.args["entry"])  # 0 = YES, 1 = NO (both the script task and the YES/NO menu)
    yield from answer_yes_no_from(ctx, ctx.snapshot, want, policy=policy)


def answer_yes_no_from(
    ctx: StepContext, snapshot, entry: int, *, policy: PromptPolicy | None = None
):
    """Answer a live generic yes/no prompt using its RAM cursor.

    This is shared by macros that intentionally cross a confirmation.  The caller supplies
    the current observation, so it cannot reuse a stale cursor from the action's opening frame.
    """
    want = int(entry)
    option_facts = getattr(ctx.option, "facts", {})
    move = option_facts.get("field_move")
    is_field_move = option_facts.get("prompt_kind") == "field_move"
    goal_id = getattr(ctx.frame, "goal_id", None)
    if policy is not None and want == 1 and is_field_move and isinstance(move, str):
        policy.finish_field_move(goal_id=goal_id, move=move, declined=True)
    arrived = yield from _navigate(menu_cursor(snapshot), want, menu_cursor, limit=1)
    if not arrived:
        raise CursorMismatch(f"yes/no cursor did not reach entry {want}; A not pressed")
    yield _tap("a")
    if policy is not None and want == 0 and is_field_move and isinstance(move, str):
        policy.finish_field_move(goal_id=goal_id, move=move, declined=False)


def _select_choice(ctx: StepContext):
    want = int(ctx.args["index"])
    arrived = yield from _navigate(
        menu_cursor(ctx.snapshot), want, menu_cursor, limit=B.MAX_ENTRIES
    )
    if not arrived:
        raise CursorMismatch(f"menu cursor did not reach entry {want}; A not pressed")
    yield _tap("a")


def _pc_destination(obs, stable_key: str) -> bool:
    scene = obs.scene.scene_id
    evidence = getattr(obs.scene, "evidence", {})
    if stable_key in ("pc:someone", "pc:bill"):
        return scene == "pc.main" and evidence.get("menu") == "pc_menu"
    if stable_key == "pc:player":
        return scene == "pc.main" and evidence.get("menu") == "player_pc"
    if stable_key == "pc:oak":
        return scene == E.YES_NO
    if stable_key == "pc:hall_of_fame":
        return scene == "special.hall_of_fame"
    if stable_key in ("pc:log_off", "cancel_choice"):
        return scene == "field.free"
    return False


def _select_pc_choice(ctx: StepContext):
    """Confirm the RAM-derived row and cross the fixed PC login messages.

    The PC script puts one or two blocking messages between its choice box and the destination
    menu.  Keeping those messages inside this action makes its postcondition the actual owner
    scene, rather than the transient fact that the choice window disappeared.
    """
    want = int(ctx.args["index"])
    stable_key = ctx.option.id
    arrived = yield from _navigate(
        menu_cursor(ctx.snapshot), want, menu_cursor, limit=B.MAX_ENTRIES
    )
    if not arrived:
        raise CursorMismatch(f"PC cursor did not reach {stable_key} at row {want}; A not pressed")
    obs = yield _tap("a")
    while not _pc_destination(obs, stable_key):
        if obs.scene.scene_id in (E.ADVANCE, E.END):
            obs = yield _tap("a")
            continue
        obs = yield WaitUntil(
            lambda current: (
                _pc_destination(current, stable_key) or current.scene.scene_id in (E.ADVANCE, E.END)
            ),
            max_frames=900,
            max_wall_s=60.0,
            check_every=4,
        )


def _select_row(ctx: StepContext):
    want = int(ctx.args["row"])
    start = list_position(ctx.snapshot)
    total = ctx.snapshot.ui.menu.list_menu.total_items if start is not None else 0
    arrived = yield from _navigate(start, want, list_position, limit=max(1, total))
    if not arrived:
        raise CursorMismatch(f"list cursor did not reach row {want}; A not pressed")
    yield _tap("a")


def _scroll_list(ctx: StepContext):
    start = list_position(ctx.snapshot)
    if start is None:
        raise CursorMismatch("no list is open; nothing scrolled")
    want = start + int(ctx.args["delta"])
    if not (yield from _navigate(start, want, list_position, limit=1)):
        raise CursorMismatch("list cursor did not scroll by one row")


def _wait_text_for(sidecar: TextSidecar | None):
    def fn(ctx: StepContext):
        started = sidecar.identity(ctx.snapshot) if sidecar is not None else None

        def done(obs) -> bool:
            """The page we waited for is over: the box moved on, or it is another page / message."""
            if obs.scene.scene_id != E.PRINTING:
                return True
            return started is not None and sidecar.identity(obs.snapshot) != started

        yield WaitUntil(done, max_frames=PRINT_WAIT_FRAMES, max_wall_s=40.0, check_every=4)
        # Reached only if the bounded wait was satisfied (a timeout stops the run before this line).
        # M2.5: the verifier re-evaluated the same predicate on later frames and failed during the
        # Pokemon Center heal, where the box flickers back to PRINTING after the wait had succeeded.
        ctx.args["_done"] = True

    return fn


def _wait_recovery(ctx: StepContext):
    return WaitUntil(
        lambda obs: obs.scene.scene_id != E.WHITEOUT,
        max_frames=RECOVERY_WAIT_FRAMES,
        max_wall_s=40.0,
        check_every=4,
    )


def _press(button: str):
    def fn(ctx: StepContext):
        return _tap(button)

    return fn


_AFTER_A = {"settle_frames": BUSY_AFTER_A, "settle_wall_s": BUSY_AFTER_WALL_S}


def executor_specs(
    sidecar: TextSidecar | None = None, policy: PromptPolicy | None = None
) -> tuple[ExecutorSpec, ...]:
    yes_no_scenes = frozenset({E.YES_NO, E.HEAL_OFFER})
    return (
        ExecutorSpec(
            B.REF_WAIT_TEXT,
            ExecutionKind.DETERMINISTIC,
            _wait_text_for(sidecar),
            max_frames=PRINT_WAIT_FRAMES + 60,
            max_inputs=1,
            settle_frames=BUSY_AFTER_WAIT_FRAMES,
            settle_wall_s=BUSY_AFTER_WALL_S,
        ),
        ExecutorSpec(
            B.REF_WAIT_RECOVERY,
            ExecutionKind.DETERMINISTIC,
            _wait_recovery,
            max_frames=RECOVERY_WAIT_FRAMES + 60,
            max_inputs=1,
            settle_frames=BUSY_AFTER_WAIT_FRAMES,
            settle_wall_s=BUSY_AFTER_WALL_S,
        ),
        ExecutorSpec(
            B.REF_ADVANCE, ExecutionKind.ATOMIC, _press("a"), autoplay_safe=True, **_AFTER_A
        ),
        ExecutorSpec(
            B.REF_PC_STORAGE_ERROR,
            ExecutionKind.ATOMIC,
            _press("b"),
            allowed_scenes=frozenset({E.ADVANCE, E.END, E.UNKNOWN_DIALOG}),
            autoplay_safe=True,
            **_AFTER_A,
        ),
        ExecutorSpec(
            B.REF_CLOSE,
            ExecutionKind.MACRO,
            _press("a"),
            max_inputs=1,
            autoplay_safe=True,
            macro_manages_busy=True,
            **_AFTER_A,
        ),
        ExecutorSpec(
            B.REF_ACK_ITEM, ExecutionKind.ATOMIC, _press("a"), autoplay_safe=True, **_AFTER_A
        ),
        ExecutorSpec(
            B.REF_ACK_WHITEOUT,
            ExecutionKind.ATOMIC,
            _press("a"),
            autoplay_safe=True,
            **_AFTER_A,
        ),
        ExecutorSpec(
            B.REF_ANSWER,
            ExecutionKind.MACRO,
            (lambda ctx: _answer_yes_no(ctx, policy)),
            args={"entry": Arg(int, min=0, max=1)},
            allowed_scenes=yes_no_scenes,
            max_inputs=2,
            max_frames=240,
            # the YES of the heal offer starts the 300-600 frame healing animation; the answer's
            # own presses stay within max_frames, only the busy wait after the last one is long
            settle_frames=BUSY_AFTER_A,
            settle_wall_s=BUSY_AFTER_WALL_S,
        ),
        ExecutorSpec(
            B.REF_SELECT_CHOICE,
            ExecutionKind.MACRO,
            _select_choice,
            args={"index": Arg(int, min=0, max=B.MAX_ENTRIES - 1)},
            allowed_scenes=frozenset({E.MULTICHOICE}),
            max_inputs=B.MAX_ENTRIES + 1,
            max_frames=600,
        ),
        ExecutorSpec(
            B.REF_SELECT_PC,
            ExecutionKind.MACRO,
            _select_pc_choice,
            args={"index": Arg(int, min=0, max=B.MAX_ENTRIES - 1)},
            allowed_scenes=frozenset(
                {
                    E.MULTICHOICE,
                    E.PRINTING,
                    E.ADVANCE,
                    E.END,
                    E.YES_NO,
                    E.UNKNOWN_DIALOG,
                    "pc.main",
                    "special.hall_of_fame",
                    "field.free",
                }
            ),
            max_inputs=B.MAX_ENTRIES + 5,
            max_frames=3000,
            settle_frames=1500,
            settle_wall_s=60.0,
        ),
        ExecutorSpec(
            B.REF_SELECT_ROW,
            ExecutionKind.MACRO,
            _select_row,
            args={"row": Arg(int, min=0, max=999)},
            allowed_scenes=frozenset({E.LIST}),
            max_inputs=64,
            max_frames=1200,
        ),
        ExecutorSpec(B.REF_CANCEL_LIST, ExecutionKind.ATOMIC, _press("b")),
        ExecutorSpec(
            B.REF_CANCEL_CHOICE,
            ExecutionKind.ATOMIC,
            _press("b"),
            allowed_scenes=frozenset({E.MULTICHOICE}),
        ),
        ExecutorSpec(
            B.REF_SCROLL_LIST,
            ExecutionKind.MACRO,
            _scroll_list,
            args={"delta": Arg(int, choices=(-1, 1))},
            allowed_scenes=frozenset({E.LIST}),
            max_inputs=1,
            max_frames=120,
        ),
    )
