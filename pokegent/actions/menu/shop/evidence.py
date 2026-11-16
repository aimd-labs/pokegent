"""Pure shop evidence extracted from the shared UI block."""

from __future__ import annotations

from typing import Any

from pokegent.actions.menu.shop import refs as R
from pokegent.actions.menu.shop import symbols as S
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.games.gen3.firered.reader.ui_schemas import TaskInfo, UiEvidence
from pokegent.state.scenes import PriorityClass, SceneResult


def _task(ui: UiEvidence, name: str) -> TaskInfo | None:
    return next((task for task in ui.tasks.active if task.func.name == name), None)


def _quantity_or_confirm_task(ui: UiEvidence) -> TaskInfo | None:
    task = _task(ui, S.QUANTITY_TASK)
    if task is not None:
        return task
    return next(
        (task for task in ui.tasks.active if task.func.ptr == (S.CONFIRM_TASK_PTR | 1)),
        None,
    )


def _sell_callback_active(ui: UiEvidence) -> bool:
    return ui.callback_id == S.SELL_CALLBACK


def _sell_setup_callback_active(ui: UiEvidence) -> bool:
    return (
        ui.main.callback2 is not None and (ui.main.callback2.ptr & ~1) == S.SELL_SETUP_CALLBACK_PTR
    )


def _selling(ui: UiEvidence) -> bool:
    """Return whether live RAM owns the native SELL flow.

    ``gBagMenuState.location`` persists after the bag closes and is never ownership by itself.
    SELL owns the layout only with its live setup/run callback or native ``Task_SellMenu``.
    """
    bag = ui.menu.bag
    return _task(ui, S.SELL_TASK) is not None or (
        bag is not None
        and bag.location == S.SELL_BAG_LOCATION
        and (_sell_callback_active(ui) or _sell_setup_callback_active(ui))
    )


def _sell_flow_task(ui: UiEvidence) -> TaskInfo | None:
    listing = ui.menu.list_menu
    if not _selling(ui) or listing is None or not _sell_callback_active(ui):
        return None
    return next(
        (
            task
            for task in ui.tasks.active
            if task.slot != listing.task_slot
            and len(task.data) > 8
            and task.data[0] == listing.task_slot
        ),
        None,
    )


def _sell_quantity_task(ui: UiEvidence) -> TaskInfo | None:
    bag = ui.menu.bag
    task = _sell_flow_task(ui)
    if (
        bag is None
        or not bag.bag_open
        or task is None
        or task.func.name == S.SELL_BAG_TASK
        or task.data[8] <= 0
    ):
        return None
    return task


def _base(ui: UiEvidence) -> dict[str, Any]:
    return {
        # Never expose an unresolved ROM pointer to the decider. Transition callbacks are
        # intentionally allowed to remain unnamed while the shop task/list proves ownership.
        "callback": ui.main.callback2.name if ui.main.callback2 is not None else None,
    }


def task_data(snapshot: StateSnapshot, name: str) -> tuple[int, ...] | None:
    ui = snapshot.ui
    if ui is None or not ui.valid:
        return None
    task = _task(ui, name)
    if task is None and name == S.QUANTITY_TASK:
        task = _quantity_or_confirm_task(ui)
        if task is None:
            task = _sell_quantity_task(ui)
    if task is None and name == S.QUANTITY_TASK and _selling(ui):
        # The sell confirmation task is a private shop function in this ROM and is
        # not in the shared symbol catalogue. Its data layout is the same native
        # quantity record; the shop location and live yes/no window are the proof.
        task = _sell_flow_task(ui)
    return task.data if task else None


def task_active(snapshot: StateSnapshot, name: str) -> bool:
    ui = snapshot.ui
    return bool(ui is not None and ui.valid and _task(ui, name) is not None)


def selected_item(snapshot: StateSnapshot) -> int | None:
    ui = snapshot.ui
    if ui is not None and ui.valid and ui.menu.bag is not None and _selling(ui):
        bag = ui.menu.bag
        data = task_data(snapshot, S.QUANTITY_TASK)
        if data is not None and len(data) > S.ITEM_TASK_DATA:
            pocket = next(
                (
                    p
                    for p in snapshot.bag
                    if p.pocket == ("items", "key_items", "poke_balls")[bag.pocket]
                ),
                None,
            )
            index = int(data[S.QUANTITY_TASK_DATA])
            if pocket is not None and 0 <= index < len(pocket.items):
                return pocket.items[index].item_id
    data = task_data(snapshot, S.QUANTITY_TASK)
    if data is None or len(data) <= S.ITEM_TASK_DATA:
        return None
    value = int(data[S.ITEM_TASK_DATA])
    return value if value > 0 else None


def quantity(snapshot: StateSnapshot) -> int | None:
    ui = snapshot.ui
    if ui is not None and ui.valid and _selling(ui):
        data = task_data(snapshot, S.QUANTITY_TASK)
        if data is not None and len(data) > 8:
            value = int(data[8])
            return value if value > 0 else None
    data = task_data(snapshot, S.QUANTITY_TASK)
    if data is None or len(data) <= S.QUANTITY_TASK_DATA:
        return None
    value = int(data[S.QUANTITY_TASK_DATA])
    return value if value > 0 else None


def _ready(snapshot: StateSnapshot) -> tuple[UiEvidence, dict[str, Any]] | None:
    if not snapshot.usable or snapshot.ui is None or not snapshot.ui.valid:
        return None
    ui = snapshot.ui
    if ui.main.in_battle is not False or not ui.tasks.valid or not ui.menu.valid:
        return None
    return ui, _base(ui)


def classify(snapshot: StateSnapshot) -> SceneResult | None:
    ready = _ready(snapshot)
    if ready is None:
        return None
    ui, ev = ready
    selling = _selling(ui)
    sell_callback = _sell_callback_active(ui)
    sell_setup_callback = _sell_setup_callback_active(ui)
    sell_bag_transition = (
        selling
        and (
            ui.menu.list_menu is None
            or (_task(ui, S.SELL_BAG_TASK) is not None and not sell_callback)
        )
        and (_task(ui, S.SELL_BAG_TASK) is not None or sell_setup_callback)
    )
    sell_context_transition = selling and any(
        (task.func.ptr & ~1) == S.SELL_CONTEXT_TASK_PTR for task in ui.tasks.active
    )
    sell_flow_task = _sell_flow_task(ui) if selling else None
    # CB2_BagMenuRun also owns the ordinary field bag; _selling is the shared ownership
    # gate that prevents a retained shop location from claiming this list or task layout.
    shop_list_callback = ui.callback_id == S.BUY_CALLBACK or (selling and sell_callback)
    if sell_bag_transition or sell_context_transition:
        ev["phase"] = "sell_list"
        return SceneResult(
            scene_id=R.SELL_LIST,
            confidence=0.8,
            evidence=ev,
            detector="shop.evidence",
            busy=True,
        )
    # The clerk's opening menu is created while the field callback still owns the screen. It
    # uses the native shop task and sMenu, but not CB2_BuyMenu yet.
    menu = ui.menu.menu
    if (
        _task(ui, S.SHOP_TASK) is not None
        and ui.callback_id != S.BUY_CALLBACK
        and menu is not None
        and menu.window_in_use
        and menu.consistent
        and menu.min_cursor_pos == 0
        and menu.max_cursor_pos == 2
    ):
        ev["phase"] = "prompt"
        return SceneResult(
            scene_id=R.SHOP_PROMPT,
            confidence=0.95,
            evidence=ev,
            detector="shop.evidence",
        )
    yes_no = ui.menu.yes_no
    if (
        selling
        and yes_no is not None
        and yes_no.window_in_use
        and menu is not None
        and menu.consistent
        and menu.max_cursor_pos == 1
    ):
        ev["phase"] = "confirm"
        return SceneResult(
            scene_id=R.SELL_CONFIRM,
            confidence=0.95,
            evidence=ev,
            detector="shop.evidence",
        )
    if sell_flow_task is not None and (sell_flow_task.func.ptr & ~1) == S.SELL_DONE_TASK_PTR:
        ev["phase"] = "sale_done"
        return SceneResult(
            scene_id=R.SELL_DONE,
            confidence=0.95,
            evidence=ev,
            detector="shop.evidence",
        )
    if selling and sell_flow_task is not None and ui.text.printing:
        ev["phase"] = "sell_transition"
        return SceneResult(
            scene_id=R.SELL_LIST,
            confidence=0.9,
            evidence=ev,
            detector="shop.evidence",
            busy=True,
        )
    if _task(ui, S.QUANTITY_TASK) is not None or _sell_quantity_task(ui) is not None:
        ev.update(
            {
                "phase": "quantity",
                "item_id": selected_item(snapshot),
                "quantity": quantity(snapshot),
            }
        )
        return SceneResult(
            scene_id=R.SELL_QUANTITY if selling else R.BUY_QUANTITY,
            confidence=0.95,
            evidence=ev,
            detector="shop.evidence",
        )
    if _task(ui, S.RETURN_TASK) is not None:
        ev["phase"] = "return_to_shop"
        return SceneResult(
            scene_id=R.SELL_DONE if selling else R.BUY_DONE,
            confidence=0.9,
            evidence=ev,
            detector="shop.evidence",
        )
    if (
        shop_list_callback
        and yes_no is not None
        and yes_no.window_in_use
        and menu is not None
        and menu.consistent
        and menu.max_cursor_pos == 1
    ):
        ev["phase"] = "confirm"
        return SceneResult(
            scene_id=R.SELL_CONFIRM if selling else R.BUY_CONFIRM,
            confidence=0.9,
            evidence=ev,
            detector="shop.evidence",
        )
    if ui.callback_id == S.BUY_CALLBACK and _task(ui, S.PURCHASE_DONE_TASK) is not None:
        ev["phase"] = "purchase_done"
        return SceneResult(
            scene_id=R.SELL_DONE if selling else R.BUY_DONE,
            confidence=0.9,
            evidence=ev,
            detector="shop.evidence",
        )
    if (
        shop_list_callback
        and _task(ui, S.BUY_TASK) is None
        and _task(ui, S.MESSAGE_TASK) is not None
    ):
        # DisplayMessageAndContinueTask temporarily replaces the owning shop task while it
        # prints both the quantity prompt and the purchase receipt.  It accepts no menu input;
        # retain list ownership as a busy handoff until the callback installs the next exact
        # shop task.
        ev["phase"] = "buy_list"
        lm = ui.menu.list_menu
        if lm is not None and lm.window_in_use:
            ev.update(
                {
                    "total_items": lm.total_items,
                    "max_showed": lm.max_showed,
                    "selected_row": lm.cursor_row,
                    "scroll_offset": lm.scroll_offset,
                    "selected_index": lm.cursor_row + lm.scroll_offset,
                }
            )
        return SceneResult(
            scene_id=R.SELL_LIST if selling else R.BUY_LIST,
            confidence=0.9,
            evidence=ev,
            detector="shop.evidence",
            busy=True,
        )
    if ui.callback_id == S.BUY_CALLBACK and _task(ui, S.EXIT_BUY_TASK) is not None:
        ev["phase"] = "buy_list"
        return SceneResult(
            scene_id=R.SELL_LIST if selling else R.BUY_LIST,
            confidence=0.95,
            evidence=ev,
            detector="shop.evidence",
            busy=True,
        )
    lm = ui.menu.list_menu
    if (
        # The list task/window are installed before CB2_BuyMenu takes ownership.  During that
        # handoff Task_BuyMenu and the ListMenu are visible, but no callback runs
        # ListMenu_ProcessInput yet; advertising the list at that point loses the first key.
        # After a purchase the callback and ListMenu can remain visible while the return task
        # hands control back.  Task_BuyMenu is the RAM-side ownership marker that actually calls
        # ListMenu_ProcessInput for this frame; classify the handoff as busy until it appears.
        shop_list_callback
        and (_task(ui, S.BUY_TASK) is not None or _task(ui, S.SELL_BAG_TASK) is not None)
        and lm is not None
        and lm.window_in_use
    ):
        ev.update(
            {
                "phase": "sell_list" if selling else "buy_list",
                "total_items": lm.total_items,
                "max_showed": lm.max_showed,
                "selected_row": lm.cursor_row,
                "scroll_offset": lm.scroll_offset,
                "selected_index": lm.cursor_row + lm.scroll_offset,
            }
        )
        # The list task exists while the item names are still being printed.
        # Let the runner settle without sending a cursor input in that window.
        return SceneResult(
            scene_id=R.SELL_LIST if selling else R.BUY_LIST,
            confidence=0.95,
            evidence=ev,
            detector="shop.evidence",
            # Task_BuyMenu calls ListMenu_ProcessInput only while the buy-menu fade is
            # inactive (FireRed src/shop.c:820-825). The list and callback are already
            # visible during that fade, so classifying it as ready loses the first key.
            busy=bool(ui.text.printing or ui.field.fade_active is not False),
        )
    if _task(ui, S.SHOP_TASK) is not None and menu is not None and menu.window_in_use:
        ev["phase"] = "main"
        return SceneResult(scene_id=R.MAIN, confidence=0.9, evidence=ev, detector="shop.evidence")
    return None


def detector(scene_id: str, priority: PriorityClass):
    class _Detector:
        name = f"shop.{scene_id.rsplit('.', 1)[-1]}"
        specificity = 2
        claimed_scenes = frozenset({scene_id})

        def detect(self, snapshot: StateSnapshot) -> SceneResult | None:
            result = classify(snapshot)
            return result if result is not None and result.scene_id == scene_id else None

    _Detector.priority = priority
    return _Detector()
