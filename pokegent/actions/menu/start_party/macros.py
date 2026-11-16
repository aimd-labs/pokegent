"""Generic MenuModel cursor execution for START and field-party choices."""

from __future__ import annotations

from pokegent.actions._exec.menu_executor import select_menu_entry
from pokegent.actions._exec.models import ExecutionKind, Tap, WaitUntil
from pokegent.actions._exec.registry import Arg, ExecutorSpec, StepContext
from pokegent.actions.field.macros import _field_free_after_bag_failure
from pokegent.actions.menu.start_party import refs as R
from pokegent.games.gen3.firered.reader.menu_decoders import (
    decode_cursor_menu,
    decode_party_action,
    decode_party_menu,
    decode_party_reorder,
    decode_start_menu,
)
from pokegent.pipeline import Observation


def _give_context_menu(snapshot):
    """Decode the post-list bag menu whose first semantic entry must be GIVE."""
    return decode_cursor_menu(
        snapshot,
        surface="bag.context",
        entries=(("bag:give", "GIVE"), ("bag:cancel", "CANCEL")),
    )


def _decoder(scene: str):
    if scene == R.START:
        return decode_start_menu
    if scene == R.PARTY_LIST:
        return decode_party_menu
    if scene == R.PARTY_REORDER:
        return decode_party_reorder
    return decode_party_action


def select(ctx: StepContext):
    scene = ctx.frame.scene_id
    contract = ctx.option.action_contract
    if contract is None or contract.cursor_key is None:
        raise RuntimeError("START/party selection has no cursor key in its action contract")
    stable_key = contract.cursor_key
    observation = Observation(ctx.snapshot, ctx.scene)
    if scene in (R.PARTY_LIST, R.PARTY_REORDER):
        # CB2_UpdatePartyMenu exposes the party cursor task before the fade finishes, but
        # FireRed deliberately ignores keys while that game-owned input gate is active.
        # Waiting here only advances a pure fade; the decider still owns every semantic input.
        observation = yield WaitUntil(
            lambda current: bool(
                current.snapshot.ui is not None
                and current.snapshot.ui.valid
                and current.snapshot.ui.menu.party is not None
                and current.snapshot.ui.menu.party.active_hint is True
                and current.snapshot.ui.field.fade_active is False
            ),
            max_frames=90,
        )
    if scene == R.PARTY_LIST and stable_key == "party:cancel":
        # The ROM's choose-mon list has no physical BACK row; B is its verified close input.
        # Keep BACK in the model so agent always has a way out, but never walk into a synthetic
        # cursor row that the game does not own.
        return (yield Tap("b", hold_frames=4, wait_frames=12))
    observation = yield from select_menu_entry(
        observation,
        stable_key,
        _decoder(scene),
        tap=lambda button: Tap(button, hold_frames=4, wait_frames=12),
        max_moves=12,
        readback_retries=2,
    )
    if scene == R.PARTY_HELD_ITEM and stable_key == "party_action:give":
        item_id = ctx.args.get("item_id")
        if not isinstance(item_id, int):
            raise RuntimeError("give-item action has no verified item id")
        selected_slot = ctx.snapshot.ui.menu.party.slot_id  # type: ignore[union-attr]
        target_personality = next(
            (mon.personality for mon in ctx.snapshot.party if mon.slot == selected_slot), None
        )
        if target_personality is None:
            raise RuntimeError("party member disappeared before giving the item")

        def bag_ready(current):
            ui = current.snapshot.ui
            return bool(
                ui is not None
                and ui.valid
                and ui.menu.bag is not None
                and ui.menu.bag.bag_open
                and ui.menu.bag.active_hint is True
                and ui.menu.list_menu is not None
                and ui.menu.list_menu.window_in_use
            )

        observation = yield WaitUntil(bag_ready, max_frames=180)
        ui = observation.snapshot.ui
        if ui is None or not ui.valid or ui.menu.bag is None:
            raise RuntimeError("bag did not become ready after selecting an item")
        pocket_names = {"items": 0, "key_items": 1, "poke_balls": 2, "tms_hms": 3, "berries": 4}
        pocket_index = next(
            (
                pocket_names[pocket.pocket]
                for pocket in observation.snapshot.bag
                if any(item.item_id == item_id and item.quantity > 0 for item in pocket.items)
            ),
            None,
        )
        if pocket_index is None:
            raise RuntimeError("held item is not available in the verified bag")
        for _ in range(6):
            current = observation.snapshot.ui.menu.bag.pocket  # type: ignore[union-attr]
            if current == pocket_index:
                break
            button = "right" if pocket_index > current else "left"
            observation = yield Tap(button, hold_frames=4, wait_frames=20)
        else:
            raise RuntimeError("bag pocket cursor did not reach the held-item pocket")

        def list_row(current):
            listing = current.snapshot.ui.menu.list_menu  # type: ignore[union-attr]
            if listing is None or not listing.window_in_use:
                return None
            return listing.scroll_offset + listing.cursor_row

        items = next(
            pocket.items
            for pocket in observation.snapshot.bag
            if pocket_names.get(pocket.pocket) == pocket_index
        )
        row = next((index for index, item in enumerate(items) if item.item_id == item_id), None)
        if row is None:
            raise RuntimeError("held item disappeared from the verified bag")
        for _ in range(len(items) + 4):
            current = list_row(observation)
            if current == row:
                break
            if current is None:
                raise RuntimeError("held-item list cursor is unreadable")
            button = "down" if row > current else "up"
            observation = yield Tap(button, hold_frames=4, wait_frames=20)
        else:
            raise RuntimeError("held-item list cursor did not reach the requested item")
        observation = yield Tap("a", hold_frames=4, wait_frames=20)
        context = _give_context_menu(observation.snapshot)
        if context is None or context.selected_key != "bag:give":
            raise RuntimeError("bag item context menu did not land on Give")
        observation = yield Tap("a", hold_frames=4, wait_frames=20)
        observation = yield WaitUntil(
            lambda current: any(
                mon.personality == target_personality and mon.held_item == item_id
                for mon in current.snapshot.party
            ),
            max_frames=240,
        )
        if not any(
            mon.personality == target_personality and mon.held_item == item_id
            for mon in observation.snapshot.party
        ):
            raise RuntimeError("giving the item did not update the party held item")
    if scene != R.PARTY_LIST or stable_key == "party:cancel":
        return observation

    # The first confirmation can land during the choose-mon -> selection-window task
    # handoff. Retry only while RAM proves that the same decoded member remains selected;
    # once action ids appear, the semantic choice has been accepted.
    def action_menu_ready(current) -> bool:
        ui = current.snapshot.ui
        return bool(
            ui is not None
            and ui.valid
            and ui.menu.party is not None
            and ui.menu.party.action_ids
            and any(task.func.name == "Task_HandleSelectionMenuInput" for task in ui.tasks.active)
        )

    def confirmation_retryable(current) -> bool:
        ui = current.snapshot.ui
        model = decode_party_menu(current.snapshot)
        return bool(
            ui is not None
            and ui.valid
            and ui.menu.party is not None
            and ui.field.fade_active is False
            and model is not None
            and model.selected_key == stable_key
            and any(task.func.name == "Task_HandleChooseMonInput" for task in ui.tasks.active)
        )

    for _ in range(2):
        if action_menu_ready(observation):
            return observation
        observation = yield WaitUntil(
            lambda current: action_menu_ready(current) or confirmation_retryable(current),
            max_frames=90,
        )
        if action_menu_ready(observation):
            return observation
        if not confirmation_retryable(observation):
            return observation
        observation = yield Tap("a", hold_frames=4, wait_frames=12)
    return observation


def executor_specs() -> tuple[ExecutorSpec, ...]:
    return (
        ExecutorSpec(
            R.REF_SELECT,
            ExecutionKind.DETERMINISTIC,
            select,
            args={"item_id": Arg(int, required=False, min=1, max=65535)},
            allowed_scenes=frozenset(
                {
                    R.START,
                    R.PARTY_LIST,
                    R.PARTY_ACTION,
                    R.PARTY_REORDER,
                    R.PARTY_HELD_ITEM,
                    "scene.unknown",
                }
            ),
            max_inputs=15,
            max_frames=2400,
            settle_frames=1800,
            settle_wall_s=90.0,
            failure_cleanup=_field_free_after_bag_failure,
        ),
    )
