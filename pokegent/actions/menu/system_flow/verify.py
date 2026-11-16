"""Postconditions of the system_flow family: "the screen moved on" in the cheapest provable form.

Each verifier is a pure predicate on ``VerifyContext`` with its own bounded wait. A press that
changes
nothing is the failure (``E_POSTCONDITION_NOT_MET``); the runner's no-progress counter escalates
repeats.
"""

from __future__ import annotations

from pokegent.actions._exec.registry import VerifierSpec, VerifyContext
from pokegent.actions.menu.dialog import evidence as dialog_evidence
from pokegent.actions.menu.system_flow import evidence as E
from pokegent.actions.menu.system_flow import layouts as L
from pokegent.actions.menu.system_flow import refs as R
from pokegent.games.gen3.moves.learn import move_list_matches
from pokegent.state.scenes import readable


def _moved_on_for(foreign: frozenset[str]):
    def check(v: VerifyContext) -> bool:
        before = E.phase_key(E.classify(v.before, foreign))
        after = E.phase_key(E.classify(v.after, foreign))
        return after != before

    return check


def _foreign_screen_left(v: VerifyContext) -> bool:
    """B must expose a callback owned by a registered family."""
    ui = v.after.ui
    if ui is None or not ui.valid or ui.main.callback2 is None:
        return False
    return ui.main.callback2.name in {
        "CB2_Overworld",
        "CB2_OverworldBasic",
        "CB2_InitPartyMenu",
        "CB2_UpdatePartyMenu",
        "CB2_BagMenuRun",
        "CB2_PokeStorage",
    }


def _input_processed(v: VerifyContext) -> bool:
    """The owning macro proved the intermediate transition; require a readable settled result."""
    return readable(v.after, v.scene) and v.after.snapshot_seq != v.before.snapshot_seq


def _evolution_cancelled(v: VerifyContext) -> bool:
    """The task left the cancel window into the cancel states (or the scene is over)."""
    verdict = E.classify(v.after)
    if verdict is None or verdict.scene_id not in R.EVOLUTION_SCENES:
        return True
    state = verdict.evidence.get("state")
    return bool(
        verdict.evidence.get("was_stopped")
        or (isinstance(state, int) and state >= L.EVOSTATE_CANCEL)
    )


def _quest_log_left(v: VerifyContext) -> bool:
    """``gQuestLogState`` is readable and no longer a playback (2/3). An unreadable UI block
    is NOT proof."""
    ui = v.after.ui
    state = ui.menu.quest_log_state if ui is not None and ui.valid else None
    return state is not None and state not in (2, 3)


def _move_forgotten(v: VerifyContext) -> bool:
    """The party member's complete move list matches the shared learn flow's expectation."""
    party_id = int(v.args["party_id"])
    snap = v.after
    expected = v.args.get("expected_moves")
    if expected is None:
        slot, new = int(v.args["slot"]), int(v.args["new_move"])
        before = tuple(int(move.move_id) for move in v.before.party[party_id].moves)
        expected = tuple(new if index == slot else move_id for index, move_id in enumerate(before))
    return move_list_matches(snap, party_id, expected)


def _pc_storage_changed(v: VerifyContext) -> bool:
    """Verify a transaction from fresh identities, never builder-time box/slot indices."""
    pid = int(v.args["pid"])
    operation = int(v.args["operation"])
    after = v.after
    if not after.usable or after.storage is None:
        return False
    in_party = any(int(mon.personality) == pid for mon in after.party)
    before_party = sum(mon is not None for mon in v.before.party)
    after_party = sum(mon is not None for mon in after.party)
    before_locations = _storage_locations(v.before.storage, pid)
    after_locations = _storage_locations(after.storage, pid)
    if operation == 0:
        # The box write can become visible one or more frames before the party slot write.
        # Require both fresh identity and cardinality so that intermediate frames are not done.
        if len(before_locations) != 1:
            return False
        box, slot = before_locations[0]
        return (
            in_party
            and after_party == before_party + 1
            and after.storage.box_option == 0
            and not after_locations
            and after.storage.cursor_area == 0
            and after.storage.cursor_position == slot
            and after.storage.cursor_settled
        )
    target_box = v.args.get("box")
    return (
        not in_party
        and after_party == before_party - 1
        and len(after_locations) == 1
        and (target_box is None or after_locations[0][0] == int(target_box))
    )


def _pc_storage_mode(v: VerifyContext) -> bool:
    storage = v.after.storage
    operation = {
        "pc_storage:withdraw": 0,
        "pc_storage:deposit": 1,
        "pc_storage:move": 2,
        "pc_storage:move_items": 3,
    }.get(
        str(
            v.args.get("stable_key")
            or (v.option.action_contract.cursor_key if v.option.action_contract else "")
        )
    )
    return bool(
        storage is not None
        and operation is not None
        and storage.box_option == operation
        and v.scene.scene_id == R.PC_MAIN
        and v.scene.evidence.get("menu") == "pc_storage"
    )


def _pc_storage_error_dismissed(v: VerifyContext) -> bool:
    storage = v.after.storage
    verdict = E.classify(v.after)
    return bool(
        storage is not None
        and int(storage.ui_state) == 0
        and verdict is not None
        and verdict.scene_id == R.PC_MAIN
        and verdict.evidence.get("menu") == "pc_storage"
        and verdict.evidence.get("phase") in {"storage", "storage_other"}
    )


def _pc_move_picked(v: VerifyContext) -> bool:
    before, after = v.before.storage, v.after.storage
    if before is None or after is None or after.box_option != 2:
        return False
    box, slot = int(v.args["box"]), int(v.args["slot"])
    return (
        before.get(box, slot) is not None
        and after.get(box, slot) is None
        and after.is_holding_mon is True
        and after.held_mon is not None
        and int(after.held_mon.personality) == int(v.args["pid"])
        and after.held_origin_kind == "box"
        and after.held_origin_box == box
        and after.held_origin_slot == slot
    )


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


def _pc_move_placed(v: VerifyContext) -> bool:
    before, after = v.before.storage, v.after.storage
    if before is None or after is None or after.box_option != 2:
        return False
    if before.is_holding_mon is not True or before.held_mon is None:
        return False
    kind = str(v.args.get("target_kind", "box"))
    box = int(v.args.get("target_box", before.current_box))
    slot = int(v.args["target_slot"])
    carried_pid = int(v.args["pid"])
    target_before = _move_target(v.before, kind, box, slot)
    target_after = _move_target(v.after, kind, box, slot)
    if (
        int(before.held_mon.personality) != carried_pid
        or target_after is None
        or int(target_after.personality) != carried_pid
        or _move_identity_count(v.after, carried_pid) != 1
    ):
        return False
    if target_before is None:
        return after.is_holding_mon is False and after.held_mon is None
    displaced_pid = int(target_before.personality)
    return bool(
        after.is_holding_mon is True
        and after.held_mon is not None
        and int(after.held_mon.personality) == displaced_pid
        and _held_origin_is(after, kind, box, slot)
        and _move_identity_count(v.after, displaced_pid) == 0
    )


def _pc_move_cancelled(v: VerifyContext) -> bool:
    storage = v.after.storage
    ui = v.after.ui
    return bool(
        storage is not None
        and storage.is_holding_mon is False
        and ui is not None
        and dialog_evidence.pc_storage_yes_no_open(ui)
    )


def _pc_move_pick_failure(v: VerifyContext) -> str:
    storage = v.after.storage
    if storage is None:
        return "move_pick_storage_unreadable"
    return (
        "move_pick_not_observed: "
        f"state={storage.ui_state}, holding={getattr(storage, 'is_holding_mon', None)}, "
        f"origin=({getattr(storage, 'held_origin_kind', None)},"
        f"{getattr(storage, 'held_origin_box', None)},"
        f"{getattr(storage, 'held_origin_slot', None)})"
    )


def _pc_move_place_failure(v: VerifyContext) -> str:
    storage = v.after.storage
    if storage is None:
        return "move_place_storage_unreadable"
    return (
        "move_place_not_observed: "
        f"state={storage.ui_state}, holding={getattr(storage, 'is_holding_mon', None)}, "
        f"cursor=({storage.cursor_area},{storage.cursor_position})"
    )


def _item_quantity(snapshot, item_id: int, *, pc: bool) -> int:
    if pc:
        storage = snapshot.storage
        items = storage.pc_items if storage is not None else ()
    else:
        items = (item for pocket in snapshot.bag for item in pocket.items)
    return sum(int(item.quantity) for item in items if int(item.item_id) == item_id)


def _pc_item_changed(v: VerifyContext) -> bool:
    item_id = int(v.args["item_id"])
    direction = str(v.args["direction"])
    bag_before = _item_quantity(v.before, item_id, pc=False)
    bag_after = _item_quantity(v.after, item_id, pc=False)
    pc_before = _item_quantity(v.before, item_id, pc=True)
    pc_after = _item_quantity(v.after, item_id, pc=True)
    if direction == "withdraw":
        return bag_after > bag_before and pc_after < pc_before
    return bag_after < bag_before and pc_after > pc_before


def _pc_storage_failure(v: VerifyContext) -> str:
    pid = int(v.args["pid"])
    operation = int(v.args["operation"])
    after = v.after
    if not after.usable or after.storage is None:
        return "core snapshot or storage RAM is not readable"
    party_slots = [index for index, mon in enumerate(after.party) if int(mon.personality) == pid]
    locations = _storage_locations(after.storage, pid)
    if operation == 0:
        return (
            f"withdraw target party_slots={party_slots}, storage_locations={locations}, "
            f"cursor_area={after.storage.cursor_area}, cursor_position={after.storage.cursor_position}"  # noqa: E501
        )
    return f"deposit target party_slots={party_slots}, storage_locations={locations}"


def _storage_locations(storage, pid: int) -> list[tuple[int, int]]:
    if storage is None:
        return []
    return [
        (box, slot)
        for box, mons in enumerate(storage.boxes)
        for slot, mon in enumerate(mons)
        if mon is not None and int(mon.personality) == pid
    ]


def _pc_contents(snapshot) -> tuple[tuple[int, ...], tuple[int, ...]]:
    party = tuple(int(mon.personality) for mon in snapshot.party)
    storage = snapshot.storage
    boxes = (
        (
            tuple(int(mon.personality) if mon is not None else 0 for mon in box)
            for box in storage.boxes
        )
        if storage is not None
        else ()
    )
    return party, tuple(value for box in boxes for value in box)


def _pc_box_contents(snapshot) -> tuple[int, ...]:
    storage = snapshot.storage
    if storage is None:
        return ()
    return tuple(
        int(mon.personality) if mon is not None else 0 for box in storage.boxes for mon in box
    )


def _field_free_unchanged(v: VerifyContext) -> bool:
    return v.scene.scene_id == "field.free" and _pc_contents(v.before) == _pc_contents(v.after)


def _pc_choice(v: VerifyContext) -> bool:
    # Exiting storage refreshes gPlayerPartyCount after a withdrawal, so the party view may
    # legitimately gain the just-withdrawn member here. The box bytes must remain unchanged.
    if v.scene.scene_id == "field.free":
        return _pc_contents(v.before) == _pc_contents(v.after)
    return v.scene.scene_id == "dialog.multichoice" and _pc_box_contents(
        v.before
    ) == _pc_box_contents(v.after)


def _pc_player_exit(v: VerifyContext) -> bool:
    """TURN OFF must expose either the PC choice box or the overworld."""
    return v.scene.scene_id in {"dialog.multichoice", "field.free"}


def _pc_previous_menu(v: VerifyContext) -> bool:
    """A PC sub-menu B press must expose its owning menu, or the field."""
    if v.scene.scene_id == "field.free":
        return True
    if v.scene.scene_id != R.PC_MAIN:
        return False
    after_menu = v.scene.evidence.get("menu")
    before = E.classify(v.before)
    before_menu = before.evidence.get("menu") if before is not None else None
    return after_menu in {"pc_menu", "player_pc"} and after_menu != before_menu


def verifier_specs(foreign: frozenset[str] = frozenset()) -> tuple[VerifierSpec, ...]:
    return (
        VerifierSpec(R.POST_MOVED_ON, _moved_on_for(foreign), max_frames=240, max_wall_s=15.0),
        VerifierSpec(
            R.POST_FOREIGN_SCREEN_LEFT, _foreign_screen_left, max_frames=240, max_wall_s=15.0
        ),
        VerifierSpec(
            R.POST_MENU_CONFIRMED, _moved_on_for(foreign), max_frames=1200, max_wall_s=60.0
        ),
        VerifierSpec(R.POST_INPUT_PROCESSED, _input_processed, max_frames=240, max_wall_s=15.0),
        VerifierSpec(R.POST_NAME_ACCEPTED, _moved_on_for(foreign), max_frames=900, max_wall_s=45.0),
        VerifierSpec(R.POST_EVO_CANCELLED, _evolution_cancelled, max_frames=240, max_wall_s=15.0),
        VerifierSpec(R.POST_EVO_ANSWERED, _moved_on_for(foreign), max_frames=240, max_wall_s=15.0),
        VerifierSpec(R.POST_QUEST_LOG_LEFT, _quest_log_left, max_frames=900, max_wall_s=45.0),
        VerifierSpec(R.POST_MOVE_FORGOTTEN, _move_forgotten, max_frames=900, max_wall_s=45.0),
        VerifierSpec(
            R.POST_PC_STORAGE_CHANGED,
            _pc_storage_changed,
            max_frames=9000,
            max_wall_s=240.0,
            failure_reason=_pc_storage_failure,
        ),
        VerifierSpec(R.POST_PC_STORAGE_MODE, _pc_storage_mode, max_frames=1200, max_wall_s=60.0),
        VerifierSpec(
            R.POST_PC_STORAGE_ERROR_DISMISSED,
            _pc_storage_error_dismissed,
            max_frames=300,
            max_wall_s=20.0,
        ),
        VerifierSpec(
            R.POST_PC_MOVE_PICKED,
            _pc_move_picked,
            max_frames=120,
            max_wall_s=8.0,
            failure_reason=_pc_move_pick_failure,
        ),
        VerifierSpec(
            R.POST_PC_MOVE_PLACED,
            _pc_move_placed,
            max_frames=120,
            max_wall_s=8.0,
            failure_reason=_pc_move_place_failure,
        ),
        VerifierSpec(
            R.POST_PC_MOVE_CANCELLED,
            _pc_move_cancelled,
            max_frames=120,
            max_wall_s=8.0,
            failure_reason=lambda v: "move_cancel_prompt_not_observed",
        ),
        VerifierSpec(R.POST_PC_ITEM_CHANGED, _pc_item_changed, max_frames=2400, max_wall_s=120.0),
        VerifierSpec(R.POST_FIELD_FREE, _field_free_unchanged, max_frames=900, max_wall_s=60.0),
        # FireRed can spend just over 1,000 frames printing the storage-menu farewell before
        # exposing the choice box.  Keep this bounded, but allow that documented transition.
        VerifierSpec(R.POST_PC_CHOICE, _pc_choice, max_frames=9000, max_wall_s=240.0),
        VerifierSpec(R.POST_PC_PLAYER_EXIT, _pc_player_exit, max_frames=300, max_wall_s=30.0),
        VerifierSpec(
            R.POST_PC_PREVIOUS_MENU,
            _pc_previous_menu,
            max_frames=1200,
            max_wall_s=60.0,
        ),
    )
