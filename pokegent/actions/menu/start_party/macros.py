"""Generic MenuModel cursor execution for START and field-party choices."""

from __future__ import annotations

from pokegent.actions._exec.menu_executor import select_menu_entry
from pokegent.actions._exec.models import ExecutionKind, Interrupt, Tap, WaitUntil
from pokegent.actions._exec.registry import Arg, ExecutorSpec, StepContext
from pokegent.actions.field.macros import _field_free_after_bag_failure
from pokegent.actions.menu.start_party import refs as R
from pokegent.games.gen3.firered.reader.menu_decoders import (
    decode_party_action,
    decode_party_menu,
    decode_party_reorder,
    decode_start_menu,
)
from pokegent.pipeline import Observation


def _bag_item_quantity(snapshot, item_id: int) -> int:
    return sum(
        item.quantity
        for pocket in snapshot.bag
        for item in pocket.items
        if item.item_id == item_id
    )


#: pret/pokefirered 037335f4 include/constants/items.h 121..132; mail_data.c ItemIsMail is
#: true exactly for those ids and false otherwise.  Public game facts, not table reads.
MAIL_ITEM_IDS = frozenset(range(121, 133))


def _swap_binding(
    snapshot,
    *,
    selected_slot,
    target_personality,
    old_held,
    item_id,
    bag_count,
    pre_action,
) -> bool:
    """Prove the prompt is the swap question for THIS member/item from owned transition facts."""
    if not 1 <= item_id <= 65535 or item_id in MAIL_ITEM_IDS:
        return False
    if old_held in (0, item_id) or old_held in MAIL_ITEM_IDS:
        return False
    if not _party_prompt_candidate(snapshot):
        return False
    ui = snapshot.ui
    party, menu, yes_no, text = ui.menu.party, ui.menu.menu, ui.menu.yes_no, ui.text
    callback = ui.main.callback2
    mon = next((member for member in snapshot.party if member.slot == selected_slot), None)
    script = ui.script
    if callback is None or callback.name != "CB2_UpdatePartyMenu" or callback.verified is not True:
        return False
    if party.slot_id != selected_slot or party.item_id != item_id:
        return False
    # The source preserves gPartyMenu.action across CursorCB_Give/CB2_GiveHoldItem.  Require the
    # same proven pre-give action, restricted to the two source-owned give entries.
    if pre_action not in ("choose_mon", "give_item") or party.action_name != pre_action:
        return False
    if mon is None or mon.personality != target_personality or mon.held_item != old_held:
        return False
    if _bag_item_quantity(snapshot, item_id) != bag_count:
        return False
    if script is not None and (script.running is True or script.controls_locked is True):
        return False
    if text is None or text.valid is not True:
        return False
    if yes_no is None or yes_no.window_in_use is not True or menu.cursor_pos != 0:
        return False
    if menu.window_id != yes_no.window_id:
        return False
    return any(window.window_id == yes_no.window_id for window in text.windows)


def _party_prompt_candidate(snapshot) -> bool:
    """A live two-entry party prompt on the give return path, used only to STOP.

    Pinned ``party_menu.c`` opens ``Task_SwitchItemsYesNo`` only when the target already holds an
    item.  The current profile does not prove that task symbol, so this structural shape cannot
    prove that YES means swap: it may only withhold input.  (The settled prompt's text printer is
    deliberately inactive, so text activity is not a valid discriminator either.)
    """
    ui = snapshot.ui
    if ui is None or not ui.valid or ui.main.in_battle is not False:
        return False
    party = ui.menu.party
    menu = ui.menu.menu
    if party is None or party.active_hint is not True or getattr(party, "menu_type", None) != 0:
        return False
    if ui.field is None or ui.field.fade_active is not False:
        return False
    if any(task.func.name == "Task_HandleSelectionMenuInput" for task in ui.tasks.active):
        return False
    if ui.menu.bag is not None and ui.menu.bag.bag_open:
        return False
    listing = ui.menu.list_menu
    if listing is not None and listing.window_in_use:
        return False
    return bool(
        menu is not None
        and menu.window_in_use
        and menu.consistent
        and menu.min_cursor_pos == 0
        and menu.max_cursor_pos == 1
        and menu.cursor_pos in (0, 1)
    )


def _party_input_ready(current) -> bool:
    """A settled party menu is the verifier's turn, not a reason to send more input."""
    ui = current.snapshot.ui
    party = ui.menu.party if ui is not None and ui.valid else None
    return bool(
        current.scene.busy is False
        and current.scene.scene_id
        in (R.PARTY_LIST, R.PARTY_ACTION, R.PARTY_REORDER, R.PARTY_HELD_ITEM)
        and party is not None
        and party.active_hint is True
        and ui.field is not None
        and ui.field.fade_active is False
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
        target = next((mon for mon in ctx.snapshot.party if mon.slot == selected_slot), None)
        if target is None:
            raise RuntimeError("party member disappeared before giving the item")
        target_personality = target.personality
        old_held = target.held_item
        pre_action = getattr(ctx.snapshot.ui.menu.party, "action_name", None)

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
        observation = yield WaitUntil(
            bag_ready, max_frames=120, failure_reason="bag declined input before the item row"
        )
        bag_count = _bag_item_quantity(observation.snapshot, item_id)
        # The item row is the give input: ``Task_ItemContext_FieldGive`` closes the bag, then the
        # game gives directly (empty slot) or asks the two-entry swap question.  There is no bag
        # context menu and no second bag confirmation.
        observation = yield Tap("a", hold_frames=4, wait_frames=20)

        def give_applied(current) -> bool:
            if _bag_item_quantity(current.snapshot, item_id) != bag_count - 1:
                return False
            return any(
                mon.personality == target_personality and mon.held_item == item_id
                for mon in current.snapshot.party
            )

        def prompt_candidate(current) -> bool:
            return _party_prompt_candidate(current.snapshot)

        def swap_bound(current) -> bool:
            return _swap_binding(
                current.snapshot,
                selected_slot=selected_slot,
                target_personality=target_personality,
                old_held=old_held,
                item_id=item_id,
                bag_count=bag_count,
                pre_action=pre_action,
            )

        observation = yield WaitUntil(
            lambda current: give_applied(current)
            or swap_bound(current)
            or prompt_candidate(current)
            or _party_input_ready(current),
            max_frames=600,
            failure_reason="party give did not reach a transfer or party menu",
        )
        if not give_applied(observation) and swap_bound(observation):
            observation = yield Tap("a", hold_frames=4, wait_frames=20)
            observation = yield WaitUntil(
                lambda current: give_applied(current) or _party_input_ready(current),
                max_frames=600,
                failure_reason="swap YES did not return to the party menu",
            )
        elif not give_applied(observation) and prompt_candidate(observation):
            # No proven swap binding: zero confirmation.  Every identity premise (callback, slot,
            # PID, old/current item, bag count, YES/NO window) must hold before the single YES.
            yield Interrupt(
                "party give reached an unverified swap prompt; refusing YES without a proven "
                "prompt/member/item binding"
            )
        return observation
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
            R.REF_GIVE,
            ExecutionKind.MACRO,
            select,
            args={"item_id": Arg(int, required=False, min=1, max=65535)},
            # Give owns the bag close -> party transfer return.  ``macro_manages_busy`` keeps the
            # input-waiting swap prompt out of the generic post-step busy settle, which can never
            # clear without an answer.  No failure cleanup: it would answer an unknown party
            # prompt with a blind A, so a failed give leaves the menu open for a safe pause.
            allowed_scenes=frozenset(
                {
                    R.START,
                    R.PARTY_LIST,
                    R.PARTY_ACTION,
                    R.PARTY_REORDER,
                    R.PARTY_HELD_ITEM,
                    "menu.open",
                    "scene.unknown",
                }
            ),
            max_inputs=15,
            max_frames=2400,
            settle_frames=1800,
            settle_wall_s=90.0,
            macro_manages_busy=True,
        ),
        ExecutorSpec(
            R.REF_SELECT,
            ExecutionKind.DETERMINISTIC,
            select,
            args={"item_id": Arg(int, required=False, min=1, max=65535)},
            # GIVE is the only selection that opens the bag and its item context menu on the way
            # to the held-item delta; the registered detector names that live menu scene
            # ``menu.open``. Every other selection still verifies its own postcondition.
            allowed_scenes=frozenset(
                {
                    R.START,
                    R.PARTY_LIST,
                    R.PARTY_ACTION,
                    R.PARTY_REORDER,
                    R.PARTY_HELD_ITEM,
                    "menu.open",
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
