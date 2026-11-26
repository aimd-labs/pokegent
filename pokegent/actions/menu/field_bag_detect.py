"""Default-off field-bag item-list detector over existing typed UI evidence only."""

from __future__ import annotations

from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.games.gen3.firered.reader.ui_schemas import FnRef, UiEvidence
from pokegent.state.scenes import PriorityClass, SceneResult

PROFILE, BAG_CALLBACK = "firered-us-rev0", "CB2_BagMenuRun"
BAG_INPUT_TASK, LIST_TASK = "Task_BagMenu_HandleInput", "ListMenuDummyTask"
FIELD_LOCATION, NUM_POCKETS = 0, 3


def _owns_bag_callback(callback: FnRef | None) -> bool:
    """Only a profile-named, profile-verified bag runner owns the item list."""
    return bool(callback and callback.verified and callback.name == BAG_CALLBACK)


def _listing_ok(ui: UiEvidence) -> bool:
    listing = ui.menu.list_menu
    owner = next((task for task in ui.tasks.active if task.slot == listing.task_slot), None) \
        if listing else None
    return bool(
        listing
        and listing.window_in_use
        and listing.max_showed > 0
        and listing.total_items > 0
        and listing.cursor_row < listing.max_showed
        and listing.scroll_offset + listing.cursor_row < listing.total_items
        and owner
        and owner.func.verified
        and owner.func.name == LIST_TASK
    )


class FieldBagDetector:
    """Claims ``bag.items`` only from a complete, input-ready field item-list frame."""

    name = "field-bag:bag.items"
    priority = PriorityClass.MENU
    specificity = 60
    claimed_scenes = frozenset({"bag.items"})

    def __init__(self, enabled: bool = False) -> None:
        if type(enabled) is not bool:
            raise TypeError("enabled must be a bool")
        self.enabled = enabled

    def detect(self, snapshot: StateSnapshot) -> SceneResult | None:
        if not self.enabled or not snapshot.usable or snapshot.rom_profile != PROFILE:
            return None
        ui = snapshot.ui
        if ui is None or not ui.valid:
            return None
        main, tasks, menu, field = ui.main, ui.tasks, ui.menu, ui.field
        if not (main.valid and tasks.valid and menu.valid and field.valid):
            return None
        if main.in_battle is not False or field.fade_active is not False:
            return None
        if not _owns_bag_callback(main.callback2):
            return None
        bag = menu.bag
        if bag is None or not bag.bag_open or bag.location != FIELD_LOCATION:
            return None
        above, cursor = bag.items_above, bag.cursor_pos
        if (
            not 0 <= bag.pocket < NUM_POCKETS
            or len(above) != NUM_POCKETS
            or len(cursor) != NUM_POCKETS
            or any(not isinstance(value, int) or value < 0 for value in (*above, *cursor))
        ):
            return None
        has_input = any(
            task.func.name == BAG_INPUT_TASK and task.func.verified for task in tasks.active
        )
        if not has_input or not _listing_ok(ui):
            return None
        listing = menu.list_menu  # bag cursor is relative; items_above is scroll
        if (
            cursor[bag.pocket] >= listing.max_showed
            or above[bag.pocket] + cursor[bag.pocket] >= listing.total_items
        ):
            return None
        return SceneResult(
            scene_id="bag.items",
            confidence=0.95,
            evidence={
                "location": bag.location,
                "pocket": bag.pocket,
                "cursor": bag.cursor_pos,
                "items_above": bag.items_above,
                "list_slot": menu.list_menu.task_slot,
            },
        )


def build_detectors(enabled: bool = False) -> tuple[FieldBagDetector, ...]:
    """No detector unless explicitly switched on; no app registration happens here."""
    detector = FieldBagDetector(enabled=enabled)  # exact-bool guard even when falsey
    return (detector,) if enabled else ()
