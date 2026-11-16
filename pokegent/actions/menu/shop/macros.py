"""Bounded shop input macros with cursor and quantity read-back."""

from __future__ import annotations

from pokegent.actions._exec.models import Advance, ExecutionKind, Tap, WaitUntil
from pokegent.actions._exec.registry import Arg, ExecutorSpec, StepContext
from pokegent.actions.menu.shop import evidence as E
from pokegent.actions.menu.shop import refs as R
from pokegent.actions.menu.shop import symbols as S
from pokegent.games.gen3.firered.reader.ui_symbols import KEY_BITS

HOLD, RELEASE = 4, 2


class CursorMismatch(RuntimeError):
    """The game did not report the cursor value that the macro expected."""


def _tap(button: str) -> Tap:
    return Tap(button, hold_frames=HOLD, wait_frames=RELEASE)


def _held(snapshot, button: str) -> bool | None:
    ui = snapshot.ui
    value = ui.main.held_keys if ui is not None and ui.main.valid else None
    return None if value is None else bool(value & KEY_BITS[button.upper()])


def _press(snapshot, button: str):
    """Press until FireRed's own gMain.heldKeys proves it sampled the button."""
    for _ in range(8):
        if not _held(snapshot, button):
            break
        observation = yield Advance(1)
        snapshot = observation.snapshot

    bit = KEY_BITS[button.upper()]

    def read(observation) -> bool:
        ui = observation.snapshot.ui
        held = ui.main.held_keys if ui is not None and ui.main.valid else None
        return held is None or bool(held & bit)

    return (yield Tap(button, hold_frames=3, wait_frames=RELEASE, until=read, max_hold_frames=8))


def _menu_cursor(snapshot) -> int | None:
    ui = snapshot.ui
    if ui is None or not ui.valid or ui.menu.menu is None:
        return None
    return ui.menu.menu.cursor_pos


def _shop_prompt_cursor(snapshot) -> int | None:
    """Return the cursor only while ``Task_ShopMenu`` is the active input handler.

    FireRed's mart prompt calls ``Menu_ProcessInputNoWrapAround`` directly from
    ``Task_ShopMenu``.  The shared ``sMenu`` bytes can remain plausible across menu handoffs, so
    the task function is the RAM fact that makes ``sMenu.cursorPos`` authoritative here.
    """
    ui = snapshot.ui
    menu = ui.menu.menu if ui is not None and ui.valid else None
    if (
        menu is None
        or not menu.window_in_use
        or not menu.consistent
        or menu.min_cursor_pos != 0
        or menu.max_cursor_pos != 2
        or not E.task_active(snapshot, S.SHOP_TASK)
    ):
        return None
    return menu.cursor_pos


def _list_index(snapshot) -> int | None:
    ui = snapshot.ui
    if ui is None or not ui.valid or ui.menu.list_menu is None:
        return None
    menu = ui.menu.list_menu
    return menu.scroll_offset + menu.cursor_row


def _move_cursor(ctx: StepContext, target: int):
    snapshot = ctx.snapshot
    current = _menu_cursor(snapshot)
    if current is None:
        raise CursorMismatch("shop menu cursor is unreadable")
    while current != target:
        button = "down" if current < target else "up"
        expected = current + (1 if current < target else -1)
        obs = yield from _press(snapshot, button)
        seen = _menu_cursor(obs.snapshot)
        if seen != expected:
            raise CursorMismatch(f"shop menu cursor read {seen}, expected {expected}")
        current = seen
        snapshot = obs.snapshot
    yield from _press(snapshot, "a")


def _main_cursor(ctx: StepContext):
    try:
        yield from _move_cursor(ctx, int(ctx.args["target"]))
    except CursorMismatch:
        yield _tap("b")


def _prompt_cursor(ctx: StepContext):
    """Select BUY on the clerk prompt and wait for CB2_BuyMenu to expose the list."""
    target = int(ctx.args["target"])
    observation = yield WaitUntil(
        lambda current: _shop_prompt_cursor(current.snapshot) is not None,
        max_frames=180,
        failure_reason="shop prompt input handler did not become ready",
    )
    current = _shop_prompt_cursor(observation.snapshot)
    if current is None:
        raise CursorMismatch("shop prompt cursor is unreadable")
    while current != target:
        button = "down" if current < target else "up"
        expected = current + (1 if current < target else -1)

        def moved(current_observation, expected=expected):
            return _shop_prompt_cursor(current_observation.snapshot) == expected

        observation = yield Tap(
            button,
            hold_frames=1,
            wait_frames=RELEASE,
            until=moved,
            max_hold_frames=8,
        )
        seen = _shop_prompt_cursor(observation.snapshot)
        if seen != expected:
            raise CursorMismatch(f"shop prompt cursor read {seen}, expected {expected}")
        current = seen

    observation = yield WaitUntil(
        lambda current: _shop_prompt_cursor(current.snapshot) == target,
        max_frames=60,
        failure_reason="shop prompt cursor did not remain on the requested entry",
    )
    observation = yield Tap(
        "a",
        hold_frames=1,
        wait_frames=RELEASE,
        until=lambda current: not E.task_active(current.snapshot, S.SHOP_TASK),
        max_hold_frames=8,
    )
    if E.task_active(observation.snapshot, S.SHOP_TASK):
        raise CursorMismatch("shop prompt did not accept the confirmed entry")
    if target == 0:
        yield WaitUntil(
            lambda observation: (
                (result := E.classify(observation.snapshot)) is not None
                and result.scene_id == R.BUY_LIST
            ),
            max_frames=180,
        )


def _list_item(ctx: StepContext):
    target = int(ctx.args["item_index"])
    try:
        current = _list_index(ctx.snapshot)
        if current is None:
            raise CursorMismatch("shop list cursor is unreadable")
        unchanged = 0
        while current != target:
            button = "down" if current < target else "up"
            expected = current + (1 if current < target else -1)
            obs = yield _tap(button)
            seen = _list_index(obs.snapshot)
            if seen == current:
                # FireRed can still be consuming the previous list input when the next
                # tap arrives.  An unchanged RAM cursor is not a mismatch; retry the
                # same bounded input rather than cancelling the item selection.
                unchanged += 1
                if unchanged > 4:
                    raise CursorMismatch(
                        f"shop list cursor did not move from {current} after retries"
                    )
                continue
            if seen != expected:
                raise CursorMismatch(f"shop list index read {seen}, expected {expected}")
            current = seen
            unchanged = 0
        yield _tap("a")
        yield WaitUntil(
            lambda observation: (
                (result := E.classify(observation.snapshot)) is not None
                and result.scene_id == R.BUY_QUANTITY
            ),
            max_frames=180,
            failure_reason="shop item selection did not reach the quantity prompt",
        )
    except CursorMismatch:
        yield _tap("b")


def _back(ctx: StepContext):
    yield _tap("b")


def _sell_list_back(ctx: StepContext):
    yield _tap("b")
    yield WaitUntil(
        lambda observation: (
            (result := E.classify(observation.snapshot)) is not None
            and result.scene_id == R.SHOP_PROMPT
        ),
        max_frames=360,
        failure_reason="shop sell list did not return to the shop menu",
    )


def _sell_failure_cleanup(actor, perceiver, _ctx: StepContext) -> None:
    """Return a failed sell-owned action to the clerk prompt using RAM-owned input gates."""
    presses = 0
    for _ in range(600):
        observation = perceiver.observe()
        result = E.classify(observation.snapshot)
        if result is not None and result.scene_id == R.SHOP_PROMPT and not result.busy:
            return
        ui = observation.snapshot.ui
        if ui is None or not ui.valid or ui.field.fade_active:
            actor.advance_frames(2)
            continue

        button = None
        bag = ui.menu.bag
        yes_no = ui.menu.yes_no
        if yes_no is not None and yes_no.window_in_use:
            button = "b"
        elif ui.text.waiting_for_button:
            button = "a"
        elif any((task.func.ptr & ~1) == S.SELL_DONE_TASK_PTR for task in ui.tasks.active):
            button = "a"
        elif E.task_active(observation.snapshot, S.SELL_BAG_TASK):
            button = "b"
        elif (
            bag is not None
            and bag.location == S.SELL_BAG_LOCATION
            and bag.bag_open
            and not ui.text.printing
        ):
            # Quantity and confirmation callbacks retain the Bag/ListMenu owner. B cancels
            # either one without committing a sale and eventually restores list input.
            button = "b"

        if button is None:
            actor.advance_frames(2)
            continue
        presses += 1
        if presses > 10:
            raise RuntimeError("sell failure cleanup exceeded its verified input budget")
        actor.tap(button, hold_frames=HOLD, wait_frames=RELEASE)
    raise RuntimeError("sell failure cleanup did not restore the shop prompt")


def _quantity(ctx: StepContext):
    target = int(ctx.args["target_quantity"])
    current = E.quantity(ctx.snapshot)
    if current is None:
        yield _tap("b")
        return
    try:
        while current != target:
            button = "up" if current < target else "down"
            expected = current + (1 if current < target else -1)
            obs = yield _tap(button)
            seen = E.quantity(obs.snapshot)
            if seen != expected:
                raise CursorMismatch(f"shop quantity read {seen}, expected {expected}")
            current = seen
        yield _tap("a")
        yield WaitUntil(
            lambda observation: (
                (result := E.classify(observation.snapshot)) is not None
                and result.scene_id in {R.BUY_CONFIRM, R.SELL_CONFIRM, R.BUY_DONE, R.SELL_DONE}
            ),
            max_frames=180,
            failure_reason="shop quantity selection did not advance",
        )
    except CursorMismatch:
        yield _tap("b")


def _confirm(ctx: StepContext):
    try:
        yield from _move_cursor(ctx, 0)
        observation = yield WaitUntil(
            lambda current: (
                E.task_active(current.snapshot, S.PURCHASE_DONE_TASK)
                or E.task_active(current.snapshot, S.BUY_TASK)
            ),
            max_frames=360,
            failure_reason="shop purchase result did not become input-ready",
        )
        if E.task_active(observation.snapshot, S.PURCHASE_DONE_TASK):
            yield _tap("a")
            yield WaitUntil(
                lambda current: E.task_active(current.snapshot, S.BUY_TASK),
                max_frames=120,
                failure_reason="shop list did not regain input ownership",
            )
    except CursorMismatch:
        yield _tap("b")


def _confirm_no(ctx: StepContext):
    try:
        yield from _move_cursor(ctx, 1)
    except CursorMismatch:
        yield _tap("b")


def _done_continue(ctx: StepContext):
    if E.task_active(ctx.snapshot, S.RETURN_TASK):
        yield WaitUntil(
            lambda observation: E.task_active(observation.snapshot, S.SHOP_TASK),
            max_frames=360,
            failure_reason="shop prompt did not become input-ready",
        )
    yield _tap("a")
    yield WaitUntil(
        lambda observation: (
            (result := E.classify(observation.snapshot)) is not None
            and result.scene_id == R.BUY_LIST
            and not result.busy
        ),
        max_frames=180,
    )


def _done_quit(ctx: StepContext):
    if E.task_active(ctx.snapshot, S.RETURN_TASK):
        yield WaitUntil(
            lambda observation: E.task_active(observation.snapshot, S.SHOP_TASK),
            max_frames=360,
            failure_reason="shop prompt did not become input-ready",
        )
    yield _tap("b")


def _sell_item(ctx: StepContext):
    """Move the RAM-owned bag cursor to the requested item and open it."""
    target_pocket = int(ctx.args["pocket_index"])
    target = int(ctx.args["item_index"])
    snapshot = ctx.snapshot
    for _ in range(4):
        bag = snapshot.ui.menu.bag if snapshot.ui and snapshot.ui.valid else None
        if bag is None:
            raise CursorMismatch("sell bag cursor is unreadable")
        if bag.pocket == target_pocket:
            break
        button = "right" if bag.pocket < target_pocket else "left"
        expected = bag.pocket + (1 if button == "right" else -1)
        obs = yield from _press(snapshot, button)
        snapshot = obs.snapshot
        current = snapshot.ui.menu.bag if snapshot.ui and snapshot.ui.valid else None
        if current is None or current.pocket != expected:
            seen = None if current is None else current.pocket
            raise CursorMismatch(f"sell bag pocket read {seen}, expected {expected}")
    else:
        raise CursorMismatch(f"sell bag pocket {target_pocket} is not reachable")

    pocket_name = ("items", "key_items", "poke_balls")[target_pocket]
    pocket = next((p for p in snapshot.bag if p.pocket == pocket_name), None)
    if pocket is None or target >= len(pocket.items):
        raise CursorMismatch(f"sell item index {target} is not present in {pocket_name}")
    if pocket.items[target].item_id != int(ctx.args["item_ref"]):
        raise CursorMismatch("sell bag contents changed before cursor navigation")

    for _ in range(len(pocket.items) + 3):
        ui = snapshot.ui
        bag = ui.menu.bag if ui is not None and ui.valid else None
        listing = ui.menu.list_menu if ui is not None and ui.valid else None
        if bag is None or bag.pocket != target_pocket:
            raise CursorMismatch("sell bag pocket changed during cursor navigation")
        if listing is not None and listing.window_in_use:
            cursor = listing.scroll_offset + listing.cursor_row
        elif target_pocket < len(bag.cursor_pos) and target_pocket < len(bag.items_above):
            cursor = bag.cursor_pos[target_pocket] + bag.items_above[target_pocket]
        else:
            raise CursorMismatch("sell bag list cursor is unreadable")
        if cursor == target:
            yield from _press(snapshot, "a")
            yield WaitUntil(
                lambda observation: (
                    (result := E.classify(observation.snapshot)) is not None
                    and result.scene_id in {R.SELL_QUANTITY, R.SELL_CONFIRM, R.SELL_DONE}
                ),
                max_frames=240,
                failure_reason="sell item selection did not open quantity input",
            )
            return
        button = "down" if cursor < target else "up"
        expected = cursor + (1 if button == "down" else -1)
        obs = yield from _press(snapshot, button)
        snapshot = obs.snapshot
        ui = snapshot.ui
        listing = ui.menu.list_menu if ui is not None and ui.valid else None
        if listing is None or not listing.window_in_use:
            raise CursorMismatch("sell bag list disappeared while moving the cursor")
        seen = listing.scroll_offset + listing.cursor_row
        if seen != expected:
            raise CursorMismatch(f"sell bag cursor read {seen}, expected {expected}")
    raise CursorMismatch(f"sell item index {target} is not reachable")


def _sell_quantity(ctx: StepContext):
    target = int(ctx.args["target_quantity"])
    current = E.quantity(ctx.snapshot)
    snapshot = ctx.snapshot
    if current is None:
        raise CursorMismatch("sell quantity is unreadable")
    while current != target:
        button = "up" if current < target else "down"
        expected = current + (1 if button == "up" else -1)
        observation = yield from _press(snapshot, button)
        snapshot = observation.snapshot
        seen = E.quantity(snapshot)
        if seen != expected:
            raise CursorMismatch(f"sell quantity read {seen}, expected {expected}")
        current = seen
    yield from _press(snapshot, "a")
    yield WaitUntil(
        lambda observation: (
            (result := E.classify(observation.snapshot)) is not None
            and result.scene_id == R.SELL_CONFIRM
        ),
        max_frames=240,
        failure_reason="sell quantity selection did not reach confirmation",
    )


def _sell_confirm(ctx: StepContext):
    snapshot = ctx.snapshot
    current = _menu_cursor(snapshot)
    if current is None:
        raise CursorMismatch("sell confirmation cursor is unreadable")
    while current != 0:
        observation = yield from _press(snapshot, "up")
        snapshot = observation.snapshot
        seen = _menu_cursor(snapshot)
        if seen != current - 1:
            raise CursorMismatch(f"sell confirmation cursor read {seen}, expected {current - 1}")
        current = seen
    yield from _press(snapshot, "a")

    before_money = ctx.snapshot.player.money if ctx.snapshot.player is not None else None
    item_id = int(ctx.args["item_ref"])
    quantity = int(ctx.args["target_quantity"])
    unit_price = int(ctx.args["unit_price"])
    before_count = sum(
        item.quantity
        for pocket in ctx.snapshot.bag
        for item in pocket.items
        if item.item_id == item_id
    )

    def committed(observation) -> bool:
        after = observation.snapshot
        if before_money is None or after.player is None:
            return False
        after_count = sum(
            item.quantity
            for pocket in after.bag
            for item in pocket.items
            if item.item_id == item_id
        )
        result = E.classify(after)
        return (
            after.player.money == before_money + quantity * unit_price
            and after_count == before_count - quantity
            and result is not None
            and result.scene_id == R.SELL_DONE
        )

    yield WaitUntil(
        committed,
        max_frames=360,
        failure_reason="sell confirmation did not commit the exact money and item deltas",
    )


def _sell_confirm_no(ctx: StepContext):
    snapshot = ctx.snapshot
    current = _menu_cursor(snapshot)
    if current is None:
        raise CursorMismatch("sell confirmation cursor is unreadable")
    while current != 1:
        observation = yield from _press(snapshot, "down")
        snapshot = observation.snapshot
        seen = _menu_cursor(snapshot)
        if seen != current + 1:
            raise CursorMismatch(f"sell confirmation cursor read {seen}, expected {current + 1}")
        current = seen
    yield from _press(snapshot, "a")


def _sell_done_continue(ctx: StepContext):
    yield from _press(ctx.snapshot, "a")
    yield WaitUntil(
        lambda observation: (
            (result := E.classify(observation.snapshot)) is not None
            and result.scene_id == R.SELL_LIST
            and not result.busy
        ),
        max_frames=240,
        failure_reason="sold-item message did not return to the sell list",
    )


def _sell_done_quit(ctx: StepContext):
    yield from _press(ctx.snapshot, "a")
    observation = yield WaitUntil(
        lambda current: (
            (result := E.classify(current.snapshot)) is not None
            and result.scene_id == R.SELL_LIST
            and not result.busy
        ),
        max_frames=240,
        failure_reason="sold-item message did not return to the sell list",
    )
    yield from _press(observation.snapshot, "b")
    yield WaitUntil(
        lambda current: (
            (result := E.classify(current.snapshot)) is not None
            and result.scene_id == R.SHOP_PROMPT
            and not result.busy
        ),
        max_frames=360,
        failure_reason="sell quit did not return to the shop prompt",
    )


def executor_specs() -> tuple[ExecutorSpec, ...]:
    return (
        ExecutorSpec(
            R.REF_MAIN_CURSOR,
            ExecutionKind.MACRO,
            _main_cursor,
            args={"target": Arg(int, min=0, max=2)},
            max_inputs=4,
            max_frames=240,
        ),
        ExecutorSpec(
            R.REF_PROMPT_CURSOR,
            ExecutionKind.MACRO,
            _prompt_cursor,
            args={"target": Arg(int, choices=(0, 1, 2))},
            allowed_scenes=frozenset(
                {R.SHOP_PROMPT, "field.map_transition", "scene.unknown", R.BUY_LIST}
            ),
            max_inputs=4,
            max_frames=480,
            macro_manages_busy=True,
        ),
        ExecutorSpec(
            R.REF_LIST_ITEM,
            ExecutionKind.MACRO,
            _list_item,
            args={"item_id": Arg(int, min=1, max=374), "item_index": Arg(int, min=0, max=30)},
            max_inputs=32,
            max_frames=1600,
        ),
        ExecutorSpec(R.REF_LIST_BACK, ExecutionKind.ATOMIC, _back),
        ExecutorSpec(
            R.REF_SELL_LIST_BACK,
            ExecutionKind.MACRO,
            _sell_list_back,
            allowed_scenes=frozenset({R.SELL_LIST, R.SELL_DONE, "scene.unknown"}),
            max_inputs=1,
            max_frames=480,
            failure_cleanup=_sell_failure_cleanup,
            macro_manages_busy=True,
        ),
        ExecutorSpec(
            R.REF_QUANTITY,
            ExecutionKind.MACRO,
            _quantity,
            args={
                "item_ref": Arg(int, min=1, max=374),
                "target_quantity": Arg(int, min=1, max=99),
                "unit_cost": Arg(int, min=1, max=999999),
            },
            max_inputs=100,
            max_frames=2400,
        ),
        ExecutorSpec(R.REF_QUANTITY_BACK, ExecutionKind.ATOMIC, _back),
        ExecutorSpec(
            R.REF_CONFIRM,
            ExecutionKind.MACRO,
            _confirm,
            args={
                "item_ref": Arg(int, min=1, max=374),
                "target_quantity": Arg(int, min=1, max=99),
                "unit_cost": Arg(int, min=1, max=999999),
            },
            allowed_scenes=frozenset(
                {R.BUY_CONFIRM, R.BUY_DONE, R.BUY_LIST, "dialog.printing", "scene.unknown"}
            ),
            max_inputs=4,
            max_frames=720,
            macro_manages_busy=True,
        ),
        ExecutorSpec(
            R.REF_CONFIRM_NO,
            ExecutionKind.MACRO,
            _confirm_no,
            args={"target": Arg(int, choices=(1,))},
            max_inputs=3,
            max_frames=180,
        ),
        ExecutorSpec(
            R.REF_DONE_CONTINUE,
            ExecutionKind.MACRO,
            _done_continue,
            allowed_scenes=frozenset(
                {
                    R.BUY_DONE,
                    R.SHOP_PROMPT,
                    R.BUY_LIST,
                    "dialog.printing",
                    "field.map_transition",
                    "scene.unknown",
                }
            ),
            max_inputs=1,
            max_frames=720,
            macro_manages_busy=True,
        ),
        ExecutorSpec(
            R.REF_DONE_QUIT,
            ExecutionKind.MACRO,
            _done_quit,
            allowed_scenes=frozenset(
                {
                    R.BUY_DONE,
                    R.SHOP_PROMPT,
                    "dialog.printing",
                    "field.map_transition",
                    "scene.unknown",
                }
            ),
            max_inputs=1,
            max_frames=600,
            macro_manages_busy=True,
        ),
        ExecutorSpec(
            R.REF_SELL_ITEM,
            ExecutionKind.MACRO,
            _sell_item,
            args={
                "item_ref": Arg(int, min=1, max=374),
                "pocket_index": Arg(int, choices=(0, 1, 2)),
                "item_index": Arg(int, min=0, max=42),
            },
            allowed_scenes=frozenset({R.SELL_LIST, R.SELL_QUANTITY, R.SELL_CONFIRM, R.SELL_DONE}),
            max_inputs=48,
            max_frames=2200,
            failure_cleanup=_sell_failure_cleanup,
            macro_manages_busy=True,
        ),
        ExecutorSpec(
            R.REF_SELL_QUANTITY,
            ExecutionKind.MACRO,
            _sell_quantity,
            args={
                "item_ref": Arg(int, min=1, max=374),
                "target_quantity": Arg(int, min=1, max=99),
                "unit_price": Arg(int, min=1, max=999999),
            },
            allowed_scenes=frozenset({R.SELL_QUANTITY, R.SELL_CONFIRM, R.SELL_DONE}),
            max_inputs=100,
            max_frames=2400,
            failure_cleanup=_sell_failure_cleanup,
            macro_manages_busy=True,
        ),
        ExecutorSpec(
            R.REF_SELL_CONFIRM,
            ExecutionKind.MACRO,
            _sell_confirm,
            args={
                "item_ref": Arg(int, min=1, max=374),
                "target_quantity": Arg(int, min=1, max=99),
                "unit_price": Arg(int, min=1, max=999999),
            },
            allowed_scenes=frozenset({R.SELL_CONFIRM, R.SELL_LIST, R.SELL_DONE}),
            max_inputs=4,
            max_frames=720,
            failure_cleanup=_sell_failure_cleanup,
            macro_manages_busy=True,
        ),
        ExecutorSpec(
            R.REF_SELL_CONFIRM_NO,
            ExecutionKind.MACRO,
            _sell_confirm_no,
            args={"target": Arg(int, choices=(1,))},
            max_inputs=3,
            max_frames=180,
            failure_cleanup=_sell_failure_cleanup,
        ),
        ExecutorSpec(
            R.REF_SELL_DONE_CONTINUE,
            ExecutionKind.MACRO,
            _sell_done_continue,
            allowed_scenes=frozenset({R.SELL_DONE, R.SELL_LIST}),
            max_inputs=1,
            max_frames=480,
            failure_cleanup=_sell_failure_cleanup,
            macro_manages_busy=True,
        ),
        ExecutorSpec(
            R.REF_SELL_DONE_QUIT,
            ExecutionKind.MACRO,
            _sell_done_quit,
            allowed_scenes=frozenset({R.SELL_DONE, R.SELL_LIST, R.SHOP_PROMPT}),
            max_inputs=2,
            max_frames=720,
            failure_cleanup=_sell_failure_cleanup,
            macro_manages_busy=True,
        ),
    )
