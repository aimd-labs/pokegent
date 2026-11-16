"""Postcondition predicates of the battle family (spec 11.3 verifier catalogue).

Each predicate is evaluated by the executor on fresh snapshots with a bounded wait; an unusable
snapshot (the party reader is invalid for a single frame while a switch swaps the party order) is
"not yet", never "failed". The macros leave ``_done`` / ``_abort`` in the shared argument dict:
``_done`` means "every step was confirmed by cursor/task/controller evidence", ``_abort`` carries
the
reason a macro gave up. RAM facts then confirm the outcome independently of the key presses.

The verified outcome is the COMMITMENT of the choice, never its effect: "a legal attack may miss
or be
immune" (spec 11.2 step 7), so no HP loss or hit is required.
"""

from __future__ import annotations

from pokegent.actions._exec.registry import VerifierSpec, VerifyContext
from pokegent.actions.battle import refs as R
from pokegent.actions.battle.reader import battle_of
from pokegent.actions.battle.state import B_ACTION_USE_MOVE
from pokegent.games.gen3.moves.learn import move_list_matches


def returned_to_field(snapshot) -> bool:
    """The battle is over and the game is back on a field callback (the map-return proof of
    ``battle.ended``: ``inBattle`` clear and ``CB2_Overworld``/``CB2_OverworldBasic`` running)."""
    ui = snapshot.ui
    if ui is None or not ui.valid or ui.main.in_battle is not False or ui.main.callback2 is None:
        return False
    return ui.main.callback2.name in ("CB2_Overworld", "CB2_OverworldBasic")


class MacroAborted(RuntimeError):
    """Carries the macro's own reason into the executor's failure text."""


def _check_abort(ctx: VerifyContext) -> None:
    why = ctx.args.get("_abort")
    if why:
        raise MacroAborted(str(why))


def _player(snapshot, battler: int = 0):
    block = battle_of(snapshot)
    return block.battler_at(battler) if block is not None and block.valid and block.active else None


def _marker_changed(before, after) -> bool:
    b, a = battle_of(before), battle_of(after)
    return bool(b and a and b.valid and a.valid and b.turn_marker != a.turn_marker)


def _choice_recorded(after, action: int, move_id: int | None = None, battler: int = 0) -> bool:
    """The choice is still in RAM: true from the commit until ``BattleTurnPassed`` clears it."""
    a = _player(after, battler)
    if a is None:
        return False
    return a.chosen_action == action and (move_id is None or a.chosen_move == move_id)


# NOTE on the design of every predicate below: the executor settles busy phases BEFORE the first
# verification read, so the first stable state is either mid-turn (a text waiting for A: the choice
# is still recorded) or after the turn (the choice is cleared, the turn left its marks). A
# predicate must hold in BOTH, because the verification wait cannot press A: waiting for a later
# state would deadlock on the very text the next decision will advance.


def move_committed(ctx: VerifyContext) -> bool:
    """Verify that the selected move was committed, not that its effect occurred.

    ``use_move`` sets ``_move_dispatched`` only after the game has accepted the move-menu A and
    the player controller has left its input phase. That is the durable fallback evidence. The
    selected move word
    (``gChosenMoveByBattler``) and the action word are transient: ``BattleTurnPassed`` clears
    them before the executor's first post-step observation in some turns.  Likewise, PP/HP,
    ``gRandomTurnNumber`` and ``gLastMoves`` describe a completed turn, not a committed choice;
    none of them changes when the player is flinched, asleep, paralysed, confused, faints before
    acting, or the opponent faints first.  A legal choice must still be APPLIED in those cases.
    """
    _check_abort(ctx)
    if ctx.args.get("_not_executed"):
        return True
    if not ctx.args.get("_done"):
        return False
    if ctx.args.get("_target_pending"):
        battle = ctx.after.ui.battle if ctx.after.ui else None
        return bool(battle and any(c.phase == "target" for c in battle.controllers))
    # Prefer the actual battle RAM evidence whenever it survived the executor's settling pass.
    # The game clears these words in BattleTurnPassed, so a cancelled/no-effect turn can only be
    # proved by the controller handoff recorded by the macro.  `_done` alone is intentionally not
    # enough: it may be supplied by a caller that pressed A without observing that handoff.
    after = ctx.after
    if after is not None and (
        _choice_recorded(after, B_ACTION_USE_MOVE, ctx.args.get("move_id"))
        or _marker_changed(ctx.before, after)
    ):
        return True
    return bool(ctx.args.get("_move_dispatched"))


def move_commit_failure_reason(ctx: VerifyContext) -> str | None:
    """Explain a move postcondition failure without calling a cancelled turn a failed action."""
    if ctx.args.get("_target_pending"):
        return "move was selected; waiting for the target menu to remain available"
    if not ctx.args.get("_done"):
        return "move was not committed: the move menu did not accept the selection"
    if not ctx.args.get("_move_dispatched"):
        return "move was not committed: controller handoff was not observed"
    return None


def switch_done(ctx: VerifyContext) -> bool:
    _check_abort(ctx)
    if not ctx.args.get("_done"):
        return False
    me = _player(ctx.after)
    if me is None:
        return False
    return (
        me.personality == ctx.args["personality"]
        or _choice_recorded(ctx.after, 2)
        or _marker_changed(ctx.before, ctx.after)
    )


def _item_message_pending(after) -> bool:
    """The item's own message is up inside the bag / party screen and waits for A (X items print
    their effect there before the turn is committed): the item was applied, the rest needs A."""
    ui = after.ui
    if ui is None or ui.main.callback2 is None:
        return False
    return ui.main.callback2.name in ("CB2_BagMenuRun", "CB2_UpdatePartyMenu") and any(
        p.waiting_for_button for p in ui.text.printers
    )


def item_used(ctx: VerifyContext) -> bool:
    _check_abort(ctx)
    if not ctx.args.get("_done"):
        return False
    block = battle_of(ctx.after)
    item = ctx.args["item_id"]

    def qty(snapshot) -> int:
        return sum(i.quantity for p in snapshot.bag for i in p.items if i.item_id == item)

    consumed = qty(ctx.after) < qty(ctx.before)
    chosen = block is not None and block.valid and block.last_used_item == item
    return (
        consumed
        or chosen
        or _choice_recorded(ctx.after, 1)
        or _marker_changed(ctx.before, ctx.after)
        or _item_message_pending(ctx.after)
    )


def escape_committed(ctx: VerifyContext) -> bool:
    _check_abort(ctx)
    return bool(ctx.args.get("_done"))


def text_advanced(ctx: VerifyContext) -> bool:
    _check_abort(ctx)
    return bool(ctx.args.get("_done"))


def menu_left(ctx: VerifyContext) -> bool:
    _check_abort(ctx)
    return bool(ctx.args.get("_done"))


def prompt_answered(ctx: VerifyContext) -> bool:
    _check_abort(ctx)
    return bool(ctx.args.get("_done"))


def safari_committed(ctx: VerifyContext) -> bool:
    _check_abort(ctx)
    if not ctx.args.get("_done"):
        return False
    before, after = battle_of(ctx.before), battle_of(ctx.after)
    if after is None or not after.active:
        return True
    if before is None:
        return False
    entry = ctx.args["entry"]
    if entry == 0:
        return after.last_used_item == 5 or _marker_changed(ctx.before, ctx.after)
    if entry == 1:
        return (
            after.safari_bait_turns != before.safari_bait_turns
            or after.safari_catch_factor != before.safari_catch_factor
            or _marker_changed(ctx.before, ctx.after)
        )
    if entry == 2:
        return (
            after.safari_rock_turns != before.safari_rock_turns
            or after.safari_catch_factor != before.safari_catch_factor
            or _marker_changed(ctx.before, ctx.after)
        )
    return after.outcome == 4 or _marker_changed(ctx.before, ctx.after)


def target_committed(ctx: VerifyContext) -> bool:
    _check_abort(ctx)
    if not ctx.args.get("_done"):
        return False
    before, after = battle_of(ctx.before), battle_of(ctx.after)
    if after is None or not after.active:
        return True
    chooser = before.decision_battler if before and before.decision_battler is not None else 0
    return (
        chooser < len(after.move_targets) and after.move_targets[chooser] == ctx.args["battler"]
    ) or _marker_changed(ctx.before, ctx.after)


def move_replaced(ctx: VerifyContext) -> bool:
    _check_abort(ctx)
    if not ctx.args.get("_done"):
        return False
    party_id, slot = int(ctx.args["party_id"]), int(ctx.args["slot"])
    if not ctx.after.usable or party_id >= len(ctx.after.party):
        return False
    expected = ctx.args.get("expected_moves")
    if expected is None:
        before = tuple(int(move.move_id) for move in ctx.before.party[party_id].moves)
        expected = tuple(
            int(ctx.args["new_move"]) if index == slot else move_id
            for index, move_id in enumerate(before)
        )
    return move_list_matches(ctx.after, party_id, expected)


def verifiers() -> tuple[VerifierSpec, ...]:
    return (
        VerifierSpec(
            R.PC_MOVE,
            move_committed,
            max_frames=900,
            max_wall_s=20.0,
            check_every=4,
            failure_reason=move_commit_failure_reason,
        ),
        VerifierSpec(R.PC_SWITCH, switch_done, max_frames=1500, max_wall_s=30.0, check_every=4),
        VerifierSpec(R.PC_SENDOUT, switch_done, max_frames=1500, max_wall_s=30.0, check_every=4),
        VerifierSpec(R.PC_ITEM, item_used, max_frames=1200, max_wall_s=25.0, check_every=4),
        VerifierSpec(R.PC_BALL, item_used, max_frames=1200, max_wall_s=25.0, check_every=4),
        VerifierSpec(R.PC_ESCAPE, escape_committed, max_frames=600, max_wall_s=15.0, check_every=4),
        VerifierSpec(R.PC_TEXT, text_advanced, max_frames=120, check_every=2),
        VerifierSpec(R.PC_LEVELUP, text_advanced, max_frames=120, check_every=2),
        VerifierSpec(R.PC_LEFT, menu_left, max_frames=300, check_every=4),
        VerifierSpec(R.PC_ANSWER, prompt_answered, max_frames=300, check_every=4),
        VerifierSpec(R.PC_SAFARI, safari_committed, max_frames=1200, check_every=4),
        VerifierSpec(R.PC_TARGET, target_committed, max_frames=1200, check_every=4),
        VerifierSpec(R.PC_MOVE_REPLACED, move_replaced, max_frames=1200, check_every=4),
    )
