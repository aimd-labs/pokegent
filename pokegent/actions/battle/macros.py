"""Executor macros of the battle family (spec 5.3, 11.2): semantic options over the real menus.

Every macro is a generator that yields :class:`Tap`/:class:`WaitUntil`/... steps and receives the
fresh :class:`Observation` after each of them (the executor settles busy phases first and checks the
scene against ``allowed_scenes``). A macro never assumes a press worked: it reads the cursor /
task /
controller evidence after EVERY step, presses only what the evidence says is next, and gives up
(``_abort``) instead of pressing blindly. The model never supplies a key.

Menu models, each verified live (see ``the runtime contracts``):

* action menu: 2x2 cursor ``gActionSelectionCursor[0]``: 0 FIGHT, 1 BAG, 2 POKEMON, 3 RUN; the
  horizontal move toggles bit 0, the vertical move bit 1 (no wrap);
* move menu: 2x2 cursor ``gMoveSelectionCursor[0]`` over the slots, empty slots are unreachable;
* party screen: a ring ``slot 0 .. n-1, CANCEL(7)`` walked with DOWN/UP (wraps); A on a mon opens a
  3 entry submenu (SHIFT / SUMMARY / CANCEL); A on entry 0 performs the switch;
* bag: pocket ``gBagMenuState.pocket`` (0 items, 1 key items, 2 balls) with LEFT/RIGHT (no wrap),
item
  row = list scroll offset + cursor row with UP/DOWN, A opens the context menu (USE / CANCEL);
* yes/no box: ``gBattleCommunication[1]`` 0 = YES, 1 = NO with UP/DOWN.

Frame budgets note: opening the party screen takes about 65 frames and the party reader is invalid
for exactly one frame inside that window (and once more when a switch swaps the party order). The
executor interrupts a macro whose boundary snapshot is unusable, so every press that triggers one of
those windows carries a ``wait_frames`` long enough to run past it before the boundary snapshot.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Any

from pokegent.actions._exec.models import Advance, ExecutionKind, Tap
from pokegent.actions._exec.registry import Arg, ExecutorSpec, StepContext
from pokegent.actions.battle import refs as R
from pokegent.actions.battle import state as S
from pokegent.actions.battle.detect import yes_no_state
from pokegent.actions.battle.reader import battle_of
from pokegent.games.gen3.firered.reader.ui_symbols import KEY_BITS
from pokegent.games.gen3.moves.learn import replace_move
from pokegent.pipeline import Observation

#: A press is held until the GAME has read the button (see :func:`press`). The first chunk covers
#: the usual read latency on the real ROM (1 to 3 frames at the action menu, 4 in the bag list,
#: the runtime contracts row 12); the bound is twice the longest measured latency.
PRESS_HOLD = 3
PRESS_MAX_HOLD = 8
NAV_WAIT = 3
# : opening the party screen: A -> fade -> load (invalid party frame at about +19) -> ready at about
# +65
PARTY_OPEN_WAIT = 80
#: A on SHIFT / SEND OUT: callbacks at +11, party swap (invalid frame) at about +37, battle at +44
PARTY_CONFIRM_WAIT = 64
BAG_OPEN_WAIT = 30

#: Post-step busy wait after a press that commits a turn or advances a text (``settle_frames`` /
#: ``settle_wall_s`` of the specs below). Measured (the runtime contracts section
# 7, row 9): with
#: the
#: slow text speed of the league saves every battle string prints for 66-96 frames and the attacks
#: animate for 150-220 frames, so one Lorelei turn (SURF, AMNESIA, hail messages) ran 1705 frames
#: without a single input-ready moment; the old 1500-frame bound ended it with
#: ``E_WAIT_TIMEOUT_FRAMES`` 190 frames before the action menu came back, three turns in a row. A
#: turn with two attacks, a faint, experience and weather needs about 2500; 6000 frames (100 s of
#: game time) keep a margin of more than 2x and are still a hard bound for a game that hangs. The
#: wall bound is the same limit for a loaded host (the league runs measured 40-150 frames/s).
TURN_SETTLE_FRAMES = 6000
TURN_SETTLE_WALL_S = 300.0

ACTION_FIGHT, ACTION_BAG, ACTION_POKEMON, ACTION_RUN = 0, 1, 2, 3
PARTY_CANCEL_SLOT = 7
MOVE_STRUGGLE = 165

Macro = Generator[Any, Observation, None]


class Abort(Exception):
    """Internal: stop the macro (the postcondition then fails with the recorded reason)."""


def _abort(ctx: StepContext, why: str) -> None:
    ctx.args["_abort"] = why  # shared with the VerifyContext: the verifier reports it


def _obs0(ctx: StepContext) -> Observation:
    return Observation(ctx.snapshot, ctx.scene)


# -- evidence readers ------------------------------------------------------------------------------


def scene_of(obs: Observation) -> str:
    return obs.scene.scene_id


def inner_scene_of(obs: Observation) -> str:
    """Concrete menu hidden by a special battle scene, when the detector supplies one."""
    return str(obs.scene.evidence.get("inner_scene") or obs.scene.scene_id)


def is_scene(obs: Observation, *scenes: str) -> bool:
    return inner_scene_of(obs) in scenes


def phase_of(obs: Observation) -> str:
    return str(obs.scene.evidence.get("phase", ""))


def block_of(obs: Observation) -> S.BattleBlock | None:
    b = battle_of(obs.snapshot)
    return b if b is not None and b.valid and b.active else None


def action_cursor(obs: Observation) -> int | None:
    ev = obs.snapshot.ui.battle if obs.snapshot.ui else None
    battler = ev.input_battlers[0] if ev and ev.input_battlers else 0
    return ev.action_cursor[battler] if ev and battler < len(ev.action_cursor) else None


def move_cursor(obs: Observation) -> int | None:
    ev = obs.snapshot.ui.battle if obs.snapshot.ui else None
    battler = ev.input_battlers[0] if ev and ev.input_battlers else 0
    return ev.move_cursor[battler] if ev and battler < len(ev.move_cursor) else None


def _player_controller(obs: Observation, phase: str):
    ui = obs.snapshot.ui
    battle = ui.battle if ui else None
    if battle is None:
        return None
    return next(
        (
            c
            for c in battle.controllers
            if c.player_side and c.phase == phase and c.accepts_input is True
        ),
        None,
    )


def _wait_for_controller_dispatch(
    obs: Observation, phase: str
) -> Generator[Any, Observation, tuple[Observation, bool]]:
    """Wait for the RAM controller handoff after a menu press.

    The input actor can finish its hold one frame before the battle controller consumes the new
    edge.  Looking only at the menu scene at that boundary therefore causes a spurious retry.
    """
    for _ in range(3):
        if _player_controller(obs, phase) is None:
            return obs, True
        obs = yield Advance(1)
    return obs, False


def target_cursor(obs: Observation) -> int | None:
    ev = obs.snapshot.ui.battle if obs.snapshot.ui else None
    return ev.target_cursor if ev else None


def party_slot(obs: Observation) -> int | None:
    ui = obs.snapshot.ui
    return ui.menu.party.slot_id if ui and ui.menu.party else None


def submenu_cursor(obs: Observation) -> tuple[int, int] | None:
    ui = obs.snapshot.ui
    m = ui.menu.menu if ui else None
    if m is None or not m.window_in_use or not m.consistent:
        return None
    return m.cursor_pos, m.max_cursor_pos


def bag_pocket(obs: Observation) -> int | None:
    ui = obs.snapshot.ui
    return ui.menu.bag.pocket if ui and ui.menu.bag else None


def bag_row(obs: Observation) -> int | None:
    ui = obs.snapshot.ui
    lm = ui.menu.list_menu if ui else None
    return lm.scroll_offset + lm.cursor_row if lm else None


def screen_slot(obs: Observation, personality: int) -> int | None:
    """Where the Pokemon with this personality stands in the party screen that is open NOW.

    Opening a battle party screen reorders ``gPlayerParty`` into the battle order
    (``UpdatePartyToBattleOrder``), so the slot a mon had in the decision snapshot is NOT its
    slot on the screen. The personality (PID) is the identity that survives; the screen slot is
    looked up in the snapshot taken once the screen is up.
    """
    for mon in obs.snapshot.party:
        if mon.personality == personality:
            return mon.slot
    return None


def printer_signature(obs: Observation) -> tuple:
    ui = obs.snapshot.ui
    if ui is None:
        return ()
    return tuple((p.window_id, p.state, p.current_char_ptr) for p in ui.text.printers)


# -- pressing a button ----------------------------------------------------------------------------


def _held_by_game(obs: Observation, bit: int) -> bool | None:
    """Did the game's last button poll (``gMain.heldKeys``) see this button down? ``None``: n/a."""
    ui = obs.snapshot.ui
    held = ui.main.held_keys if ui is not None and ui.main.valid else None
    return None if held is None else bool(held & bit)


def press(obs: Observation, button: str, wait: int) -> Generator[Any, Observation, Observation]:
    """Press ``button`` until the game has read it, then wait ``wait`` frames with it released.

    The game reads its buttons once per main-loop iteration, not once per frame. Right after a turn,
    and in the bag list after the cursor scrolled, an iteration takes up to four frames, so the old
    two-frame tap fell between two reads and the game never saw the press: 9 of 80 uses of one item
    in M4 run 1 (the runtime contracts row 12). The controller and the cursor
    already looked ready,
    so no menu evidence could tell. The game's own record of what it read can: the press is held
    until ``gMain.heldKeys`` shows the button (bounded by ``PRESS_MAX_HOLD``). Before it, the game
    must have seen the button UP, else this press would merge with the previous one into a single
    long press without a new edge.
    """
    bit = KEY_BITS[button.upper()]
    for _ in range(PRESS_MAX_HOLD):
        if not _held_by_game(obs, bit):
            break
        obs = yield Advance(1)

    def read(now: Observation) -> bool:
        return _held_by_game(now, bit) is not False  # unknown register: do not hold on

    return (yield Tap(button, PRESS_HOLD, wait, until=read, max_hold_frames=PRESS_MAX_HOLD))


# -- cursor navigation -----------------------------------------------------------------------------


def grid_button(cur: int, target: int) -> str | None:
    """The next D-pad key on a 2x2 grid (bit 0 = column, bit 1 = row, no wrap)."""
    if (cur & 1) != (target & 1):
        return "right" if target & 1 else "left"
    if (cur & 2) != (target & 2):
        return "down" if target & 2 else "up"
    return None


def ring_button(ring: list[int], cur: int, target: int) -> str | None:
    """The shorter direction on the wrapping party ring (DOWN = next entry of ``ring``)."""
    if cur not in ring or target not in ring:
        return None
    n = len(ring)
    delta = (ring.index(target) - ring.index(cur)) % n
    if delta == 0:
        return None
    return "down" if delta <= n // 2 else "up"


def linear_button(cur: int, target: int) -> str | None:
    if cur == target:
        return None
    return "down" if target > cur else "up"


def goto(
    obs: Observation,
    read: Callable[[Observation], int | None],
    target: int,
    button_for: Callable[[int, int], str | None],
    *,
    limit: int = 10,
    wait: int = NAV_WAIT,
) -> Generator[Any, Observation, Observation | None]:
    """Press until ``read(obs) == target``; ``None`` if the cursor stops responding or runs out."""
    stuck = presses = 0
    while True:
        cur = read(obs)
        if cur == target:
            return obs
        if cur is None or presses >= limit:
            return None
        button = button_for(cur, target)
        if button is None:
            return None
        obs = yield from press(obs, button, wait)
        presses += 1
        if read(obs) == cur:
            stuck += 1
            if stuck >= 2:
                return None
        else:
            stuck = 0


# -- shared sub-routines ---------------------------------------------------------------------------


def _to_action_menu(
    ctx: StepContext, obs: Observation
) -> Generator[Any, Observation, Observation | None]:
    """Back out of a move menu (B) until the action menu is up; ``None`` when not possible."""
    for _ in range(3):
        sc = inner_scene_of(obs)
        if sc == "battle.action":
            return obs
        if sc == "battle.moves_ui":
            obs = yield from press(obs, "b", 20)
            continue
        _abort(ctx, f"cannot reach the action menu from {sc}")
        return None
    _abort(ctx, "action menu did not come back")
    return None


def _select_action(
    ctx: StepContext, obs: Observation, action: int, *, wait: int
) -> Generator[Any, Observation, Observation | None]:
    """From the action menu: put the cursor on ``action`` and press A. Returns the observation."""
    obs = yield from _to_action_menu(ctx, obs)
    if obs is None:
        return None
    obs = yield from goto(obs, action_cursor, action, grid_button, limit=4)
    if obs is None:
        _abort(ctx, f"action cursor did not reach {action}")
        return None
    if not is_scene(obs, "battle.action"):
        _abort(ctx, f"left the action menu while moving the cursor: {scene_of(obs)}")
        return None
    obs = yield from press(obs, "a", wait)
    obs, dispatched = yield from _wait_for_controller_dispatch(obs, "action")
    ctx.args["_action_dispatched"] = dispatched
    return obs


# -- USE_MOVE --------------------------------------------------------------------------------------


def use_move(ctx: StepContext) -> Macro:
    slot, move_id = ctx.args["slot"], ctx.args["move_id"]
    obs = _obs0(ctx)
    for _ in range(3):
        sc = inner_scene_of(obs)
        if sc == "battle.action":
            obs = yield from _select_action(ctx, obs, ACTION_FIGHT, wait=NAV_WAIT)
            if obs is None:
                return
            if is_scene(obs, "battle.action"):
                # M2.5 soak: the A on FIGHT was not taken (18 of 62 decisions of one gym battle);
                # `_select_action` re-reads the cursor on this fresh observation before every press,
                # so a second look is safe and bounded by the loop
                continue
            if not is_scene(obs, "battle.moves_ui"):
                break  # the game chose by itself (Encore / Struggle) or the turn already runs
            continue
        if sc == "battle.moves_ui":
            break
        _abort(ctx, f"use_move started in {sc}")
        return
    if ctx.args.get("_action_dispatched") is False:
        ctx.args["_not_executed"] = "FIGHT was not accepted by the battle controller"
        return
    if is_scene(obs, "battle.moves_ui"):
        block = block_of(obs)
        battler = int(ctx.args.get("battler", block.decision_battler if block else 0) or 0)
        actor = block.battler_at(battler) if block is not None else None
        if actor is None or actor.moves[slot].move_id != move_id:
            _abort(ctx, "the move at the slot is not the one that was offered")
            return
        obs = yield from goto(obs, move_cursor, slot, grid_button, limit=4)
        if obs is None or not is_scene(obs, "battle.moves_ui"):
            _abort(ctx, "move cursor did not reach the slot")
            return
        obs = yield from press(obs, "a", NAV_WAIT)
        obs, dispatched = yield from _wait_for_controller_dispatch(obs, "move")
        if is_scene(obs, "battle.double_target", "battle.choose_target") and dispatched:
            ctx.args["_target_pending"] = True
            ctx.args["_move_dispatched"] = True
            ctx.args["_done"] = True
            return
        if is_scene(obs, "battle.moves_ui"):
            _abort(ctx, "the game kept the move menu open after A (move refused?)")
            return
        if not dispatched:
            _abort(ctx, "move controller did not leave its input phase after A")
            return
    elif is_scene(obs, "battle.action"):
        # A controller can legitimately decline a move (for example while the prior turn's
        # message still owns the input).  Do not manufacture a commitment; let the next battle
        # decision retry it, and report this action as applied-but-not-executed.
        ctx.args["_not_executed"] = "FIGHT was not accepted by the battle controller"
        return
    # The executor settles busy phases before this observation, so the turn may already have run
    # and the game may have reset gChosenMoveByBattler (BattleTurnPassed). The controller handoff
    # records that the move menu accepted A; the verifier must not require the turn's effect.
    ctx.args["_move_dispatched"] = True
    ctx.args["_done"] = True


def use_struggle(ctx: StepContext) -> Macro:
    obs = _obs0(ctx)
    obs = yield from _select_action(ctx, obs, ACTION_FIGHT, wait=NAV_WAIT)
    if obs is None:
        return
    if is_scene(obs, "battle.action", "battle.moves_ui") or not ctx.args.get("_action_dispatched"):
        _abort(ctx, "a menu is still up after FIGHT: Struggle was not forced")
        return
    ctx.args["_move_dispatched"] = True
    ctx.args["_done"] = True


# -- ATTEMPT_ESCAPE --------------------------------------------------------------------------------


def attempt_escape(ctx: StepContext) -> Macro:
    obs = _obs0(ctx)
    obs = yield from _select_action(ctx, obs, ACTION_RUN, wait=NAV_WAIT)
    if obs is None:
        return
    if is_scene(obs, "battle.action"):
        block = block_of(obs)
        if block is None or block.outcome != 4:
            _abort(ctx, "the action menu is still up after RUN")
            return
    ctx.args["_done"] = True


def safari_action(ctx: StepContext) -> Macro:
    """Select BALL / BAIT / ROCK / RUN on the Safari controller's verified 2x2 menu."""
    entry = ctx.args["entry"]
    obs = _obs0(ctx)
    if scene_of(obs) != "battle.safari" or phase_of(obs) != "action":
        _abort(ctx, f"Safari action started in {scene_of(obs)}/{phase_of(obs)}")
        return
    obs = yield from goto(obs, action_cursor, entry, grid_button, limit=4)
    if obs is None or scene_of(obs) != "battle.safari":
        _abort(ctx, "Safari cursor did not reach the offered action")
        return
    yield from press(obs, "a", NAV_WAIT)
    ctx.args["_done"] = True


def choose_target(ctx: StepContext) -> Macro:
    """Walk the game's own target ring and confirm the requested present battler."""
    wanted = ctx.args["battler"]
    obs = _obs0(ctx)
    if not is_scene(obs, "battle.choose_target", "battle.double_target"):
        _abort(ctx, f"target choice started in {scene_of(obs)}")
        return
    for _ in range(S.MAX_BATTLERS + 1):
        if target_cursor(obs) == wanted:
            yield from press(obs, "a", NAV_WAIT)
            ctx.args["_done"] = True
            return
        obs = yield from press(obs, "right", NAV_WAIT)
    _abort(ctx, f"target cursor did not reach battler {wanted}")


# -- party screen: SWITCH_TO / SEND_OUT ------------------------------------------------------------


def _in_party(obs: Observation) -> bool:
    return is_scene(obs, "battle.party", "battle.forced_switch", "battle.item_target")


def _walk_party_to(
    ctx: StepContext, obs: Observation, personality: int, what: str
) -> Generator[Any, Observation, Observation | None]:
    """Walk the party cursor onto the Pokemon with ``personality`` (looked up on the open screen)"""
    slot = screen_slot(obs, personality)
    if slot is None:
        _abort(ctx, f"the offered Pokemon is not in the open party screen ({what})")
        return None
    ring = list(range(len(obs.snapshot.party))) + [PARTY_CANCEL_SLOT]
    obs = yield from goto(
        obs, party_slot, slot, lambda c, t: ring_button(ring, c, t), limit=len(ring) + 2
    )
    if obs is None or not _in_party(obs):
        _abort(ctx, f"party cursor did not reach the Pokemon ({what})")
        return None
    if screen_slot(obs, personality) != slot or party_slot(obs) != slot:
        _abort(ctx, f"the party order changed under the cursor ({what})")
        return None
    return obs


def _party_select_and_confirm(
    ctx: StepContext, obs: Observation, personality: int
) -> Generator[Any, Observation, bool]:
    """On a ready party screen: walk to the Pokemon, A, then A on the first submenu entry."""
    if phase_of(obs) == "submenu":
        obs = yield from press(obs, "b", 20)  # close a submenu left open by an earlier attempt
        if not _in_party(obs) or phase_of(obs) != "choose":
            _abort(ctx, "could not close the party submenu")
            return False
    obs = yield from _walk_party_to(ctx, obs, personality, "switch")
    if obs is None:
        return False
    obs = yield from press(obs, "a", 8)  # opens the submenu (Task_HandleSelectionMenuInput)
    if not _in_party(obs) or phase_of(obs) != "submenu":
        _abort(ctx, f"no selection submenu after A (scene {scene_of(obs)}/{phase_of(obs)})")
        return False
    cur = submenu_cursor(obs)
    if cur is None or cur != (0, 2):
        _abort(ctx, f"unexpected submenu shape {cur}")
        return False
    yield from press(obs, "a", PARTY_CONFIRM_WAIT)  # SHIFT / SEND OUT
    return True


def switch_to(ctx: StepContext) -> Macro:
    personality = ctx.args["personality"]
    obs = _obs0(ctx)
    if is_scene(obs, "battle.action", "battle.moves_ui"):
        obs = yield from _select_action(ctx, obs, ACTION_POKEMON, wait=PARTY_OPEN_WAIT)
        if obs is None:
            return
    if not is_scene(obs, "battle.party"):
        _abort(ctx, f"the party screen did not open ({scene_of(obs)})")
        return
    ok = yield from _party_select_and_confirm(ctx, obs, personality)
    if ok:
        ctx.args["_done"] = True


def send_out(ctx: StepContext) -> Macro:
    personality = ctx.args["personality"]
    obs = _obs0(ctx)
    if not is_scene(obs, "battle.forced_switch"):
        _abort(ctx, f"send_out started in {scene_of(obs)}")
        return
    ok = yield from _party_select_and_confirm(ctx, obs, personality)
    if ok:
        ctx.args["_done"] = True


# -- bag: USE_BATTLE_ITEM / THROW_BALL -------------------------------------------------------------


def _open_bag(
    ctx: StepContext, obs: Observation
) -> Generator[Any, Observation, Observation | None]:
    if is_scene(obs, "battle.action", "battle.moves_ui"):
        obs = yield from _select_action(ctx, obs, ACTION_BAG, wait=BAG_OPEN_WAIT)
        if obs is None:
            return None
    if not is_scene(obs, "battle.bag") or phase_of(obs) != "list":
        _abort(ctx, f"the bag did not open ({scene_of(obs)}/{phase_of(obs)})")
        return None
    return obs


def _pick_bag_item(
    ctx: StepContext, obs: Observation, item_id: int, pocket: int
) -> Generator[Any, Observation, Observation | None]:
    obs = yield from goto(obs, bag_pocket, pocket, linear_pocket, limit=4, wait=NAV_WAIT)
    if obs is None or not is_scene(obs, "battle.bag") or phase_of(obs) != "list":
        _abort(ctx, "bag pocket did not reach the wanted pocket")
        return None
    pocket_names = {0: "items", 1: "key_items", 2: "poke_balls"}
    pocket_items = next(
        (p.items for p in ctx.snapshot.bag if p.pocket == pocket_names.get(pocket)), ()
    )
    index = next((i for i, it in enumerate(pocket_items) if it.item_id == item_id), None)
    if index is None:
        _abort(ctx, "the item is not in the offered pocket")
        return None
    obs = yield from goto(obs, bag_row, index, linear_button, limit=len(pocket_items) + 3)
    if obs is None or not is_scene(obs, "battle.bag"):
        _abort(ctx, "bag cursor did not reach the item")
        return None
    obs = yield from press(obs, "a", 10)  # context menu
    if not is_scene(obs, "battle.bag") or phase_of(obs) != "context":
        _abort(ctx, f"no item context menu ({scene_of(obs)}/{phase_of(obs)})")
        return None
    cur = submenu_cursor(obs)
    if cur is None or cur[0] != 0 or cur[1] != 1:
        _abort(ctx, f"unexpected context menu {cur}: the item has no USE entry")
        return None
    return obs


def linear_pocket(cur: int, target: int) -> str | None:
    if cur == target:
        return None
    return "right" if target > cur else "left"


def use_item(ctx: StepContext) -> Macro:
    item_id, target, pocket = ctx.args["item_id"], ctx.args["target_slot"], ctx.args["pocket"]
    obs = _obs0(ctx)
    obs = yield from _open_bag(ctx, obs)
    if obs is None:
        return
    obs = yield from _pick_bag_item(ctx, obs, item_id, pocket)
    if obs is None:
        return
    if target < 0:  # immediate item: USE closes the bag and the turn is committed
        yield from press(obs, "a", NAV_WAIT)
        ctx.args["_done"] = True
        return
    obs = yield from press(obs, "a", PARTY_OPEN_WAIT)  # USE -> party screen of the item
    if not is_scene(obs, "battle.item_target") or phase_of(obs) != "choose":
        _abort(ctx, f"the item target screen did not open ({scene_of(obs)}/{phase_of(obs)})")
        return
    wanted = next((m.personality for m in ctx.snapshot.party if m.slot == target), None)
    if wanted is None:
        _abort(ctx, "the item target is not in the party")
        return
    obs = yield from _walk_party_to(ctx, obs, wanted, "item target")
    if obs is None or not is_scene(obs, "battle.item_target"):
        return
    yield from press(obs, "a", NAV_WAIT)
    ctx.args["_done"] = True


def throw_ball(ctx: StepContext) -> Macro:
    item_id = ctx.args["item_id"]
    obs = _obs0(ctx)
    obs = yield from _open_bag(ctx, obs)
    if obs is None:
        return
    obs = yield from _pick_bag_item(ctx, obs, item_id, 2)
    if obs is None:
        return
    yield from press(obs, "a", NAV_WAIT)  # USE
    ctx.args["_done"] = True


# -- single presses --------------------------------------------------------------------------------


def advance_text(ctx: StepContext) -> Macro:
    obs = _obs0(ctx)
    before = printer_signature(obs)
    msg = block_of(obs).message if block_of(obs) else ""
    scene = scene_of(obs)
    obs = yield from press(obs, "a", NAV_WAIT)
    after = block_of(obs)
    if (
        printer_signature(obs) != before
        or scene_of(obs) != scene
        or (after is not None and after.message != msg)
    ):
        ctx.args["_done"] = True
        return
    _abort(ctx, "the text did not advance after A")


def acknowledge(ctx: StepContext) -> Macro:
    obs = _obs0(ctx)
    block = block_of(obs)
    before = block.lvlup_state if block else None
    obs = yield from press(obs, "a", NAV_WAIT)
    after = block_of(obs)
    if scene_of(obs) != "battle.level_up" or (after is not None and after.lvlup_state != before):
        ctx.args["_done"] = True
        return
    _abort(ctx, "the level-up box did not react to A")


def back(ctx: StepContext) -> Macro:
    obs = _obs0(ctx)
    start = inner_scene_of(obs)
    for _ in range(3):
        obs = yield from press(obs, "b", 30 if start != "battle.moves_ui" else 12)
        if inner_scene_of(obs) not in (
            "battle.party",
            "battle.bag",
            "battle.item_target",
            "battle.moves_ui",
        ):
            ctx.args["_done"] = True
            return
    _abort(ctx, "the menu is still open after three B presses")


PROMPT_STATE = {
    R.PROMPT_SWITCH_OFFER: "yesnobox",
    R.PROMPT_USE_NEXT: "yesnobox",
    R.PROMPT_LEARN_MOVE: "learn_move",
    R.PROMPT_STOP_LEARNING: "stop_learning",
    R.PROMPT_NICKNAME: "nickname",
}


def answer(ctx: StepContext) -> Macro:
    prompt, cursor = ctx.args["box"], ctx.args["cursor"]
    kind = PROMPT_STATE[prompt]
    obs = _obs0(ctx)
    # a stale command-state byte can look like a ready box for one frame: look again before any key
    obs = yield Advance(4)
    block = block_of(obs)
    if yes_no_state(block) != kind:
        _abort(ctx, "the yes/no box is not (or no longer) up")
        return
    obs = yield from goto(
        obs,
        lambda o: block_of(o).yes_no_cursor if block_of(o) else None,
        cursor,
        lambda c, t: "down" if t > c else "up",
        limit=3,
    )
    block = block_of(obs) if obs is not None else None
    if obs is None or yes_no_state(block) != kind:
        _abort(ctx, "the yes/no cursor did not reach the answer")
        return
    opens_party = cursor == 0 and prompt in (R.PROMPT_SWITCH_OFFER, R.PROMPT_USE_NEXT)
    obs = yield from press(obs, "a", PARTY_OPEN_WAIT if opens_party else NAV_WAIT)
    after = block_of(obs)
    if yes_no_state(after) != kind or scene_of(obs) not in (
        "battle.switch_offer",
        "battle.faint",
        "battle.learn_move",
        "battle.double_target",
        "battle.choose_target",
        "battle.double_turn",
        "battle.double_target",
        "battle.safari",
        "battle.ghost",
        "battle.tutorial_scripted",
        "battle.ended",
    ):
        ctx.args["_done"] = True
        return
    _abort(ctx, "the prompt is still up after A")


# -- registry --------------------------------------------------------------------------------------

_MENUS = frozenset(
    {
        "battle.action",
        "battle.moves_ui",
        "battle.party",
        "battle.forced_switch",
        "battle.bag",
        "battle.item_target",
        "battle.animation",
        "battle.message",
        "battle.entering",
        "battle.faint",
        "battle.ended",
        "battle.level_up",
        "battle.switch_offer",
        "battle.learn_move",
        "battle.double_turn",
        "battle.double_target",
        "battle.choose_target",
        "battle.ghost",
        "battle.safari",
        "battle.tutorial_scripted",
    }
)  # scenes a macro may pass through (busy phases are waited out regardless)


def executors() -> tuple[ExecutorSpec, ...]:
    # The post-step busy wait is the only place a whole turn is waited out: the commit press ends
    # the
    # macro, the game then runs the turn on its own and the executor waits for the first input-ready
    # state (the next action menu, a text waiting for A, a level-up box ...). It must outlast the
    # longest legitimate turn, see TURN_SETTLE_FRAMES.
    big = dict(max_frames=4000, settle_frames=TURN_SETTLE_FRAMES, settle_wall_s=TURN_SETTLE_WALL_S)
    text = dict(settle_frames=TURN_SETTLE_FRAMES, settle_wall_s=TURN_SETTLE_WALL_S)
    menu = dict(settle_frames=1500, settle_wall_s=60.0)  # B out of a menu: a short hand-over only
    return (
        ExecutorSpec(
            R.USE_MOVE,
            ExecutionKind.MACRO,
            use_move,
            {
                "slot": Arg(int, min=0, max=3),
                "move_id": Arg(int, min=1, max=0xFFFF),
                "battler": Arg(int, required=False, min=0, max=3),
            },
            allowed_scenes=_MENUS,
            max_inputs=12,
            **big,
        ),
        ExecutorSpec(
            R.USE_STRUGGLE,
            ExecutionKind.MACRO,
            use_struggle,
            {},
            allowed_scenes=_MENUS,
            max_inputs=8,
            **big,
        ),
        ExecutorSpec(
            R.FORGET_MOVE,
            ExecutionKind.MACRO,
            replace_move,
            {
                "slot": Arg(int, min=0, max=3),
                "old_move": Arg(int, min=1, max=0xFFFF),
                "new_move": Arg(int, min=1, max=0xFFFF),
                "party_id": Arg(int, min=0, max=5),
            },
            # The replacement writes party RAM before the battle's follow-up message is
            # presented. Let the executor's move-list verifier consume that readable scene.
            allowed_scenes=frozenset({"battle.learn_move", "battle.message"}),
            max_inputs=6,
            max_frames=2000,
            settle_frames=900,
            settle_wall_s=60.0,
        ),
        ExecutorSpec(
            R.ESCAPE,
            ExecutionKind.MACRO,
            attempt_escape,
            {},
            allowed_scenes=_MENUS,
            max_inputs=8,
            **big,
        ),
        ExecutorSpec(
            R.SWITCH_TO,
            ExecutionKind.MACRO,
            switch_to,
            {"slot": Arg(int, min=0, max=5), "personality": Arg(int, min=0, max=0xFFFFFFFF)},
            allowed_scenes=_MENUS,
            max_inputs=20,
            **big,
        ),
        ExecutorSpec(
            R.SEND_OUT,
            ExecutionKind.MACRO,
            send_out,
            {"slot": Arg(int, min=0, max=5), "personality": Arg(int, min=0, max=0xFFFFFFFF)},
            allowed_scenes=_MENUS,
            max_inputs=20,
            **big,
        ),
        ExecutorSpec(
            R.USE_ITEM,
            ExecutionKind.MACRO,
            use_item,
            {
                "item_id": Arg(int, min=1, max=374),
                "target_slot": Arg(int, min=-1, max=5),
                "pocket": Arg(int, min=0, max=2),
            },
            allowed_scenes=_MENUS,
            max_inputs=30,
            **big,
        ),
        ExecutorSpec(
            R.THROW_BALL,
            ExecutionKind.MACRO,
            throw_ball,
            {"item_id": Arg(int, min=1, max=374)},
            allowed_scenes=_MENUS,
            max_inputs=24,
            **big,
        ),
        ExecutorSpec(
            R.ADVANCE_TEXT,
            ExecutionKind.ATOMIC,
            advance_text,
            {},
            allowed_scenes=_MENUS,
            max_frames=900,
            **text,
        ),
        ExecutorSpec(
            R.ACKNOWLEDGE,
            ExecutionKind.ATOMIC,
            acknowledge,
            {},
            allowed_scenes=_MENUS,
            max_frames=900,
            **text,
        ),
        ExecutorSpec(
            R.BACK,
            ExecutionKind.MACRO,
            back,
            {},
            allowed_scenes=_MENUS,
            max_inputs=4,
            max_frames=1200,
            **menu,
        ),
        ExecutorSpec(
            R.ANSWER,
            ExecutionKind.MACRO,
            answer,
            {"box": Arg(int, min=1, max=5), "cursor": Arg(int, min=0, max=1)},
            allowed_scenes=_MENUS,
            max_inputs=6,
            max_frames=1500,
            **text,
        ),
        ExecutorSpec(
            R.SAFARI_ACTION,
            ExecutionKind.MACRO,
            safari_action,
            {"entry": Arg(int, min=0, max=3)},
            allowed_scenes=frozenset({"battle.safari"}),
            max_inputs=5,
            **big,
        ),
        ExecutorSpec(
            R.CHOOSE_TARGET,
            ExecutionKind.MACRO,
            choose_target,
            {"battler": Arg(int, min=0, max=3)},
            allowed_scenes=frozenset(
                {"battle.choose_target", "battle.double_target", "battle.double_turn"}
            ),
            max_inputs=6,
            **big,
        ),
    )


__all__ = ["executors", "Abort"]
