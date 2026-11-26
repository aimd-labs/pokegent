"""``ObjectiveTracker``: evaluate the active objective's success probes.

The tracker is fed snapshots and answers with plain
event data. It never emits into the event log itself: the integrator wires it with a
:class:`ProgressSink` (a callback taking ``(event_type, details)``) or reads the returned
:class:`ProgressEvent` tuples. The three event types are exactly the runner's
``progress.goal_changed``, ``progress.milestone_completed`` and ``progress.stuck``.

Success is never a claim: a probe is a RAM fact (flag, var, badge, current map, tile, party health).
Every probe is evaluated against the current usable frame; an unreadable frame is unknown.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.goals.models import Objective
from pokegent.goals.store import ObjectiveStore
from pokegent.goals.validate import ResolvedObjective, ResolvedProbe

GOAL_CHANGED = "progress.goal_changed"
MILESTONE_COMPLETED = "progress.milestone_completed"
STUCK = "progress.stuck"
CELL = 4
MAX_CELLS = 4096


class ProgressSink(Protocol):
    def __call__(self, event_type: str, details: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True)
class ProgressEvent:
    type: str
    details: Mapping[str, Any]


@dataclass(frozen=True)
class ReviewRequest:
    objective_id: str
    revision: int
    details: Mapping[str, Any]


def _move_max_pp(tables: Any, move_id: int, pp_bonuses: int, slot: int) -> int | None:
    """Return ROM base PP plus PP Ups, or unknown without ROM data."""
    if tables is None or move_id <= 0:
        return None
    try:
        base = int(tables.move(move_id).pp)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return None
    if base <= 0:
        return None
    bonuses = (int(pp_bonuses) >> (2 * slot)) & 3
    return base + (base * 20 * bonuses) // 100


def _party_healed(snapshot: StateSnapshot, tables: Any) -> bool:
    """Check the stronger derived heal postcondition; missing PP data is unknown."""
    members = [mon for mon in snapshot.party if not mon.is_egg]
    if not members:
        return False
    for mon in members:
        if mon.max_hp <= 0 or mon.hp != mon.max_hp or mon.status != 0:
            return False
        for slot, move in enumerate(mon.moves):
            if move.move_id == 0:
                continue
            max_pp = _move_max_pp(tables, move.move_id, mon.pp_bonuses, slot)
            if max_pp is None or move.pp != max_pp:
                return False
    return True


def evaluate_probe(probe: ResolvedProbe, snapshot: StateSnapshot, *, tables: Any = None) -> bool:
    """One probe against one snapshot (False when the snapshot cannot say)."""
    if not snapshot.usable:
        return False
    if probe.kind == "any_of":
        return any(evaluate_probe(child, snapshot, tables=tables) for child in probe.children)
    if probe.kind == "var_at_least":
        if snapshot.vars is None or probe.var is None or probe.value is None:
            return False
        return snapshot.vars.get(probe.var) >= probe.value
    if probe.kind == "flag":
        assert probe.flag is not None and snapshot.flags is not None
        return snapshot.flags.is_set(probe.flag) == probe.expected
    if probe.kind == "var":
        assert probe.var is not None and probe.value is not None and snapshot.vars is not None
        return snapshot.vars.get(probe.var) == probe.value
    player = snapshot.player
    if probe.kind == "map_arrival":
        return player is not None and (player.map_group, player.map_num) == probe.map_key
    if probe.kind == "at_tile":
        return (
            player is not None
            and (player.map_group, player.map_num) == probe.map_key
            and (player.x, player.y) == (probe.x, probe.y)
        )
    if probe.kind == "party_healthy":
        members = snapshot.party
        if not members:
            return False
        return all(
            m.max_hp > 0 and m.hp > 0 and m.hp / m.max_hp >= probe.min_fraction for m in members
        )
    if probe.kind == "party_healed":
        return _party_healed(snapshot, tables)
    if probe.kind == "party_level":
        return bool(snapshot.party) and all(
            m.level >= (probe.min_level or 1) for m in snapshot.party
        )
    if probe.kind == "mon_level":
        member = next(
            (
                m
                for m in snapshot.party
                if (probe.personality is not None and m.personality == probe.personality)
                or (probe.slot is not None and m.slot == probe.slot)
            ),
            None,
        )
        return member is not None and member.level >= (probe.min_level or 1)
    if probe.kind == "party_species":
        return probe.species is not None and any(
            int(m.species) == probe.species for m in snapshot.party
        )
    if probe.kind == "party_knows_move":
        return probe.move_id is not None and any(
            move.move_id == probe.move_id for mon in snapshot.party for move in mon.moves
        )
    if probe.kind == "item":
        held = sum(
            entry.quantity
            for pocket in snapshot.bag
            for entry in pocket.items
            if entry.item_id == probe.item
        )
        return held >= probe.min_quantity
    if probe.kind == "money_at_least":
        return snapshot.player is not None and snapshot.player.money >= (probe.amount or 0)
    if probe.kind == "dex_owned":
        return snapshot.pokedex is not None and probe.species in snapshot.pokedex.owned
    if probe.kind == "dex_count":
        return snapshot.pokedex is not None and snapshot.pokedex.owned_count >= probe.min_count
    return False


@dataclass
class ObjectiveTracker:
    store: ObjectiveStore | None = None
    sink: ProgressSink | None = None
    tables: Any = None
    stuck_after: int = 120
    #: Objective revisions that may arrive while the emulator frame is frozen before one
    #: ``progress.stuck`` is reported; 0 keeps the pre-churn behavior exactly.
    churn_limit: int = 0
    reviews: deque[ReviewRequest] = field(default_factory=lambda: deque(maxlen=64))
    _idle: int = 0
    _stuck_reported: bool = False
    _cells: set[tuple[int, int, int, int]] = field(default_factory=set)
    _flags_seen: int = -1
    _satisfied_count: int = 0
    _revision: int = 0
    _resolved: ResolvedObjective | None = None
    _completed: bool = False
    _churn_frame: int | None = None
    _churn_changes: int = 0
    _churn_reported: bool = False

    def __post_init__(self) -> None:
        if (isinstance(self.stuck_after, bool) or not isinstance(self.stuck_after, int)
                or self.stuck_after < 1):
            raise ValueError("objective_stuck_after must be an integer >= 1")
        if (isinstance(self.churn_limit, bool) or not isinstance(self.churn_limit, int)
                or (self.churn_limit != 0 and self.churn_limit < 2)):
            raise ValueError("objective_churn_limit must be 0 (off) or an integer >= 2")

    # -- the objective ----------------------------------------------------------------------------

    @property
    def objective(self) -> Objective | None:
        return self._resolved.objective if self._resolved else None

    @property
    def completed(self) -> bool:
        """Every success probe of the active objective is true (never for ``hold``)."""
        return self._completed

    @property
    def done(self) -> bool:
        return self._completed

    def set_objective(self, resolved: ResolvedObjective | None) -> list[ProgressEvent]:
        """Make ``resolved`` the tracked objective (tests, or a caller that holds no store)."""
        return self._switch(resolved)

    def _switch(self, resolved: ResolvedObjective | None) -> list[ProgressEvent]:
        old = self._resolved
        new_rev = resolved.objective.revision if resolved else 0
        if (new_rev == self._revision and (old is None) == (resolved is None)
                and (old is None or old.objective.objective_id == resolved.objective.objective_id)):
            return []
        if self.churn_limit and self._churn_frame is not None:
            self._churn_changes = min((1 << 31) - 1, self._churn_changes + 1)
        self._resolved = resolved
        self._revision = new_rev
        self._completed = False
        self._idle, self._flags_seen, self._satisfied_count = 0, -1, 0
        self._stuck_reported = False
        self._cells.clear()
        details = {
            "from_objective": old.objective.objective_id if old else None,
            "from_revision": old.objective.revision if old else None,
            "objective_id": resolved.objective.objective_id if resolved else None,
            "revision": new_rev,
            "kind": resolved.objective.kind if resolved else "hold",
            "set_by": resolved.objective.set_by if resolved else None,
        }
        return [self._event(GOAL_CHANGED, details)]

    # -- feeding ----------------------------------------------------------------------------------

    def update(self, snapshot: StateSnapshot) -> tuple[ProgressEvent, ...]:
        """Evaluate the active objective on a fresh snapshot. Pure with respect to the game."""
        events: list[ProgressEvent] = []
        if self.store is not None:
            self.store.current()
            events += self._switch(self.store.resolved())
        if not snapshot.usable:
            # A stale completion is not evidence on an unusable frame.  The next usable
            # snapshot must recompute the probes before the session can stop or advance.
            self._completed = False
            if self.churn_limit:
                self._reset_churn()
            return tuple(events)
        churn_fired = (
            self._note_churn_frame(snapshot.emulator_frame) if self.churn_limit else False
        )
        resolved = self._resolved
        if resolved is None:
            return tuple(events)

        probes = resolved.probes
        satisfied = 0
        for probe in probes:
            ok = evaluate_probe(probe, snapshot, tables=self.tables)
            if ok:
                satisfied += 1
        was_completed = self._completed
        self._completed = bool(probes) and satisfied == len(probes)
        if self._completed and not was_completed:
            events.append(
                self._event(
                    MILESTONE_COMPLETED,
                    self._details(resolved, satisfied, len(probes), partial=False),
                )
            )
        if churn_fired and not self._churn_reported:
            details = {"objective_id": resolved.objective.objective_id,
                       "revision": resolved.objective.revision,
                       "reason": "objective_churn",
                       "churn_changes": self._churn_changes,
                       "emulator_frame": self._churn_frame}
            events.append(self._event(STUCK, details))
            self._churn_reported = True
            self.note_stuck(details)
        if not self._completed and resolved.objective.kind != "hold":
            progress = self._note_progress(snapshot) or satisfied > self._satisfied_count
            self._satisfied_count = max(self._satisfied_count, satisfied)
            if progress:
                self._idle, self._stuck_reported = 0, False
            else:
                self._idle += 1
                if self._idle >= self.stuck_after and not self._stuck_reported:
                    details = {"objective_id": resolved.objective.objective_id,
                               "revision": resolved.objective.revision, "idle_updates": self._idle}
                    events.append(self._event(STUCK, details))
                    self.note_stuck({"idle_updates": self._idle})
        else:
            self._idle = 0
        return tuple(events)

    def _reset_churn(self) -> None:
        self._churn_frame, self._churn_changes, self._churn_reported = None, 0, False

    def _note_churn_frame(self, frame: int | None) -> bool:
        """Track goal churn against real emulator frames; True when the limit is reached."""
        if frame is None:  # an unknown frame cannot prove a freeze
            self._reset_churn()
            return False
        if self._churn_frame != frame:  # first frame or real advance: a new incident
            self._churn_frame, self._churn_changes, self._churn_reported = frame, 0, False
            return False
        return not self._churn_reported and self._churn_changes >= self.churn_limit

    def _note_progress(self, snapshot: StateSnapshot) -> bool:
        progress = False
        player = snapshot.player
        if player is not None:
            cell = (player.map_group, player.map_num, player.x // CELL, player.y // CELL)
            if cell not in self._cells:
                if len(self._cells) >= MAX_CELLS:
                    self._cells.clear()
                self._cells.add(cell)
                progress = True
        flags = len(snapshot.flags.set_ids) if snapshot.flags else 0
        if flags != self._flags_seen:
            progress = progress or self._flags_seen >= 0
            self._flags_seen = flags
        return progress

    def note_stuck(self, details: Mapping[str, Any]) -> None:
        obj = self.objective
        if obj is None or obj.kind == "hold" or self._completed or self._stuck_reported:
            return
        self._stuck_reported = True
        if "stuck" in obj.next_review_on:
            self.reviews.append(ReviewRequest(obj.objective_id, obj.revision, dict(details)))

    @staticmethod
    def _details(
        resolved: ResolvedObjective, satisfied: int, total: int, *, partial: bool
    ) -> dict[str, Any]:
        obj = resolved.objective
        return {
            "objective_id": obj.objective_id,
            "revision": obj.revision,
            "kind": obj.kind,
            "satisfied": satisfied,
            "probes": total,
            "completed": not partial,
        }

    def _event(self, type_: str, details: Mapping[str, Any]) -> ProgressEvent:
        event = ProgressEvent(type_, dict(details))
        if self.sink is not None:
            self.sink(type_, event.details)
        return event


def event_sink_adapter(events: Any) -> Callable[[str, Mapping[str, Any]], None]:
    """Adapt a runner ``EventSink`` (``emit(type, *, details=...)``) to a :class:`ProgressSink`."""

    def sink(event_type: str, details: Mapping[str, Any]) -> None:
        events.emit(event_type, details=dict(details), source="objective")

    return sink


__all__ = [
    "GOAL_CHANGED",
    "MILESTONE_COMPLETED",
    "ObjectiveTracker",
    "ProgressEvent",
    "ProgressSink",
    "evaluate_probe",
    "event_sink_adapter",
]
