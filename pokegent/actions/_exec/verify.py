"""Generic expected-state predicates evaluated only from RAM-derived snapshots/models."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict

from pokegent.actions._exec.contracts import ActionContract, ExpectedState, MenuModel
from pokegent.actions._exec.registry import VerifierSpec, VerifyContext
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot

POST_EXPECTED = "adapter.expected"


class PredicateResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    matched: bool
    reason: Literal[
        "matched",
        "menu_still_open",
        "menu_not_open",
        "menu_surface_mismatch",
        "cursor_key_mismatch",
        "item_count_unchanged",
        "item_delta_mismatch",
        "party_member_missing",
        "party_move_set_unchanged",
        "move_presence_mismatch",
        "party_order_unchanged",
        "held_item_unchanged",
        "bag_count_mismatch",
        "map_unreadable",
        "map_unchanged",
        "map_destination_mismatch",
    ]


class VerifyResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    matched: bool
    reason: str
    predicates: tuple[PredicateResult, ...]


class ExpectedStateMismatch(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.failure_reason = reason
        super().__init__(reason)


def _item_count(snapshot: StateSnapshot, item_id: int) -> int:
    return sum(
        item.quantity for pocket in snapshot.bag for item in pocket.items if item.item_id == item_id
    )


def _moves(snapshot: StateSnapshot, personality: int | None) -> tuple[int, ...] | None:
    members = (
        snapshot.party
        if personality is None
        else tuple(mon for mon in snapshot.party if mon.personality == personality)
    )
    if not members:
        return None
    return tuple(move.move_id for mon in members for move in mon.moves if move.move_id)


def _map(snapshot: StateSnapshot) -> tuple[int, int] | None:
    if snapshot.map is not None:
        return snapshot.map.identity.group, snapshot.map.identity.number
    if snapshot.player is not None:
        return snapshot.player.map_group, snapshot.player.map_num
    return None


def evaluate_predicate(
    predicate: ExpectedState,
    before: StateSnapshot,
    after: StateSnapshot,
    *,
    before_menu: MenuModel | None = None,
    after_menu: MenuModel | None = None,
) -> PredicateResult:
    """Evaluate one declaration. No wall clock, text matching, or emulator access is used."""
    kind = predicate.kind
    if kind == "menu_closed":
        was_open = before_menu is not None and (
            predicate.surface is None or before_menu.surface == predicate.surface
        )
        if not was_open:
            return PredicateResult(kind=kind, matched=False, reason="menu_not_open")
        open_same = after_menu is not None and (
            predicate.surface is None or after_menu.surface == predicate.surface
        )
        return PredicateResult(
            kind=kind,
            matched=not open_same,
            reason="menu_still_open" if open_same else "matched",
        )
    if kind == "menu_open_cursor":
        if after_menu is None:
            return PredicateResult(kind=kind, matched=False, reason="menu_not_open")
        if predicate.surface is not None and after_menu.surface != predicate.surface:
            return PredicateResult(kind=kind, matched=False, reason="menu_surface_mismatch")
        matched = after_menu.selected_key == predicate.stable_key
        return PredicateResult(
            kind=kind,
            matched=matched,
            reason="matched" if matched else "cursor_key_mismatch",
        )
    if kind == "item_count_changed":
        assert predicate.item_id is not None
        delta = _item_count(after, predicate.item_id) - _item_count(before, predicate.item_id)
        if predicate.delta is not None:
            matched = delta == predicate.delta
            reason = "matched" if matched else "item_delta_mismatch"
        else:
            matched = delta != 0
            reason = "matched" if matched else "item_count_unchanged"
        return PredicateResult(kind=kind, matched=matched, reason=reason)
    if kind == "party_move_set_changed":
        old = _moves(before, predicate.personality)
        new = _moves(after, predicate.personality)
        if old is None or new is None:
            return PredicateResult(kind=kind, matched=False, reason="party_member_missing")
        if predicate.move_id is not None:
            matched = predicate.move_id not in old and predicate.move_id in new
            reason = "matched" if matched else "move_presence_mismatch"
        else:
            matched = old != new
            reason = "matched" if matched else "party_move_set_unchanged"
        return PredicateResult(kind=kind, matched=matched, reason=reason)
    if kind == "party_order_changed":
        old = tuple(mon.personality for mon in before.party)
        new = tuple(mon.personality for mon in after.party)
        matched = len(old) == len(new) and sorted(old) == sorted(new) and old != new
        return PredicateResult(
            kind=kind,
            matched=matched,
            reason="matched" if matched else "party_order_unchanged",
        )
    if kind == "held_item_changed":
        assert predicate.personality is not None
        old = next(
            (mon.held_item for mon in before.party if mon.personality == predicate.personality),
            None,
        )
        new = next(
            (mon.held_item for mon in after.party if mon.personality == predicate.personality),
            None,
        )
        matched = old is not None and new is not None and old != new
        if predicate.held_item_id is not None:
            matched = matched and new == predicate.held_item_id
        if predicate.item_id is not None:
            matched = matched and old == predicate.item_id and new == 0
        if predicate.bag_item_id is not None:
            assert predicate.bag_delta is not None
            delta = _item_count(after, predicate.bag_item_id) - _item_count(
                before, predicate.bag_item_id
            )
            matched = matched and delta == predicate.bag_delta
            if not matched and delta != predicate.bag_delta:
                return PredicateResult(kind=kind, matched=False, reason="bag_count_mismatch")
        return PredicateResult(
            kind=kind,
            matched=matched,
            reason="matched" if matched else "held_item_unchanged",
        )
    if kind == "map_changed":
        old, new = _map(before), _map(after)
        if old is None or new is None:
            return PredicateResult(kind=kind, matched=False, reason="map_unreadable")
        if old == new:
            return PredicateResult(kind=kind, matched=False, reason="map_unchanged")
        destination = (
            (predicate.map_group, predicate.map_number)
            if predicate.map_group is not None and predicate.map_number is not None
            else None
        )
        matched = destination is None or new == destination
        return PredicateResult(
            kind=kind,
            matched=matched,
            reason="matched" if matched else "map_destination_mismatch",
        )
    raise AssertionError(f"unknown expected-state predicate {kind!r}")


def evaluate_contract(
    contract: ActionContract,
    before: StateSnapshot,
    after: StateSnapshot,
    *,
    before_menu: MenuModel | None = None,
    after_menu: MenuModel | None = None,
) -> VerifyResult:
    results = tuple(
        evaluate_predicate(
            predicate,
            before,
            after,
            before_menu=before_menu,
            after_menu=after_menu,
        )
        for predicate in contract.expected
    )
    failed = next((result for result in results if not result.matched), None)
    return VerifyResult(
        matched=failed is None,
        reason="matched" if failed is None else failed.reason,
        predicates=results,
    )


def verifier_spec(
    menu_decoder: Callable[[StateSnapshot], MenuModel | None] | None = None,
    *,
    max_frames: int = 120,
    max_wall_s: float = 5.0,
) -> VerifierSpec:
    """Build the opt-in generic verifier; legacy actions keep their existing verifier refs."""

    def expected(ctx: VerifyContext) -> bool:
        contract = ctx.option.action_contract
        if contract is None or not contract.expected:
            raise ExpectedStateMismatch("expected_state_missing")
        result = evaluate_contract(
            contract,
            ctx.before,
            ctx.after,
            before_menu=menu_decoder(ctx.before) if menu_decoder is not None else None,
            after_menu=menu_decoder(ctx.after) if menu_decoder is not None else None,
        )
        if not result.matched:
            raise ExpectedStateMismatch(result.reason)
        return True

    return VerifierSpec(
        POST_EXPECTED,
        expected,
        max_frames=max_frames,
        max_wall_s=max_wall_s,
    )
