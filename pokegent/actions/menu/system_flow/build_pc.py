"""RAM-backed Pokemon storage options and the PC safety option."""

from __future__ import annotations

from typing import Any

from pokegent.actions._exec.contracts import ActionContract
from pokegent.actions.menu.system_flow import foreign as F
from pokegent.actions.menu.system_flow import refs as R
from pokegent.actions.menu.system_flow.evidence import Verdict
from pokegent.actions.menu.system_flow.policy import SystemFlowOptions
from pokegent.games.gen3.mechanics.team_planning import _mon_level
from pokegent.options.models import Legality, Option, Risk, fingerprint
from pokegent.options.registry import BuildContext, FrameDraft

PARTY_SIZE = 6


def _level(mon: Any, tables: Any) -> int | None:
    if isinstance(getattr(mon, "level", None), int):
        return int(mon.level)
    if tables is None:
        return None
    level = _mon_level(mon, tables)
    return level or None


def _mon_facts(mon: Any, tables: Any, *, summary: str | None = None) -> dict[str, Any]:
    species = int(mon.species)
    name = None
    types: list[str] = []
    if tables is not None:
        try:
            species_data = tables.species(species)
            name = species_data.name
            types = [tables.type_name(t) for t in species_data.types]
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
    moves = []
    for move in getattr(mon, "moves", ()):
        move_id = int(getattr(move, "move_id", 0))
        if not move_id:
            continue
        move_name = None
        if tables is not None:
            try:
                move_name = tables.move_name(move_id)
            except (AttributeError, KeyError, TypeError, ValueError):
                pass
        moves.append({"id": move_id, "name": move_name, "pp": int(getattr(move, "pp", 0))})
    result = {
        "personality": int(mon.personality),
        "species": species,
        "species_name": name,
        "level": _level(mon, tables),
        "types": types,
        "key_moves": moves,
        "nickname": str(getattr(mon, "nickname", "")),
    }
    return result


def _mon_label(facts: dict[str, Any]) -> str:
    name = facts["species_name"] or f"species {facts['species']}"
    level = f"Lv. {facts['level']}" if facts["level"] is not None else "level unknown"
    return f"{facts['nickname'] or name} ({name}, {level})"


def _item_name(item_id: int, tables: Any) -> str:
    if tables is not None:
        try:
            value = tables.item_name(item_id)
        except (AttributeError, KeyError, TypeError, ValueError):
            value = None
        if value:
            return str(value)
    return f"item {item_id}"


def _action_option(
    *,
    operation: str,
    mon: Any,
    facts: dict[str, Any],
    args: dict[str, int],
    legal: Legality,
    reason: str | None = None,
) -> Option:
    facts = {**facts, "operation": operation, **({"reason": reason} if reason else {})}
    pid = int(mon.personality)
    return Option(
        id=f"{operation}:{pid}",
        label=f"{operation.title()} {_mon_label(facts)}",
        description=(
            f"{reason}."
            if reason
            else f"{operation.title()} this Pokemon selected by personality value {pid}."
        ),
        legality=legal,
        facts=facts,
        executor_ref=R.REF_PC_STORAGE if legal.chooseable else None,
        executor_args=args if legal.chooseable else {},
        postcondition_ref=R.POST_PC_STORAGE_CHANGED if legal.chooseable else None,
        risk=Risk.NORMAL,
        verb=None,
        action_contract=ActionContract(
            cursor_key=f"pokemon:{pid}",
            expected_outcome_scenes=("dialog.yes_no",),
        ),
    )


def _box_candidates(snapshot: Any, tables: Any):
    storage = snapshot.storage
    if storage is None:
        return []
    rows = [
        (box, slot, mon, _mon_facts(mon, tables))
        for box, mons in enumerate(storage.boxes)
        for slot, mon in enumerate(mons)
        if mon is not None
    ]
    current_box = int(storage.current_box)
    rows.sort(key=lambda row: (row[0] != current_box, row[0], row[1]))
    return rows


def _first_empty_slot(storage: Any, box: int) -> int | None:
    return next((slot for slot, mon in enumerate(storage.boxes[box]) if mon is None), None)


def _menu_count(snapshot: Any) -> int | None:
    ui = getattr(snapshot, "ui", None)
    menu = ui.menu.menu if ui is not None and ui.valid else None
    if menu is None or not menu.window_in_use or not menu.consistent:
        return None
    return menu.max_cursor_pos - menu.min_cursor_pos + 1


def _box_content_facts(storage: Any) -> tuple[dict[str, Any], ...]:
    if storage is None:
        return ()
    return tuple(
        {
            "box": box,
            "name": storage.names[box],
            "occupied": sum(mon is not None for mon in mons),
            "free": sum(mon is None for mon in mons),
            "species": tuple(int(mon.species) for mon in mons if mon is not None),
        }
        for box, mons in enumerate(storage.boxes)
    )


def _party_count(snapshot: Any) -> int:
    """Count the freshly decoded non-empty party slots, never planner metadata."""
    return sum(1 for member in getattr(snapshot, "party", ()) if member is not None)


def _party_readable(snapshot: Any) -> bool:
    """Positive party-read admission from the existing ``Health.confidence`` semantics.

    ``1.0`` means every party check passed. An absent or degraded section means the party
    evidence is not proven, even when the decoded fields look usable.
    """
    health = getattr(snapshot, "health", None)
    confidence = getattr(health, "confidence", None)
    return isinstance(confidence, dict) and confidence.get("party") == 1.0


def _battle_capable(mon: Any) -> bool | None:
    """True only when decoded fields prove a non-egg member can still battle.

    ``False`` is a proven non-battler (egg or zero HP). ``None`` means the evidence is
    missing, unreadable or inconsistent, so it must never be counted as a healthy member.
    """
    if mon is None:
        return False
    is_egg = getattr(mon, "is_egg", None)
    if is_egg is True:
        return False
    if is_egg is not False:
        return None
    hp = getattr(mon, "hp", None)
    max_hp = getattr(mon, "max_hp", None)
    if type(hp) is not int or type(max_hp) is not int or max_hp <= 0 or hp < 0:
        return None
    if hp > max_hp:
        return None
    return hp > 0


def _resolved_menu(options: SystemFlowOptions, method: str, snapshot: Any):
    count = _menu_count(snapshot)
    resolver = options.menus
    if count is None or resolver is None:
        return None
    return getattr(resolver, method)(count)


def _storage_main(snapshot: Any, verdict: Verdict, options: SystemFlowOptions) -> FrameDraft:
    entries = _resolved_menu(options, "pokemon_storage", snapshot)
    if entries is None:
        return FrameDraft(
            options=(
                Option(
                    id="pc_storage:unreadable",
                    label="PC storage menu is unreadable",
                    legality=Legality.UNKNOWN,
                    facts={"reason": "ROM rows do not match the live sMenu count"},
                ),
            ),
            state_fingerprint=fingerprint(R.PC_MAIN, menu="pc_menu", resolved=False),
            context={"summary": "The Pokémon-storage menu could not be resolved from ROM."},
        )
    storage = snapshot.storage
    boxes = _box_content_facts(storage)
    party_count = _party_count(snapshot)
    semantic = {
        "pc_storage:withdraw": (
            "Open Pokémon storage in withdraw mode.",
            "party has room and a stored Pokémon is available",
            0,
        ),
        "pc_storage:deposit": (
            "Open Pokémon storage in deposit mode.",
            "a party member can be stored and a box has space",
            1,
        ),
        "pc_storage:move": (
            "Move Pokémon only within Pokémon storage.",
            "rearranging box contents is a legal storage action",
            2,
        ),
        "pc_storage:move_items": (
            "Move held items between Pokémon inside Pokémon storage.",
            "a held item needs to move between stored Pokémon",
            3,
        ),
        "pc_storage:see_ya": (
            "Return to the PC destination list without changing storage.",
            "leave Pokémon storage",
            None,
        ),
    }
    built: list[Option] = []
    for entry in entries:
        does, need, operation = semantic[entry.stable_key]
        exit_row = operation is None
        party_full = operation == 0 and party_count >= PARTY_SIZE
        built.append(
            Option(
                id=entry.stable_key,
                label=entry.label,
                description="Withdraw is unavailable: party full: deposit first."
                if party_full
                else does,
                legality=Legality.UNAVAILABLE if party_full else Legality.LEGAL,
                executor_ref=None if party_full else R.REF_PC_MENU_CHOICE,
                executor_args={},
                postcondition_ref=None
                if party_full
                else (R.POST_PC_CHOICE if exit_row else R.POST_PC_STORAGE_MODE),
                risk=Risk.LOW if exit_row else Risk.NORMAL,
                verb="LOG_OFF" if exit_row else "SELECT_CHOICE",
                facts={
                    "does": does,
                    "need": need,
                    **({"reason": "party full: deposit first"} if party_full else {}),
                    "repeat_count": 0,
                    "party_count": party_count,
                    "box_contents": boxes,
                    "target_screen": {
                        "scene": R.PC_MAIN,
                        "callback": "CB2_PokeStorage",
                        "family": R.FAMILY,
                    },
                    **({"operation": operation} if operation is not None else {}),
                    **({"escape": True} if exit_row else {}),
                },
                action_contract=ActionContract(cursor_key=entry.stable_key),
            )
        )
    return FrameDraft(
        options=tuple(built),
        state_fingerprint=fingerprint(
            R.PC_MAIN,
            menu="pc_menu",
            entries=tuple(entry.stable_key for entry in entries),
            selected=verdict.evidence.get("selected"),
            storage=getattr(storage, "occupied", None),
        ),
        context={"summary": "Choose a Pokémon-storage operation.", "repetition_scope": "pc"},
        deadline_ms=10000,
    )


def _player_pc(snapshot: Any, phase: str, options: SystemFlowOptions) -> FrameDraft:
    # The top-level table proves the labels/callback identities.  The decision surface is flattened
    # to the four useful actions; the executor uses the same generic cursor adapter to traverse the
    # Item Storage row before selecting withdraw/deposit.
    if phase == "player_pc_item_menu":
        entries = _resolved_menu(options, "player_pc_items", snapshot)
        if entries is None:
            return FrameDraft(
                options=(
                    Option(
                        id="player_pc:item_unreadable",
                        label="Player's PC item menu is unreadable",
                        legality=Legality.UNKNOWN,
                        facts={"reason": "ROM rows do not match the live sMenu count"},
                    ),
                ),
                state_fingerprint=fingerprint(R.PC_MAIN, menu="player_pc_items", resolved=False),
            )
        storage = snapshot.storage
        pc_items = tuple(getattr(storage, "pc_items", ())) if storage is not None else ()
        bag_items = tuple(item for pocket in snapshot.bag for item in pocket.items)
        item_ids = dict.fromkeys(int(item.item_id) for item in (*pc_items, *bag_items))
        tables = options.game_tables()
        inventory = tuple(
            {
                "item_id": item_id,
                "name": _item_name(item_id, tables),
                "pc_quantity": sum(
                    int(item.quantity) for item in pc_items if item.item_id == item_id
                ),
                "bag_quantity": sum(
                    int(item.quantity) for item in bag_items if item.item_id == item_id
                ),
            }
            for item_id in item_ids
        )
        first_pc = next(iter(pc_items), None)
        item_pockets = tuple(
            item
            for pocket in snapshot.bag
            if str(getattr(pocket, "pocket", "")).lower() in {"items", "item"}
            for item in pocket.items
        )
        first_bag = next(iter(item_pockets), None)
        built: list[Option] = []
        for entry in entries:
            if entry.stable_key == "player_pc:withdraw_item":
                target, direction = first_pc, "withdraw"
                reason = "the Player's PC has no stored items"
            elif entry.stable_key == "player_pc:deposit_item":
                target, direction = first_bag, "deposit"
                reason = "the Bag has no items"
            else:
                built.append(
                    Option(
                        id=entry.stable_key,
                        label=entry.label,
                        description="Leave item storage and return to the Player's PC menu.",
                        legality=Legality.LEGAL,
                        executor_ref=R.REF_PRESS_B,
                        postcondition_ref=R.POST_PC_PREVIOUS_MENU,
                        risk=Risk.LOW,
                        verb="LOG_OFF",
                        facts={
                            "menu": "player_pc_items",
                            "item_inventory": inventory,
                            "physical": True,
                            "escape": True,
                        },
                        action_contract=ActionContract(cursor_key=entry.stable_key),
                    )
                )
                continue
            item_id = int(target.item_id) if target is not None else None
            name = _item_name(item_id, tables) if item_id is not None else entry.label
            built.append(
                Option(
                    id=entry.stable_key,
                    label=f"{entry.label}: {name}" if item_id is not None else entry.label,
                    description=(
                        f"Transfer one {name} between the Bag and the Player's PC."
                        if item_id is not None
                        else reason
                    ),
                    legality=Legality.LEGAL if item_id is not None else Legality.UNAVAILABLE,
                    executor_ref=R.REF_PC_PLAYER if item_id is not None else None,
                    executor_args=(
                        {"stable_key": entry.stable_key, "item_id": item_id, "direction": direction}
                        if item_id is not None
                        else {}
                    ),
                    postcondition_ref=R.POST_PC_ITEM_CHANGED if item_id is not None else None,
                    facts={
                        "menu": "player_pc_items",
                        "item_inventory": inventory,
                        "item_id": item_id,
                        "physical": item_id is not None,
                        "pc_quantity": sum(
                            int(item.quantity)
                            for item in pc_items
                            if item_id is not None and item.item_id == item_id
                        ),
                        "bag_quantity": sum(
                            int(item.quantity)
                            for item in bag_items
                            if item_id is not None and item.item_id == item_id
                        ),
                        "quantity_default": 1,
                        "quantity_max": int(target.quantity) if target is not None else 0,
                        "target_screen": {
                            "scene": R.PC_MAIN,
                            "callback": "CB2_PokeStorage",
                            "family": R.FAMILY,
                        },
                        **({"reason": reason} if item_id is None else {}),
                    },
                    action_contract=ActionContract(cursor_key=entry.stable_key),
                )
            )
        return FrameDraft(
            options=tuple(built),
            state_fingerprint=fingerprint(
                R.PC_MAIN, menu="player_pc_items", phase=phase, items=inventory
            ),
            context={"summary": "Choose a Player's PC item-storage action."},
            deadline_ms=10000,
        )

    top = _resolved_menu(options, "player_pc_top", snapshot)
    if top is None:
        return FrameDraft(
            options=(
                Option(
                    id="player_pc:unreadable",
                    label="Player's PC menu is unreadable",
                    legality=Legality.UNKNOWN,
                    facts={"reason": "ROM rows do not match the live sMenu count"},
                ),
            ),
            state_fingerprint=fingerprint(R.PC_MAIN, menu="player_pc", resolved=False),
        )
    storage = snapshot.storage
    pc_items = tuple(getattr(storage, "pc_items", ())) if storage is not None else ()
    bag_items = tuple(item for pocket in snapshot.bag for item in pocket.items)
    withdraw_target = next(
        (
            item
            for item in sorted(pc_items, key=lambda value: (value.quantity != 1, value.item_id))
            if item.quantity > 0
        ),
        None,
    )
    # The deposit bag opens on the ordinary Items pocket.  A full different pocket is not a
    # concrete reason for depositing an item the current generic adapter cannot identify there.
    deposit_target = next(
        (
            item
            for pocket in snapshot.bag
            if str(getattr(pocket, "pocket", "")).lower() in {"items", "item"}
            for item in pocket.items
            if item.quantity > 0
        ),
        None,
    )
    actionable = withdraw_target is not None or deposit_target is not None

    def item_option(direction: str, target: Any, reason: str) -> Option:
        chooseable = target is not None
        item_id = int(target.item_id) if chooseable else None
        return Option(
            id=f"player_pc:{direction}_item",
            label=f"{direction.title()} item",
            description=reason,
            legality=Legality.LEGAL if chooseable else Legality.UNAVAILABLE,
            executor_ref=R.REF_PC_PLAYER if chooseable else None,
            executor_args=(
                {
                    "stable_key": f"player_pc:{direction}_item",
                    "item_id": item_id,
                    "direction": direction,
                }
                if chooseable
                else {}
            ),
            postcondition_ref=R.POST_PC_ITEM_CHANGED if chooseable else None,
            facts={
                "does": f"{direction.title()} one item between the Bag and Player's PC.",
                "need": reason,
                "serves_goal": reason if chooseable else None,
                "repeat_count": 0,
                "item_id": item_id,
                "bag_quantity": sum(i.quantity for i in bag_items if i.item_id == item_id),
                "pc_quantity": sum(i.quantity for i in pc_items if i.item_id == item_id),
            },
        )

    withdraw = item_option(
        "withdraw",
        withdraw_target,
        (
            f"stored item {withdraw_target.item_id} is absent from the Bag"
            if withdraw_target is not None
            else "the Player's PC has no stored items"
        ),
    )
    deposit = item_option(
        "deposit",
        deposit_target,
        (
            f"store one {deposit_target.item_id} from the Bag"
            if deposit_target is not None
            else "the Bag has no items"
        ),
    )
    mailbox = Option(
        id="player_pc:mailbox",
        label="Mailbox",
        description="Open mail stored on the Player's PC.",
        legality=Legality.LEGAL,
        executor_ref=R.REF_PC_PLAYER,
        executor_args={"stable_key": "player_pc:mailbox"},
        postcondition_ref=R.POST_INPUT_PROCESSED,
        facts={
            "does": "Read, move, or give stored mail.",
            "need": "mail management",
            "serves_goal": "manage stored mail",
            "repeat_count": 0,
        },
    )
    cancel = Option(
        id="player_pc:cancel",
        label="Cancel",
        description="Turn off the Player's PC and return to the destination list.",
        legality=Legality.LEGAL,
        executor_ref=R.REF_PC_PLAYER,
        executor_args={"stable_key": "player_pc:cancel"},
        postcondition_ref=R.POST_PC_PLAYER_EXIT,
        risk=Risk.LOW,
        verb="LOG_OFF",
        facts={
            "does": "Leave Player's PC without changing Bag or PC-item storage.",
            "need": "leave item storage",
            "serves_goal": "safe exit" if not actionable else None,
            "repeat_count": 0,
            "next_step": not actionable,
            "escape": True,
        },
    )
    return FrameDraft(
        options=(withdraw, deposit, mailbox, cancel),
        state_fingerprint=fingerprint(
            R.PC_MAIN,
            menu="player_pc",
            phase=phase,
            pc_items=tuple((item.item_id, item.quantity) for item in pc_items),
            bag=tuple((item.item_id, item.quantity) for item in bag_items),
        ),
        context={"summary": "Choose a Player's PC action.", "repetition_scope": "pc"},
        deadline_ms=10000,
    )


def pc_main(
    ctx: BuildContext, snapshot, verdict: Verdict, options: SystemFlowOptions
) -> FrameDraft:
    menu = str(verdict.evidence.get("menu"))
    phase = str(verdict.evidence.get("phase"))
    tables = options.game_tables()

    def facts_for(mon: Any) -> dict[str, Any]:
        return _mon_facts(mon, tables)

    storage = snapshot.storage
    mode = storage.box_option if menu == "pc_storage" and storage is not None else None
    if menu == "pc_menu":
        return _storage_main(snapshot, verdict, options)
    if menu == "pc_storage" and phase == "storage_error":
        return FrameDraft(
            options=(
                Option(
                    id="dismiss_pc_storage_error",
                    label="Dismiss PC storage error",
                    description="Back out of FireRed's storage error and return to the storage menu.",  # noqa: E501
                    legality=Legality.LEGAL,
                    executor_ref=R.REF_PC_STORAGE_ERROR,
                    postcondition_ref=R.POST_PC_STORAGE_ERROR_DISMISSED,
                    risk=Risk.LOW,
                    facts={"menu": "pc_storage", "phase": "storage_error", "escape": True},
                ),
            ),
            state_fingerprint=fingerprint(R.PC_MAIN, menu="pc_storage", phase="storage_error"),
            context={"summary": "Dismiss the FireRed Pokémon-storage error."},
            deadline_ms=10000,
        )
    if menu in {"player_pc", "player_pc_items"}:
        return _player_pc(snapshot, phase, options)
    back = Option(
        id=R.OPT_LOG_OFF,
        label="Leave the PC (return to the PC choice box)",
        description="Press B as needed to back out of storage and return to the PC choice box without changing party or box contents.",  # noqa: E501
        legality=Legality.LEGAL,
        executor_ref=R.REF_CLOSE_MENU if verdict.scene_id == R.MENU_OPEN else R.REF_LEAVE_PC,
        postcondition_ref=R.POST_MOVED_ON if verdict.scene_id == R.MENU_OPEN else R.POST_PC_CHOICE,
        risk=Risk.LOW,
        verb="LOG_OFF",
        facts={"menu": menu, "owner": "none", "phase": phase, "inputs": 1, "escape": True},
    )
    if menu == "pc_storage" and storage is not None:
        back = Option(
            id=R.OPT_LOG_OFF,
            label="Leave the PC (return to the PC choice box)",
            description="Press B through the storage and PC menus until the PC choice box is verified.",  # noqa: E501
            legality=Legality.LEGAL,
            executor_ref=R.REF_LEAVE_PC,
            postcondition_ref=R.POST_PC_CHOICE,
            risk=Risk.LOW,
            verb="LOG_OFF",
            facts={
                "menu": menu,
                "owner": "none",
                "phase": phase,
                "inputs": "bounded B presses",
                "escape": True,
            },
        )
        back_to_menu = Option(
            id="back_to_pc_menu",
            label="Back to the PC menu",
            description="Back out of storage; answer Continue BOX operations? NO to return to Bill's PC menu.",  # noqa: E501
            legality=Legality.LEGAL,
            executor_ref=R.REF_PC_STORAGE_BACK,
            postcondition_ref=R.POST_PC_PREVIOUS_MENU,
            risk=Risk.LOW,
            verb="LOG_OFF",
            facts={
                "menu": menu,
                "owner": "none",
                "phase": phase,
                "inputs": "B, NO, verified PC menu",
                "escape": True,
            },
        )
    else:
        back_to_menu = None
    actions: list[Option] = []
    if mode == 0:
        candidates = _box_candidates(snapshot, tables)
        for box, slot, mon, facts in candidates:
            facts.update({"box": box, "slot": slot})
            facts["menu"] = "pc_storage"
            # Let FireRed report a full-party withdrawal in its own bounded error frame.
            party_full = _party_count(snapshot) >= PARTY_SIZE
            legal = Legality.UNAVAILABLE if party_full else Legality.LEGAL
            reason = "party full: deposit first" if party_full else None
            actions.append(
                _action_option(
                    operation="withdraw",
                    mon=mon,
                    facts=facts,
                    args={
                        "pid": int(mon.personality),
                        "operation": 0,
                        **({"box": box, "slot": slot} if box != int(storage.current_box) else {}),
                    },
                    legal=legal,
                    reason=reason,
                )
            )
    if mode == 1:
        deposit_box = 0
        destination_slot = None
        if storage is not None:
            deposit_box = next(
                (
                    box
                    for box, mons in enumerate(storage.boxes)
                    if sum(mon is not None for mon in mons) < 30
                ),
                int(storage.current_box),
            )
            destination_slot = _first_empty_slot(storage, deposit_box)
        party_count = _party_count(snapshot)
        party_readable = _party_readable(snapshot)
        for slot, mon in enumerate(snapshot.party):
            if mon is None:
                continue
            facts = facts_for(mon)
            capable = _battle_capable(mon)
            another_capable = any(
                _battle_capable(other) is True
                for other in snapshot.party
                if other is not None and other is not mon
            )
            if not party_readable:
                legal = Legality.UNKNOWN
                reason = "party read is not proven: cannot verify battle-capable members"
            elif party_count <= 1:
                legal = Legality.UNAVAILABLE
                reason = "cannot deposit the last party Pokemon"
            elif destination_slot is None:
                legal = Legality.UNAVAILABLE
                reason = "PC storage unreadable"
            elif capable is not False and not another_capable:
                # The target is (or may be) battle-capable and no other member proves one
                # remains: depositing it could leave the party with no usable Pokemon.
                if capable is True:
                    legal = Legality.UNAVAILABLE
                    reason = "cannot deposit the last battle-capable party Pokemon"
                else:
                    legal = Legality.UNKNOWN
                    reason = "party HP is unreadable: cannot prove another battle-capable Pokemon"
            else:
                legal = Legality.LEGAL
                reason = None
            facts["destination_box"] = deposit_box
            facts["destination_slot"] = destination_slot
            args = {"pid": int(mon.personality), "operation": 1}
            if destination_slot is not None:
                args["box"] = deposit_box
                args["slot"] = destination_slot
            actions.append(
                _action_option(
                    operation="deposit", mon=mon, facts=facts, args=args, legal=legal, reason=reason
                )
            )
    if mode == 2:
        # MOVE mode is the same generic RAM-backed box cursor as withdraw, but its action
        # semantics are pick/place rather than a party transfer.
        holding = getattr(storage, "is_holding_mon", None)
        if holding is False:
            candidates = _box_candidates(snapshot, tables)
            for box, slot, mon, facts in candidates:
                facts.update(
                    {
                        "box": box,
                        "slot": slot,
                        "operation": "move",
                        "move_phase": "pick",
                        "menu": "pc_storage",
                    }
                )
                option = Option(
                    id=f"move:{int(mon.personality)}",
                    label=f"Pick up {_mon_label(facts)}",
                    description="Pick up this Pokémon; the next RAM-backed move screen chooses its destination.",  # noqa: E501
                    legality=Legality.LEGAL,
                    executor_ref=R.REF_PC_MOVE,
                    executor_args={"pid": int(mon.personality), "box": box, "slot": slot},
                    postcondition_ref=R.POST_PC_MOVE_PICKED,
                    risk=Risk.NORMAL,
                    facts=facts,
                    action_contract=ActionContract(cursor_key=f"pokemon:{int(mon.personality)}"),
                )
                actions.append(option)
        elif holding is True and storage.held_mon is not None:
            destination_options: list[Option] = []
            current_box = int(storage.current_box)
            destination_slots = sorted(
                enumerate(storage.boxes[current_box]), key=lambda row: row[1] is not None
            )
            for slot, mon in destination_slots:
                destination_options.append(
                    Option(
                        id=f"move:place:box:{current_box}:{slot}",
                        label=f"Place in box {current_box + 1}, slot {slot + 1}",
                        description="Place into an empty slot or swap with the Pokémon shown there.",  # noqa: E501
                        legality=Legality.LEGAL,
                        executor_ref=R.REF_PC_MOVE_PLACE,
                        executor_args={
                            "pid": int(storage.held_mon.personality),
                            "target_kind": "box",
                            "target_box": current_box,
                            "target_slot": slot,
                        },
                        postcondition_ref=R.POST_PC_MOVE_PLACED,
                        risk=Risk.NORMAL,
                        facts={
                            "operation": "move",
                            "move_phase": "place",
                            "box": current_box,
                            "slot": slot,
                            "destination": "empty" if mon is None else "swap",
                            "occupant": _mon_facts(mon, tables) if mon is not None else None,
                        },
                    )
                )
            for member in snapshot.party:
                destination_options.append(
                    Option(
                        id=f"move:place:party:{int(member.slot)}",
                        label=f"Place in party slot {int(member.slot) + 1}",
                        description="Place into or swap with the Pokémon shown in this party slot.",
                        legality=Legality.LEGAL,
                        executor_ref=R.REF_PC_MOVE_PLACE,
                        executor_args={
                            "pid": int(storage.held_mon.personality),
                            "target_kind": "party",
                            "target_slot": int(member.slot),
                        },
                        postcondition_ref=R.POST_PC_MOVE_PLACED,
                        risk=Risk.NORMAL,
                        facts={
                            "operation": "move",
                            "move_phase": "place",
                            "party_slot": int(member.slot),
                            "destination": "swap",
                            "occupant": _mon_facts(member, tables),
                        },
                    )
                )
            actions.extend(destination_options)
        if holding is False:
            actions.append(
                Option(
                    id="move:cancel",
                    label="Cancel MOVE mode",
                    description="Open FireRed's Continue BOX operations prompt.",
                    legality=Legality.LEGAL,
                    executor_ref=R.REF_PC_MOVE_CANCEL,
                    postcondition_ref=R.POST_PC_MOVE_CANCELLED,
                    risk=Risk.LOW,
                    verb="CANCEL",
                    facts={"operation": "move", "move_phase": "cancel", "escape": True},
                )
            )
    return FrameDraft(
        options=(back, *actions, back_to_menu) if back_to_menu is not None else (back, *actions),
        state_fingerprint=fingerprint(
            R.PC_MAIN,
            menu=menu,
            phase=phase,
            selected=verdict.evidence.get("selected"),
            storage=getattr(storage, "occupied", None),
            mode=mode,
        ),
        context={
            "summary": "The PC storage screen is open.",
            "repetition_scope": "pc_choice",
        },
        deadline_ms=10000,
    )


def foreign_menu(
    ctx: BuildContext, snapshot, verdict: Verdict, options: SystemFlowOptions
) -> FrameDraft:
    # ``menu.open`` is the generic agent #67 fallback and is deliberately not
    # part of the opt-in per-menu table.  It still needs the same safe B escape
    # as an explicitly configured foreign menu.
    entry = F.FOREIGN_MENUS.get(verdict.scene_id, F.GENERIC_MENU)
    menu_type = verdict.evidence.get("menu")
    foreign_screen = verdict.scene_id in F.FOREIGN_MENUS and verdict.scene_id != "menu.start"
    cancel = Option(
        id="cancel",
        label=entry.label,
        description=entry.description,
        legality=Legality.LEGAL,
        executor_ref=(R.REF_BACK_OUT_FOREIGN if foreign_screen else R.REF_PRESS_B),
        postcondition_ref=(R.POST_FOREIGN_SCREEN_LEFT if foreign_screen else R.POST_MOVED_ON),
        risk=Risk.LOW,
        verb=entry.verb,
        facts={
            "menu": entry.menu,
            "menu_type": menu_type,
            "prompt_kind": "yes_no",
            "affirmative": False,
            "escape": True,
            "owner": "none",
            "inputs": 1,
            "foreign_screen": foreign_screen,
        },
    )
    # A START save prompt is a corroborated yes/no menu, not an unknown scene.
    # Keep the universal B escape, but expose the affirmative branch so a
    # deterministic decider can complete the save and observe the game return
    # to the field.
    confirm = Option(
        id="confirm",
        label="Confirm",
        description="Confirm the open START-menu prompt.",
        legality=Legality.LEGAL,
        executor_ref=R.REF_PRESS_A,
        postcondition_ref=R.POST_MENU_CONFIRMED,
        risk=Risk.LOW,
        verb="CONFIRM",
        facts={
            "menu": entry.menu,
            "menu_type": menu_type,
            "prompt_kind": "yes_no",
            "affirmative": True,
            "owner": "none",
            "inputs": 1,
        },
    )
    built = (confirm, cancel) if menu_type == "start_menu_subprompt" else (cancel,)
    return FrameDraft(
        options=built,
        state_fingerprint=fingerprint(verdict.scene_id, menu=entry.menu),
        context={"summary": f"The {entry.menu.replace('_', ' ')} is open and no plan owns it."},
    )
