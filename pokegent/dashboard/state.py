"""What the viewer knows about the run: a bounded, thread-safe store fed by three read-only taps.

* :meth:`ViewerState.on_event` is registered on the event log (``EventLog.add_listener``);
* :meth:`ViewerState.on_perception` on the perceiver (``Perceiver.observers``);
* :meth:`ViewerState.on_decider_call` on the wrapped decider (:class:`ObservedDecider`).

All three run on the runner thread, copy only whitelisted fields (never an event's ``details``
wholesale: they hold ``executor_ref`` and fingerprints) and never raise into the run. The HTTP
threads call :meth:`ViewerState.view`, which returns plain copies; nothing in here holds a
reference to the emulator, the runner or a socket.
"""

from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from typing import Any

from pokegent.dashboard.game import badges_block, map_block, state_block
from pokegent.dashboard.names import NameBook
from pokegent.dashboard.records import cap, decision_record, error_label, model_view
from pokegent.events import EventEnvelope, EventType
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.options.models import ChoiceResult, DecisionFrame
from pokegent.pipeline import Perception

KEEP_DECISIONS = 10
KEEP_ACTIONS = 30
KEEP_HIGHLIGHTS = 40
KEEP_PROBLEMS = 10
KEEP_FRAMES = 24
KEEP_EVENTS = 300
#: the keys of an event's ``details`` the operator surface may show (never ``executor_ref``,
#: fingerprints, state hashes, free-text reasons or UI diagnostics)
EVENT_DETAILS = frozenset(
    {
        "option_id", "status", "error_code", "verified", "elapsed_frames", "inputs", "buttons",
        "hold_frames", "wait_frames", "index", "measured_ms", "confidence", "model_id", "late",
        "truncated", "chooseable", "kind", "streak", "attempt", "iterations", "source", "busy",
        "detector", "component", "code", "revision", "objective_id", "trigger",
    }
)  # fmt: skip
KEEP_PICKS = 600
#: the window of ``pokegent.decision.seen_10m`` (the old card's "same pick again and again" signal)
PICK_WINDOW_S = 600.0
#: ``action.*`` event type -> the record status it sets
_ACTION_STATUS = {
    EventType.ACTION_STARTED.value: "executing",
    EventType.ACTION_VERIFIED.value: "done",
    EventType.ACTION_INTERRUPTED.value: "interrupted",
    EventType.ACTION_FAILED.value: "failed",
}
_PROBLEM_EVENTS = {
    EventType.SCENE_UNKNOWN.value: "unknown scene",
    EventType.SCENE_UNSUPPORTED.value: "unsupported scene",
    EventType.RUNTIME_PAUSED.value: "paused",
    EventType.RUNTIME_BLOCKED.value: "blocked",
    EventType.RUNTIME_DEGRADED.value: "degraded",
    EventType.ESCALATION_REQUESTED.value: "escalation",
    EventType.PROGRESS_STUCK.value: "stuck",
    EventType.DECISION_SYSTEMONE_TIMEOUT.value: "decider timeout",
    EventType.DECISION_SYSTEMONE_INVALID.value: "decider answer rejected",
}
GoalFn = Callable[[], "dict[str, Any] | None"]
CountersFn = Callable[[], "dict[str, int]"]


def event_details(details: Any) -> dict[str, Any]:
    """The whitelisted, plain-JSON part of an event's ``details`` (tuples become lists)."""
    out: dict[str, Any] = {}
    for key, value in details.items():
        if key not in EVENT_DETAILS:
            continue
        if isinstance(value, (list, tuple)):
            value = [v for v in value if isinstance(v, (str, int, float, bool))]
        elif not isinstance(value, (str, int, float, bool, type(None))):
            continue
        out[key] = value
    return out


class ViewerState:
    def __init__(
        self,
        *,
        run_id: str,
        decider: str,
        max_steps: int,
        realtime: bool,
        names: NameBook | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.run_id = run_id
        self.decider = decider
        self.max_steps = max_steps
        self.realtime = realtime
        self.names = names or NameBook()
        self._clock = clock
        self._started_at = clock()
        self._lock = threading.RLock()
        self._status = "starting"
        self._stop_reason: str | None = None
        self._exit_code: int | None = None
        self._ended_at: float | None = None
        self._ready = False
        self._scene: dict[str, Any] = {}
        self._scene_since: float | None = None
        self._snapshot: StateSnapshot | None = None
        self._snapshot_at: float | None = None
        self._state_cache: tuple[tuple[int, str], dict[str, Any]] | None = None
        self._frame_no: int | None = None
        self._decisions: deque[dict[str, Any]] = deque(maxlen=KEEP_DECISIONS)
        self._pending: dict[str, Any] | None = None
        self._events: deque[dict[str, Any]] = deque(maxlen=KEEP_EVENTS)
        self._by_id: dict[str, dict[str, Any]] = {}
        self._frames: OrderedDict[str, DecisionFrame] = OrderedDict()
        self._actions: deque[dict[str, Any]] = deque(maxlen=KEEP_ACTIONS)
        self._actions_by_id: dict[str, dict[str, Any]] = {}
        self._highlights: deque[dict[str, Any]] = deque(maxlen=KEEP_HIGHLIGHTS)
        self._problems: deque[dict[str, Any]] = deque(maxlen=KEEP_PROBLEMS)
        self._goal: dict[str, Any] | None = None
        self._goal_fn: GoalFn | None = None
        self._counters_fn: CountersFn | None = None
        self._in_battle = False
        self._map_id: str | None = None
        self._badge_count: int | None = None
        self._levels: dict[int, int] = {}
        self._picks: deque[tuple[float, str, str]] = deque(maxlen=KEEP_PICKS)
        self.stats = {
            "battles": 0,
            "blackouts": 0,
            "decisions": 0,
            "forced": 0,
            "decider_errors": 0,
            "latency_ms_total": 0.0,
            "latency_ms_last": None,
        }

    # -- wiring (before the run) --------------------------------------------------------------

    def bind(self, *, goal: GoalFn | None = None, counters: CountersFn | None = None) -> None:
        self._goal_fn = goal
        self._counters_fn = counters

    # -- tap 1: events ------------------------------------------------------------------------

    def on_event(self, env: EventEnvelope) -> None:
        with self._lock:
            try:
                self._event(env)
            except Exception:  # noqa: BLE001 - a viewer bug never costs the run an event
                pass

    def _event(self, env: EventEnvelope) -> None:
        now = self._clock()
        kind = env.type
        if env.emulator_frame is not None:
            self._frame_no = env.emulator_frame
        self._events.append(
            {
                "seq": env.seq,
                "ts": round(now, 3),
                "type": kind,
                "scene": env.scene_id,
                "request_id": env.request_id,
                "outcome": env.outcome,
                "source": env.source,
                "provider": env.provider,
                "frame": env.emulator_frame,
                "details": event_details(env.details),
            }
        )
        if kind == EventType.RUNTIME_STARTED.value:
            self._status, self._ready = "running", True
        elif kind == EventType.RUNTIME_STOPPED.value:
            self._stop_reason = env.outcome
        elif kind == EventType.SCENE_ENTERED.value:
            self._scene_entered(env, now)
        elif kind in _ACTION_STATUS:
            self._action(env, now)
        elif kind == EventType.OPTION_REJECTED.value and env.request_id:
            self._finish_action(env, "rejected")
        elif kind == EventType.PROGRESS_GOAL_CHANGED.value:
            self._highlight(now, "progress", "OBJECTIVE", "The objective changed")
        elif kind == EventType.PROGRESS_MILESTONE_COMPLETED.value:
            self._highlight(now, "milestone", "MILESTONE", "A milestone was completed")
        elif kind == EventType.DECISION_SYSTEMONE_TIMEOUT.value and env.request_id:
            self._decision_status(env.request_id, "timeout")
        elif kind == EventType.DECISION_SYSTEMONE_INVALID.value and env.request_id:
            self._decision_status(env.request_id, "invalid")
        elif kind == EventType.DECISION_ROUTE.value and env.request_id:
            self._route_decision(env, now)
        if self.decider == "hybrid" and env.request_id:
            record = (
                self._pending
                if self._pending and self._pending["id"] == env.request_id
                else self._by_id.get(env.request_id)
            )
            if record is not None:
                if kind == EventType.DECISION_AGENT_REQUESTED.value:
                    record["awaiting_agent"] = True
                elif kind in {
                    EventType.DECISION_SYSTEMONE_COMPLETED.value,
                    EventType.DECISION_AGENT_COMPLETED.value,
                } and env.outcome == "accepted":
                    decided_by = env.details.get("decided_by")
                    if decided_by in {"system1", "agent"}:
                        record["decided_by"] = decided_by
                    record["awaiting_agent"] = False
        if kind in _PROBLEM_EVENTS:
            self._problems.append(
                {
                    "ts": round(now, 3),
                    "what": _PROBLEM_EVENTS[kind],
                    "scene": env.scene_id,
                    "outcome": env.outcome,
                    "error_code": env.details.get("error_code"),
                    "step": env.seq,
                }
            )

    def _scene_entered(self, env: EventEnvelope, now: float) -> None:
        scene = env.scene_id or ""
        details = env.details
        self._scene = {
            "id": scene,
            "family": scene.split(".", 1)[0] if scene else None,
            "kind": details.get("kind"),
            "busy": bool(details.get("busy", False)),
            "confidence": details.get("confidence"),
        }
        self._scene_since = now
        in_battle = scene.startswith("battle.")
        if in_battle and not self._in_battle:
            self.stats["battles"] += 1
            self._highlight(now, "battle", "BATTLE", self._battle_line())
        self._in_battle = in_battle
        if scene == "system.whiteout":
            self.stats["blackouts"] += 1
            self._highlight(now, "blackout", "BLACKOUT", "The party blacked out")

    def _battle_line(self) -> str:
        snap = self._snapshot
        block = getattr(snap, "battle", None) if snap is not None else None
        foe = block.opponent if block is not None and block.active else None
        if foe is None:
            return "A battle began"
        name = block.lookups.species.get(foe.species) or self.names.species_name(foe.species)
        return f"Battle vs {name.title() if name else 'an opponent'}"

    def _action(self, env: EventEnvelope, now: float) -> None:
        rid = env.request_id
        if not rid:
            return
        status = _ACTION_STATUS[env.type]
        row = self._actions_by_id.get(rid)
        if row is None:
            option_id = env.details.get("option_id")
            frame = self._frames.get(rid)
            option = frame.get(option_id) if frame is not None and option_id else None
            row = {
                "id": rid,
                "ts": round(now, 3),
                "scene": env.scene_id,
                "option": option_id,
                "label": cap(option.label, 80) if option is not None else None,
                "source": env.details.get("source"),
                "status": status,
                "error_code": None,
            }
            if env.details.get("source") == "forced":
                self.stats["forced"] += 1
            self._actions.append(row)
            self._actions_by_id[rid] = row
            if len(self._actions_by_id) > 4 * KEEP_ACTIONS:
                live = {a["id"] for a in self._actions}
                self._actions_by_id = {k: v for k, v in self._actions_by_id.items() if k in live}
        if env.type != EventType.ACTION_STARTED.value:
            self._finish_action(env, status)

    def _finish_action(self, env: EventEnvelope, status: str) -> None:
        rid = env.request_id
        code = env.details.get("error_code")
        if code is None and env.outcome and env.outcome.startswith("E_"):
            code = env.outcome
        row = self._actions_by_id.get(rid or "")
        if row is not None:
            row["status"], row["error_code"] = status, code
        record = self._by_id.get(rid or "")
        if record is not None:
            record["status"] = status
            record["result"] = {
                "status": env.outcome or status,
                "verified": env.details.get("verified"),
                "error_code": code,
                "elapsed_frames": env.details.get("elapsed_frames"),
            }

    def _decision_status(self, rid: str, status: str) -> None:
        record = self._by_id.get(rid)
        if record is not None:
            record["status"] = status

    def _route_decision(self, env: EventEnvelope, now: float) -> None:
        """A proven route step decides without a provider: no model, confidence or latency."""
        rid = env.request_id
        if not rid or rid in self._by_id:
            return
        frame = self._frames.get(rid)
        option_id = env.details.get("option_id")
        if frame is None or option_id not in frame.chooseable_ids():
            return
        goal = self._goal or {}
        record = decision_record(
            frame,
            result=ChoiceResult(option_id=option_id, provider_id=env.provider or ""),
            source="route",
            elapsed_ms=None,
            now=now,
            status="executing",
            goal_summary=goal.get("summary"),
            model=None,
        )
        record["decided_by"] = "route"
        if len(self._decisions) == self._decisions.maxlen:
            self._by_id.pop(self._decisions[0]["id"], None)
        self._decisions.append(record)
        self._by_id[record["id"]] = record
        self._picks.append((record["ts"], record["scene"], record["selected"]))
        self.stats["decisions"] += 1

    def _highlight(self, now: float, category: str, title: str, text: str) -> None:
        self._highlights.append(
            {"ts": round(now, 3), "category": category, "title": title, "text": text}
        )

    # -- tap 2: perceptions -------------------------------------------------------------------

    def on_perception(self, perception: Perception) -> None:
        with self._lock:
            try:
                self._perception(perception)
            except Exception:  # noqa: BLE001
                pass

    def _perception(self, perception: Perception) -> None:
        now = self._clock()
        snapshot = perception.snapshot
        if snapshot.emulator_frame is not None:
            self._frame_no = snapshot.emulator_frame
        if snapshot.usable:
            self._snapshot, self._snapshot_at = snapshot, now
            self._note_world(snapshot, now)
        frame = perception.frame
        if frame is not None:
            self._frames[frame.decision_id] = frame
            while len(self._frames) > KEEP_FRAMES:
                self._frames.popitem(last=False)
        if self._goal_fn is not None:
            try:
                self._goal = self._goal_fn()
            except Exception:  # noqa: BLE001 - a broken goal source is "no goal" here too
                self._goal = None

    def _note_world(self, snapshot: StateSnapshot, now: float) -> None:
        info = map_block(snapshot)
        map_id = info.get("id")
        if map_id and map_id != self._map_id:
            if self._map_id is not None:
                self._highlight(now, "location", "LOCATION", f"Entered {info.get('name')}")
            self._map_id = map_id
        badges = badges_block(snapshot)
        if self._badge_count is not None and len(badges) > self._badge_count:
            self._highlight(now, "badge", "BADGE", f"Badge earned: {badges[-1]}")
        self._badge_count = len(badges)
        seen: dict[int, int] = {}
        for mon in snapshot.party:  # personality is only a private key here, never sent
            seen[mon.personality] = mon.level
            before = self._levels.get(mon.personality)
            if before is not None and mon.level > before:
                name = self.names.species_name(mon.species) or mon.nickname or "A Pokemon"
                self._highlight(
                    now, "progress", "LEVEL UP", f"{name.title()} reached Lv{mon.level}"
                )
        self._levels = seen

    # -- tap 3: the decider ---------------------------------------------------------------------

    def on_decider_start(self, frame: DecisionFrame) -> None:
        """The decider was just asked: show the options as "thinking" until it answers."""
        with self._lock:
            try:
                goal = self._goal or {}
                self._pending = decision_record(
                    frame,
                    result=None,
                    source=self.decider,
                    elapsed_ms=None,
                    now=self._clock(),
                    status="thinking",
                    goal_summary=goal.get("summary"),
                    model=model_view(frame),
                )
            except Exception:  # noqa: BLE001
                self._pending = None

    def on_decider_call(
        self,
        frame: DecisionFrame,
        result: ChoiceResult | None,
        error: BaseException | None,
        elapsed_ms: float,
    ) -> None:
        with self._lock:
            try:
                self._decider_call(frame, result, error, elapsed_ms)
            except Exception:  # noqa: BLE001
                pass

    def _decider_call(
        self,
        frame: DecisionFrame,
        result: ChoiceResult | None,
        error: BaseException | None,
        elapsed_ms: float,
    ) -> None:
        goal = self._goal or {}
        pending, self._pending = self._pending, None
        model = pending["model_view"] if pending and pending["id"] == frame.decision_id else None
        record = decision_record(
            frame,
            result=result,
            source=self.decider,
            elapsed_ms=elapsed_ms,
            now=self._clock(),
            status="executing" if result is not None else "error",
            error=error_label(error) if error is not None else None,
            goal_summary=goal.get("summary"),
            model=model if model is not None else model_view(frame),
        )
        if len(self._decisions) == self._decisions.maxlen:
            self._by_id.pop(self._decisions[0]["id"], None)
        self._decisions.append(record)
        self._by_id[record["id"]] = record
        if record["selected"] is not None:
            self._picks.append((record["ts"], record["scene"], record["selected"]))
        self.stats["decisions"] += 1
        if result is None:
            self.stats["decider_errors"] += 1
        else:
            self.stats["latency_ms_total"] += elapsed_ms
            self.stats["latency_ms_last"] = round(elapsed_ms, 1)

    # -- the end ------------------------------------------------------------------------------

    def finish(self, *, stop_reason: str | None, exit_code: int | None) -> None:
        with self._lock:
            self._status = "stopped"
            self._ready = False
            self._stop_reason = stop_reason or self._stop_reason
            self._exit_code = exit_code
            self._ended_at = self._clock()
            if self._pending is not None:
                self._pending["awaiting_agent"] = False

    # -- tap output (HTTP threads) -----------------------------------------------------------

    def view(self) -> dict[str, Any]:
        """A consistent plain copy of everything the telemetry is built from."""
        counters = {}
        if self._counters_fn is not None:
            try:
                counters = dict(self._counters_fn())
            except Exception:  # noqa: BLE001
                counters = {}
        with self._lock:
            snapshot = self._snapshot
            cached = self._state_cache
            key = (snapshot.snapshot_seq, snapshot.state_hash) if snapshot is not None else None
            if snapshot is not None and (cached is None or cached[0] != key):
                cached = (key, state_block(snapshot, self.names))
                self._state_cache = cached
            now = self._clock()
            return {
                "now": now,
                "run": {
                    "run_id": self.run_id,
                    "decider": self.decider,
                    "status": self._status,
                    "stop_reason": self._stop_reason,
                    "exit_code": self._exit_code,
                    "max_steps": self.max_steps,
                    "realtime": self.realtime,
                    "started_at": self._started_at,
                    "ended_at": self._ended_at,
                    "emulator_frame": self._frame_no,
                    "counters": counters,
                },
                "ready": self._ready,
                "scene": copy.deepcopy(self._scene),
                "scene_since": self._scene_since,
                "state": copy.deepcopy(cached[1]) if snapshot is not None and cached else None,
                "state_at": self._snapshot_at,
                "decisions": copy.deepcopy(list(self._decisions)),
                "pending": copy.deepcopy(self._pending),
                "actions": copy.deepcopy(list(self._actions)),
                "highlights": copy.deepcopy(list(self._highlights)),
                "problems": copy.deepcopy(list(self._problems)),
                "goal": copy.deepcopy(self._goal),
                "events": copy.deepcopy(list(self._events)),
                "stats": dict(self.stats),
                "picks": [p for p in self._picks if now - p[0] <= PICK_WINDOW_S],
            }
