"""Postconditions for shop navigation and the money/bag transaction invariant."""

from __future__ import annotations

from pokegent.actions._exec.registry import VerifierSpec, VerifyContext
from pokegent.actions.menu.shop import evidence as E
from pokegent.actions.menu.shop import refs as R


def _phase(snapshot) -> str | None:
    result = E.classify(snapshot)
    return str(result.evidence.get("phase")) if result is not None else None


def moved_on(ctx: VerifyContext) -> bool:
    before = _phase(ctx.before)
    after_result = E.classify(ctx.after)
    # A list choice can cross callback teardown frames where no shop detector owns the screen.
    # Those frames do not prove that the choice was accepted: wait for the next RAM-owned shop
    # phase (quantity/confirm/return-to-shop) instead of completing on a transient absence.
    if before == "buy_list":
        return after_result is not None and before != after_result.evidence.get("phase")
    if before == "sell_list":
        # The bag task installs its quantity record a few frames after the list
        # cursor closes; accept that RAM-owned handoff even if the phase label
        # is still the transient list phase for one snapshot.
        return after_result is not None and (
            after_result.scene_id != R.SELL_LIST
            or (E.selected_item(ctx.after) is not None and E.quantity(ctx.after) is not None)
        )
    return before != _phase(ctx.after) or after_result is None


def transaction(ctx: VerifyContext) -> bool:
    if ctx.before.player is None or ctx.after.player is None:
        return False
    item_id = int(ctx.args["item_ref"])
    quantity = int(ctx.args["target_quantity"])
    price = int(ctx.args["unit_cost"])

    def count(snapshot) -> int:
        return sum(
            item.quantity
            for pocket in snapshot.bag
            for item in pocket.items
            if item.item_id == item_id
        )

    return (
        ctx.after.player.money == ctx.before.player.money - price * quantity
        and count(ctx.after) == count(ctx.before) + quantity
    )


def sell_transaction(ctx: VerifyContext) -> bool:
    if ctx.before.player is None or ctx.after.player is None:
        return False
    item_id, quantity, price = (
        int(ctx.args[k]) for k in ("item_ref", "target_quantity", "unit_price")
    )

    def count(snapshot):
        return sum(
            item.quantity
            for pocket in snapshot.bag
            for item in pocket.items
            if item.item_id == item_id
        )

    return (
        ctx.after.player.money == ctx.before.player.money + price * quantity
        and count(ctx.after) == count(ctx.before) - quantity
    )


def verifier_specs() -> tuple[VerifierSpec, ...]:
    return (
        VerifierSpec("shop.moved_on", moved_on, max_frames=900, max_wall_s=45.0),
        VerifierSpec("shop.transaction", transaction, max_frames=900, max_wall_s=45.0),
        VerifierSpec("shop.sell_transaction", sell_transaction, max_frames=900, max_wall_s=45.0),
    )
