"""The one learn/forget flow shared by level-up, TM and HM teaching.

The game uses the same four-row summary picker for all three callers.  This module owns the
stable option model, the bounded summary walk, prompt confirmation, and the single RAM
postcondition: the complete move list is the expected list.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass
from typing import Any

from pokegent.actions._exec.menu_executor import select_menu_entry
from pokegent.actions._exec.models import Advance, Tap, WaitUntil
from pokegent.actions.menu.system_flow.moves import HM_MOVE_IDS

SUMMARY_READY_FRAMES = 240
SUMMARY_SETTLE_FRAMES = 600
SUMMARY_INPUT_WAIT = 30
SUMMARY_MOVE_ROWS = 4
HOLD = 4
RELEASE = 4


@dataclass(frozen=True)
class MoveFact:
    move_id: int
    name: str
    type: str | None = None
    power: int | None = None
    pp: int | None = None
    accuracy: int | None = None

    def as_fact(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "move_id": self.move_id,
                "name": self.name,
                "type": self.type,
                "power": self.power,
                "pp": self.pp,
                "accuracy": self.accuracy,
            }.items()
            if value is not None
        }


@dataclass(frozen=True)
class MoveChoice:
    """A replacement option; ``slot=None`` is the stop-learning choice."""

    id: str
    slot: int | None
    old: MoveFact | None
    new: MoveFact
    enabled: bool = True


def build_move_choices(
    moves: Sequence[Any],
    new_move: MoveFact,
    *,
    hm_ids: frozenset[int] = HM_MOVE_IDS,
) -> tuple[MoveChoice, ...]:
    """Build replacement choices from the learn prompt's RAM move list.

    HMs remain visible as disabled choices so the reason is explainable, but can never be
    selected.  The final entry is always the safe stop-learning choice.
    """
    choices = []
    for slot, move in enumerate(moves):
        move_id = int(getattr(move, "move_id", move[0] if isinstance(move, tuple) else 0))
        if not move_id:
            continue
        old = move if isinstance(move, MoveFact) else MoveFact(move_id, f"move {move_id}")
        choices.append(
            MoveChoice(
                id=f"replace:{slot}",
                slot=slot,
                old=old,
                new=new_move,
                enabled=move_id not in hm_ids,
            )
        )
    choices.append(MoveChoice("stop_learning", None, None, new_move))
    return tuple(choices)


def move_list_matches(snapshot: Any, party_id: int, expected: Sequence[int]) -> bool:
    """Return true only when RAM exposes exactly the expected party move list."""
    if not getattr(snapshot, "usable", False) or not 0 <= party_id < len(snapshot.party):
        return False
    actual = tuple(int(move.move_id) for move in snapshot.party[party_id].moves)
    return actual == tuple(int(move_id) for move_id in expected)


def confirm_learning(
    observation: Any,
    decode: Callable[[Any], Any],
    *,
    tap: Callable[[str], Tap],
) -> Generator:
    """Confirm YES through the generic cursor executor and its RAM readback."""
    return (
        yield from select_menu_entry(
            observation, "yes", decode, tap=tap, max_moves=1, confirm_retries=2
        )
    )


def replace_move(ctx: Any) -> Generator:
    """Walk the shared summary picker and verify the complete expected move list.

    Callers may provide ``expected_moves``; otherwise it is derived from the fresh RAM state at
    entry.  The latter keeps the legacy macro contract useful to direct callers and unit tests.
    """
    slot = int(ctx.args["slot"])
    party_id = int(ctx.args["party_id"])
    old_move = int(ctx.args.get("old_move", 0))
    new_move = int(ctx.args.get("new_move", 0))
    if not new_move:
        battle = getattr(ctx.snapshot, "battle", None)
        new_move = int(getattr(battle, "move_to_learn", 0))
    expected = tuple(ctx.args.get("expected_moves", ()))
    before = ()
    if 0 <= party_id < len(getattr(ctx.snapshot, "party", ())):
        member = ctx.snapshot.party[party_id]
        before = tuple(int(move.move_id) for move in member.moves)
    if not expected and before:
        expected = tuple(
            new_move if index == slot else move_id for index, move_id in enumerate(before)
        )
    ctx.args["expected_moves"] = expected

    yield WaitUntil(
        lambda current: bool(
            current.snapshot.ui
            and current.snapshot.ui.main
            and current.snapshot.ui.main.callback2
            and current.snapshot.ui.main.callback2.name == "obs_cb2_pokemon_summary_screen"
        ),
        max_frames=SUMMARY_READY_FRAMES,
    )
    yield Advance(SUMMARY_SETTLE_FRAMES)
    for _ in range(slot):
        yield Tap("down", hold_frames=HOLD, wait_frames=SUMMARY_INPUT_WAIT)
    yield Tap("a", hold_frames=HOLD, wait_frames=RELEASE)
    for _ in range(SUMMARY_MOVE_ROWS - slot):
        yield Tap("down", hold_frames=HOLD, wait_frames=SUMMARY_INPUT_WAIT)
    yield Tap("a", hold_frames=HOLD, wait_frames=RELEASE)

    def committed(current: Any) -> bool:
        snapshot = current.snapshot
        if expected and move_list_matches(snapshot, party_id, expected):
            ctx.args["_done"] = True
            return True
        if (
            not expected
            and getattr(snapshot, "usable", False)
            and 0 <= party_id < len(snapshot.party)
        ):
            moves = snapshot.party[party_id].moves
            if 0 <= slot < len(moves) and int(moves[slot].move_id) != old_move:
                ctx.args["_done"] = True
                return True
        callback = snapshot.ui.main.callback2 if snapshot.ui and snapshot.ui.main else None
        if old_move and callback and callback.name != "obs_cb2_pokemon_summary_screen":
            raise RuntimeError(
                f"forget_move slot {slot} completed without changing move {old_move}"
            )
        return False

    yield WaitUntil(committed, max_frames=900)
