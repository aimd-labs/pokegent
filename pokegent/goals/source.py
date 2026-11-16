"""The goal as the runner sees it: ``GoalSource``, ``ObjectiveSession``, the scripted queue.

M3.2 (contracts v1.3). The objective file (``store.py``) and the tracker (``tracker.py``) existed;
this module puts them into the loop without making the loop wait for anyone.

* :class:`GoalView` / :class:`GoalSource`: what the ``Perceiver`` asks, per perception, for the goal
  id and the English one-liner the model sees. :class:`FixedGoal` adapts the old ``goal_id=``
  string.
* :class:`ObjectiveSession`: the store and tracker behind ONE consistent view per runner iteration.
  ``refresh()`` reads the file without raising and hands out typed warnings; ``observe(snapshot)``
  evaluates success probes against the current frame. A corrupt or stale file keeps the last valid
  objective (the store's rule) and becomes a warning, never a crash.
* :class:`ScriptedObjectiveSource`: a list of objectives advanced when the previous one's probes
  succeed, written through the same :class:`~.store.ObjectiveStore` (history included), so a
  scripted league-run plan runs without agent. File format: ``docs/objective-file.md`` section 9.

The source never makes a strategic decision: the operator-written plan queue advances only after the
current objective is satisfied at a safe point.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.goals.models import Objective
from pokegent.goals.store import ObjectiveStore, Proposal, StoreWarning
from pokegent.goals.tracker import (
    MILESTONE_COMPLETED,
    ObjectiveTracker,
    ProgressEvent,
    ProgressSink,
)
from pokegent.goals.validate import Reason, ResolvedObjective, validate

#: bound of the objective line the model sees (``context["objective"]``)
SUMMARY_MAX_CHARS = 400
PLAN_STATE_SCHEMA = "objective-plan-state/1"
OBJECTIVE_REJECTION_CODES = frozenset(
    {"schema_invalid", "unreadable_file", "file_too_large", "stale_revision", "source_priority"}
)
_NON_REJECTION_CODES = frozenset(
    {"forbid_dropped", "expired", "history_unwritable", "file_missing", "session_error"}
)


def is_objective_rejection(warning: StoreWarning) -> bool:
    """Whether a warning represents an objective rejection visible to the operator."""
    if warning.code in _NON_REJECTION_CODES:
        return False
    return bool(warning.reasons) or warning.code in OBJECTIVE_REJECTION_CODES


def _plan_state_error(path: Path) -> PlanError:
    return PlanError(
        f"plan-state {path} has missing or mismatched plan fingerprint; "
        "delete or move it to start this plan fresh"
    )


def bound_summary(text: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    """Cut ``text`` at a word boundary to ``limit`` characters (``...`` marks the cut)."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: max(1, limit - 3)]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:.") + "..."


@dataclass(frozen=True)
class GoalView:
    """The goal at one instant: the id a frame carries, the line the model sees, the revision."""

    goal_id: str
    summary: str = ""
    revision: int = 0
    forbid: tuple[str, ...] = ()
    kind: str | None = None
    target_item: int | None = None
    target_quantity: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "summary", bound_summary(self.summary))


@runtime_checkable
class GoalSource(Protocol):
    """``Perceiver`` asks this once per perception; ``None`` means goal-less."""

    def current(self) -> GoalView | None: ...


class FixedGoal:
    """Adapter for the pre-v1.3 call sites: a fixed ``goal_id`` string (no summary)."""

    def __init__(self, goal_id: str | None, summary: str = "", revision: int = 0) -> None:
        self.goal_id = goal_id
        self.summary = summary
        self.revision = revision

    def current(self) -> GoalView | None:
        if not self.goal_id:
            return None
        return GoalView(self.goal_id, self.summary, self.revision)


@dataclass(frozen=True)
class Refreshed:
    warnings: tuple[StoreWarning, ...] = ()
    events: tuple[ProgressEvent, ...] = ()
    changed: bool = False


@dataclass
class _Held:
    resolved: ResolvedObjective | None = None
    revision: int = 0


class ObjectiveSession:
    """Store + tracker + script behind one per-iteration goal. Never raises, never blocks."""

    def __init__(
        self,
        store: ObjectiveStore,
        *,
        tracker: ObjectiveTracker | None = None,
        script: ScriptedObjectiveSource | None = None,
        stuck_after: int = 120,
        sink: ProgressSink | None = None,
    ) -> None:
        self.store = store
        # The tracker is driven through ``set_objective`` (not handed the store): the goal changes
        # at ``refresh()``, before the next perception, and not at some later ``update``.
        self.tracker = tracker if tracker is not None else ObjectiveTracker()
        if sink is not None:
            self.tracker.sink = sink
        self.script = script
        self._held = _Held()
        self._view: GoalView | None = None
        self._warned_seen = store.warning_count
        self._pending_script_completions: list[tuple[str, int]] = []
        self._started = False
        self._hold_waiting_reason: str | None = None
        self._rejection: str | None = None
        self.revisions_seen: list[int] = []

    # -- the GoalSource the Perceiver reads ----------------------------------------------

    def current(self) -> GoalView | None:
        return self._view

    def objective_source(self) -> Callable[[str | None], ResolvedObjective | None]:
        """The field builder's objective source: the resolved objective of THIS iteration's view."""

        def source(goal_id: str | None) -> ResolvedObjective | None:
            held = self._held.resolved
            if goal_id is None or held is None or held.objective.objective_id != goal_id:
                return None
            return held

        return source

    # -- state the runner asks about -----------------------------------------------------

    @property
    def objective(self) -> Objective | None:
        held = self._held.resolved
        return held.objective if held else None

    @property
    def done(self) -> bool:
        """The active objective is achieved or ended early."""
        obj = self.objective
        return obj is not None and self.tracker.done

    @property
    def hold_waiting_reason(self) -> str | None:
        """Why an unmet RAM-only hold has no useful input to offer."""
        return self._hold_waiting_reason

    @property
    def completed(self) -> bool:
        return self.tracker.completed

    def attach_sink(self, sink: ProgressSink) -> None:
        if self.tracker.sink is None:
            self.tracker.sink = sink

    # -- start of an iteration -----------------------------------------------------------

    def refresh(self) -> Refreshed:
        """Read the file (cheap: an ``os.stat`` unless it changed), switch the tracker on a new
        revision, and fix the view for this iteration. Never raises."""
        events: list[ProgressEvent] = []
        try:
            if not self._started:
                self._started = True
                if self.script is not None:
                    self.script.start()
            self.store.current()  # polls; a bad file becomes a warning and the last objective stays
            resolved = self.store.resolved()
            events = self._switch(resolved)
        except Exception as exc:  # the loop must survive anything the file system does
            warning = StoreWarning(
                "session_error", f"{type(exc).__name__}: {exc}"[:300], "", self._held.revision
            )
            return Refreshed((warning,), ())
        warnings = self.store.warnings_since(self._warned_seen)
        self._warned_seen = self.store.warning_count
        rejected = [warning for warning in warnings if is_objective_rejection(warning)]
        if rejected and self._held.resolved is None:
            warning = rejected[-1]
            field = warning.reasons[0].field if warning.reasons else "objective"
            self._rejection = f"objective rejected: {warning.code} {field}".rstrip()
        elif resolved is not None:
            self._rejection = None
        self._view = self._make_view()
        return Refreshed(warnings, tuple(events), changed=bool(events))

    def _switch(self, resolved: ResolvedObjective | None) -> list[ProgressEvent]:
        held = self._held
        new_rev = resolved.objective.revision if resolved else 0
        same = (held.resolved is None) == (resolved is None) and held.revision == new_rev
        if same:
            return []
        events = self.tracker.set_objective(resolved)
        self._held = _Held(resolved, new_rev)
        self._hold_waiting_reason = None
        if new_rev:
            self.revisions_seen.append(new_rev)
        return events

    def _make_view(self) -> GoalView | None:
        resolved = self._held.resolved
        if resolved is None:
            return GoalView("", self._rejection) if self._rejection else None
        obj = resolved.objective
        summary = obj.summary()
        if self.done:
            summary = f"Objective achieved, waiting for the next one. Was: {summary}"
        return GoalView(
            obj.goal_id,
            summary,
            obj.revision,
            obj.forbid,
            kind=obj.kind,
            target_item=resolved.target.item,
            target_quantity=resolved.target.quantity,
        )

    # -- after a perception --------------------------------------------------------------

    def observe(
        self, snapshot: StateSnapshot, *, safe_boundary: bool = False
    ) -> tuple[ProgressEvent, ...]:
        """Evaluate the objective's success probes on a fresh snapshot."""
        try:
            events = self.tracker.update(snapshot)
        except Exception:  # a tracker bug must not stop the game; the file stays the truth
            return ()
        if self.script is not None and not self.tracker.completed:
            # A later fresh frame (including an unusable one) invalidates a pending handoff;
            # completion must be observed again before the script can take ownership.
            self._pending_script_completions.clear()
        self._hold_waiting_reason = None
        obj = self.objective
        if obj is not None and obj.kind == "hold" and not self.tracker.done:
            resolved = self._held.resolved
            probes = resolved.probes if resolved is not None else ()
            if probes and all(probe.kind in ("flag", "var") for probe in probes):
                self._hold_waiting_reason = (
                    "hold objective has only unmet RAM probes; no input can satisfy "
                    + ", ".join(probe.label() for probe in probes)
                )
        if self.script is not None:
            for event in events:
                if event.type == MILESTONE_COMPLETED and event.details.get("completed"):
                    completion = (
                        str(event.details.get("objective_id")),
                        int(event.details.get("revision", 0)),
                    )
                    if completion not in self._pending_script_completions:
                        self._pending_script_completions.append(completion)
            if safe_boundary:
                pending = self._pending_script_completions
                self._pending_script_completions = []
                for objective_id, revision in pending:
                    self.script.completed(objective_id, revision)
        if safe_boundary and snapshot.usable and self.tracker.completed:
            objective = self.objective
            if objective is not None:
                self.store.commit_completion(
                    objective_id=objective.objective_id,
                    expected_revision=objective.revision,
                )
        return events

    def superseded(self) -> bool:
        """A newer valid revision is in the file than the one this iteration runs under (the
        script just advanced, or agent wrote one): the runner re-perceives instead of acting."""
        try:
            current = self.store.current()  # None when expired: not newer than what we hold
            return current is not None and current.revision > self._held.revision
        except Exception:
            return False

    # -- inspection ----------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """The ``objective`` block of ``summary.json``."""
        obj = self.objective
        doc: dict[str, Any] = {
            "file": str(self.store.path),
            "active": (
                None
                if obj is None
                else {
                    "objective_id": obj.objective_id,
                    "revision": obj.revision,
                    "kind": obj.kind,
                    "set_by": obj.set_by,
                    **(
                        {"navigation_reason": self._held.resolved.navigation_reason}
                        if self._held.resolved is not None
                        and self._held.resolved.navigation_reason is not None
                        else {}
                    ),
                }
            ),
            "completed": self.tracker.completed,
            "done": self.done,
            "revisions_seen": list(self.revisions_seen),
            "warnings": [w.as_dict() for w in list(self.store.warnings)[-10:]],
            "warnings_total": self.store.warning_count,
        }
        if self.script is not None:
            doc["script"] = self.script.status()
        return doc


# -- the scripted queue ---------------------------------------------------------------------------

PLAN_SCHEMA = "objective-plan/1"
MAX_PLAN_ENTRIES = 64


class PlanError(ValueError):
    """The plan file is unusable; ``reasons`` are the machine-readable validation reasons."""

    def __init__(self, message: str, reasons: Sequence[Reason] = ()) -> None:
        super().__init__(message)
        self.reasons = tuple(reasons)


def _entry_with_revision(entry: Mapping[str, Any], revision: int, set_by: str) -> dict[str, Any]:
    data = dict(entry)
    data["revision"] = revision
    data.setdefault("set_by", set_by)
    return data


def load_plan(
    source: str | Path | Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """The objectives of a plan, checked: a file ``{"schema_version": "objective-plan/1",
    "objectives": [...]}`` (or a bare JSON list), or an in-memory list. Every entry must validate
    as an objective (map names resolve, probes are well-formed); ``revision`` may be omitted
    (the queue numbers them) and ``set_by`` defaults to ``script``. Raises :class:`PlanError`."""
    if isinstance(source, (str, Path)):
        try:
            raw = json.loads(Path(source).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PlanError(f"cannot read the plan {source}: {type(exc).__name__}") from None
    else:
        raw = source if isinstance(source, Mapping) else list(source)
    if isinstance(raw, Mapping):
        if raw.get("schema_version", PLAN_SCHEMA) != PLAN_SCHEMA:
            raise PlanError(f"unsupported plan schema {raw.get('schema_version')!r}")
        unknown = sorted(set(raw) - {"schema_version", "objectives", "note"})
        if unknown:
            raise PlanError(f"unknown plan keys {unknown}")
        raw = raw.get("objectives")
    if not isinstance(raw, list) or not raw:
        raise PlanError("the plan must be a non-empty list of objectives")
    if len(raw) > MAX_PLAN_ENTRIES:
        raise PlanError(f"a plan holds at most {MAX_PLAN_ENTRIES} objectives")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            raise PlanError(f"entry {index} is not an object")
        data = _entry_with_revision(entry, 1, "script")
        verdict = validate(data)
        if not verdict.ok or verdict.objective is None:
            detail = "; ".join(f"{r.code} {r.field}: {r.detail}" for r in verdict.reasons)[:300]
            raise PlanError(f"entry {index} is not a valid objective: {detail}", verdict.reasons)
        oid = verdict.objective.objective_id
        if oid in seen:
            raise PlanError(f"entry {index}: objective_id {oid!r} appears twice")
        seen.add(oid)
        entries.append({k: v for k, v in entry.items()})
    return entries


class ScriptedObjectiveSource:
    """A list of objectives, each proposed to the store when the previous one's probes succeeded.

    The queue is a stand-in for agent in scripted runs and tests; it uses
    ``ObjectiveStore.propose`` so the file and the history look exactly as if agent had written
    them (``set_by`` says ``script``). Revisions are assigned at proposal time
    (``store.revision() + 1``) unless an entry names a larger one. It never skips.
    Resuming: when the store
    already holds an objective of the plan, the script continues after it; a foreign objective is
    left alone.
    """

    def __init__(
        self,
        store: ObjectiveStore,
        plan: str | Path | Sequence[Mapping[str, Any]],
        *,
        set_by: str = "script",
    ) -> None:
        self.store = store
        self.plan = load_plan(plan)
        self.set_by = set_by
        self.plan_fingerprint = _plan_fingerprint(self.plan)
        self._state_path = self.store.path.with_name(self.store.path.name + ".plan-state.json")
        self._completed_ids = self._load_state()
        self.position = self._next_pending()  # the index of the NEXT entry to propose
        self.proposed: list[tuple[str, int]] = []  # (objective_id, revision) in order
        self.rejected: list[dict[str, Any]] = []  # typed rejections (the script then stops)
        self.stopped = False

    def _load_state(self) -> set[str]:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return set()
            if data.get("schema_version") != PLAN_STATE_SCHEMA:
                return set()
            if data.get("plan_fingerprint") != self.plan_fingerprint:
                raise _plan_state_error(self._state_path)
            completed = data.get("completed_ids", [])
            if not isinstance(completed, list):
                return set()
            ids = {str(entry["objective_id"]) for entry in self.plan}
            return {str(item) for item in completed if str(item) in ids}
        except PlanError:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            return set()

    def _save_state(self) -> None:
        from pokegent.goals.store import atomic_write_json

        atomic_write_json(
            self._state_path,
            {
                "schema_version": PLAN_STATE_SCHEMA,
                "plan_fingerprint": self.plan_fingerprint,
                "completed_ids": [
                    entry["objective_id"]
                    for entry in self.plan
                    if entry["objective_id"] in self._completed_ids
                ],
            },
        )

    def _next_pending(self) -> int:
        return next(
            (
                index
                for index, entry in enumerate(self.plan)
                if entry["objective_id"] not in self._completed_ids
            ),
            len(self.plan),
        )

    def _entry_set_by(self, index: int) -> str:
        return str(self.plan[index].get("set_by", self.set_by))

    # -- driving -------------------------------------------------------------------------

    def start(self) -> Proposal | None:
        """Propose the first entry if the store is empty; resume after a known objective."""
        current = self.store.current()
        if current is None:
            self.position = self._next_pending()
            return self._propose_next()
        ids = [e["objective_id"] for e in self.plan]
        if current.objective_id in ids:
            if current.set_by != self._entry_set_by(ids.index(current.objective_id)):
                self.stopped = True  # agent/operator owns the file, even if the id is in this plan
                return None
            if current.objective_id in self._completed_ids:
                self.position = self._next_pending()
                return self._propose_next()
            self.position = ids.index(current.objective_id) + 1
        else:
            self.stopped = True  # a foreign objective is agent's (or the operator's): hands off
        return None

    def completed(self, objective_id: str, revision: int) -> Proposal | None:
        """The tracker reports ``objective_id`` complete: propose the next entry (if the file still
        holds that revision; a newer foreign revision is never overwritten)."""
        if self.stopped:
            return None
        current = self.store.current()
        if current is None or current.objective_id != objective_id or current.revision != revision:
            return None
        ids = [e["objective_id"] for e in self.plan]
        if objective_id not in ids:
            return None
        if current.set_by != self._entry_set_by(ids.index(objective_id)):
            return None
        if ids.index(objective_id) + 1 != self.position:
            return None
        if objective_id in self._completed_ids:
            return None
        if not self.store.commit_completion(objective_id=objective_id, expected_revision=revision):
            return None
        self._completed_ids.add(objective_id)
        self.position = self._next_pending()
        self._save_state()
        return self._propose_next()

    def _propose_next(self) -> Proposal | None:
        if self.position >= len(self.plan):
            return None
        entry = self.plan[self.position]
        floor = self.store.revision() + 1
        wanted = entry.get("revision")
        revision = max(floor, wanted) if isinstance(wanted, int) else floor
        result = self.store.propose(_entry_with_revision(entry, revision, self.set_by))
        if result.accepted:
            self.proposed.append((entry["objective_id"], revision))
            self.position += 1
        else:
            self.rejected.append({"index": self.position, **result.as_dict()})
            self.stopped = True
        return result

    @property
    def exhausted(self) -> bool:
        return self.position >= len(self.plan)

    def status(self) -> dict[str, Any]:
        return {
            "total": len(self.plan),
            "position": self.position,
            "plan_fingerprint": self.plan_fingerprint,
            "completed_ids": [
                entry["objective_id"]
                for entry in self.plan
                if entry["objective_id"] in self._completed_ids
            ],
            "exhausted": self.exhausted,
            "stopped": self.stopped,
            "proposed": [{"objective_id": o, "revision": r} for o, r in self.proposed],
            "rejected": self.rejected,
        }


def _plan_fingerprint(plan: Sequence[Mapping[str, Any]]) -> str:
    """Stable identity for the plan contents, independent of JSON key order."""
    encoded = json.dumps(list(plan), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_plan_state(plan: Sequence[Mapping[str, Any]], state_path: str | Path) -> None:
    path = Path(state_path)
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict) or data.get("plan_fingerprint") != _plan_fingerprint(plan):
        raise _plan_state_error(path)


__all__ = [
    "MAX_PLAN_ENTRIES",
    "PLAN_SCHEMA",
    "SUMMARY_MAX_CHARS",
    "FixedGoal",
    "GoalSource",
    "GoalView",
    "ObjectiveSession",
    "PlanError",
    "Refreshed",
    "ScriptedObjectiveSource",
    "bound_summary",
    "load_plan",
    "validate_plan_state",
]
