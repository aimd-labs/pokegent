"""Attempt ledger: retain bounded outcome history as an explicit option hint.

Key: ``(target id, map, capability signature)``. A failed attempt counts; three failures without
progress is reported on the still-selectable option;
a change of the key makes it available again by itself: a new map is a new key, and the capability
signature changes with badges, party species, field moves among the party's moves, and any event
flag. That is the "after a flag/team/move/map change allow it again" rule without a lifetime ban.

A failure that still made progress (the walk got closer) does not count and clears the counter. The
ledger is deterministic (no clock), bounded, and serialisable (``to_dict`` / ``from_dict``) so a
supervisor can persist it across restarts.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from pokegent.games.gen3.firered.reader.schemas import StateSnapshot

#: gen-3 move ids of the field moves (include/constants/moves.h); used only to detect a change of
#: what the party can do on the map
FIELD_MOVES = {
    15: "cut",
    19: "fly",
    57: "surf",
    70: "strength",
    127: "waterfall",
    148: "flash",
    249: "rock_smash",
    291: "dive",
}

LedgerKey = tuple[str, int, int, str]
SignReadKey = tuple[str, int, int, str | None]
FieldMoveDeclineKey = tuple[str, int, int, str | None]
PendingObstacle = tuple[str, int, int, int, int | None]


def capability_signature(snapshot: StateSnapshot) -> str:
    """A short digest that changes when the player's options change (badge, team, move, flag)."""
    badges = snapshot.player.badges if snapshot.player else ()
    species = sorted(m.species for m in snapshot.party)
    moves = sorted(
        {
            FIELD_MOVES[mv.move_id]
            for m in snapshot.party
            for mv in m.moves
            if mv.move_id in FIELD_MOVES
        }
    )
    flags = snapshot.flags.set_ids if snapshot.flags else ()
    h = hashlib.sha1(usedforsecurity=False)
    h.update(repr((tuple(badges), species, moves)).encode("ascii"))
    h.update(bytes(len(flags).to_bytes(4, "little")))
    h.update(b",".join(str(f).encode("ascii") for f in flags))
    return h.hexdigest()[:16]


@dataclass
class Attempt:
    failures: int = 0
    last_reason: str = ""
    total: int = 0


@dataclass
class AttemptLedger:
    limit: int = 3
    max_entries: int = 512
    entries: dict[LedgerKey, Attempt] = field(default_factory=dict)
    #: Successfully read signs, scoped to the objective and map.
    sign_reads: set[SignReadKey] = field(default_factory=set)
    #: Field moves declined at a prompt, scoped to the objective and map.
    field_move_declines: set[FieldMoveDeclineKey] = field(default_factory=set)
    #: Scripted blockers whose battle has started but whose durable field state must be checked
    #: after battle cleanup returns control to ``field.free``.
    pending_obstacles: set[PendingObstacle] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("limit must be >= 1")

    @staticmethod
    def key(target_id: str, map_key: tuple[int, int], signature: str) -> LedgerKey:
        return (target_id, map_key[0], map_key[1], signature)

    def record_failure(self, key: LedgerKey, reason: str, *, progressed: bool = False) -> int:
        """Count a failed attempt; returns the failures now counted for the key."""
        entry = self.entries.pop(key, None) or Attempt()
        entry.total += 1
        entry.last_reason = reason
        entry.failures = 0 if progressed else entry.failures + 1
        self.entries[key] = entry  # most recently touched last
        while len(self.entries) > self.max_entries:
            self.entries.pop(next(iter(self.entries)))
        return entry.failures

    def record_success(self, key: LedgerKey) -> None:
        self.entries.pop(key, None)

    def record_sign_read(
        self, option_id: str, map_key: tuple[int, int], goal_id: str | None
    ) -> None:
        self.sign_reads.add((option_id, map_key[0], map_key[1], goal_id))

    def sign_was_read(self, option_id: str, map_key: tuple[int, int], goal_id: str | None) -> bool:
        return (option_id, map_key[0], map_key[1], goal_id) in self.sign_reads

    def record_field_move_decline(
        self, option_id: str, map_key: tuple[int, int], goal_id: str | None
    ) -> None:
        self.field_move_declines.add((option_id, map_key[0], map_key[1], goal_id))

    def field_move_was_declined(
        self, option_id: str, map_key: tuple[int, int], goal_id: str | None
    ) -> bool:
        return (option_id, map_key[0], map_key[1], goal_id) in self.field_move_declines

    def defer_obstacle_check(
        self,
        option_id: str,
        map_key: tuple[int, int],
        local_id: int,
        hide_flag: int | None,
    ) -> None:
        self.pending_obstacles.add((option_id, map_key[0], map_key[1], local_id, hide_flag))

    def settle_obstacle_checks(self, snapshot: StateSnapshot) -> None:
        """Confirm deferred blockers only after the battle/script returned to the field."""
        state = snapshot.map
        if state is None:
            return
        here = (state.identity.group, state.identity.number)
        for pending in tuple(self.pending_obstacles):
            _option_id, group, number, local_id, hide_flag = pending
            if here != (group, number):
                continue
            flag_set = bool(
                hide_flag is not None
                and snapshot.flags is not None
                and snapshot.flags.is_set(hide_flag)
            )
            obj = state.object_by_local_id(local_id)
            removed = obj is None or obj.hidden_flag_set is True or not obj.blocks_tile
            if flag_set or removed:
                self.pending_obstacles.discard(pending)

    def failures(self, key: LedgerKey) -> int:
        entry = self.entries.get(key)
        return entry.failures if entry else 0

    def hidden(self, key: LedgerKey) -> bool:
        return self.failures(key) >= self.limit

    def reason(self, key: LedgerKey) -> str:
        entry = self.entries.get(key)
        return entry.last_reason if entry else ""

    def reset(self) -> None:
        self.entries.clear()
        self.field_move_declines.clear()
        self.pending_obstacles.clear()

    # -- persistence --------------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "limit": self.limit,
            "entries": [
                {"key": list(k), "failures": a.failures, "reason": a.last_reason, "total": a.total}
                for k, a in self.entries.items()
            ],
            "sign_reads": [list(k) for k in sorted(self.sign_reads, key=repr)],
            "field_move_declines": [list(k) for k in sorted(self.field_move_declines, key=repr)],
            "pending_obstacles": [list(k) for k in sorted(self.pending_obstacles, key=repr)],
        }

    @classmethod
    def from_dict(cls, data: dict) -> AttemptLedger:
        ledger = cls(limit=int(data.get("limit", 3)))
        for item in data.get("entries", []):
            t, g, n, sig = item["key"]
            ledger.entries[(str(t), int(g), int(n), str(sig))] = Attempt(
                int(item["failures"]), str(item.get("reason", "")), int(item.get("total", 0))
            )
        for item in data.get("sign_reads", []):
            if len(item) == 4:
                sign, group, number, goal = item
                ledger.sign_reads.add(
                    (str(sign), int(group), int(number), None if goal is None else str(goal))
                )
        for item in data.get("field_move_declines", []):
            if len(item) == 4:
                option, group, number, goal = item
                ledger.field_move_declines.add(
                    (str(option), int(group), int(number), None if goal is None else str(goal))
                )
        for item in data.get("pending_obstacles", []):
            if len(item) == 5:
                option, group, number, local_id, hide_flag = item
                ledger.pending_obstacles.add(
                    (
                        str(option),
                        int(group),
                        int(number),
                        int(local_id),
                        None if hide_flag is None else int(hide_flag),
                    )
                )
        return ledger
