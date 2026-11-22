"""Executor specs of the field family: walking in short verified segments, then the final action.

Every function only *yields steps*; nothing here touches the emulator. A walk is a loop over one
decision at a time, and every decision is taken on the OBSERVATION the executor hands back after the
previous step (a fresh snapshot with the map block):

1. the player must stand where the plan says (tile and map); anything else re-plans (bounded);
2. a turn is its own tap (hold 4 frames: the game turns in place for any hold below 8) and the
facing
   is read back before the step; a step is one tap of 6 frames (the game walks exactly one tile for
   any hold from 1 to 16; measured live: 20 frames walk two);
3. the next step is checked with ``predict_step`` on the fresh state right before it is pressed: a
   walking NPC that stepped into the way is waited for (bounded) and then planned around; a step the
   game's rules predict as blocked is never pressed;
4. a foreign scene at a boundary (a dialog, a battle, a trainer who spotted us, a map change nobody
   planned) stops the macro: it yields a step the executor refuses to send (``INTERRUPTED``), so the
   remaining presses are never sent (contract invariant 6);
5. the final action (walk onto the warp tile, press toward the door, hold the arrow, face the person
   and press A) is the LAST thing the macro does, and its effect is read back before it ends.

A typed failure (``unreachable:<reason>``, ``blocked``, ``no_effect`` ...) is recorded in the
:class:`~pokegent.actions.field.attempts.AttemptLedger` (three failures without progress hide the
option) and the macro ends without further input. NEVER presses into something it has not verified:
no random directions, no "try the other way".
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from dataclasses import dataclass, replace

from pokegent.actions._exec.menu_executor import select_menu_entry
from pokegent.actions._exec.models import (
    Advance,
    ExecutionKind,
    Interrupt,
    Step,
    Tap,
    WaitUntil,
)
from pokegent.actions._exec.registry import Arg, ExecutorSpec, StepContext
from pokegent.actions.field import builder as B
from pokegent.actions.field import evidence as E
from pokegent.actions.field import fieldmenu, fly
from pokegent.actions.field import nav as N
from pokegent.actions.field import targets as T
from pokegent.actions.field.attempts import AttemptLedger, LedgerKey, capability_signature
from pokegent.actions.items import EffectClass, ItemCatalog, ItemTarget
from pokegent.actions.menu.dialog import evidence as D
from pokegent.actions.menu.dialog.prompts import PromptPolicy
from pokegent.actions.menu.system_flow import moves as system_moves
from pokegent.actions.menu.system_flow import refs as system_refs
from pokegent.games.gen3.firered.maps import map_constants as K
from pokegent.games.gen3.firered.maps.map_motion import predict_step, traverse_forced
from pokegent.games.gen3.firered.maps.map_schema import MapState
from pokegent.games.gen3.moves.learn import confirm_learning
from pokegent.games.gen3.moves.learn import replace_move as forget_move_macro
from pokegent.pipeline import Observation

BUTTON = {"N": "up", "S": "down", "W": "left", "E": "right"}
_MOVEMENT_SCENES = frozenset({E.FREE, E.DARKNESS})

TURN_HOLD, TURN_WAIT = 4, 1  # < 8: the game only turns
STEP_HOLD, STEP_WAIT = 6, 1  # 1..16: exactly one tile
# A mounted edge transition needs a little more input time than a mounted tile step: the
# connection seam is reached after the avatar's final bike movement has settled.
BIKE_STEP_MAX_HOLD = 24
BIKE_EDGE_MAX_HOLD = 32
PRESS_A_HOLD, PRESS_A_WAIT = 4, 2
DOOR_HOLD = 6  # facing north in front of a warp door: the press starts the door warp
ARROW_HOLD = 4
ARROW_MAX_HOLD = 70  # bounded fallback for directional warps that do not start immediately

MAX_REPLANS = 5
NPC_WAITS = 4  # bounded waits for a walking NPC to leave the way
NPC_WAIT_FRAMES = 12
MAP_RETRIES = 3  # an unreadable map block around a transition: re-read this often
UNKNOWN = "scene.unknown"
UNKNOWN_WAITS = 40  # a transition frame no detector names (an unnamed callback): wait, never press
UNKNOWN_WAIT_FRAMES = 6
BIKE_ITEM = 360
MENU_HOLD, MENU_WAIT = 4, 20
START_POKEMON, START_BAG = 1, 2
TM_CASE_ITEM, TM_CASE_POCKET = 364, 1
KEY_ITEMS_POCKET = 1
# TM/HM teaching uses the verified TM Case path above.
TMHM_POCKET = 3


def party_item_use_verified(
    before_snapshot, current_snapshot, item_id: int, slot: int, kind: str
) -> bool:
    """Confirm a targeted field item from the party and bag facts in RAM."""
    before = next((m for m in before_snapshot.party if m.slot == slot), None)
    after = next((m for m in current_snapshot.party if m.slot == slot), None)
    if before is None or after is None:
        return False
    if fieldmenu.quantity(current_snapshot, item_id) >= fieldmenu.quantity(
        before_snapshot, item_id
    ):
        return False
    if kind == "evolution":
        return after.species != before.species
    if kind in {"hp", "revive"}:
        return after.hp != before.hp
    if kind == "status":
        return after.status != before.status
    return after != before


def _begin_teach_input_budget(ctx: StepContext) -> int:
    """Bound navigation from the RAM-sized START, Bag, and TM Case lists."""
    snapshot = ctx.snapshot
    ui = snapshot.ui
    start = ui.menu.start_menu if ui is not None and ui.menu is not None else None
    start_rows = max(1, int(getattr(start, "num_items", 8)))
    pockets = tuple(getattr(snapshot, "bag", ()))
    key_items = next((p.items for p in pockets if p.pocket == "key_items"), ())
    machines = next((p.items for p in pockets if p.pocket == "tms_hms"), ())
    # Seven confirmations open START/Bag/TM Case and select each USE entry. Every cursor can
    # reopen at either end of its RAM-owned list, and Bag can reopen on any available pocket.
    return (
        7
        + max(0, start_rows - 1)
        + max(0, len(pockets) - 1)
        + max(0, len(key_items) - 1)
        + max(0, len(machines) - 1)
    )


_BAG_POCKET = {
    1: ("items", 0),
    2: ("key_items", 1),
    3: ("poke_balls", 2),
    4: ("tms_hms", 3),
    5: ("berries", 4),
}

#: scenes that mean "something took over": reaching one after the final A press is the effect
EFFECT_PREFIXES = ("dialog.", "battle.", "system.")
EFFECT_SCENES = frozenset({E.TRAINER_VISION, "field.scripted_movement", "field.map_transition"})


def _map_key(state: MapState) -> tuple[int, int]:
    return (state.identity.group, state.identity.number)


def _map_changed(before, current) -> bool:
    before_keys = E._snapshot_map_keys(before)
    current_keys = E._snapshot_map_keys(current.snapshot)
    return any(
        old is not None and new is not None and old != new
        for old, new in zip(before_keys, current_keys)
    )


def _directional_warp_started(obs: Observation, start_map: tuple[int, int]) -> bool:
    """Whether a held arrow/stair direction has committed to one map transition.

    Release the direction at this boundary.  Holding it through the load can activate a matching
    directional warp on the destination tile and return to the source map before verification.
    """
    if _left(obs, start_map):
        return True
    ui = obs.snapshot.ui
    return bool(ui is not None and ui.valid and ui.field.warp_pending is True)


def _rope_message_owns_input(obs: Observation) -> bool:
    """FireRed's Rope prompt can remain classified as field.free during the fade."""
    if obs.scene.scene_id == "dialog.advance":
        return True
    ui = obs.snapshot.ui
    return bool(
        ui is not None and ui.valid and ui.text is not None and ui.text.waiting_for_button is True
    )


def took_over(obs: Observation) -> bool:
    """True when a script, dialog or battle now has the screen (the effect of pressing A)."""
    scene = obs.scene.scene_id
    if scene.startswith(EFFECT_PREFIXES) or scene in EFFECT_SCENES:
        return True
    ui = obs.snapshot.ui
    return bool(ui is not None and ui.valid and (ui.script.running or ui.script.controls_locked))


def _tool_done_observation(snapshot, slot: int) -> bool:
    """The selected HM carrier learned Fly and every field-menu owner released the screen."""
    if not any(
        member.slot == slot and any(move.move_id == fly.FLY_MOVE_ID for move in member.moves)
        for member in snapshot.party
    ):
        return False
    ui = snapshot.ui
    if ui is None or not ui.valid or ui.menu is None:
        return False
    if ui.main.callback2 is None or ui.main.callback2.name != "CB2_Overworld":
        return False
    if ui.script.running is not False or ui.script.controls_locked is not False:
        return False
    if ui.field.forced_movement is not False:
        return False
    menu = ui.menu
    return not any(
        (
            menu.start_menu is not None and menu.start_menu.task_active,
            menu.bag is not None and menu.bag.active_hint is True,
            menu.party is not None and menu.party.active_hint is True,
            menu.list_menu is not None and menu.list_menu.window_in_use,
            menu.menu is not None and menu.menu.window_in_use,
        )
    )


def _teach_prompt_observation(current) -> bool:
    snapshot = current.snapshot
    ui = snapshot.ui
    yes_no = ui.menu.yes_no if ui is not None and ui.valid and ui.menu is not None else None
    choice = ui.menu.menu if ui is not None and ui.valid and ui.menu is not None else None
    return bool(
        yes_no is not None
        and yes_no.window_in_use
        and (
            (ui.text is not None and ui.text.waiting_for_button)
            or yes_no.task_active is True
            or (choice is not None and choice.window_in_use and choice.max_cursor_pos == 1)
        )
    )


def _teach_target_observation(current, item_id: int | None = None) -> bool:
    ui = current.snapshot.ui
    party = ui.menu.party if ui is not None and ui.valid and ui.menu is not None else None
    return bool(
        party is not None
        and party.active_hint is True
        and party.action_name == "use_item"
        and (item_id is None or party.item_id == item_id)
        and ui.field.fade_active is False
        and any(task.func.name == "Task_HandleChooseMonInput" for task in ui.tasks.active)
    )


def _teach_cleanup_observation(current, slot: int, move_id: int) -> bool:
    snapshot = current.snapshot
    if _tm_done_observation(current, slot, move_id):
        return True
    ui = snapshot.ui
    if ui is None or not ui.valid or ui.menu is None:
        return False
    menu = ui.menu
    return bool(
        ui.main.callback2 is None
        or ui.main.callback2.name != "CB2_Overworld"
        or menu.summary_screen_open
        or (ui.text is not None and ui.text.waiting_for_button)
        or (menu.yes_no is not None and menu.yes_no.window_in_use)
        or (menu.start_menu is not None and menu.start_menu.task_active)
        or (menu.party is not None and menu.party.active_hint is True)
        or (menu.bag is not None and menu.bag.active_hint is True)
        or (menu.list_menu is not None and menu.list_menu.window_in_use)
        or (menu.menu is not None and menu.menu.window_in_use)
    )


def _tm_done_observation(current, slot: int, move_id: int) -> bool:
    snapshot = current.snapshot
    if not any(
        member.slot == slot and any(move.move_id == move_id for move in member.moves)
        for member in snapshot.party
    ):
        return False
    ui = snapshot.ui
    if ui is None or not ui.valid or ui.menu is None or ui.main.callback2 is None:
        return False
    menu = ui.menu
    return (
        ui.main.callback2.name == "CB2_Overworld"
        and ui.script.running is False
        and ui.script.controls_locked is False
        and not any(
            (
                menu.start_menu is not None and menu.start_menu.task_active,
                menu.bag is not None and menu.bag.active_hint is True,
                menu.party is not None and menu.party.active_hint is True,
                menu.list_menu is not None and menu.list_menu.window_in_use,
                menu.menu is not None and menu.menu.window_in_use,
            )
        )
    )


def _task_active(ui, name: str) -> bool:
    return bool(
        ui is not None
        and ui.valid
        and ui.tasks is not None
        and any(task.func.name == name for task in ui.tasks.active)
    )


def _free_field_observation(obs) -> bool:
    snapshot = obs.snapshot
    ui = snapshot.ui
    if ui is None or not ui.valid or ui.main.callback2 is None or ui.menu is None:
        return False
    menu = ui.menu
    return bool(
        obs.scene.scene_id in _MOVEMENT_SCENES
        and not obs.scene.busy
        and ui.main.callback2.name == "CB2_Overworld"
        and ui.script.running is False
        and ui.script.controls_locked is False
        and ui.field.forced_movement is False
        and not any(
            (
                menu.start_menu is not None and menu.start_menu.task_active,
                menu.bag is not None and menu.bag.active_hint is True,
                menu.party is not None and menu.party.active_hint is True,
                menu.list_menu is not None and menu.list_menu.window_in_use,
                menu.menu is not None and menu.menu.window_in_use,
                menu.yes_no is not None and menu.yes_no.window_in_use,
                menu.summary_screen_open,
            )
        )
    )


def _field_free_after_bag_failure(actor, perceiver, _ctx) -> None:
    """Close every field-menu layer after a bag-owned macro fails.

    This is called by the executor even when a WaitUntil timed out outside the generator, so it
    covers the live failure mode where the item context menu remains on screen.  Every input is
    chosen from a RAM-confirmed owner and followed by a fresh observation; the bounded loop never
    assumes that a press was consumed.
    """
    obs = perceiver.observe()
    presses = 0
    # Item-use owns several non-input transitions: Bag fade-out, party item animation, party
    # fade-out, Bag reload, and the return-to-field callback.  Advance those transitions without
    # imposing the runner's generic "busy" deadline; press only when a RAM-named task owns input.
    for _ in range(750):
        if _free_field_observation(obs):
            return
        ui = obs.snapshot.ui
        if ui is None or not ui.valid or ui.field.fade_active:
            actor.advance_frames(4)
            obs = perceiver.observe()
            continue

        button = None
        callback = ui.main.callback2.name if ui.main.callback2 is not None else None
        task_names = {task.func.name for task in ui.tasks.active}
        # The move summary and the party-owned yes/no prompts need different exits.  These
        # task/callback names are the RAM owners from party_menu.c; a raw allocated window is not
        # sufficient because sYesNoWindowId remains stale after some prompts close.
        # The "Learn <move>?" prompt (Task_HandleChooseMonInput -> sYesNoWindowId) also exits on
        # B: a raw A (or A from the text-box fallback below) confirms the teach, then the follow-up
        # "Stop learning <move>?" prompt re-opens and this loop never restores field control.
        # Live incident: live-0113 external-461 -> E_WAIT_TIMEOUT_FRAMES + E_BUSY_STUCK (issue 161).
        if "Task_HandleChooseMonInput" in task_names:
            button = "b"
        elif (
            callback in {"CB2_SetUpPSS", "obs_cb2_pokemon_summary_screen"}
            and ui.menu.summary_screen_open
        ):
            button = "b"
        elif "Task_HandleReplaceMoveYesNoInput" in task_names:
            button = "b"  # NO -> proceed to the stop-learning confirmation
        elif "Task_HandleStopLearningMoveYesNoInput" in task_names:
            button = "a"  # YES -> abandon teaching and close the party menu
        elif ui.text is not None and ui.text.waiting_for_button:
            button = "a"
        elif any(
            task.func.name == "Task_UseItem_Normal"
            and task.item_use_anim is not None
            and task.item_use_anim.state == 12
            and task.item_use_anim.cancel_disabled
            for task in ui.tasks.active
        ):
            button = "a"
        elif any(task.func.name == "Task_DisplayLevelUpStatsPg2" for task in ui.tasks.active) or (
            getattr(ui.field, "fanfare_counter", None) == 0
            and any(
                task.func.name in {"Task_DisplayLevelUpStatsPg1", "Task_TryLearnNewMoves"}
                for task in ui.tasks.active
            )
        ):
            button = "a"
        elif any(
            _task_active(ui, task)
            for task in (
                "Task_HandleChooseMonInput",
                "Task_HandleSelectionMenuInput",
                "Task_FieldItemContextMenuHandleInput",
                "Task_BagMenu_HandleInput",
                "Task_StartMenuHandleInput",
            )
        ):
            button = "b"
        elif (
            ui.menu.list_menu is not None
            and ui.menu.list_menu.window_in_use
            and ui.menu.menu is not None
            and ui.menu.menu.window_in_use
            and ui.menu.menu.consistent
        ):
            # TM Case's list and item context menu use anonymous ROM tasks here. Both live
            # windows corroborate the context layer that B safely closes.
            button = "b"
        elif ui.menu.list_menu is not None and ui.menu.list_menu.window_in_use:
            # Once its context closes, the remaining live list is the next verified layer.
            button = "b"

        if button is None:
            actor.advance_frames(4)
        else:
            presses += 1
            if presses > 12:
                raise RuntimeError("bag failure cleanup exceeded its verified input budget")
            actor.tap(button, MENU_HOLD, MENU_WAIT)
        obs = perceiver.observe()
    raise RuntimeError("bag failure cleanup did not restore field control")


@dataclass
class _Walk:
    """Mutable state of one walk (what the generator needs to remember)."""

    state: MapState
    obs: Observation
    replans: int = 0
    npc_waits: int = 0
    map_retries: int = 0
    unknown_waits: int = 0
    steps: int = 0
    # ``approach`` inputs are allowed to be pre-empted by unrelated field events.  Once RAM
    # proves that the avatar is on the interaction tile and facing the target, the final input and
    # every scene it starts belong to this macro instead (notably Snorlax's flute text/battle).
    phase: str = "approach"


class FieldMacros:
    """The executor specs, sharing one attempt ledger with the builder."""

    def __init__(
        self,
        ledger: AttemptLedger | None = None,
        prompt_policy: PromptPolicy | None = None,
        item_catalog: ItemCatalog | None = None,
        walk_step_limit: int | None = None,
    ) -> None:
        self.ledger = ledger if ledger is not None else AttemptLedger()
        self.prompt_policy = prompt_policy
        self.item_catalog = item_catalog
        self.walk_step_limit = walk_step_limit

    # -- registration ---------------------------------------------------------------------------

    def specs(self) -> list[ExecutorSpec]:
        walk_budget = dict(max_inputs=500, max_frames=9000, settle_frames=900)
        allowed = frozenset(
            {
                *_MOVEMENT_SCENES,
                E.CURRENT,
                UNKNOWN,
                # agent #67's generic menu observation can occur at a field macro
                # handoff (START -> bag -> party).  It is transient here: the
                # executor keeps the field macro as owner and never exposes a
                # close-menu decision while the macro is running.
                "menu.open",
                "menu.start",
                "party.list",
                "party.action",
                "menu.fly_map",
                "dialog.yes_no",
                "field.map_transition",
            }
        )
        item_allowed = (
            allowed
            | system_refs.EVOLUTION_SCENES
            | frozenset(
                {
                    "menu.start",
                    "bag.items",
                    "bag.key_items",
                    "bag.poke_balls",
                    "bag.tms_hms",
                    "bag.berries",
                    "party.list",
                    "party.summary",
                    "party.action",
                    "party.held_item",
                    "party.mail",
                    "tm.list",
                    "move.list",
                    "dialog.yes_no",
                    "dialog.advance",
                    "field.scripted_movement",
                }
            )
        )
        coord = Arg(int, min=0, max=1023)
        ident = Arg(int, min=-1, max=255)
        return [
            ExecutorSpec(
                B.SWAP_PARTY,
                ExecutionKind.MACRO,
                self.swap_party,
                args={"target_personality": Arg(int, min=0, max=0xFFFF_FFFF)},
                allowed_scenes=frozenset(
                    {E.FREE, UNKNOWN, "menu.start", "party.list", "party.action", "party.reorder"}
                ),
                max_inputs=40,
                max_frames=2400,
                settle_frames=1800,
                settle_wall_s=90.0,
                failure_cleanup=_field_free_after_bag_failure,
                macro_manages_busy=True,
            ),
            ExecutorSpec(
                B.TOGGLE_BIKE,
                ExecutionKind.MACRO,
                self.toggle_bike,
                allowed_scenes=frozenset({E.FREE, UNKNOWN, "menu.start"}),
                max_inputs=40,
                max_frames=1800,
                settle_frames=900,
            ),
            ExecutorSpec(
                B.ENTER_WARP,
                ExecutionKind.INTENT,
                self.enter_warp,
                args={
                    "warp": Arg(int, min=0, max=255),
                    "dest_group": ident,
                    "dest_number": ident,
                    "dest_warp": ident,
                },
                allowed_scenes=allowed,
                **walk_budget,
            ),  # fmt: skip
            ExecutorSpec(
                B.TRAVEL_EDGE,
                ExecutionKind.INTENT,
                self.travel_edge,
                args={
                    "heading": Arg(str, choices=("N", "S", "W", "E")),
                    "dest_group": Arg(int, min=0, max=255),
                    "dest_number": Arg(int, min=0, max=255),
                    "seam_x": Arg(int, required=False, min=0, max=1023),
                    "seam_y": Arg(int, required=False, min=0, max=1023),
                },
                allowed_scenes=allowed,
                **walk_budget,
            ),  # fmt: skip
            ExecutorSpec(
                B.USE_WATERFALL,
                ExecutionKind.INTENT,
                self.use_waterfall,
                args={
                    "source_x": coord,
                    "source_y": coord,
                    "direction": Arg(str, choices=("N", "S", "W", "E")),
                    "destination_x": coord,
                    "destination_y": coord,
                },
                allowed_scenes=allowed,
                **walk_budget,
            ),  # fmt: skip
            ExecutorSpec(
                B.TALK_TO,
                ExecutionKind.MACRO,
                self.talk_to,
                args={"local_id": Arg(int, min=1, max=254)},
                allowed_scenes=allowed
                | frozenset(
                    {
                        D.PRINTING,
                        D.ADVANCE,
                        D.END,
                        D.YES_NO,
                        "field.scripted_movement",
                    }
                ),
                max_inputs=520,
                max_frames=12000,
                settle_frames=1800,
                settle_wall_s=60.0,
            ),
            ExecutorSpec(
                B.PICK_UP,
                ExecutionKind.INTENT,
                self.pick_up,
                args={"local_id": Arg(int, min=1, max=254)},
                allowed_scenes=allowed,
                **walk_budget,
            ),  # fmt: skip
            ExecutorSpec(
                B.INTERACT_WITH,
                ExecutionKind.INTENT,
                self.interact_with,
                args={"bg": Arg(int, min=0, max=255)},
                allowed_scenes=allowed,
                **walk_budget,
            ),  # fmt: skip
            ExecutorSpec(
                B.USE_PC,
                ExecutionKind.INTENT,
                self.use_pc,
                args={"x": coord, "y": coord},
                allowed_scenes=allowed,
                **walk_budget,
            ),  # fmt: skip
            ExecutorSpec(
                B.WALK_TO,
                ExecutionKind.INTENT,
                self.walk_to,
                args={"x": coord, "y": coord},
                allowed_scenes=allowed,
                **walk_budget,
            ),  # fmt: skip
            ExecutorSpec(
                B.WALK_IN_GRASS,
                ExecutionKind.INTENT,
                self.walk_in_grass,
                args={
                    "start_x": coord,
                    "start_y": coord,
                    "end_x": coord,
                    "end_y": coord,
                    "max_steps": Arg(int, min=2, max=500),
                },
                allowed_scenes=allowed,
                **walk_budget,
            ),  # fmt: skip
            ExecutorSpec(
                B.FACE,
                ExecutionKind.ATOMIC,
                self.face,
                args={"turn_to": Arg(str, choices=("N", "S", "W", "E"))},
                allowed_scenes=allowed,
            ),  # fmt: skip
            ExecutorSpec(
                B.WAIT_SCRIPT,
                ExecutionKind.DETERMINISTIC,
                self.wait_script,
                allowed_scenes=frozenset({E.TRAINER_VISION, E.FREE}),
                max_frames=2400,
                settle_frames=900,
            ),  # fmt: skip
            ExecutorSpec(
                B.TEACH_HM,
                ExecutionKind.MACRO,
                self.teach_hm,
                args={"party_slot": Arg(int, min=0, max=5)},
                allowed_scenes=allowed,
                max_inputs=60,
                max_frames=6000,
                settle_frames=1800,
                settle_wall_s=90.0,
                failure_cleanup=_field_free_after_bag_failure,
                macro_manages_busy=True,
            ),  # fmt: skip
            ExecutorSpec(
                B.USE_FLASH,
                ExecutionKind.MACRO,
                self.use_flash,
                args={
                    "party_slot": Arg(int, min=0, max=5),
                    "move_id": Arg(int, min=1, max=354),
                },
                allowed_scenes=allowed | frozenset({E.DARKNESS}),
                max_inputs=60,
                max_frames=6000,
                settle_frames=1800,
                settle_wall_s=90.0,
            ),  # fmt: skip
            ExecutorSpec(
                "item.use",
                ExecutionKind.MACRO,
                self.use_item,
                args={
                    "item_id": Arg(int, min=1, max=374),
                    "party_slot": Arg(int, min=-1, max=5),
                    "move_slot": Arg(int, min=-1, max=3),
                    "forget_move": Arg(int, min=0, max=354),
                    "move_id": Arg(int, min=0, max=354),
                },
                allowed_scenes=item_allowed,
                max_inputs=90,
                max_frames=7000,
                settle_frames=1800,
                failure_cleanup=_field_free_after_bag_failure,
                macro_manages_busy=True,
            ),
            ExecutorSpec(
                "field.use_item",
                ExecutionKind.MACRO,
                self._use_party_item,
                args={"item_id": Arg(int, min=1, max=374), "party_slot": Arg(int, min=0, max=5)},
                allowed_scenes=item_allowed,
                max_inputs=80,
                max_frames=6000,
                settle_frames=1800,
                settle_wall_s=90.0,
                failure_cleanup=_field_free_after_bag_failure,
                macro_manages_busy=True,
            ),  # fmt: skip
            ExecutorSpec(
                "field.teach_tm",
                ExecutionKind.MACRO,
                self.teach_tm,
                args={
                    "item_id": Arg(int, min=289, max=346),
                    "party_slot": Arg(int, min=0, max=5),
                    "forget_move": Arg(int, min=0, max=354),
                    "move_id": Arg(int, min=1, max=354),
                },
                allowed_scenes=item_allowed,
                max_inputs=90,
                max_frames=7000,
                settle_frames=1800,
                settle_wall_s=90.0,
                failure_cleanup=_field_free_after_bag_failure,
                macro_manages_busy=True,
            ),  # fmt: skip
            ExecutorSpec(
                B.BEGIN_TEACH_TM,
                ExecutionKind.MACRO,
                self.begin_teach_tm,
                args={"item_id": Arg(int, min=289, max=346), "move_id": Arg(int, min=1, max=354)},
                allowed_scenes=item_allowed,
                max_inputs=_begin_teach_input_budget,
                max_frames=2400,
                settle_frames=120,
                settle_wall_s=30.0,
                failure_cleanup=_field_free_after_bag_failure,
                macro_manages_busy=True,
            ),  # fmt: skip
            ExecutorSpec(
                B.FINISH_TEACH_TM,
                ExecutionKind.MACRO,
                self.finish_teach_tm,
                args={
                    "item_id": Arg(int, min=289, max=346),
                    "party_slot": Arg(int, min=0, max=5),
                    "forget_move": Arg(int, min=0, max=354),
                    "move_id": Arg(int, min=1, max=354),
                },
                allowed_scenes=item_allowed,
                max_inputs=70,
                max_frames=6000,
                settle_frames=1800,
                settle_wall_s=90.0,
                failure_cleanup=_field_free_after_bag_failure,
                macro_manages_busy=True,
            ),  # fmt: skip
            ExecutorSpec(
                B.CANCEL_TEACH_TM,
                ExecutionKind.MACRO,
                self.cancel_teach_tm,
                args={"item_id": Arg(int, min=289, max=346)},
                allowed_scenes=item_allowed,
                max_inputs=20,
                max_frames=2400,
                settle_frames=240,
                settle_wall_s=30.0,
                failure_cleanup=_field_free_after_bag_failure,
                macro_manages_busy=True,
            ),  # fmt: skip
            ExecutorSpec(
                B.USE_FLY,
                ExecutionKind.MACRO,
                self.use_fly,
                args={
                    "mapsec": Arg(int, min=0, max=255),
                    "destination_group": Arg(int, min=0, max=255),
                    "destination_number": Arg(int, min=0, max=255),
                    "party_slot": Arg(int, min=0, max=5),
                },
                allowed_scenes=allowed,
                max_inputs=80,
                max_frames=9000,
                settle_frames=2400,
                settle_wall_s=120.0,
            ),  # fmt: skip
            ExecutorSpec(
                B.FLY_MAP_RESUME,
                ExecutionKind.MACRO,
                self.resume_fly_map,
                args={
                    "mapsec": Arg(int, min=0, max=255),
                    "destination_group": Arg(int, min=0, max=255),
                    "destination_number": Arg(int, min=0, max=255),
                    "cell_x": Arg(int, min=0, max=31),
                    "cell_y": Arg(int, min=0, max=31),
                },
                allowed_scenes=allowed,
                max_inputs=80,
                max_frames=9000,
                settle_frames=2400,
                settle_wall_s=120.0,
                macro_manages_busy=True,
            ),  # fmt: skip
            ExecutorSpec(
                B.FLY_MAP_CANCEL,
                ExecutionKind.MACRO,
                self.cancel_fly_map,
                allowed_scenes=frozenset(
                    {"menu.fly_map", "party.list", UNKNOWN, E.FREE, "field.map_transition"}
                ),
                max_inputs=8,
                max_frames=3000,
                settle_frames=240,
                settle_wall_s=20.0,
                macro_manages_busy=True,
            ),  # fmt: skip
            ExecutorSpec(
                B.USE_DIG_TELEPORT,
                ExecutionKind.MACRO,
                self.use_dig_teleport,
                args={"party_slot": Arg(int, min=0, max=5), "move_id": Arg(int, choices=(91, 100))},
                allowed_scenes=allowed,
                max_inputs=80,
                max_frames=2400,
                settle_frames=600,
                settle_wall_s=45.0,
            ),  # fmt: skip
            ExecutorSpec(
                B.USE_BIKE,
                ExecutionKind.MACRO,
                self.use_bike,
                args={"mounting": Arg(bool)},
                allowed_scenes=allowed,
                max_inputs=30,
                max_frames=4000,
                settle_frames=1800,
                settle_wall_s=90.0,
            ),  # fmt: skip
        ]

    def toggle_bike(self, ctx: StepContext):
        """Toggle Bicycle with SELECT when registered, registering it once when unregistered."""
        registered = ctx.snapshot.registered_item
        if registered == BIKE_ITEM:
            if not self._overworld_free(ctx.snapshot):
                raise RuntimeError("cannot use registered Bicycle while overworld is not free")
            mounted = bool(
                ctx.snapshot.map and ctx.snapshot.map.player and ctx.snapshot.map.player.biking
            )
            yield Tap("select", hold_frames=MENU_HOLD, wait_frames=30)
            obs = yield WaitUntil(
                lambda o: (
                    o.snapshot.map is not None
                    and o.snapshot.map.player is not None
                    and o.snapshot.map.player.biking != mounted
                ),
                max_frames=180,
            )
            return obs
        if registered == 0:
            registered_ok = yield from self._toggle_bike_via_bag(ctx, register=True)
            if not registered_ok:
                raise RuntimeError("Bicycle registration did not complete")
            mounted = bool(
                ctx.snapshot.map and ctx.snapshot.map.player and ctx.snapshot.map.player.biking
            )
            yield Tap("select", hold_frames=MENU_HOLD, wait_frames=30)
            obs = yield WaitUntil(
                lambda o: (
                    o.snapshot.map is not None
                    and o.snapshot.map.player is not None
                    and o.snapshot.map.player.biking != mounted
                ),
                max_frames=180,
            )
            return obs
        yield from self._toggle_bike_via_bag(ctx)

    @staticmethod
    def _overworld_free(snapshot) -> bool:
        if not getattr(snapshot, "usable", False):
            return False
        if snapshot.map is None or snapshot.map.player is None:
            return False
        ui = getattr(snapshot, "ui", None)
        if ui is None:
            return False
        menu = getattr(ui, "menu", None)
        if menu is None:
            return False
        start = getattr(menu, "start_menu", None) if menu is not None else None
        bag = getattr(menu, "bag", None) if menu is not None else None
        if bag is not None and bag.bag_open:
            return False
        # FireRed can retain a stale START-menu task record after returning from
        # the bag; CB2_Overworld plus a closed bag is the authoritative boundary.
        if start is not None and start.task_active:
            return getattr(ui, "callback_id", None) == "CB2_Overworld"
        return True

    def _toggle_bike_via_bag(self, ctx: StepContext, *, register: bool = False):
        """Fallback for a different registered item, or legacy snapshots without the field."""
        snap = ctx.snapshot
        if snap.map is None or snap.map.player is None or not snap.usable:
            raise RuntimeError("Bicycle bag path requires a usable overworld snapshot")
        mounted = snap.map.player.biking
        pocket = next((p for p in snap.bag if p.pocket == "key_items"), None)
        if pocket is None:
            raise RuntimeError("Bicycle is unavailable: Key Items pocket is missing")
        index = next((n for n, item in enumerate(pocket.items) if item.item_id == BIKE_ITEM), None)
        if index is None:
            raise RuntimeError("Bicycle is unavailable in Key Items")
        obs = yield Tap("start", hold_frames=4, wait_frames=20)
        ui = obs.snapshot.ui
        if ui is None or ui.menu.start_menu is None:
            raise RuntimeError("Bicycle bag path could not open the START menu")
        cursor = ui.menu.start_menu.cursor_pos
        while cursor < 2:
            obs = yield Tap("down", hold_frames=4, wait_frames=8)
            ui = obs.snapshot.ui
            if ui is None or ui.menu.start_menu is None:
                raise RuntimeError("START menu disappeared while selecting Bag")
            cursor = ui.menu.start_menu.cursor_pos
        while cursor > 2:
            obs = yield Tap("up", hold_frames=4, wait_frames=8)
            ui = obs.snapshot.ui
            if ui is None or ui.menu.start_menu is None:
                raise RuntimeError("START menu disappeared while selecting Bag")
            cursor = ui.menu.start_menu.cursor_pos
        obs = yield Tap("a", hold_frames=4, wait_frames=120)
        ui = obs.snapshot.ui
        if ui is None or ui.menu.bag is None or not ui.menu.bag.bag_open:
            raise RuntimeError("Bicycle bag path did not open the Bag")
        # The UI pocket index for Key Items is 1 (the menu remembers the last
        # pocket), so never
        # assume that opening the bag starts on Items or that the saved cursor
        # is already on the Bicycle.
        bag = ui.menu.bag
        pocket = bag.pocket
        while pocket != 1:
            direction = "right" if pocket < 1 else "left"
            obs = yield Tap(direction, hold_frames=4, wait_frames=30)
            bag = obs.snapshot.ui.menu.bag if obs.snapshot.ui and obs.snapshot.ui.menu else None
            if bag is None or bag.pocket == pocket:
                raise RuntimeError("could not select Key Items pocket")
            pocket = bag.pocket
        bag = obs.snapshot.ui.menu.bag if obs.snapshot.ui and obs.snapshot.ui.menu else None
        if bag is None:
            raise RuntimeError("Bag evidence disappeared while selecting Bicycle")

        def absolute_cursor(info, menu) -> int:
            # The bag struct's key-item cursor is stale while the list task is
            # active; the list evidence is the cursor the game is reading.
            listing = menu.list_menu if menu is not None else None
            if listing is not None and listing.window_in_use:
                return listing.scroll_offset + listing.cursor_row
            return info.items_above[1] + info.cursor_pos[1]

        # The live list cursor is one-based, with the final row reserved for
        # Cancel; item index 0 therefore appears at cursor row 1.
        target_cursor = index
        if target_cursor == 0:
            target_cursor = 1
        current = absolute_cursor(bag, obs.snapshot.ui.menu)
        for _ in range(abs(target_cursor - current)):
            direction = "down" if current < target_cursor else "up"
            obs = yield Tap(direction, hold_frames=4, wait_frames=8)
            menu = obs.snapshot.ui.menu if obs.snapshot.ui else None
            bag = menu.bag if menu else None
            if bag is None:
                raise RuntimeError("Bag evidence disappeared while moving to Bicycle")
            next_cursor = absolute_cursor(bag, menu)
            if next_cursor == current:
                raise RuntimeError("Bicycle cursor did not move")
            current = next_cursor
        if current != target_cursor:
            raise RuntimeError("Bicycle cursor did not reach the Bicycle")
        obs = yield Tap("a", hold_frames=4, wait_frames=30)
        menu = obs.snapshot.ui.menu if obs.snapshot.ui else None
        context = menu.menu if menu is not None else None
        if (
            context is None
            or not context.window_in_use
            or context.top != 2
            or context.cursor_pos != 0
        ):
            raise RuntimeError("Bicycle context menu is not on Use")
        if register:
            obs = yield Tap("down", hold_frames=4, wait_frames=8)
            context = (
                obs.snapshot.ui.menu.menu if obs.snapshot.ui and obs.snapshot.ui.menu else None
            )
            if context is None or not context.window_in_use or context.cursor_pos != 1:
                raise RuntimeError("Bicycle context menu is not on Register")
            obs = yield Tap("a", hold_frames=4, wait_frames=60)
            obs = yield WaitUntil(
                lambda current: bool(
                    current.snapshot.ui
                    and current.snapshot.ui.menu.bag
                    and current.snapshot.ui.menu.bag.bag_open
                ),
                max_frames=120,
            )
            obs = yield Tap("b", hold_frames=4, wait_frames=30)
            obs = yield WaitUntil(
                lambda current: (
                    not bool(
                        current.snapshot.ui
                        and current.snapshot.ui.menu.bag
                        and current.snapshot.ui.menu.bag.bag_open
                    )
                ),
                max_frames=120,
            )

            def menus_closed(current) -> bool:
                snapshot = current.snapshot
                ui = snapshot.ui
                menu = ui.menu if ui else None
                bag = menu.bag if menu else None
                start = menu.start_menu if menu else None
                return not (bag and bag.bag_open) and (
                    start is None or getattr(ui, "callback_id", None) == "CB2_Overworld"
                )

            for _ in range(2):
                start = (
                    obs.snapshot.ui.menu.start_menu
                    if obs.snapshot.ui and obs.snapshot.ui.menu
                    else None
                )
                if start is None or not start.task_active:
                    break
                obs = yield Tap("b", hold_frames=4, wait_frames=30)
                obs = yield WaitUntil(menus_closed, max_frames=120)
            if obs.snapshot.registered_item != BIKE_ITEM or not self._overworld_free(obs.snapshot):
                raise RuntimeError(
                    "Bicycle registration did not produce a free registered overworld"
                )
            return True
        yield Tap("a", hold_frames=4, wait_frames=60)
        obs = yield WaitUntil(
            lambda o: (
                o.snapshot.map is not None
                and o.snapshot.map.player is not None
                and o.snapshot.map.player.biking != mounted
            ),
            max_frames=180,
        )
        return obs

    # -- the simple ones ---------------------------------------------------------------------------

    @staticmethod
    def face(ctx: StepContext) -> Step:
        return Tap(BUTTON[ctx.args["turn_to"]], hold_frames=TURN_HOLD, wait_frames=TURN_WAIT)

    @staticmethod
    def wait_script(ctx: StepContext) -> Step:
        """A trainer's approach plays out by itself; the dialog family takes over after it."""
        return WaitUntil(lambda o: o.scene.scene_id != E.TRAINER_VISION, max_frames=2400)

    # -- field tools -----------------------------------------------------------------------------

    @staticmethod
    def _ui(snapshot):
        ui = snapshot.ui
        if ui is None or not ui.valid:
            raise RuntimeError("ui evidence is not valid")
        return ui

    @classmethod
    def _start_menu(cls, snapshot):
        menu = cls._ui(snapshot).menu.start_menu
        if menu is None or menu.task_active is False:
            raise RuntimeError("start menu is not active")
        return menu

    @classmethod
    def _s_menu(cls, snapshot):
        menu = cls._ui(snapshot).menu.menu
        if menu is None or not menu.window_in_use or not menu.consistent:
            raise RuntimeError("menu cursor is not corroborated")
        return menu

    @classmethod
    def _bag(cls, snapshot):
        bag = cls._ui(snapshot).menu.bag
        if bag is None or not bag.bag_open or bag.active_hint is False:
            raise RuntimeError("bag is not active")
        return bag

    @classmethod
    def _party(cls, snapshot):
        party = cls._ui(snapshot).menu.party
        if party is None or party.active_hint is False:
            raise RuntimeError("party menu is not active")
        return party

    @staticmethod
    def _step_cursor(current: int, wanted: int) -> str | None:
        if current == wanted:
            return None
        return "down" if wanted > current else "up"

    @classmethod
    def _move_cursor(cls, ctx, obs, wanted: int, read, *, limit: int = 12):
        current = read(obs.snapshot)
        for _ in range(limit):
            if current == wanted:
                return obs
            if current is None:
                raise RuntimeError("cursor is unreadable")
            button = cls._step_cursor(current, wanted)
            assert button is not None
            new = current
            for _ in range(3):
                obs = yield Tap(button, hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
                try:
                    new = read(obs.snapshot)
                except RuntimeError:
                    new = current
                if new != current:
                    break
            if new != current + (1 if button == "down" else -1):
                raise RuntimeError("cursor did not move by one")
            current = new
        raise RuntimeError("cursor budget exceeded")

    @staticmethod
    def _read_is(read, snapshot, expected) -> bool:
        try:
            return read(snapshot) == expected
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return False

    @classmethod
    def _move_cursor_gated(cls, ctx, obs, wanted: int, read, *, label: str, limit: int = 12):
        """Move one row at a time; every press must reach the exact next RAM cursor."""
        current = read(obs.snapshot)
        for _ in range(limit):
            if current == wanted:
                return obs
            if current is None:
                raise RuntimeError(f"{label} cursor is unreadable")
            button = cls._step_cursor(current, wanted)
            assert button is not None
            expected = current + (1 if button == "down" else -1)
            obs = yield Tap(button, hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
            obs = yield WaitUntil(
                lambda observation, expected=expected: cls._read_is(
                    read, observation.snapshot, expected
                ),
                max_frames=90,
                failure_reason=f"{label} cursor did not reach verified row {expected}",
            )
            current = read(obs.snapshot)
        raise RuntimeError(f"{label} cursor budget exceeded")

    @classmethod
    def _select_start_for_tm(cls, ctx):
        """Open START and choose Bag only from its live RAM cursor."""
        obs = Observation(ctx.snapshot, ctx.scene)

        def start_ready(current):
            ui = current.snapshot.ui
            start = ui.menu.start_menu if ui is not None and ui.valid else None
            return bool(
                current.scene.scene_id in {"menu.start", "menu.open", UNKNOWN}
                and not current.scene.busy
                and start is not None
                and start.task_active is True
                and ui.field.fade_active is False
            )

        if not start_ready(obs):
            obs = yield Tap("start", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
            obs = yield WaitUntil(
                start_ready,
                max_frames=90,
                failure_reason="teach_tm could not open an input-ready START menu",
            )
        obs = yield from cls._move_cursor_gated(
            ctx,
            obs,
            START_BAG,
            lambda snapshot: cls._start_menu(snapshot).cursor_pos,
            label="START Bag",
            limit=8,
        )
        obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)

        def bag_ready(current):
            ui = current.snapshot.ui
            menu = ui.menu if ui is not None and ui.valid else None
            return bool(
                current.scene.scene_id
                in {
                    "menu.open",
                    UNKNOWN,
                    "bag.items",
                    "bag.key_items",
                    "bag.poke_balls",
                    "bag.tms_hms",
                    "bag.berries",
                }
                and not current.scene.busy
                and ui.field.fade_active is False
                and menu is not None
                and menu.bag is not None
                and menu.bag.bag_open
                and menu.bag.active_hint is True
                and menu.list_menu is not None
                and menu.list_menu.window_in_use
            )

        return (
            yield WaitUntil(
                bag_ready,
                max_frames=180,
                failure_reason="teach_tm START Bag selection did not open the verified Bag list",
            )
        )

    @classmethod
    def _select_tm_case_from_bag(cls, ctx, obs):
        """Select TM Case by its RAM item id in Key Items, then verify USE."""
        current = cls._bag(obs.snapshot).pocket
        for _ in range(6):
            if current == TM_CASE_POCKET:
                break
            button = "right" if TM_CASE_POCKET > current else "left"
            expected = current + (1 if button == "right" else -1)
            obs = yield Tap(button, hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
            obs = yield WaitUntil(
                lambda observation, expected=expected: cls._read_is(
                    lambda snapshot: cls._bag(snapshot).pocket,
                    observation.snapshot,
                    expected,
                ),
                max_frames=90,
                failure_reason=f"teach_tm Bag did not reach verified pocket {expected}",
            )
            current = cls._bag(obs.snapshot).pocket
        else:
            raise RuntimeError("teach_tm Bag pocket cursor budget exceeded")

        items = cls._bag_items(obs.snapshot, "key_items")
        row = next(
            (index for index, item in enumerate(items) if item.item_id == TM_CASE_ITEM), None
        )
        if row is None:
            raise RuntimeError("TM Case item id 364 is absent from the verified Key Items pocket")

        def bag_row(snapshot):
            ui = cls._ui(snapshot)
            listing = ui.menu.list_menu
            if ui.menu_type != "bag" or listing is None or not listing.window_in_use:
                raise RuntimeError("Bag item list cursor is not corroborated")
            return listing.scroll_offset + listing.cursor_row

        obs = yield from cls._move_cursor_gated(
            ctx, obs, row, bag_row, label="TM Case item", limit=len(items) + 4
        )
        obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)

        def use_ready(current):
            ui = current.snapshot.ui
            if ui is None or not ui.valid or ui.menu_type != "bag":
                return False
            menu = ui.menu.menu
            return bool(
                menu is not None
                and menu.window_in_use
                and menu.consistent
                and menu.cursor_pos == menu.min_cursor_pos
                and any(
                    task.func.name == "Task_FieldItemContextMenuHandleInput"
                    for task in ui.tasks.active
                )
            )

        return (
            yield WaitUntil(
                use_ready,
                max_frames=120,
                failure_reason="teach_tm TM Case context menu did not reach verified USE",
            )
        )

    @classmethod
    def _open_tm_case_for_teaching(cls, ctx):
        obs = yield from cls._select_start_for_tm(ctx)
        obs = yield from cls._select_tm_case_from_bag(ctx, obs)
        before_tasks = tuple(
            (task.slot, task.func.ptr) for task in cls._ui(obs.snapshot).tasks.active
        )
        obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)

        def tm_list_ready(current):
            ui = current.snapshot.ui
            if ui is None or not ui.valid or ui.menu_type != "list":
                return False
            listing = ui.menu.list_menu
            tasks = tuple((task.slot, task.func.ptr) for task in ui.tasks.active)
            return bool(
                current.scene.scene_id in {"menu.open", "tm.list", UNKNOWN}
                and not current.scene.busy
                and ui.field.fade_active is False
                and listing is not None
                and listing.window_in_use
                and tasks != before_tasks
            )

        obs = yield WaitUntil(
            tm_list_ready,
            max_frames=240,
            failure_reason="teach_tm verified USE did not open the TM Case list",
        )
        # The list window and tasks are allocated just before the TM Case restores its saved
        # cursor and enables list input.  Settle without input, then re-verify the RAM-owned list;
        # no directional key is sent into that handoff window.
        obs = yield Advance(frames=12)
        return (
            yield WaitUntil(
                tm_list_ready,
                max_frames=60,
                failure_reason="teach_tm TM Case list did not become input-ready after cursor restore",  # noqa: E501
            )
        )

    @classmethod
    def _open_start(cls, ctx):
        obs = Observation(ctx.snapshot, ctx.scene)
        start = cls._ui(obs.snapshot).menu.start_menu
        if start is None or not start.task_active:
            obs = yield Tap("start", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
            obs = yield WaitUntil(
                lambda current: (
                    current.snapshot.ui is not None
                    and current.snapshot.ui.menu.start_menu is not None
                    and current.snapshot.ui.menu.start_menu.task_active is True
                ),
                max_frames=60,
            )
            # The start-menu task becomes visible a few frames before its input handler is
            # installed.  A press in that window is ignored and leaves the menu open forever.
            obs = yield Advance(frames=8)
            start = cls._start_menu(obs.snapshot)
        return obs

    @classmethod
    def _select_start(cls, ctx, wanted: int):
        obs = yield from cls._open_start(ctx)
        obs = yield from cls._move_cursor(
            ctx,
            obs,
            wanted,
            lambda snapshot: cls._start_menu(snapshot).cursor_pos,
            limit=8,
        )
        obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
        if wanted == START_BAG:
            obs = yield WaitUntil(
                lambda current: bool(
                    current.snapshot.ui
                    and current.snapshot.ui.menu.bag
                    and current.snapshot.ui.menu.bag.active_hint
                    and current.snapshot.ui.field.fade_active is False
                    and current.snapshot.ui.tasks is not None
                    and any(
                        task.func.name == "Task_BagMenu_HandleInput"
                        for task in current.snapshot.ui.tasks.active
                    )
                ),
                max_frames=90,
            )
        elif wanted == START_POKEMON:
            obs = yield WaitUntil(
                lambda current: bool(
                    current.snapshot.ui
                    and current.snapshot.ui.menu.party
                    and current.snapshot.ui.menu.party.active_hint
                ),
                max_frames=90,
            )
        return obs

    @classmethod
    def _select_party_slot(cls, ctx, obs, slot: int, *, generic: bool = False):
        # CB2_UpdatePartyMenu installs Task_HandleChooseMonInput before the fade-in finishes,
        # but that task deliberately ignores every key while gPaletteFade.active is set.  Wait
        # for the game-owned input gate instead of sending a fixed-delay A press into the fade.
        obs = yield WaitUntil(
            lambda current: bool(
                current.snapshot.ui is not None
                and current.snapshot.ui.valid
                and current.snapshot.ui.menu.party is not None
                and current.snapshot.ui.menu.party.active_hint is True
                and current.snapshot.ui.field.fade_active is False
                and (
                    not hasattr(current.snapshot.ui, "tasks")
                    or _task_active(current.snapshot.ui, "Task_HandleChooseMonInput")
                )
                and (
                    not generic
                    or (
                        current.snapshot.ui.menu.party.action_name == "use_item"
                        and current.snapshot.ui.main.callback2.name == "CB2_UpdatePartyMenu"
                    )
                )
            ),
            max_frames=90,
            failure_reason="item USE did not open an input-ready party target list",
        )
        if generic:
            member = next((m for m in ctx.snapshot.party if m.slot == slot), None)
            if member is None:
                raise RuntimeError("party slot disappeared before generic menu selection")
            obs = yield from cls._move_cursor(
                ctx,
                obs,
                slot,
                lambda snapshot: cls._party(snapshot).slot_id,
                limit=6,
            )

            def selection_started(current) -> bool:
                ui = current.snapshot.ui
                if ui is None or not ui.valid or ui.main.callback2 is None:
                    return False
                return bool(
                    ui.field.fade_active
                    or ui.main.callback2.name != "CB2_UpdatePartyMenu"
                    or not _task_active(ui, "Task_HandleChooseMonInput")
                )

            obs = yield Tap(
                "a",
                hold_frames=MENU_HOLD,
                wait_frames=MENU_WAIT,
                until=selection_started,
                max_hold_frames=8,
            )
            return (
                yield WaitUntil(
                    selection_started,
                    max_frames=120,
                    failure_reason=(
                        f"party target {member.personality:08x} did not accept item confirmation"
                    ),
                )
            )
        obs = yield from cls._move_cursor(
            ctx,
            obs,
            slot,
            lambda snapshot: cls._party(snapshot).slot_id,
            limit=6,
        )
        return (yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT))

    def swap_party(self, ctx):
        """Swap the lead with a RAM-identified party member through the native menus."""
        from pokegent.games.gen3.firered.reader.menu_decoders import (
            decode_party_action,
            decode_party_menu,
            decode_party_reorder,
        )

        target = int(ctx.args["target_personality"])
        obs = yield from self._select_start(ctx, START_POKEMON)
        obs = yield from self._select_party_slot(ctx, obs, 0)

        def party_action_ready(current) -> bool:
            ui = current.snapshot.ui
            return bool(
                ui is not None
                and ui.valid
                and ui.field.fade_active is False
                and ui.menu.party is not None
                and ui.menu.party.active_hint is True
                and ui.menu.party.action_name is not None
                and ui.menu.party.action_ids
                and _task_active(ui, "Task_HandleSelectionMenuInput")
            )

        # The choose-mon task can remain visible while the ROM builds the action window.  The
        # party action decoder must not inspect that transitional/stale menu model.  If the first
        # A landed in the task handoff, retry only while the RAM party model proves the same lead
        # is still selected, matching the shared START/party executor's confirmation policy.
        def confirmation_retryable(current) -> bool:
            ui = current.snapshot.ui
            model = decode_party_menu(current.snapshot)
            return bool(
                ui is not None
                and ui.valid
                and ui.field.fade_active is False
                and ui.menu.party is not None
                and ui.menu.party.slot_id == 0
                and model is not None
                and model.selected_key == f"party:{ctx.snapshot.party[0].personality:08x}"
                and _task_active(ui, "Task_HandleChooseMonInput")
            )

        for _ in range(2):
            obs = yield WaitUntil(
                lambda current: party_action_ready(current) or confirmation_retryable(current),
                max_frames=90,
            )
            if party_action_ready(obs):
                break
            obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
        else:
            raise RuntimeError("party target confirmation did not open the action menu")
        obs = yield from select_menu_entry(
            obs,
            "party_action:switch",
            decode_party_action,
            tap=lambda button: Tap(button, hold_frames=MENU_HOLD, wait_frames=MENU_WAIT),
            max_moves=12,
            readback_retries=2,
        )

        def party_reorder_ready(current) -> bool:
            ui = current.snapshot.ui
            return bool(
                ui is not None
                and ui.valid
                and ui.field.fade_active is False
                and ui.menu.party is not None
                and ui.menu.party.active_hint is True
                and ui.menu.party.action_name == "switch"
                and _task_active(ui, "Task_HandleChooseMonInput")
            )

        # SWITCH reuses the party structures, so action-menu evidence can survive until the
        # reorder target task owns input.  Wait for that RAM fact before decoding slot_id2.
        obs = yield WaitUntil(
            party_reorder_ready,
            max_frames=120,
        )
        obs = yield from select_menu_entry(
            obs,
            f"party:{target:08x}",
            decode_party_reorder,
            tap=lambda button: Tap(button, hold_frames=MENU_HOLD, wait_frames=MENU_WAIT),
            max_moves=12,
            readback_retries=2,
        )

        # The native reorder confirmation leaves the party list open.  Close that owned menu so
        # this one-step field option has the same defined post-state as the manual path.
        obs = yield WaitUntil(
            lambda current: bool(
                current.snapshot.ui is not None
                and current.snapshot.ui.valid
                and current.snapshot.ui.menu.party is not None
                and current.snapshot.ui.menu.party.active_hint is True
                and current.snapshot.ui.field.fade_active is False
                and _task_active(current.snapshot.ui, "Task_HandleChooseMonInput")
            ),
            max_frames=120,
        )
        for _ in range(4):
            obs = yield Tap("b", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
            obs = yield WaitUntil(
                lambda current: (
                    current.scene.scene_id == E.FREE
                    or _task_active(current.snapshot.ui, "Task_StartMenuHandleInput")
                    or _task_active(current.snapshot.ui, "Task_HandleChooseMonInput")
                ),
                max_frames=120,
            )
            if obs.scene.scene_id == E.FREE:
                break
            if _task_active(obs.snapshot.ui, "Task_StartMenuHandleInput"):
                yield Advance(frames=8)
        # If the ROM published a stale menu observation at the boundary, give the now-owned
        # start-menu task another bounded close opportunity before waiting for field control.
        for _ in range(2):
            yield Tap("b", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
            yield Advance(frames=8)
        # A shop/door exit can publish one transient field.free frame before the map warp callback
        # completes; let that owned transition settle before handing control back.
        yield Advance(frames=180)
        yield WaitUntil(
            lambda current: current.scene.scene_id == E.FREE and _free_field_observation(current),
            max_frames=180,
        )

    @staticmethod
    def _bag_items(snapshot, pocket: str):
        return next((p.items for p in snapshot.bag if p.pocket == pocket), ())

    @classmethod
    def _item_context_menu_use_ready(cls, current) -> bool:
        """Return true only after the bag's item context menu owns the input.

        The first observation after confirming a bag row can still describe the old list
        window while the field task creates the context menu.  Both windows and the RAM
        cursor are read on every observation so callers never press through that handoff.
        """
        try:
            ui = cls._ui(current.snapshot)
            bag = ui.menu.bag
            menu = cls._s_menu(current.snapshot)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return False
        tasks = getattr(ui, "tasks", None)
        if tasks is not None and not any(
            task.func.name == "Task_FieldItemContextMenuHandleInput" for task in tasks.active
        ):
            return False
        return bool(
            getattr(ui, "menu_type", None) in {None, "bag"}
            and bag is not None
            and bag.bag_open
            and bag.active_hint is True
            and menu.cursor_pos == menu.min_cursor_pos
            and menu.max_cursor_pos >= menu.min_cursor_pos
        )

    @classmethod
    def _select_bag_item(cls, ctx, obs, item_id: int, pocket: str, pocket_index: int):
        current = cls._bag(obs.snapshot).pocket
        for _ in range(6):
            if current == pocket_index:
                break
            button = "right" if pocket_index > current else "left"
            new = current
            for _ in range(3):
                obs = yield Tap(button, hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
                try:
                    new = cls._bag(obs.snapshot).pocket
                except RuntimeError:
                    new = current
                if new != current:
                    break
            if new != current + (1 if button == "right" else -1):
                raise RuntimeError("bag pocket did not move by one")
            current = new
        else:
            raise RuntimeError("bag pocket cursor budget exceeded")
        items = cls._bag_items(ctx.snapshot, pocket)
        row = next((i for i, item in enumerate(items) if item.item_id == item_id), None)
        if row is None:
            raise RuntimeError(f"item {item_id} is not in the verified bag pocket")

        def list_row(snapshot):
            listing = cls._ui(snapshot).menu.list_menu
            if listing is None or not listing.window_in_use:
                raise RuntimeError("bag item list cursor is not corroborated")
            return listing.scroll_offset + listing.cursor_row

        obs = yield from cls._move_cursor(
            ctx,
            obs,
            row,
            list_row,
            limit=len(items) + 4,
        )
        obs = yield Tap(
            "a",
            hold_frames=MENU_HOLD,
            wait_frames=MENU_WAIT,
            until=cls._item_context_menu_use_ready,
            max_hold_frames=8,
        )
        return (
            yield WaitUntil(
                cls._item_context_menu_use_ready,
                max_frames=120,
                failure_reason="item context menu did not open at USE",
            )
        )

    @classmethod
    def _open_tm_case(cls, ctx):
        """Open the TM Case key item; FireRed's normal Bag has no TM/HM pocket."""
        obs = yield from cls._select_start(ctx, START_BAG)
        obs = yield from cls._select_bag_item(ctx, obs, TM_CASE_ITEM, "key_items", TM_CASE_POCKET)
        obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
        obs = yield WaitUntil(
            lambda current: bool(
                current.snapshot.ui
                and current.snapshot.ui.menu.list_menu
                and current.snapshot.ui.menu.list_menu.window_in_use
            ),
            max_frames=180,
        )
        # The list window is visible before the TM Case has restored its cursor from RAM.  Do not
        # walk from that transient row: the next observation is the authoritative list cursor.
        return (yield Advance(frames=12))

    @staticmethod
    def _tm_list_row(snapshot):
        listing = snapshot.ui.menu.list_menu if snapshot.ui else None
        if listing is None or not listing.window_in_use:
            raise RuntimeError("TM Case list cursor is not corroborated")
        return listing.scroll_offset + listing.cursor_row

    @staticmethod
    def _tm_case_item_row(items, item_id: int) -> int:
        """Return the TM Case row for an item in the verified pocket.

        FireRed presents held HMs first, followed by held TMs in item-number order; the save
        pocket itself is not in that display order.  The final CLOSE row is outside this list.
        """
        hms = sorted(item.item_id for item in items if 339 <= item.item_id <= 346)
        tms = sorted(item.item_id for item in items if 289 <= item.item_id <= 338)
        ordered = hms + tms
        try:
            return ordered.index(item_id)
        except ValueError as exc:
            raise RuntimeError(f"item {item_id} is not in the verified TM Case inventory") from exc

    def use_bike(self, ctx):
        obs = yield from self._select_start(ctx, START_BAG)
        obs = yield from self._select_bag_item(ctx, obs, 360, "key_items", KEY_ITEMS_POCKET)
        yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)

    def teach_hm(self, ctx):
        item_id = int(ctx.args.get("item_id", 340))
        move_id = int(ctx.args.get("move_id", 19 if item_id == 340 else 0))
        if move_id == 0 and self.item_catalog is not None:
            move_id = int(self.item_catalog.item(item_id).move_id or 0)
        obs = yield from self._open_tm_case(ctx)
        items = self._bag_items(ctx.snapshot, "tms_hms")
        row = self._tm_case_item_row(items, item_id)
        obs = yield from self._move_cursor(ctx, obs, row, self._tm_list_row, limit=len(items) + 4)
        obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
        self._s_menu(obs.snapshot)
        obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
        obs = yield WaitUntil(
            lambda current: bool(
                current.snapshot.ui
                and current.snapshot.ui.menu.party
                and current.snapshot.ui.menu.party.active_hint
            ),
            max_frames=180,
        )
        slot = int(ctx.args["party_slot"])
        mon = next((member for member in ctx.snapshot.party if member.slot == slot), None)
        if mon is None:
            raise RuntimeError("HM carrier disappeared from the verified party")
        obs = yield from self._select_party_slot(ctx, obs, slot)
        # A four-move party member exposes a text-backed YES/NO prompt.  Advance the text first,
        # then confirm YES only after the prompt's own window is ready.
        if sum(1 for move in mon.moves if move.move_id) >= 4:
            for _ in range(20):
                obs = yield WaitUntil(_teach_prompt_observation, max_frames=240)
                yes_no = obs.snapshot.ui.menu.yes_no
                choice = obs.snapshot.ui.menu.menu
                if (yes_no is not None and yes_no.task_active is True) or (
                    choice is not None and choice.window_in_use and choice.max_cursor_pos == 1
                ):
                    break
                obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
            else:
                raise RuntimeError("HM learn prompt did not become input-ready")
            # The yes/no window is allocated before its party-menu input task starts reading keys.
            # Let the party-menu callback finish installing its handler before confirming.
            obs = yield Advance(frames=30)
            obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
            # The first A dismisses the YES/NO window; FireRed then prints one more message before
            # it opens the move summary.  Acknowledge that message from its live printer state.
            for _ in range(4):
                obs = yield WaitUntil(
                    lambda current: (
                        bool(
                            current.snapshot.ui
                            and current.snapshot.ui.main
                            and current.snapshot.ui.main.callback2
                            and current.snapshot.ui.main.callback2.name
                            == "obs_cb2_pokemon_summary_screen"
                        )
                        or bool(
                            current.snapshot.ui
                            and current.snapshot.ui.text
                            and current.snapshot.ui.text.waiting_for_button
                        )
                    ),
                    max_frames=240,
                )
                if (
                    obs.snapshot.ui
                    and obs.snapshot.ui.main
                    and obs.snapshot.ui.main.callback2
                    and obs.snapshot.ui.main.callback2.name == "obs_cb2_pokemon_summary_screen"
                ):
                    break
                obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
            else:
                raise RuntimeError("HM learn confirmation did not open the move summary")
            member_index = next(
                (index for index, member in enumerate(ctx.snapshot.party) if member.slot == slot),
                None,
            )
            if member_index is None:
                raise RuntimeError("HM carrier index disappeared from the verified party")
            forget_slot = next(
                (
                    index
                    for index, move in enumerate(mon.moves)
                    if move.move_id not in system_moves.HM_MOVE_IDS
                ),
                None,
            )
            if forget_slot is None:
                raise RuntimeError("HM carrier has no safe non-HM move to forget")
            # Reuse the merged PV-16 summary cursor implementation verbatim.  This handoff supplies
            # only fresh move facts; the field family does not reproduce its cursor walk.
            forget_ctx = replace(
                ctx,
                args={
                    "slot": forget_slot,
                    "old_move": mon.moves[forget_slot].move_id,
                    "new_move": move_id,
                    "party_id": member_index,
                },
            )

            def move_changed(current) -> bool:
                member = next(
                    (candidate for candidate in current.snapshot.party if candidate.slot == slot),
                    None,
                )
                return bool(
                    member is not None and any(move.move_id == move_id for move in member.moves)
                )

            # Drive the merged PV-16 generator verbatim through its cursor taps.  Its final wait
            # assumes evolution's summary closes only after the move mutation; field HM teaching
            # has extra replacement-animation pages, so substitute only that final wait.
            pv16 = forget_move_macro(forget_ctx)
            pv16_obs = None
            pv16_waits = 0
            while True:
                try:
                    pv16_step = next(pv16) if pv16_obs is None else pv16.send(pv16_obs)
                except StopIteration:
                    break
                if isinstance(pv16_step, WaitUntil):
                    pv16_waits += 1
                    if pv16_waits == 2:
                        break
                pv16_obs = yield pv16_step
            for _ in range(12):
                obs = yield WaitUntil(
                    lambda current: (
                        move_changed(current)
                        or bool(
                            current.snapshot.ui
                            and current.snapshot.ui.text
                            and current.snapshot.ui.text.waiting_for_button
                        )
                    ),
                    max_frames=900,
                )
                if move_changed(obs):
                    break
                obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
            else:
                raise RuntimeError("HM replacement animation did not commit Fly")
            for _ in range(8):
                obs = yield WaitUntil(
                    lambda current: _teach_cleanup_observation(current, slot, move_id),
                    max_frames=300,
                    failure_reason="teach_hm cleanup did not reach a verified menu or field boundary",  # noqa: E501
                )
                if _tm_done_observation(obs, slot, move_id):
                    return
                ui = obs.snapshot.ui
                menu = ui.menu if ui else None
                if menu is not None and menu.summary_screen_open:
                    button = "b"
                elif ui is not None and ui.text is not None and ui.text.waiting_for_button:
                    button = "a"
                elif menu is not None and menu.yes_no is not None and menu.yes_no.window_in_use:
                    button = "a"
                else:
                    button = "b"
                obs = yield Tap(button, hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
            yield WaitUntil(
                lambda current: _tm_done_observation(current, slot, move_id),
                max_frames=300,
                failure_reason="teach_hm did not return to field with the learned move in party RAM",  # noqa: E501
            )
            return
        # With a free move slot the ROM displays one final message after the party selection.  A
        # single corroborated A closes that message; the RAM verifier below then waits for both the
        # new move and the field callback, so this macro never returns with a menu open.
        obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
        yield WaitUntil(
            lambda current: _tm_done_observation(current, slot, move_id),
            max_frames=300,
            failure_reason="teach_hm did not return to field with the learned move in party RAM",
        )

    def _use_party_item(self, ctx, record=None):
        """Use a field item, checking every cursor boundary and the party RAM result."""
        item_id = int(ctx.args["item_id"])
        slot = int(ctx.args["party_slot"])
        before = next((m for m in ctx.snapshot.party if m.slot == slot), None)
        if before is None or fieldmenu.quantity(ctx.snapshot, item_id) < 1:
            raise RuntimeError("item or party slot disappeared from the fresh snapshot")
        pocket, pocket_index = (
            _BAG_POCKET.get(getattr(record.data, "pocket", 1), ("items", 0))
            if record is not None
            else ("items", 0)
        )
        obs = yield from self._select_start(ctx, START_BAG)
        obs = yield from self._select_bag_item(ctx, obs, item_id, pocket, pocket_index)
        menu = self._s_menu(obs.snapshot)
        if menu.cursor_pos != menu.min_cursor_pos:
            raise RuntimeError("item USE cursor is not verified")
        obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
        obs = yield from self._select_party_slot(ctx, obs, slot, generic=True)

        kind = fieldmenu.ITEM_EFFECTS.get(item_id, ("", 0))[0]

        def changed(current):
            mon = next((m for m in current.snapshot.party if m.slot == slot), None)
            if mon is None:
                return False
            if record is not None:
                return party_item_use_verified(
                    ctx.snapshot, current.snapshot, item_id, slot, kind
                )
            if kind == "evolution":
                return (
                    current.scene.scene_id.startswith("evolution.") or mon.species != before.species
                )
            if kind in {"hp", "revive"}:
                return mon.hp != before.hp
            return mon.status != before.status

        def item_animation_ack_ready(current) -> bool:
            ui = current.snapshot.ui
            if ui is None or not ui.valid or ui.main.callback2 is None:
                return False
            return any(
                task.func.name == "Task_UseItem_Normal"
                and task.item_use_anim is not None
                and task.item_use_anim.state == 12
                and task.item_use_anim.cancel_disabled
                and task.item_use_anim.item_id == item_id
                for task in ui.tasks.active
            )

        def item_flow_started(current) -> bool:
            """Distinguish PSA-backed items from direct party-menu effects using live RAM."""
            ui = current.snapshot.ui
            return bool(
                ui is not None
                and ui.valid
                and (
                    _task_active(ui, "Task_UseItem_Normal")
                    or changed(current)
                    or current.scene.scene_id in system_refs.EVOLUTION_SCENES
                    or (
                        ui.main.callback2 is not None
                        and ui.main.callback2.name == "CB2_UpdatePartyMenu"
                        and ui.field.fade_active is False
                        and ui.text is not None
                        and ui.text.waiting_for_button
                    )
                )
            )

        obs = yield WaitUntil(
            item_flow_started,
            max_frames=900,
            failure_reason="party item did not enter a RAM-confirmed result path",
        )

        # Potion-style PSA is not universal (some party items remain in the party callback).
        # Once RAM proves this task owns the flow, and only then, wait for pret state 12 and
        # acknowledge it. Direct result paths skip the PSA gate entirely.
        if _task_active(obs.snapshot.ui, "Task_UseItem_Normal"):
            if not item_animation_ack_ready(obs):
                obs = yield WaitUntil(
                    item_animation_ack_ready,
                    max_frames=900,
                    failure_reason="item-use animation did not reach its RAM acknowledgment state",
                )
            obs = yield Tap(
                "a",
                hold_frames=MENU_HOLD,
                wait_frames=MENU_WAIT,
                until=lambda current: not item_animation_ack_ready(current),
                max_hold_frames=8,
            )

            obs = yield WaitUntil(
                lambda current: bool(
                    current.snapshot.ui is not None
                    and current.snapshot.ui.valid
                    and current.snapshot.ui.main.callback2 is not None
                    and current.snapshot.ui.main.callback2.name != "CB2_PSA"
                    and not _task_active(current.snapshot.ui, "Task_UseItem_Normal")
                ),
                max_frames=240,
                failure_reason="item-use animation callback did not return to the party flow",
            )

        obs = yield WaitUntil(
            changed,
            max_frames=360,
            failure_reason="party item effect did not commit in RAM",
        )
        effect_verified = True

        is_evolution = (
            record is not None and EffectClass.EVOLVE in record.capability.effects
        ) or kind == "evolution"
        if is_evolution:
            # Item evolutions need no strategic choice: wait through pret's evolution task, then
            # acknowledge its terminal message before continuing through Bag/START teardown.
            obs = yield WaitUntil(
                lambda current: current.scene.scene_id == system_refs.EVO_DONE,
                max_frames=4800,
                failure_reason="item evolution did not reach its RAM-confirmed done message",
            )
            obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
            obs = yield WaitUntil(
                lambda current: current.scene.scene_id not in system_refs.EVOLUTION_SCENES,
                max_frames=600,
                failure_reason="item evolution callback did not return to the field menu flow",
            )

        # Ordinary medicine effects have already committed both RAM facts above.  The live
        # field-option path does not expose its final callback as a party/bag input gate.
        if kind in {"hp", "revive", "status"}:
            ui = obs.snapshot.ui
            if ui is not None and ui.valid and ui.text is not None and ui.text.waiting_for_button:
                obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
            for _ in range(12):
                if _free_field_observation(obs):
                    return
                ui = obs.snapshot.ui
                menu = ui.menu if ui is not None and ui.valid else None
                if ui is not None and ui.text is not None and ui.text.waiting_for_button:
                    button = "a"
                elif menu is not None and any(
                    (
                        menu.start_menu is not None and menu.start_menu.task_active,
                        menu.bag is not None and menu.bag.active_hint is True,
                        menu.party is not None and menu.party.active_hint is True,
                        menu.list_menu is not None and menu.list_menu.window_in_use,
                        menu.menu is not None and menu.menu.window_in_use,
                    )
                ):
                    button = "b"
                else:
                    obs = yield Advance(frames=30)
                    continue
                obs = yield Tap(button, hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
                if not _free_field_observation(obs):
                    obs = yield Advance(frames=30)
            yield WaitUntil(
                _free_field_observation,
                max_frames=360,
                failure_reason="verified item use did not return to free field control",
            )
            return

        def party_result_input_ready(current) -> bool:
            ui = current.snapshot.ui
            party = ui.menu.party if ui is not None and ui.valid and ui.menu is not None else None
            if not (
                ui is not None
                and ui.main.callback2 is not None
                and ui.main.callback2.name == "CB2_UpdatePartyMenu"
                and ui.field.fade_active is False
                and party is not None
                and party.active_hint is True
            ):
                return False
            if ui.text is not None and ui.text.waiting_for_button:
                return True
            # Rare Candy's two stat pages and its no-new-move continuation read JOY_NEW directly.
            # Pret's WaitFanfare gate is exactly sFanfareCounter == 0 for the first/third presses.
            names = {task.func.name for task in ui.tasks.active}
            if "Task_DisplayLevelUpStatsPg2" in names:
                return True
            return bool(
                ui.field.fanfare_counter == 0
                and names
                & {
                    "Task_DisplayLevelUpStatsPg1",
                    "Task_TryLearnNewMoves",
                }
            )

        def bag_input_ready(current) -> bool:
            ui = current.snapshot.ui
            bag = ui.menu.bag if ui is not None and ui.valid and ui.menu is not None else None
            return bool(
                ui is not None
                and ui.main.callback2 is not None
                and ui.main.callback2.name == "CB2_BagMenuRun"
                and ui.field.fade_active is False
                and bag is not None
                and bag.bag_open
                and bag.active_hint is True
                and _task_active(ui, "Task_BagMenu_HandleInput")
            )

        def result_text_input_ready(current) -> bool:
            ui = current.snapshot.ui
            return bool(
                ui is not None
                and ui.valid
                and ui.text is not None
                and ui.text.waiting_for_button
            )

        def field_item_done(current) -> bool:
            """Accept the verified return to the field while the scene busy bit settles."""
            if not effect_verified or current.scene.scene_id not in _MOVEMENT_SCENES:
                return False
            ui = current.snapshot.ui
            return ui is None or not ui.valid or ui.text is None or not ui.text.waiting_for_button

        # A normal medicine has one party result page; Rare Candy has three. Re-read the owning
        # task after every press and stop only when the Bag's actual input task is restored.
        for _ in range(12):
            obs = yield WaitUntil(
                lambda current: (
                    bag_input_ready(current)
                    or party_result_input_ready(current)
                    or result_text_input_ready(current)
                    or field_item_done(current)
                ),
                max_frames=360,
                failure_reason="item-use result flow did not reach a RAM-confirmed input gate",
            )
            if bag_input_ready(obs) or field_item_done(obs):
                break
            if result_text_input_ready(obs):
                obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
                continue
            obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
        else:
            # The result callback can be an unclassified hand-off for the final Bag -> field
            # return.  RAM already proved the effect and decrement above; do not turn that
            # applied use into a timeout merely because no detector owns the intermediate gate.
            return
        if field_item_done(obs):
            return
        obs = yield Tap("b", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)

        def start_input_ready(current) -> bool:
            ui = current.snapshot.ui
            start = (
                ui.menu.start_menu if ui is not None and ui.valid and ui.menu is not None else None
            )
            return bool(
                ui is not None
                and ui.main.callback2 is not None
                and ui.main.callback2.name == "CB2_Overworld"
                and ui.field.fade_active is False
                and start is not None
                and start.task_active is True
                and any(
                    task.func.name == "Task_StartMenuHandleInput" and task.data[0] == 1
                    for task in ui.tasks.active
                )
            )

        obs = yield WaitUntil(
            start_input_ready,
            max_frames=240,
            failure_reason="Bag exit did not restore the START menu input task",
        )
        obs = yield Tap("b", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
        yield WaitUntil(
            _free_field_observation,
            max_frames=360,
            failure_reason="START menu did not close to free field control",
        )

    def _use_untargeted_item(self, ctx, record):
        item_id = record.item_id
        before_snapshot = ctx.snapshot
        before_quantity = fieldmenu.quantity(ctx.snapshot, item_id)
        if before_quantity < 1:
            raise RuntimeError("item disappeared from the fresh snapshot")
        try:
            pocket, pocket_index = _BAG_POCKET[record.data.pocket]
        except KeyError as exc:
            raise RuntimeError(f"item {item_id} has an unknown bag pocket") from exc
        obs = yield from self._select_start(ctx, START_BAG)
        obs = yield from self._select_bag_item(ctx, obs, item_id, pocket, pocket_index)
        menu = self._s_menu(obs.snapshot)
        if menu.cursor_pos != menu.min_cursor_pos:
            raise RuntimeError("item USE cursor is not verified")
        obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)

        def applied(current):
            if record.capability.consumable:
                return fieldmenu.quantity(current.snapshot, item_id) < before_quantity
            return took_over(current)

        obs = yield WaitUntil(
            applied,
            max_frames=240,
            failure_reason=(
                "Escape Rope bag decrement was not observed"
                if record.capability.field_action == "escape_rope"
                else "item use was not observed"
            ),
        )

        # Escape Rope decrements the bag immediately, but its script still owns the
        # controls while the fade and map-load callbacks run.  Returning at the bag
        # decrement lets the postcondition poll in the executor spin without being
        # able to press the A that advances the final field message.  Drive that
        # handoff here, then return only after the destination map is readable.
        if record.capability.field_action == "escape_rope":
            yield from self._escape_rope_transition(before_snapshot, obs)

    def _escape_rope_transition(self, before_snapshot, obs):
        """Drive the Rope script in bounded observation chunks until its map load completes."""
        # A short WaitUntil raises on a slow fade, so it cannot implement a retry loop.  Advance
        # returns a fresh observation without a timeout failure; inspect every chunk and press A
        # only when the message evidence owns input.
        remaining = 8 * 300
        while remaining:
            chunk = min(300, remaining)
            obs = yield Advance(frames=chunk)
            remaining -= chunk
            if _map_changed(before_snapshot, obs):
                return obs
            if not _rope_message_owns_input(obs):
                continue
            obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
            if _map_changed(before_snapshot, obs):
                return obs
        raise RuntimeError("Escape Rope transition did not change maps")

    def use_item(self, ctx):
        """Dispatch generic item options to the verified field menu paths."""
        if self.item_catalog is None:
            raise RuntimeError("item catalog is unavailable")
        record = self.item_catalog.item(int(ctx.args["item_id"]))
        if EffectClass.TEACH_MOVE in record.capability.effects:
            if 339 <= record.item_id <= 346:
                yield from self.teach_hm(ctx)
            else:
                yield from self.teach_tm(ctx)
            return
        if record.capability.field_action == "bicycle":
            yield from self.toggle_bike(ctx)
            return
        if record.capability.target is ItemTarget.NONE:
            yield from self._use_untargeted_item(ctx, record)
            return
        yield from self._use_party_item(ctx, record)

    def teach_tm(self, ctx):
        """Teach a TM, including the optional move replacement prompt."""
        item_id = int(ctx.args["item_id"])
        slot = int(ctx.args["party_slot"])
        move_id = int(ctx.args["move_id"])
        before_quantity = fieldmenu.quantity(ctx.snapshot, item_id)
        if before_quantity < 1:
            raise RuntimeError("TM disappeared from the verified bag")
        mon = next((member for member in ctx.snapshot.party if member.slot == slot), None)
        if mon is None or any(move.move_id == move_id for move in mon.moves):
            raise RuntimeError("TM target is absent or already knows the move")
        obs = yield from self._begin_teach_tm(ctx)
        yield from self._finish_teach_tm(ctx, obs, mon, before_quantity)

    def begin_teach_tm(self, ctx):
        """Open the real TM/HM party target screen, then yield control for a choice."""
        yield from self._begin_teach_tm(ctx)

    def finish_teach_tm(self, ctx):
        """Finish teaching from the RAM-owned party target screen."""
        item_id = int(ctx.args["item_id"])
        slot = int(ctx.args["party_slot"])
        move_id = int(ctx.args["move_id"])
        obs = Observation(ctx.snapshot, ctx.scene)
        if not _teach_target_observation(obs, item_id):
            raise RuntimeError("TM target choice is no longer owned by the party use-item flow")
        mon = next((member for member in ctx.snapshot.party if member.slot == slot), None)
        if mon is None or any(move.move_id == move_id for move in mon.moves):
            raise RuntimeError("TM target is absent or already knows the move")
        before_quantity = fieldmenu.quantity(ctx.snapshot, item_id)
        if before_quantity < 1:
            raise RuntimeError("TM disappeared from the verified bag")
        yield from self._finish_teach_tm(ctx, obs, mon, before_quantity)

    def cancel_teach_tm(self, ctx):
        """Unwind the party, TM Case, Bag and START layers from verified owners."""
        obs = Observation(ctx.snapshot, ctx.scene)
        party = self._party(ctx.snapshot)
        item_id = int(ctx.args["item_id"])
        if party.action_name != "use_item" or party.item_id != item_id:
            yield Interrupt(f"TM cancel item mismatch: expected {item_id}, RAM has {party.item_id}")
            return
        for _ in range(16):
            if _free_field_observation(obs):
                return
            ui = obs.snapshot.ui
            if ui is None or not ui.valid or ui.field.fade_active:
                obs = yield Advance(frames=8)
                continue
            button = "a" if ui.text is not None and ui.text.waiting_for_button else "b"
            obs = yield Tap(button, hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
        yield WaitUntil(
            _free_field_observation,
            max_frames=600,
            failure_reason="TM teaching cancel did not return to free field state",
        )

    def _begin_teach_tm(self, ctx):
        item_id = int(ctx.args["item_id"])
        obs = yield from self._open_tm_case_for_teaching(ctx)
        items = self._bag_items(ctx.snapshot, "tms_hms")
        row = self._tm_case_item_row(items, item_id)
        obs = yield from self._move_cursor_gated(
            ctx,
            obs,
            row,
            self._tm_list_row,
            label=f"TM item {item_id}",
            limit=len(items) + 4,
        )
        before_tasks = tuple(
            (task.slot, task.func.ptr) for task in self._ui(obs.snapshot).tasks.active
        )
        obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)

        def tm_use_ready(current):
            ui = current.snapshot.ui
            if ui is None or not ui.valid or ui.menu_type != "list":
                return False
            menu = ui.menu.menu
            tasks = tuple((task.slot, task.func.ptr) for task in ui.tasks.active)
            return bool(
                current.scene.scene_id in {"menu.open", "tm.list", UNKNOWN}
                and not current.scene.busy
                and menu is not None
                and menu.window_in_use
                and menu.consistent
                and menu.cursor_pos == menu.min_cursor_pos
                and tasks != before_tasks
            )

        obs = yield WaitUntil(
            tm_use_ready,
            max_frames=120,
            failure_reason=f"teach_tm item {item_id} context did not reach verified USE",
        )
        obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
        obs = yield WaitUntil(
            lambda current: _teach_target_observation(current, item_id),
            max_frames=180,
            failure_reason="teach_tm USE did not open an input-ready party target list",
        )
        # As with the TM list, the choose-mon task is visible just before it begins consuming
        # direction keys.  Settle without input and verify the same RAM-owned party handoff.
        obs = yield Advance(frames=8)
        obs = yield WaitUntil(
            lambda current: _teach_target_observation(current, item_id),
            max_frames=60,
            failure_reason="teach_tm party target list did not remain input-ready after settle",
        )
        return obs

    def _finish_teach_tm(self, ctx, obs, mon, before_quantity):
        item_id = int(ctx.args["item_id"])
        slot = int(ctx.args["party_slot"])
        forget = int(ctx.args["forget_move"])
        move_id = int(ctx.args["move_id"])
        is_hm = 339 <= item_id <= 346
        obs = yield from self._move_cursor_gated(
            ctx,
            obs,
            slot,
            lambda snapshot: self._party(snapshot).slot_id,
            label=f"TM party target {mon.personality:08x}",
            limit=6,
        )

        from pokegent.games.gen3.firered.reader.menu_decoders import decode_party_menu

        party_model = decode_party_menu(obs.snapshot)
        if party_model is None or party_model.selected_key != f"party:{mon.personality:08x}":
            raise RuntimeError("TM party cursor does not identify the requested personality")
        obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
        obs = yield WaitUntil(
            lambda current: bool(
                current.snapshot.ui is not None
                and current.snapshot.ui.valid
                and not any(
                    task.func.name == "Task_HandleChooseMonInput"
                    for task in current.snapshot.ui.tasks.active
                )
            ),
            max_frames=180,
            failure_reason=(
                f"teach_tm party target {mon.personality:08x} did not accept confirmation"
            ),
        )

        def move_changed(current) -> bool:
            return any(
                member.slot == slot and any(move.move_id == move_id for move in member.moves)
                for member in current.snapshot.party
            )

        def tm_applied(current) -> bool:
            quantity = fieldmenu.quantity(current.snapshot, item_id)
            quantity_changed = quantity == before_quantity if is_hm else quantity < before_quantity
            return move_changed(current) and quantity_changed

        if sum(1 for move in mon.moves if move.move_id) >= 4:

            def decode_replacement_prompt(snapshot):
                ui = self._ui(snapshot)
                yes_no = ui.menu.yes_no
                choice = ui.menu.menu
                if (
                    yes_no is None
                    or not yes_no.window_in_use
                    or choice is None
                    or not choice.window_in_use
                    or not choice.consistent
                    or choice.max_cursor_pos != 1
                ):
                    return None
                from pokegent.actions._exec.contracts import MenuEntry, MenuModel

                entries = (
                    MenuEntry(stable_key="yes", label="YES"),
                    MenuEntry(stable_key="no", label="NO"),
                )
                return MenuModel(
                    surface="party.tm_replace",
                    entries=entries,
                    selected_key=entries[choice.cursor_pos].stable_key,
                    cancel=True,
                )

            for _ in range(20):
                obs = yield WaitUntil(_teach_prompt_observation, max_frames=240)
                if decode_replacement_prompt(obs.snapshot) is not None:
                    break
                obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
            else:
                raise RuntimeError("TM replacement prompt is not input-ready")
            obs = yield from confirm_learning(
                obs,
                decode_replacement_prompt,
                tap=lambda button: Tap(button, hold_frames=MENU_HOLD, wait_frames=MENU_WAIT),
            )
            for _ in range(4):
                obs = yield WaitUntil(
                    lambda current: (
                        bool(
                            current.snapshot.ui
                            and current.snapshot.ui.main
                            and current.snapshot.ui.main.callback2
                            and current.snapshot.ui.main.callback2.name
                            == "obs_cb2_pokemon_summary_screen"
                        )
                        or bool(
                            current.snapshot.ui
                            and current.snapshot.ui.text
                            and current.snapshot.ui.text.waiting_for_button
                        )
                    ),
                    max_frames=240,
                )
                if (
                    obs.snapshot.ui
                    and obs.snapshot.ui.main
                    and obs.snapshot.ui.main.callback2
                    and obs.snapshot.ui.main.callback2.name == "obs_cb2_pokemon_summary_screen"
                ):
                    break
                obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
            else:
                raise RuntimeError("TM replacement did not open the move summary")
            forget_slot = next(
                (i for i, move in enumerate(mon.moves) if move.move_id == forget), None
            )
            member_index = next(
                (i for i, member in enumerate(ctx.snapshot.party) if member.slot == slot), None
            )
            if forget_slot is None or member_index is None:
                raise RuntimeError("TM replacement move is not in the verified party slot")
            forget_ctx = replace(
                ctx,
                args={
                    "slot": forget_slot,
                    "old_move": forget,
                    "new_move": move_id,
                    "party_id": member_index,
                },
            )
            macro = forget_move_macro(forget_ctx)
            prior = None
            replacement_waits = 0
            while True:
                try:
                    step = next(macro) if prior is None else macro.send(prior)
                except StopIteration:
                    break
                if isinstance(step, WaitUntil):
                    replacement_waits += 1
                    if replacement_waits == 2:
                        break
                prior = yield step
        for _ in range(12):
            obs = yield WaitUntil(
                lambda current: (
                    tm_applied(current)
                    or bool(
                        current.snapshot.ui
                        and current.snapshot.ui.text
                        and current.snapshot.ui.text.waiting_for_button
                    )
                ),
                max_frames=900,
                failure_reason="teach_tm learn animation did not reach a readable message",
            )
            if tm_applied(obs):
                break
            obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
        else:
            raise RuntimeError("TM replacement animation did not commit the new move")
        # Task_LearnNextMoveOrClosePartyMenu ignores A/B until the learned-move fanfare ends.
        # The UI block exposes the waiting text but not fanfare activity, so settle once before
        # acknowledging that corroborated message instead of exhausting retries during the song.
        obs = yield Advance(frames=600)
        # Replacement traverses summary -> party -> TM Case -> Bag -> START; each is a distinct
        # RAM-owned layer, so keep a bounded allowance for every layer plus text acknowledgements.
        for _ in range(12):
            obs = yield WaitUntil(
                lambda current: (
                    _tm_done_observation(current, slot, move_id)
                    or _teach_cleanup_observation(current, slot, move_id)
                ),
                max_frames=300,
                failure_reason="teach_tm cleanup did not reach a verified menu or field boundary",
            )
            if _tm_done_observation(obs, slot, move_id):
                return
            ui = obs.snapshot.ui
            menu = ui.menu if ui else None
            if menu is not None and menu.summary_screen_open:
                button = "b"
            elif ui is not None and ui.text is not None and ui.text.waiting_for_button:
                button = "a"
            elif (
                menu is not None
                and menu.yes_no is not None
                and menu.yes_no.window_in_use
                and (
                    menu.yes_no.task_active is True
                    or (
                        menu.menu is not None
                        and menu.menu.window_in_use
                        and menu.menu.max_cursor_pos == 1
                    )
                )
            ):
                button = "a"
            else:
                button = "b"
            obs = yield Tap(button, hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
        yield WaitUntil(
            lambda current: _tm_done_observation(current, slot, move_id),
            max_frames=600,
            failure_reason="teach_tm did not return to field with the learned move in party RAM",
        )

    def use_fly(self, ctx):
        obs = yield from self._select_start(ctx, START_POKEMON)
        party_slot = int(ctx.args["party_slot"])
        obs = yield from self._select_party_slot(ctx, obs, party_slot)

        # The A press first switches the party task to Task_TryCreateSelectionWindow; only the
        # next task turn populates PartyMenuInternal.actions/numActions.  The old sMenu data is
        # still readable in between, so the action list is the authoritative construction gate.
        # A confirmation sent during the task handoff can be acknowledged by gMain yet ignored
        # by the party handler.  Retry only while RAM proves the same slot and choose-mon handler
        # still own the screen; no other party action can be triggered by these bounded retries.
        def action_menu_ready(snapshot):
            ui = snapshot.ui
            return bool(
                ui is not None
                and ui.valid
                and ui.menu.party is not None
                and ui.menu.party.action_ids
                and any(
                    task.func.name == "Task_HandleSelectionMenuInput" for task in ui.tasks.active
                )
            )

        def confirmation_retryable(snapshot):
            ui = snapshot.ui
            return bool(
                ui is not None
                and ui.valid
                and ui.menu.party is not None
                and ui.menu.party.slot_id == party_slot
                and ui.field.fade_active is False
                and any(task.func.name == "Task_HandleChooseMonInput" for task in ui.tasks.active)
            )

        for _ in range(3):
            if action_menu_ready(obs.snapshot):
                break
            obs = yield WaitUntil(
                lambda current: (
                    action_menu_ready(current.snapshot) or confirmation_retryable(current.snapshot)
                ),
                max_frames=90,
            )
            if action_menu_ready(obs.snapshot):
                break
            if not confirmation_retryable(obs.snapshot):
                raise RuntimeError("party action menu did not reach a retryable input state")
            obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
        else:
            raise RuntimeError("party action menu was not built after bounded confirmation retries")
        menu = self._s_menu(obs.snapshot)
        if menu.max_cursor_pos < menu.min_cursor_pos:
            raise RuntimeError("party action menu is unreadable")

        def decode(snapshot):
            party = self._party(snapshot)
            if not party.action_ids:
                return None
            from pokegent.games.gen3.firered.reader.menu_decoders import decode_party_action

            return decode_party_action(snapshot)

        obs = yield from select_menu_entry(
            obs,
            f"party_action:field_move:{fly.FLY_MOVE_ID}",
            decode,
            tap=lambda button: Tap(button, hold_frames=MENU_HOLD, wait_frames=MENU_WAIT),
            max_moves=menu.max_cursor_pos + 2,
            readback_retries=2,
            confirm_retries=2,
        )
        # Task_FlyMap is installed asynchronously after the menu confirmation, but its states
        # 0..3 only build the map and wait for the palette fade.  State 4 is the first one that
        # calls GetRegionMapInput.  Wait for the fade, then give the task one turn to enter that
        # input state before sending cursor directions.
        obs = yield WaitUntil(
            lambda current: bool(
                self._ui(current.snapshot).field.fade_active is False
                and any(
                    task.func.name == "Task_FlyMap"
                    for task in self._ui(current.snapshot).tasks.active
                )
            ),
            max_frames=180,
        )
        obs = yield Advance(frames=1)
        destination = fly.DESTINATIONS_BY_KEY.get(
            (int(ctx.args["destination_group"]), int(ctx.args["destination_number"]))
        )
        if destination is None:
            raise RuntimeError("Fly destination is unknown")
        info = getattr(self._ui(obs.snapshot), "fly_map", None)
        if info is None or info.input_ready is not True:
            raise RuntimeError("Fly-map cursor is not RAM-verified for selection")
        if not all(isinstance(value, int) for value in (info.cursor_x, info.cursor_y)):
            raise RuntimeError("Fly-map cursor coordinates are unreadable")
        # CreateMapCursor's arguments are tile/palette tags; GetPlayerPositionOnRegionMap_
        # HandleOverrides positions the cursor on the player's current mapsec (with overrides
        # for maps such as Safari Zone and Silph). Read the live RAM cursor so selection does not
        # depend on any assumed start cell, then move to the ROM-derived destination cell.
        cursor = (info.cursor_x, info.cursor_y)
        cursor_steps = 0
        while (direction := fly.cursor_step(cursor, destination.cell)) is not None:
            cursor_steps += 1
            if cursor_steps > 2 * (22 + 15):
                raise RuntimeError("Fly-map cursor did not reach the requested destination")
            obs = yield Tap(direction, hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
            if obs.scene.scene_id not in ("menu.fly_map", UNKNOWN):
                raise RuntimeError("Fly-map cursor left the expected screen before selection")
            current = getattr(self._ui(obs.snapshot), "fly_map", None)
            if current is None or current.input_ready is not True:
                raise RuntimeError("Fly-map cursor readback became unavailable")
            if not all(isinstance(value, int) for value in (current.cursor_x, current.cursor_y)):
                raise RuntimeError("Fly-map cursor coordinates are unreadable")
            cursor = (current.cursor_x, current.cursor_y)
        current = getattr(self._ui(obs.snapshot), "fly_map", None)
        if (
            current is None
            or current.input_ready is not True
            or current.selected_mapsec != destination.mapsec
            or current.selected_type != 2  # MAPSECTYPE_VISITED in region_map.c
        ):
            raise RuntimeError("Fly destination map section is not RAM-verified")
        obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
        # FireRed selects an outdoor Fly destination directly; tolerate a verified yes/no prompt
        # as a compatibility path for profiles whose ROM behavior differs.
        if obs.scene.scene_id == "dialog.yes_no":
            menu = self._ui(obs.snapshot).menu.menu
            if menu is None or menu.cursor_pos != menu.min_cursor_pos:
                raise RuntimeError("Fly confirmation cursor is not verified on YES")
            obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
        elif obs.scene.scene_id == "menu.fly_map":
            info = getattr(self._ui(obs.snapshot), "fly_map", None)
            if info is None or info.state is None or info.state < 5 or info.input_ready is True:
                raise RuntimeError("Fly selection did not open the native confirmation state")
        elif obs.scene.scene_id not in ("field.map_transition", E.FREE, UNKNOWN):
            raise RuntimeError("Fly selection did not open confirmation or transition")
        yield WaitUntil(
            lambda current: (
                current.snapshot.map is not None
                and current.snapshot.map.identity.group == destination.map_key[0]
                and current.snapshot.map.identity.number == destination.map_key[1]
            ),
            max_frames=2400,
            max_wall_s=90.0,
        )

    def resume_fly_map(self, ctx):
        """Finish a Fly selection that was already open when the process restarted."""
        info = getattr(self._ui(ctx.snapshot), "fly_map", None)
        if info is None or info.input_ready is not True:
            raise RuntimeError("Fly-map cursor is not RAM-verified for resumed selection")
        if not all(isinstance(value, int) for value in (info.cursor_x, info.cursor_y)):
            raise RuntimeError("Fly-map cursor coordinates are unreadable")
        cursor = (info.cursor_x, info.cursor_y)
        target = (int(ctx.args["cell_x"]), int(ctx.args["cell_y"]))
        obs = Observation(ctx.snapshot, ctx.scene)
        while (direction := fly.cursor_step(cursor, target)) is not None:
            obs = yield Tap(direction, hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
            if obs.scene.scene_id not in ("menu.fly_map", UNKNOWN):
                raise RuntimeError("resumed Fly map left the expected screen")
            current = getattr(self._ui(obs.snapshot), "fly_map", None)
            if current is None or current.input_ready is not True:
                raise RuntimeError("Fly-map cursor readback became unavailable")
            if not all(isinstance(value, int) for value in (current.cursor_x, current.cursor_y)):
                raise RuntimeError("Fly-map cursor readback is unreadable")
            cursor = (current.cursor_x, current.cursor_y)
        current = getattr(self._ui(obs.snapshot), "fly_map", None)
        if current is None or current.selected_mapsec != int(ctx.args["mapsec"]):
            raise RuntimeError("Fly destination map section is not verified")
        if current.selected_type != 2:  # MAPSECTYPE_VISITED in region_map.c
            raise RuntimeError("Fly destination is not RAM-verified as visited")
        obs = yield Tap("a", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
        if obs.scene.scene_id == "menu.fly_map":
            current = getattr(self._ui(obs.snapshot), "fly_map", None)
            if (
                current is None
                or current.state is None
                or current.state < 5
                or current.input_ready is True
            ):
                raise RuntimeError(
                    "resumed Fly selection did not open the native confirmation state"
                )
        elif obs.scene.scene_id not in ("field.map_transition", E.FREE, UNKNOWN):
            raise RuntimeError("resumed Fly selection left the map without confirmation")
        yield WaitUntil(
            lambda current: (
                current.snapshot.map is not None
                and current.snapshot.map.identity.group == int(ctx.args["destination_group"])
                and current.snapshot.map.identity.number == int(ctx.args["destination_number"])
            ),
            max_frames=2400,
            max_wall_s=90.0,
        )

    def cancel_fly_map(self, ctx):
        """Close Fly -> party -> start and verify that overworld control returns."""

        def field_free(observation):
            return observation.scene.scene_id == E.FREE and not observation.scene.busy

        obs = yield Tap("b", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
        obs = yield Advance(frames=30)
        if getattr(obs.snapshot, "ui", None) is None:
            return

        # The native party handoff can publish stale callback/menu evidence for several frames.
        # Close it using the same task-gated bounded recovery as the field party-swap macro.
        for _ in range(4):
            if field_free(obs):
                return
            obs = yield Tap("b", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
            obs = yield WaitUntil(
                lambda current: (
                    field_free(current)
                    or _task_active(current.snapshot.ui, "Task_StartMenuHandleInput")
                    or _task_active(current.snapshot.ui, "Task_HandleChooseMonInput")
                    or _task_active(current.snapshot.ui, "Task_HandleSelectionMenuInput")
                ),
                max_frames=120,
            )
            if field_free(obs):
                return
            if _task_active(obs.snapshot.ui, "Task_StartMenuHandleInput"):
                obs = yield Advance(frames=8)
        for _ in range(2):
            obs = yield Tap("b", hold_frames=MENU_HOLD, wait_frames=MENU_WAIT)
            obs = yield Advance(frames=8)
        yield WaitUntil(field_free, max_frames=180)

    def use_dig_teleport(self, ctx):
        """Use a known Dig/Teleport move from the party menu and await the map hand-off."""
        before = ctx.snapshot.map
        if before is None:
            raise RuntimeError("map is unreadable before Dig/Teleport")
        obs = yield from self._select_start(ctx, START_POKEMON)
        party_slot = int(ctx.args["party_slot"])
        move_id = int(ctx.args["move_id"])
        obs = yield from self._select_party_slot(ctx, obs, party_slot)
        menu = self._s_menu(obs.snapshot)
        if menu.max_cursor_pos < menu.min_cursor_pos:
            raise RuntimeError("party action menu is unreadable")

        def decode(snapshot):
            party = self._party(snapshot)
            if not party.action_ids:
                return None
            from pokegent.games.gen3.firered.reader.menu_decoders import decode_party_action

            return decode_party_action(snapshot)

        obs = yield from select_menu_entry(
            obs,
            f"party_action:field_move:{move_id}",
            decode,
            tap=lambda button: Tap(button, hold_frames=MENU_HOLD, wait_frames=MENU_WAIT),
            max_moves=menu.max_cursor_pos + 2,
            readback_retries=2,
            confirm_retries=2,
        )
        yield WaitUntil(
            lambda current: (
                current.snapshot.map is not None
                and (current.snapshot.map.identity.group, current.snapshot.map.identity.number)
                != (before.identity.group, before.identity.number)
            ),
            max_frames=1800,
            max_wall_s=45.0,
        )

    def use_flash(self, ctx):
        """Use a known Flash move from the party menu and await a lit map."""
        obs = yield from self._select_start(ctx, START_POKEMON)
        party_slot = int(ctx.args["party_slot"])
        move_id = int(ctx.args["move_id"])
        obs = yield from self._select_party_slot(ctx, obs, party_slot)
        menu = self._s_menu(obs.snapshot)
        if menu.max_cursor_pos < menu.min_cursor_pos:
            raise RuntimeError("party action menu is unreadable")

        def decode(snapshot):
            party = self._party(snapshot)
            if not party.action_ids:
                return None
            from pokegent.games.gen3.firered.reader.menu_decoders import decode_party_action

            return decode_party_action(snapshot)

        yield from select_menu_entry(
            obs,
            f"party_action:field_move:{move_id}",
            decode,
            tap=lambda button: Tap(button, hold_frames=MENU_HOLD, wait_frames=MENU_WAIT),
            max_moves=menu.max_cursor_pos + 2,
            readback_retries=2,
            confirm_retries=2,
        )

    # -- the walkers -------------------------------------------------------------------------------

    def enter_warp(self, ctx: StepContext):
        warp = ctx.args["warp"]
        ident = ctx.snapshot.map.identity
        return self._run(ctx, lambda st, nav: _find(st, nav, f"warp:{ident.group}:{ident.number}:{warp}"),  # noqa: E501
                         expect_map=_dest(ctx))  # fmt: skip

    def travel_edge(self, ctx: StepContext):
        if ctx.snapshot.map is not None and ctx.snapshot.map.player is not None:
            if ctx.snapshot.map.player.biking:
                obs = yield from self.toggle_bike(ctx)
                if obs is None or obs.snapshot.map is None or obs.snapshot.map.player is None:
                    return
                if obs.snapshot.map.player.biking:
                    return
                ctx = replace(ctx, snapshot=obs.snapshot, scene=obs.scene)

        def resolve(state: MapState, nav: N.Navigator) -> T.Target | None:
            target = _find(state, nav, ctx.option.id)
            seam_x = ctx.args.get("seam_x")
            seam_y = ctx.args.get("seam_y")
            if target is not None and isinstance(seam_x, int) and isinstance(seam_y, int):
                return replace(target, at=(seam_x, seam_y))
            return target

        yield from self._run(
            ctx,
            resolve,
            expect_map=(ctx.args["dest_group"], ctx.args["dest_number"]),
        )

    def use_waterfall(self, ctx: StepContext):
        """Walk to the planned surf-side tile and invoke Waterfall there.

        The route supplies the exact facing tile; this deliberately does not search for a
        nearby waterfall.  The dialog family owns the ROM's confirmation prompt, after which
        the field action's postcondition observes the resulting top-side position.
        """
        source = (ctx.args["source_x"], ctx.args["source_y"])
        direction = ctx.args["direction"]
        destination = (ctx.args["destination_x"], ctx.args["destination_y"])
        target = T.Target(
            ctx.option.id,
            "waterfall",
            "talk",
            "Climb the waterfall",
            (T.Approach(source, direction),),
            at=destination,
            info={"role": "waterfall", "destination": destination},
        )
        return self._run(ctx, lambda _state, _nav: target, press_a=True)

    def talk_to(self, ctx: StepContext):
        return self._run(
            ctx,
            lambda st, nav: _find(st, nav, ctx.option.id),
            press_a=True,
            complete_obstacle=ctx.option.facts.get("target", {}).get("kind") == "obstacle",
        )

    def pick_up(self, ctx: StepContext):
        return self._run(ctx, lambda st, nav: _find(st, nav, ctx.option.id), press_a=True)

    def interact_with(self, ctx: StepContext):
        return self._run(ctx, lambda st, nav: _find(st, nav, ctx.option.id), press_a=True)

    def use_pc(self, ctx: StepContext):
        """Walk to the ROM PC furniture and hand off to the existing PC scene owner."""
        return self._run(ctx, lambda st, nav: _find(st, nav, ctx.option.id), press_a=True)

    def walk_to(self, ctx: StepContext):
        x, y = ctx.args["x"], ctx.args["y"]
        return self._run(ctx, lambda st, nav: T.tile_target(st, x, y), at_tile=(x, y))

    def walk_in_grass(self, ctx: StepContext):
        return self._run_grass(ctx)

    # -- the generator -----------------------------------------------------------------------------

    @staticmethod
    def _bind_planned_approach(ctx: StepContext, target: T.Target) -> tuple[T.Target, str | None]:
        planned = ctx.option.facts.get("planned_approach")
        if not isinstance(planned, dict):
            return target, None
        stand = planned.get("stand")
        face = planned.get("face")
        if (
            not isinstance(stand, (tuple, list))
            or len(stand) != 2
            or not all(isinstance(value, int) for value in stand)
            or face not in (*N.DIRECTIONS, None)
        ):
            return target, None
        point = (stand[0], stand[1])
        approaches = tuple(
            approach
            for approach in target.approaches
            if approach.stand == point and (face is None or approach.face == face)
        )
        if not approaches:
            return target, "planned_approach_unavailable"
        return replace(target, approaches=approaches), None

    def _run(
        self,
        ctx: StepContext,
        resolve: Callable[[MapState, N.Navigator], T.Target | None],
        *,
        expect_map: tuple[int, int] | None = None,
        press_a: bool = False,
        at_tile: tuple[int, int] | None = None,
        complete_obstacle: bool = False,
    ) -> Generator[Step, Observation, None]:
        state0 = ctx.snapshot.map
        assert state0 is not None
        start_map = _map_key(state0)
        key = self.ledger.key(ctx.option.id, start_map, capability_signature(ctx.snapshot))
        walk = _Walk(state0, Observation(ctx.snapshot, ctx.scene))
        reason = ""
        progressed = False
        interaction_started = False
        try:
            target = resolve(state0, N.Navigator(state0))
            if target is None:
                reason = "target_gone"
                return
            target, bind_failure = self._bind_planned_approach(ctx, target)
            if bind_failure is not None:
                reason = bind_failure
                return
            while True:
                state = walk.state
                nav = N.Navigator(state)
                plan = T.plan_target(nav, nav.search(), target)
                if plan is None:
                    # reachable once a walking person moves out of the way? then walk up to them
                    # and wait (bounded) instead of giving up before the first step
                    easy = N.Navigator(state, N.Constraints(wanderers=False))
                    plan = T.plan_target(easy, easy.search(), target)
                if plan is None:
                    # Forced movement is a bounded macro transition. Keep static blockers and
                    # water conservative; only the wandering-NPC relaxation is executable here.
                    forced = N.Navigator(state, N.Constraints(wanderers=False, forced=True))
                    plan = T.plan_target(forced, forced.search(), target)
                if plan is None:
                    reason = (
                        "planned_seam_unavailable"
                        if target.final == "edge" and target.at is not None
                        else "unreachable:" + T.unreachable_reason(state, target, nav)
                    )
                    progressed = walk.steps > 0
                    return
                outcome = yield from self._follow(walk, plan, target, start_map)
                if outcome == "replan":
                    walk.replans += 1
                    if walk.replans > MAX_REPLANS:
                        reason = "blocked"
                        progressed = walk.steps > 0
                        return
                    continue
                if outcome == "left_map":
                    # a planned warp / edge, or an unplanned map change: the walk is over either way
                    planned = expect_map is not None and (
                        expect_map == (-1, -1) or _map_key(walk.state) == expect_map
                    )
                    if planned:
                        self.ledger.record_success(key)
                    else:
                        reason = "unexpected_map"
                    return
                if outcome == "failed":
                    reason = "step_failed"
                    progressed = walk.steps > 0
                    return
                if outcome == "engaged":
                    interaction_started = True
                break  # "arrived": run the final action
            # -- the final action -----------------------------------------------------------------
            final = target.final
            if at_tile is not None or final == "none":
                self.ledger.record_success(key)
                return
            if final == "door":
                walk.phase = "await_map"
                walk.obs = yield Tap("up", hold_frames=DOOR_HOLD, wait_frames=1)
                yield from self._wait_out_unknown(walk)
            elif final == "hold":
                walk.phase = "await_map"
                walk.obs = yield Tap(
                    BUTTON[target.press],
                    hold_frames=ARROW_HOLD,
                    wait_frames=1,
                    until=lambda candidate: _directional_warp_started(candidate, start_map),
                    max_hold_frames=ARROW_MAX_HOLD,
                )
                yield from self._wait_out_unknown(walk)
            elif final == "talk" and press_a:
                walk.phase = "interact"
                if not interaction_started:
                    walk.obs = yield Tap("a", hold_frames=PRESS_A_HOLD, wait_frames=PRESS_A_WAIT)
                    yield from self._wait_out_unknown(walk)
            else:  # "step" / "edge": the last move was the final action and did not leave the map
                walk.phase = "await_map"
                yield from self._wait_out_unknown(walk)
            if walk.phase == "interact":
                if not took_over(walk.obs):
                    walk.obs = yield WaitUntil(took_over, max_frames=60)
                if not took_over(walk.obs):
                    reason = "no_effect"
                    return
                if complete_obstacle:
                    cleared = yield from self._complete_obstacle(walk, target)
                    if cleared:
                        if walk.obs.scene.scene_id.startswith("battle."):
                            self.ledger.defer_obstacle_check(
                                target.option_id,
                                start_map,
                                target.info["local_id"],
                                target.info.get("hide_flag"),
                            )
                        self.ledger.record_success(key)
                    else:
                        reason = "obstacle_not_cleared"
                    return
                field_move_prompt = (
                    target.kind == "obstacle" and target.info.get("role") == "cut_tree"
                ) or target.kind == "waterfall"
                if self.prompt_policy is not None and field_move_prompt:
                    self.prompt_policy.set_field_move_trigger(
                        goal_id=ctx.frame.goal_id,
                        move="waterfall" if target.kind == "waterfall" else "cut",
                        serves_goal=ctx.option.facts.get("serves_goal") is True,
                        next_route_step=ctx.option.facts.get("on_route") is True,
                        expected_destination=(
                            target.info.get("destination") if target.kind == "waterfall" else None
                        ),
                        on_decline=lambda: (
                            self.ledger.record_failure(
                                key, "field_move_declined", progressed=False
                            ),
                            self.ledger.record_field_move_decline(
                                ctx.option.id, start_map, ctx.frame.goal_id
                            ),
                        ),
                    )
                if target.kind == "sign":
                    self.ledger.record_sign_read(ctx.option.id, start_map, ctx.frame.goal_id)
                # A Cut interaction only opens the yes/no prompt here.  The tree is not gone until
                # the dialog answers YES and the next field snapshot proves the object disappeared.
                if not field_move_prompt:
                    self.ledger.record_success(key)
                return
            if not _left(walk.obs, start_map):
                walk.obs = yield WaitUntil(lambda o: _left(o, start_map), max_frames=60)
            self.ledger.record_success(key)
        except GeneratorExit:
            # the executor dropped the macro: a bound ran out while we waited for the effect
            if walk.phase == "interact":
                reason = "no_effect"
            elif walk.phase == "await_map":
                reason = "no_warp"
            raise
        finally:
            if reason and reason != "unexpected_map":
                self.ledger.record_failure(key, reason, progressed=progressed)

    @staticmethod
    def _obstacle_complete(obs: Observation, target: T.Target) -> bool:
        completion = target.info.get("completion")
        state = obs.snapshot.map
        if state is None:
            return False
        if completion == "strength_active":
            return state.strength_active is True
        flag = target.info.get("hide_flag")
        if isinstance(flag, int) and flag > 0 and obs.snapshot.flags is not None:
            if obs.snapshot.flags.is_set(flag):
                return True
        obj = state.object_by_local_id(target.info.get("local_id"))
        # Never accept a battle scene by itself: a random encounter on the approach is not
        # completion.  Snorlax sets its hide flag before ``dowildbattle``, so this RAM proof still
        # releases the intended battle to the ordinary battle family immediately.
        return obj is None or obj.hidden_flag_set is True or not obj.blocks_tile

    def _complete_obstacle(
        self, walk: _Walk, target: T.Target
    ) -> Generator[Step, Observation, bool]:
        """Own the verified prompt/text sequence until RAM proves the route edge opened.

        The option chooses the semantic action once.  Subsequent waits, text advances and the
        default-YES field prompt are deterministic parts of the existing field-move macro.
        """

        for _ in range(64):
            if self._obstacle_complete(walk.obs, target):
                return True
            scene = walk.obs.scene.scene_id
            if scene in (D.PRINTING, "field.scripted_movement"):
                before = scene
                walk.obs = yield WaitUntil(
                    lambda current, previous=before: current.scene.scene_id != previous,
                    max_frames=2400,
                    max_wall_s=40.0,
                    check_every=4,
                    failure_reason=f"obstacle script stayed in {scene}",
                )
                continue
            if scene in (D.ADVANCE, D.END):
                walk.obs = yield Tap("a", hold_frames=PRESS_A_HOLD, wait_frames=PRESS_A_WAIT)
                continue
            if scene == D.YES_NO:
                ui = walk.obs.snapshot.ui
                menu = ui.menu.menu if ui is not None and ui.valid else None
                if menu is None or not menu.consistent:
                    return False
                if menu.cursor_pos == menu.min_cursor_pos + 1:
                    walk.obs = yield Tap("up", hold_frames=PRESS_A_HOLD, wait_frames=PRESS_A_WAIT)
                    continue
                if menu.cursor_pos != menu.min_cursor_pos:
                    return False
                walk.obs = yield Tap("a", hold_frames=PRESS_A_HOLD, wait_frames=PRESS_A_WAIT)
                continue
            if scene == UNKNOWN:
                walk.obs = yield Advance(UNKNOWN_WAIT_FRAMES)
                continue
            if scene in _MOVEMENT_SCENES:
                walk.obs = yield WaitUntil(
                    lambda current: (
                        self._obstacle_complete(current, target)
                        or current.scene.scene_id not in _MOVEMENT_SCENES
                    ),
                    max_frames=180,
                    max_wall_s=10.0,
                    check_every=4,
                    failure_reason="obstacle interaction returned to the field without clearing it",
                )
                continue
            if scene.startswith("battle.") and walk.phase == "interact":
                # The interaction itself can hand control to Snorlax's ordinary wild battle
                # before the hide flag/object update is readable.  The caller records a pending
                # obstacle proof, settled at the first post-battle field.free snapshot.  A random
                # encounter during walking never reaches this phase: ``_adopt`` interrupts it.
                return True
            return False
        return False

    def _run_grass(self, ctx: StepContext) -> Generator[Step, Observation, None]:
        """Walk between two tiles of one patch until a battle or the step bound."""
        state0 = ctx.snapshot.map
        assert state0 is not None
        start_map = _map_key(state0)
        key = self.ledger.key(ctx.option.id, start_map, capability_signature(ctx.snapshot))
        target = _find(state0, N.Navigator(state0), ctx.option.id)
        if target is None:
            self.ledger.record_failure(key, "target_gone")
            return
        max_steps = ctx.args["max_steps"]
        if self.walk_step_limit is not None:
            max_steps = min(max_steps, self.walk_step_limit)
        walk = _Walk(state0, Observation(ctx.snapshot, ctx.scene))
        progressed = False
        reason = ""
        try:
            endpoint = 0
            while walk.steps < max_steps:
                if walk.obs.scene.scene_id not in _MOVEMENT_SCENES:
                    return
                plan_target = replace(target, approaches=(target.approaches[endpoint],))
                plan = T.plan_target(
                    N.Navigator(walk.state), N.Navigator(walk.state).search(), plan_target
                )
                if plan is None:
                    reason = "unreachable:no_path"
                    return
                remaining = max_steps - walk.steps
                if plan.length > remaining:
                    plan = replace(plan, moves=plan.moves[:remaining])
                outcome = yield from self._follow(walk, plan, plan_target, start_map)
                if outcome != "arrived":
                    reason = "grass_walk_" + outcome
                    progressed = walk.steps > 0
                    return
                progressed = progressed or walk.steps > 0
                endpoint = 1 - endpoint
            return
        finally:
            if not reason and walk.steps == 0:
                reason = "no_effect"
            if reason:
                self.ledger.record_failure(key, reason, progressed=progressed)
            else:
                self.ledger.record_success(key)

    # -- following one plan ------------------------------------------------------------------------

    def _follow(
        self, walk: _Walk, plan: T.Plan, target: T.Target, start_map: tuple[int, int]
    ) -> Generator[Step, Observation, str]:
        """Walk ``plan``. Returns ``arrived`` | ``engaged`` | ``replan`` | ``left_map`` |
        ``failed``.

        ``engaged`` means a final in-place turn reached the RAM-proven interaction boundary and
        the target's script took over immediately.  That takeover is the interaction phase, so the
        caller must own it without sending a second A press.
        """
        moves = list(plan.moves)
        want = plan.approach.face
        while True:
            state = walk.state
            player = state.player
            assert player is not None
            if moves:
                move = moves[0]
                if (player.avatar_x, player.avatar_y) != move.src:
                    return "replan"
                if player.facing != move.direction:
                    obs = yield Tap(
                        BUTTON[move.direction], hold_frames=TURN_HOLD, wait_frames=TURN_WAIT
                    )
                    ok = yield from self._adopt(walk, obs, start_map)
                    if ok != "ok":
                        return ok
                    if walk.state.player.facing != move.direction:
                        return "failed"
                    continue
                traversal = (
                    traverse_forced(state, move.direction) if move.kind == "forced" else None
                )
                pred = (
                    traversal.entry
                    if traversal is not None
                    else predict_step(state, move.direction)
                )
                if pred.outcome == "blocked" and pred.reason == "object":
                    if walk.npc_waits >= NPC_WAITS:
                        return "replan"
                    walk.npc_waits += 1
                    obs = yield Advance(NPC_WAIT_FRAMES)
                    ok = yield from self._adopt(walk, obs, start_map)
                    if ok != "ok":
                        return ok
                    continue
                predicted_dst = traversal.landing_xy if traversal is not None else pred.end_xy
                if not (pred.walks and predicted_dst == move.dst):
                    return "replan"  # the game's rules say this press does not do the plan's move
                obs = yield Tap(
                    BUTTON[move.direction],
                    hold_frames=STEP_HOLD,
                    wait_frames=STEP_WAIT,
                    until=lambda candidate, dst=move.dst: _at_observation(candidate, dst),
                    until_hint=("player_xy", move.dst),
                    settle_hint=("player_idle", None),
                    max_hold_frames=(
                        BIKE_EDGE_MAX_HOLD
                        if state.player.biking and move.connection is not None
                        else BIKE_STEP_MAX_HOLD
                        if state.player.biking
                        else 16
                    ),
                )
                walk.steps += 1
                walk.npc_waits = 0
                ok = yield from self._adopt(walk, obs, start_map, final_step=len(moves) == 1)
                if ok != "ok":
                    return ok
                if move.kind == "forced" and not _at(walk.state, move.dst):
                    obs = yield WaitUntil(
                        lambda candidate: _at_observation(candidate, move.dst), max_frames=240
                    )
                    ok = yield from self._adopt(walk, obs, start_map, final_step=len(moves) == 1)
                    if ok != "ok":
                        return ok
                new = walk.state.player
                if (new.avatar_x, new.avatar_y) != move.dst:
                    return "replan"
                if len(moves) == 1 and target.final in ("hold", "door"):
                    # The final input is sensitive to the avatar still being in its tile
                    # transition.  A position read can reach the destination before the ROM
                    # releases movement control; if we press a stair/arrow during that window,
                    # the input is consumed without entering the warp.  Give this final boundary
                    # a bounded margin proportional to the route that led here, and let foreign
                    # scenes escape through the normal interruption path.
                    wait_frames = max(60, plan.length * 8 + 60)
                    obs = yield WaitUntil(
                        lambda candidate: (
                            candidate.scene.scene_id not in _MOVEMENT_SCENES
                            or _player_idle(candidate)
                        ),
                        max_frames=wait_frames,
                    )
                    ok = yield from self._adopt(walk, obs, start_map, final_step=True)
                    if ok != "ok":
                        return ok
                moves.pop(0)
                continue
            # no moves left: standing on the approach tile; the final action may need a facing
            if want is not None:
                if target.final == "talk" and not self._interaction_still_valid(
                    state, plan.approach, target
                ):
                    return "replan"
                if player.facing != want:
                    pred = predict_step(state, want)
                    turn_only = (
                        pred.outcome == "blocked"
                        or (target.final == "door" and pred.outcome == "door_warp")
                        or target.final == "hold"
                    )
                    if not turn_only:
                        return "replan"  # pressing would walk or warp: the way in front changed
                    obs = yield Tap(BUTTON[want], hold_frames=TURN_HOLD, wait_frames=TURN_WAIT)
                    ok = yield from self._adopt(
                        walk,
                        obs,
                        start_map,
                        interaction=(plan.approach, target),
                    )
                    if ok == "engaged":
                        return ok
                    if ok != "ok":
                        return ok
                    if walk.state.player.facing != want:
                        return "failed"
                    continue
            if target.final == "talk":
                walk.phase = "interact"
            return "arrived"

    @staticmethod
    def _wait_out_unknown(walk: _Walk) -> Generator[Step, Observation, None]:
        """After a final tap the screen may be in a transition no detector names (an unnamed
        callback, e.g. a cave exit): wait, bounded, with no input, until a scene is recognised."""
        waits = 0
        while walk.obs.scene.scene_id == UNKNOWN and waits < UNKNOWN_WAITS:
            waits += 1
            walk.obs = yield Advance(UNKNOWN_WAIT_FRAMES)

    # -- observation handling ----------------------------------------------------------------------

    def _adopt(
        self,
        walk: _Walk,
        obs: Observation,
        start_map: tuple[int, int],
        *,
        final_step: bool = False,
        interaction: tuple[T.Approach, T.Target] | None = None,
    ) -> Generator[Step, Observation, str]:
        """Take the observation the executor handed back after the last step.

        Returns ``ok`` (controllable field scene, same map, readable), ``left_map`` or ``failed``.
        A foreign scene makes this generator yield a step the executor refuses to send: the macro
        is interrupted there (``INTERRUPTED``), exactly as the contract wants. A map block that is
        unreadable for a moment (a transition) is re-read a few times.
        """
        while True:
            walk.obs = obs
            if obs.scene.scene_id == UNKNOWN:
                walk.unknown_waits += 1
                if walk.unknown_waits > UNKNOWN_WAITS:
                    return "failed"
                obs = yield Advance(UNKNOWN_WAIT_FRAMES)  # no input: the screen is in a transition
                continue
            walk.unknown_waits = 0
            if obs.scene.scene_id not in _MOVEMENT_SCENES:
                m = obs.snapshot.map
                if final_step and m is not None and _map_key(m) != start_map:
                    walk.state = m
                    return "left_map"
                # A final turn can cross directly into a scripted obstacle's text/battle on live
                # FireRed.  Accept that takeover only when the post-input RAM still proves the
                # exact stand tile, facing and object.  Movement steps never pass ``interaction``:
                # a wild encounter while walking onto the approach tile remains an interruption.
                if (
                    interaction is not None
                    and m is not None
                    and (
                        interaction[1].kind == "obstacle"
                        or interaction[1].info.get("objective_target") is True
                    )
                    and self._at_interaction_boundary(m, *interaction)
                ):
                    walk.state = m
                    walk.phase = "interact"
                    return "engaged"
                yield Interrupt(
                    f"field approach interrupted by {obs.scene.scene_id!r}; retry after it ends"
                )
                return "failed"
            state = obs.snapshot.map
            if state is None or state.player is None:
                walk.map_retries += 1
                if walk.map_retries > MAP_RETRIES:
                    return "failed"
                obs = yield Advance(2)
                continue
            walk.map_retries = 0
            walk.state = state
            return "left_map" if _map_key(state) != start_map else "ok"

    @classmethod
    def _at_interaction_boundary(
        cls, state: MapState, approach: T.Approach, target: T.Target
    ) -> bool:
        player = state.player
        return bool(
            player is not None
            and (player.avatar_x, player.avatar_y) == approach.stand
            and (approach.face is None or player.facing == approach.face)
            and cls._interaction_still_valid(state, approach, target)
        )

    @staticmethod
    def _interaction_still_valid(state: MapState, approach: T.Approach, target: T.Target) -> bool:
        """The thing to talk to is still in front of the standing tile (NPCs wander)."""
        if target.kind in ("sign", "pc") or approach.face is None:
            return True
        vx, vy = K.VECTOR[approach.face]
        front = (approach.stand[0] + vx, approach.stand[1] + vy)
        local_id = target.info.get("local_id")
        obj = state.object_by_local_id(local_id) if local_id is not None else None
        if obj is None or obj.hidden_flag_set is True:
            return False
        if target.kind == "obstacle":
            return (obj.x, obj.y) == front
        if obj.live is None and not target.info.get("objective_target"):
            return False
        if obj.live is None:
            return (obj.x, obj.y) == front
        beyond = (front[0] + vx, front[1] + vy)
        return (obj.x, obj.y) == front or (
            (obj.x, obj.y) == beyond and N.Navigator(state).is_counter(*front)
        )


# -- small helpers ---------------------------------------------------------------------------------


def _left(obs: Observation, start_map: tuple[int, int]) -> bool:
    """The observation shows a different, readable map than the one the walk started in."""
    m = obs.snapshot.map
    return m is not None and _map_key(m) != start_map


def _at(state: MapState, xy: tuple[int, int]) -> bool:
    player = state.player
    return player is not None and (player.avatar_x, player.avatar_y) == xy


def _at_observation(obs: Observation, xy: tuple[int, int]) -> bool:
    return (
        obs.scene.scene_id in _MOVEMENT_SCENES
        and obs.snapshot.map is not None
        and _at(obs.snapshot.map, xy)
    )


def _player_idle(obs: Observation) -> bool:
    """Whether the field snapshot has released control after a tile movement."""
    state = obs.snapshot.map
    player = state.player if state is not None else None
    return bool(
        obs.scene.scene_id in _MOVEMENT_SCENES
        and player is not None
        and not player.moving
        and player.tile_transition_state == 0
    )


def _find(state: MapState, nav: N.Navigator, option_id: str) -> T.Target | None:
    required_npc = None
    if option_id.startswith("talk:"):
        required_npc = int(option_id.removeprefix("talk:"))
    for target in T.enumerate_targets(state, nav, training=True, required_npc=required_npc):
        if target.option_id == option_id:
            return target
    return None


def _dest(ctx: StepContext) -> tuple[int, int] | None:
    g, n = ctx.args["dest_group"], ctx.args["dest_number"]
    return (g, n) if g >= 0 else (-1, -1)


def executor_specs(
    ledger: AttemptLedger | None = None,
    plans=None,
    prompt_policy: PromptPolicy | None = None,
    *,
    item_catalog: ItemCatalog | None = None,
    walk_step_limit: int | None = None,
) -> list[ExecutorSpec]:
    """The field executors; ``plans`` (a ``BoulderPlans``, shared with the builder) adds
    ``field.push_boulders``."""
    macros = FieldMacros(ledger, prompt_policy, item_catalog, walk_step_limit)
    specs = macros.specs()
    if plans is not None:
        from pokegent.actions.field.boulder_macro import macro_spec

        specs.append(macro_spec(macros, plans))
    return specs


__all__ = ["FieldMacros", "LedgerKey", "executor_specs", "took_over"]
