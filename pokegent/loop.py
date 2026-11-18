"""Router loop (spec 5.4), dependency-injected. No agent, no model, no randomness.

One iteration = perceive -> classify -> (wait | diagnose+escalate | decide) -> execute -> record.

* invalid snapshot       SAFE_PAUSE event, bounded settle-and-reread, never an input;
* busy scene             bounded wait through the executor; a wait that never ends escalates;
* unknown / unsupported  diagnostics event, NO input, escalation event (after bounded settle
                         retries for "unknown", which may be a transition frame);
* no chooseable option   bounded settle, then escalation;
* exactly one option     executed without calling the decider (``decision.forced``);
* otherwise              decider called; the answer is validated against the frame (id in the
                         chooseable set, decision id current) before anything is executed;
* execution              always through the re-validating executor; the outcome feeds the
                         progress counters, the coverage report and the event log.

Safety nets that bound every loop: ``max_steps``, ``deadline_s``, the ``no_progress`` counter
keyed by (scene, option, state fingerprint), the busy-streak limit, and the invalid-choice limit.
agent plugs in later behind :class:`EscalationSink`; the runner never calls it on the button path
except through that interface at an escalation point.

Objectives (M3.2, contracts v1.3). With an :class:`~pokegent.goals.source.ObjectiveSession` the
goal is dynamic: the session reads the objective file at the start of every iteration (a bad file
keeps the last valid objective and becomes a ``runtime.degraded`` warning), the tracker evaluates
probes on every perception. A finished objective never starts a new free-roaming decision
(``field.free``): ``on_objective_done`` is ``hold`` (default, stay alive without input and poll
the file), ``pause`` (stop with ``WAITING_OBJECTIVE``) or ``continue`` (clear the completed
revision and keep playing in this run).
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
import traceback
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Protocol

from pokegent.actions._exec.executor import Executor
from pokegent.actions._exec.models import ActionResult, ActionStatus
from pokegent.actions._exec.observation import (
    ObservationTooLarge,
    StuckObservation,
    observation_from_frame,
    write_observation,
)
from pokegent.errors import ErrorCode
from pokegent.events import EventSink, EventType
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.goals.source import ObjectiveSession, is_objective_rejection
from pokegent.options.models import (
    Decider,
    DeciderTimeout,
    DecisionFrame,
    Legality,
    PayloadLeakError,
    check_choice,
    to_system_one_payload,
)
from pokegent.options.registry import (
    BuilderRegistry,
    option_target_key,
    relevant_state_signature,
)
from pokegent.pipeline import (
    Perceiver,
    Perception,
    PerceptionKind,
    SnapshotSource,
    is_transient_invalid,
)
from pokegent.recovery import (
    ProgressMonitor,
    semantic_progress_signature,
)
from pokegent.state.catalog import SceneCatalog
from pokegent.state.scenes import (
    ConfirmPolicy,
    DetectorRegistry,
    SceneCoverageReport,
    SceneResult,
)

#: Rejections that mean "the world moved on since the decision" (coverage counter ``stale``).
STALE_CODES = frozenset(
    {
        ErrorCode.E_STALE_SNAPSHOT,
        ErrorCode.E_SCENE_CHANGED,
        ErrorCode.E_FINGERPRINT_CHANGED,
        ErrorCode.E_OPTION_GONE,
        ErrorCode.E_OPTION_CHANGED,
        ErrorCode.E_SCENE_BUSY,
    }
)


def stop_message(reason: str, details: Mapping[str, Any]) -> str:
    """Render the single operator-facing INFO line emitted at the end of every run."""
    if reason == "max_steps":
        return f"stopped: max_steps reached ({details['max_steps']})"
    if reason == "objective_satisfied":
        return f"stopped: objective satisfied ({details['objective_id']})"
    if reason == "interrupted":
        return "stopped: interrupted"
    if reason == "error":
        return f"stopped: error {details['status']}"
    if reason == "deadline":
        return "stopped: deadline reached"
    if reason == "no_progress":
        return f"stopped: no progress ({details['reason']})"
    if reason == "provider_configuration":
        return f"stopped: provider configuration requires owner review: {details['error']}"
    if reason == "escalated":
        return f"stopped: escalated ({details['kind']})"
    return f"stopped: {reason}"


# -- escalation (agent plugs in here later) -------------------------------------------------------


class EscalationKind(str, Enum):
    UNKNOWN_SCENE = "unknown_scene"
    UNSUPPORTED_SCENE = "unsupported_scene"
    ESCALATE_SCENE = "escalate_scene"  # catalog row whose only answer is escalation
    NO_BUILDER = "no_builder"
    NO_LEGAL_OPTION = "no_legal_option"
    NO_PROGRESS = "no_progress"
    BUSY_STUCK = "busy_stuck"
    DECIDER_FAILED = "decider_failed"
    SNAPSHOT_INVALID = "snapshot_invalid"


#: escalations that mean "the system cannot act in this scene" (what the scene budget counts)
_CANNOT_ACT_KINDS = frozenset(
    {
        EscalationKind.UNKNOWN_SCENE,
        EscalationKind.UNSUPPORTED_SCENE,
        EscalationKind.ESCALATE_SCENE,
        EscalationKind.NO_BUILDER,
        EscalationKind.NO_LEGAL_OPTION,
    }
)


class EscalationAction(str, Enum):
    CONTINUE = "continue"  # the sink did what it could; re-perceive and go on
    PAUSE = "pause"  # stop the loop in a safe state (the supervisor resumes it)
    STOP = "stop"


@dataclass(frozen=True)
class EscalationRequest:
    kind: EscalationKind
    scene_id: str | None
    step: int
    error_code: ErrorCode | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EscalationResponse:
    action: EscalationAction
    note: str = ""


class EscalationSink(Protocol):
    """Receives structured escalation requests. It must not press buttons or write the emulator."""

    def escalate(self, request: EscalationRequest) -> EscalationResponse: ...


class PauseOnEscalation:
    """Default sink: record nothing and pause at an escalation point."""

    def escalate(self, request: EscalationRequest) -> EscalationResponse:
        return EscalationResponse(EscalationAction.PAUSE, "default sink pauses")


class RecordingEscalationSink:
    """Keeps every request (tests, dry runs) and answers with a fixed or scripted action."""

    def __init__(
        self,
        action: EscalationAction = EscalationAction.PAUSE,
        script: list[EscalationAction] | None = None,
    ) -> None:
        self.requests: list[EscalationRequest] = []
        self._action = action
        self._script = list(script or [])

    def escalate(self, request: EscalationRequest) -> EscalationResponse:
        self.requests.append(request)
        action = self._script.pop(0) if self._script else self._action
        return EscalationResponse(action, "recorded")


# -- configuration and results --------------------------------------------------------------------


class RunStatus(str, Enum):
    MAX_STEPS = "max_steps"
    DEADLINE = "deadline"
    SAFE_PAUSE = "safe_pause"  # invalid snapshot / unsupported ROM; supervisor decides
    PAUSED = "paused"  # an escalation answered PAUSE
    STOPPED = "stopped"  # an escalation answered STOP
    ESCALATED = "escalated"  # local/System One stopped for its supervising agent
    #: the active objective is achieved (or ended early) and nothing newer is in the file; the run
    #: ``pause`` remains available for callers that need the legacy stop-at-completion behavior.
    WAITING_OBJECTIVE = "waiting_objective"
    #: a stop was requested from outside (SIGTERM/SIGINT via ``Runner.stop_event``): the run ended
    #: between two iterations, ``final.state`` is written, a supervisor may restart it with new code
    INTERRUPTED = "interrupted"


ON_OBJECTIVE_DONE = ("pause", "hold", "continue")
#: scenes where a finished objective does not start a new decision (the free overworld)
IDLE_SCENE_IDS = frozenset({"field.free", "dialog.end"})
# Field-menu surfaces whose action-bearing decisions count toward the progress detector.
_FIELD_MENU_SCENES = frozenset(
    {
        "menu.open",
        "menu.start",
        "party.list",
        "party.action",
        "party.reorder",
        "party.held_item",
        "party.mail",
        "bag.items",
        "bag.key_items",
        "bag.poke_balls",
        "bag.tms_hms",
        "bag.berries",
        "tm.list",
        "move.list",
    }
)
WHITEOUT_SCENE_ID = "system.whiteout"


@dataclass(frozen=True)
class RunnerConfig:
    run_id: str = ""
    goal_id: str | None = None
    max_steps: int = 1000
    deadline_s: float | None = None
    #: Same (scene, option, fingerprint) attempted this many times inside the window => escalate.
    no_progress_limit: int = 8
    no_progress_window: int = 64
    invalid_retries: int = 2
    unknown_retries: int = 2
    settle_frames: int = 2
    busy_wait_frames: int = 120
    busy_wait_wall_s: float = 5.0
    busy_stuck_limit: int = 20
    decider_retries: int = 1
    invalid_choice_limit: int = 3
    # Keep enough same-frame failures to suppress sibling option ids (PC party
    # actions can expose six targets) until the frame context changes.
    recent_failed_keep: int = 64
    confirm_scenes: bool = True
    #: Consecutive transient invalid frames (``party/mon_bad_checksum``, WP1c) the perceiver
    #: tolerates before the read counts as real; only used when the runner builds the perceiver.
    transient_invalid_frames: int = 4
    #: What a finished objective does at the next free-roaming decision: ``hold`` (default) stays
    #: alive without input and polls the file; ``pause`` stops with ``WAITING_OBJECTIVE``;
    #: ``continue`` atomically clears the completed revision and keeps playing. Only matters with
    #: an objective session.
    on_objective_done: str = "hold"
    #: ``hold``: frames advanced (no input) and seconds slept between two polls of the file.
    hold_idle_frames: int = 8
    hold_poll_s: float = 0.25
    #: Headless/maintenance opt-out: a satisfied objective success probe does not stop (nor hold)
    #: the loop; the run plays on until ``max_steps``/``deadline`` or an escalation. An objective
    #: that ended early (``abort_if``) still finishes the run. Default off: the run stops with
    #: ``WAITING_OBJECTIVE`` as always. Top-level: ``run.ignore_objective`` / CLI flag.
    ignore_objective: bool = False
    #: Escalations about the SAME scene the system cannot act in (unknown, unsupported, no builder,
    #: escalation-only, no legal option) that a supervisor may answer ``continue`` before the run
    #: stops with ``SAFE_PAUSE`` (the counter restarts after an applied action). Without it an
    #: unknown screen repeats forever when a test sink explicitly continues (pre-fork replays:
    #: 120 iterations, 40 escalations, no input). 0 disables the budget.
    scene_escalation_budget: int = 6
    #: ``forced`` preserves the historical single-option shortcut; ``off`` asks the decider for
    #: every action-bearing frame and only auto-runs pure waits/animation advances.
    autoplay: str = "off"
    system1_outage: str = "wait"
    system1_max_outage_s: float = 0.0

    def __post_init__(self) -> None:
        if self.system1_outage not in ("wait", "pause"):
            raise ValueError("system1_outage must be wait|pause")
        if self.system1_max_outage_s < 0:
            raise ValueError("system1_max_outage_s must be >= 0")
        for name in (
            "max_steps",
            "no_progress_limit",
            "no_progress_window",
            "settle_frames",
            "busy_wait_frames",
            "busy_stuck_limit",
            "invalid_choice_limit",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")
        for name in (
            "invalid_retries",
            "unknown_retries",
            "decider_retries",
            "recent_failed_keep",
            "transient_invalid_frames",
            "hold_idle_frames",
            "hold_poll_s",
            "scene_escalation_budget",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.on_objective_done not in ON_OBJECTIVE_DONE:
            raise ValueError(f"on_objective_done must be one of {ON_OBJECTIVE_DONE}")
        if self.autoplay != "off":
            raise ValueError("autoplay=forced was removed: the decider owns action selection")
        if self.deadline_s is not None and self.deadline_s <= 0:
            raise ValueError("deadline_s must be positive")


class GameClearObserver(Protocol):
    """Observe exactly once when a usable snapshot shows the cleared game.

    ``pokegent.actions.menu.system_flow.facts.GameClearWatch`` is the one
    in use.
    """

    def observe(self, snapshot: StateSnapshot) -> bool: ...


@dataclass
class ProgressCounters:
    iterations: int = 0
    actions_applied: int = 0
    actions_verified: int = 0
    actions_rejected: int = 0
    actions_interrupted: int = 0
    actions_failed: int = 0  # timed_out + uncertain
    forced: int = 0
    assisted: int = 0
    decided: int = 0
    #: Frames handed to the external agent (agent mode and hybrid escalations).
    agent_frames: int = 0
    invalid_choices: int = 0
    busy_waits: int = 0
    unknown_scenes: int = 0
    unsupported_scenes: int = 0
    invalid_snapshots: int = 0
    escalations: int = 0
    stuck: int = 0
    #: one-frame settles the perceiver spent on transient invalid reads (observation, no input)
    transient_invalid_settled: int = 0
    #: objective bookkeeping: store warnings and iterations spent holding for a new objective
    objective_warnings: int = 0
    objective_idle: int = 0
    observation_errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class RunSummary:
    status: RunStatus
    iterations: int
    counters: dict[str, int]
    coverage: dict[str, Any]
    last_scene: str | None
    review_pending: bool = False
    #: the objective block of ``summary.json`` (``None`` without an objective session)
    objective: dict[str, Any] | None = None
    #: ``progress.game_cleared`` was emitted in this run (the game was cleared before or during it)
    game_cleared: bool = False
    #: Operator-facing reason for the terminal status, also carried by runtime.stopped.
    stop_reason: str = ""
    stop_details: dict[str, Any] = field(default_factory=dict)
    #: Machine-readable supervisor disposition written to ``summary.json``.
    verdict: str = "other"


# -- the runner -----------------------------------------------------------------------------------


class Runner:
    def __init__(
        self,
        *,
        decider: Decider,
        executor: Executor,
        events: EventSink,
        perceiver: Perceiver | None = None,
        reader: SnapshotSource | None = None,
        detectors: DetectorRegistry | None = None,
        builders: BuilderRegistry | None = None,
        catalog: SceneCatalog | None = None,
        escalation: EscalationSink | None = None,
        clock: Callable[[], float] = time.monotonic,
        config: RunnerConfig | None = None,
        coverage: SceneCoverageReport | None = None,
        objectives: ObjectiveSession | None = None,
        sleep: Callable[[float], None] = time.sleep,
        game_clear: GameClearObserver | None = None,
        observation_path: str | None = None,
    ) -> None:
        self.config = config or RunnerConfig()
        self.decider = decider
        self.executor = executor
        self.events = events
        self._observation_path = observation_path or self._default_observation_path(events)
        self._observation_seq = 0
        self._observation_logged_errors: set[type[Exception]] = set()
        self._last_battle_context: dict[str, Any] | None = None
        self.escalation: EscalationSink = escalation or PauseOnEscalation()
        self.clock = clock
        self.coverage = coverage if coverage is not None else SceneCoverageReport()
        self.counters = ProgressCounters()
        if perceiver is None:
            if reader is None or detectors is None or builders is None:
                raise ValueError("give either a perceiver or reader + detectors + builders")
            cat = catalog if catalog is not None else detectors.catalog
            perceiver = Perceiver(
                reader,
                detectors,
                builders,
                run_id=self.config.run_id,
                goal_id=self.config.goal_id,
                catalog=cat,
                confirm=(
                    ConfirmPolicy(scene_ids=cat.confirm_ids())
                    if cat is not None and self.config.confirm_scenes
                    else None
                ),
                transient_retries=self.config.transient_invalid_frames,
            )
        self.perceiver = perceiver
        if executor.perceiver is None:
            executor.bind_perceiver(perceiver)
        self.objectives = objectives
        self._sleep = sleep
        #: the cleared-game watch (``None``: nothing is emitted) and whether it already fired
        self._game_clear = game_clear
        self.game_cleared = False
        self._first_snapshot_seq: int | None = None
        self._holding = False
        self._briefing_public_events: deque[dict[str, Any]] = deque(maxlen=5)
        self._last_snapshot: StateSnapshot | None = None
        self._last_bike_on_position: tuple[int, int, int, int, int] | None = None
        if objectives is not None:
            perceiver.bind_goal(objectives)
            objectives.attach_sink(self._objective_sink)
        self._blocked_route_fingerprints: OrderedDict[str, None] = OrderedDict()
        #: callbacks ``fn(iteration)`` after every finished iteration (checkpoints); a failing one
        #: is reported as ``runtime.degraded`` and never stops the run
        self.after_iteration: list[Callable[[int], None]] = []
        #: set from a signal handler or a supervisor: the loop ends before the next iteration
        self.stop_event = threading.Event()
        # consecutive escalations per scene the system cannot act in (reset by an applied action)
        self._scene_escalations: dict[str, int] = {}
        self._decision_seq = 0
        self._previous: SceneResult | None = None
        self._entered: str | None = None
        self._frame_no: int | None = None
        self._last_snapshot = None
        self._recent_failed: deque[dict[str, Any]] = deque(
            maxlen=max(1, self.config.recent_failed_keep)
        )
        # A failed warp can open a dialog before the next field frame. Keep its verified
        # map-unchanged result outside the scene-family failure deque so transitions cannot erase
        # the evidence before the route returns to this door.
        self._warp_blocks: deque[dict[str, Any]] = deque(maxlen=20)
        # Dialog-block repetitions survive the field -> dialog -> field handoff. Ordinary execution
        # failures deliberately retain their existing reset policy.
        self._dialog_blocks: deque[dict[str, Any]] = deque(maxlen=20)
        self._attempts: dict[tuple[str, str, str, str], deque[int]] = {}
        self._postcondition_attempts: dict[tuple[str, str], int] = {}
        self._postcondition_scope: tuple[tuple[str | None, int | None], object] | None = None
        self._unknown_streak = 0
        self._no_options_streak = 0
        self._busy_streak = 0
        self._invalid_choice_streak = 0
        self._rejected_choice_attempts: dict[tuple[str, str, ErrorCode], int] = {}
        self._progress = ProgressMonitor(self.config.no_progress_limit, now=self.clock())
        self._last_perception: Perception | None = None
        self._last_frame: DecisionFrame | None = None
        self._pending_objective_key: tuple[str, int] | None = None
        self._stop_reason_override: tuple[str, dict[str, Any]] | None = None
        self._verdict = "other"
        self._hybrid_no_progress_active = False
        self._hybrid_recovery_decisions = 0
        self._objective_lock = threading.RLock()
        refresh_pending = getattr(self.decider, "bind_pending_frame", None)
        if callable(refresh_pending):
            refresh_pending(self._refresh_pending_frame)
        objective_status = getattr(self.decider, "bind_objective_status", None)
        if callable(objective_status) and self.objectives is not None:
            objective_status(
                lambda: {
                    "objective_done": self.objectives.completed,
                    "objective_id": (
                        self.objectives.objective.objective_id
                        if self.objectives.objective is not None
                        else None
                    ),
                    "objective_revision": (
                        self.objectives.objective.revision
                        if self.objectives.objective is not None
                        else None
                    ),
                    "waiting_for_objective": (
                        self.config.on_objective_done == "hold"
                        and (
                            self.objectives.done or self.objectives.hold_waiting_reason is not None
                        )
                    ),
                }
            )

    @staticmethod
    def _default_observation_path(events: EventSink) -> str | None:
        directory = getattr(events, "directory", None)
        return str(directory / "adapter-observation.json") if directory is not None else None

    # -- public -----------------------------------------------------------------------------------

    def run(self) -> RunSummary:
        cfg = self.config
        started = self.clock()
        self._emit(
            EventType.RUNTIME_STARTED,
            details={
                "max_steps": cfg.max_steps,
                "deadline_s": cfg.deadline_s,
                "goal_id": cfg.goal_id,
                "no_progress_limit": cfg.no_progress_limit,
                **(
                    {
                        "objective_file": str(self.objectives.store.path),
                        "on_objective_done": cfg.on_objective_done,
                    }
                    if self.objectives is not None
                    else {}
                ),
            },
        )
        status = RunStatus.MAX_STEPS
        run_iterations = 0
        while run_iterations < cfg.max_steps:
            if self.stop_event.is_set():
                status = RunStatus.INTERRUPTED
                break
            if cfg.deadline_s is not None and self.clock() - started >= cfg.deadline_s:
                status = RunStatus.DEADLINE
                break
            was_holding = self._holding
            polling_completed_objective = (
                was_holding and self.objectives is not None and self.objectives.completed
            )
            held_revision = (
                self.objectives.objective.revision
                if self.objectives is not None and self.objectives.objective is not None
                else None
            )
            stop = self._iteration()
            # Polling a held objective keeps the run alive, but is not a decision
            # iteration and must not consume the max_steps budget.
            revision_changed = (
                self.objectives is not None
                and self.objectives.objective is not None
                and self.objectives.objective.revision != held_revision
            )
            if not polling_completed_objective or not self._holding or revision_changed:
                run_iterations += 1
                self.counters.iterations += 1
                self._run_after_iteration()
            if stop is not None:
                status = stop
                break
        reason, reason_details = self.stop_reason(status)
        reason_details = {**reason_details, "agent_frames": self.counters.agent_frames}
        self.counters.transient_invalid_settled = getattr(self.perceiver, "transient_settled", 0)
        self._emit(
            EventType.RUNTIME_STOPPED,
            outcome=status.value,
            details={
                "stop_reason": reason,
                **reason_details,
                "iterations": self.counters.iterations,
                "counters": self.counters.as_dict(),
            },
        )
        session = self.objectives
        return RunSummary(
            status,
            self.counters.iterations,
            self.counters.as_dict(),
            self.coverage.to_dict(),
            self._previous.scene_id if self._previous else None,
            objective=session.snapshot() if session is not None else None,
            game_cleared=self.game_cleared,
            stop_reason=reason,
            stop_details=reason_details,
            verdict=self._verdict,
        )

    def request_stop(self) -> None:
        """Request the next safe-boundary stop and wake a blocked decider, if supported."""
        self.stop_event.set()
        cancel = getattr(self.decider, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "decider cancellation failed during stop: %s", type(exc).__name__
                )

    def stop_reason(self, status: RunStatus) -> tuple[str, dict[str, Any]]:
        """The end-of-run stop reason in operator vocabulary (live findings 2026-10-18, the INFO
        line the CLI and ``run()`` print): ``objective_satisfied`` (the success probes are met —
        possibly already true at the start — with the objective id/revision),
        ``max_steps`` (with the count), ``interrupted``,
        ``deadline``, ``error`` (SAFE_PAUSE/PAUSED/STOPPED). A run that ends for another reason
        while its objective is already completed carries ``goal_met`` plus the ids."""
        if self._stop_reason_override is not None:
            return self._stop_reason_override
        if status is RunStatus.WAITING_OBJECTIVE and self.objectives is not None:
            ids = self._objective_ids()
            if self.objectives.completed:
                return "objective_satisfied", ids
            return "waiting_objective", ids
        details: dict[str, Any] = {}
        if self.objectives is not None and self.objectives.completed:
            details.update(self._objective_ids(), goal_met=True)
        if status is RunStatus.MAX_STEPS:
            return "max_steps", {
                **details,
                "steps": self.counters.iterations,
                "max_steps": self.config.max_steps,
            }
        if status is RunStatus.INTERRUPTED:
            return "interrupted", details
        if status is RunStatus.DEADLINE:
            return "deadline", details
        return "error", {**details, "status": status.value}

    def _run_after_iteration(self) -> None:
        for callback in self.after_iteration:
            try:
                callback(self.counters.iterations)
            except Exception as exc:  # an optional aid (checkpoints) must not stop the game
                self._emit(
                    EventType.RUNTIME_DEGRADED,
                    details={
                        "component": "after_iteration",
                        "reason": f"{type(exc).__name__}: {exc}"[:200],
                    },
                )

    # -- one iteration ----------------------------------------------------------------------------

    def _next_decision_id(self) -> str:
        self._decision_seq += 1
        return f"d-{self._decision_seq:06d}"

    def _perceive(self) -> Perception:
        perception = self.perceiver.perceive(
            self._next_decision_id(),
            self._previous,
            (*self._recent_failed, *self._warp_blocks, *self._dialog_blocks),
        )
        if perception.snapshot.emulator_frame is not None:
            self._frame_no = perception.snapshot.emulator_frame
        self._last_snapshot = perception.snapshot
        return perception

    def _iteration(self) -> RunStatus | None:
        if self.objectives is not None:
            stop = self._objective_refresh()
            if stop is not None:
                return stop
        try:
            perception = self._perceive()
        except Exception as exc:  # reader/actor failure: fail closed, no input
            self.counters.invalid_snapshots += 1
            self._emit(
                EventType.RUNTIME_PAUSED,
                outcome="safe_pause",
                details={
                    "reason": "SAFE_PAUSE",
                    "cause": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                },
            )
            return RunStatus.SAFE_PAUSE

        perception = self._guard_immediate_bike_off(perception)
        self._last_snapshot = perception.snapshot
        self._last_perception = perception

        if self.stop_event.is_set():
            return RunStatus.INTERRUPTED

        if perception.kind is PerceptionKind.INVALID:
            recovered = self._recover_invalid(perception)
            if recovered is None:
                return RunStatus.SAFE_PAUSE
            perception = recovered
            self._last_snapshot = perception.snapshot
            self._last_perception = perception

        observed_at = self.clock()
        progressed = self._progress.observe(
            semantic_progress_signature(perception.snapshot, perception.frame),
            now=observed_at,
            iteration=self.counters.iterations,
        )
        # Position changes reset the field monitor, but only executor-proven durable
        # progress may finish/rearm an agent recovery window (see _execute).
        if (
            self._hybrid_no_progress_active
            and self._hybrid_recovery_decisions >= self.config.no_progress_limit
        ):
            return self._report_no_progress("agent recovery window exhausted", perception.frame)
        if (
            not progressed
            and not (self.objectives is not None and self.objectives.done)
            and self._progress_tracks_scene(perception.scene.scene_id)
        ):
            due = self._progress.due_reason()
            if due is not None:
                return self._report_no_progress(due, perception.frame)

        scene = perception.scene
        self._note_scene(perception)
        self._observe_game_clear(perception)
        kind = perception.kind

        if self.objectives is not None:
            objective_before = self._objective_ids()
            stop = self._objective_observe(perception)
            if stop is not None:
                return stop
            if self._objective_ids() != objective_before or self.objectives.superseded():
                return None  # changed or cleared objective: re-perceive before offering input
            if self._objective_waits(perception):
                return self._wait_for_objective(perception)
            self._leave_hold()

        if self.objectives is not None and perception.frame is not None:
            blocked = perception.frame.context.get("blocked_route")
            fingerprint = perception.frame.state_fingerprint
            if isinstance(blocked, str) and fingerprint not in self._blocked_route_fingerprints:
                self._blocked_route_fingerprints[fingerprint] = None
                self._blocked_route_fingerprints.move_to_end(fingerprint)
                if len(self._blocked_route_fingerprints) > 256:
                    self._blocked_route_fingerprints.popitem(last=False)

        if kind is PerceptionKind.PAUSE:
            self._emit(
                EventType.RUNTIME_PAUSED,
                scene_id=scene.scene_id,
                outcome="safe_pause",
                details={"reason": "SAFE_PAUSE", "scene": scene.scene_id},
            )
            return RunStatus.SAFE_PAUSE
        if kind is PerceptionKind.BUSY:
            return self._handle_busy(perception)
        self._busy_streak = 0
        if kind is PerceptionKind.UNKNOWN:
            return self._handle_unknown(perception)
        self._unknown_streak = 0
        if kind is not PerceptionKind.NO_OPTIONS:
            self._no_options_streak = 0
        if kind is PerceptionKind.UNSUPPORTED or kind is PerceptionKind.NO_BUILDER:
            return self._handle_unsupported(perception)
        if kind is PerceptionKind.ESCALATE:
            return self._handle_escalate_scene(perception)
        if kind is PerceptionKind.NO_OPTIONS:
            return self._handle_no_options(perception)
        assert perception.frame is not None
        return self._handle_ready(perception)

    # -- invalid snapshot -------------------------------------------------------------------------

    def _recover_invalid(self, perception: Perception) -> Perception | None:
        reasons = [f"{e.subsystem}/{e.code}" for e in perception.snapshot.health.read_errors]
        self.counters.invalid_snapshots += 1
        transient = is_transient_invalid(
            perception.snapshot, getattr(self.perceiver, "transient_codes", frozenset())
        )
        if not transient:
            self._emit(
                EventType.RUNTIME_PAUSED,
                scene_id=perception.scene.scene_id,
                outcome="safe_pause",
                details={"reason": "SAFE_PAUSE", "read_errors": reasons},
            )
        for attempt in range(1, self.config.invalid_retries + 1):
            self._emit(
                EventType.RUNTIME_RECOVERING,
                details={"attempt": attempt, "action": "settle_and_reread"},
            )
            self.executor.settle(self.config.settle_frames)
            try:
                again = self._perceive()
            except Exception:
                continue
            if again.kind is not PerceptionKind.INVALID:
                self._emit(EventType.RUNTIME_RECOVERED, details={"attempts": attempt})
                self._emit(EventType.RUNTIME_RESUMED)
                return again
        self._emit(
            EventType.RUNTIME_BLOCKED,
            outcome="safe_pause",
            details={
                "reason": "snapshot stayed invalid",
                "read_errors": reasons,
                "retries": self.config.invalid_retries,
            },
        )
        if transient:
            self._emit(
                EventType.RUNTIME_PAUSED,
                scene_id=perception.scene.scene_id,
                outcome="safe_pause",
                details={"reason": "SAFE_PAUSE", "read_errors": reasons},
            )
        self._escalate(
            EscalationKind.SNAPSHOT_INVALID,
            perception.scene.scene_id,
            ErrorCode.E_SNAPSHOT_INVALID,
            {"read_errors": reasons},
            terminal=True,
        )
        return None

    # -- progress detection --------------------------------------------------------------------

    @staticmethod
    def _progress_tracks_scene(scene_id: str | None) -> bool:
        return scene_id == "field.free" or scene_id in _FIELD_MENU_SCENES

    def _finish_no_progress_recovery(self) -> None:
        if not self._hybrid_no_progress_active:
            return
        self._hybrid_no_progress_active = False
        self._hybrid_recovery_decisions = 0
        self._attempts.clear()
        self._postcondition_attempts.clear()
        self._rejected_choice_attempts.clear()
        notify = getattr(self.decider, "notify_progress", None)
        if callable(notify):
            notify()

    def _report_no_progress(self, reason: str, frame: DecisionFrame | None) -> RunStatus | None:
        """Report a detector verdict, then escalate through the bounded hybrid recovery gate."""
        scene_id = frame.scene_id if frame is not None else None
        self.counters.stuck += 1
        details = {
            "reason": reason,
            "decisions_since_progress": self._progress.decisions_since_progress,
            "last_progress_iteration": self._progress.last_progress_iteration,
            **self._objective_ids(),
        }
        self._stop_reason_override = ("no_progress", {"reason": reason, **details})
        self._verdict = "no_progress"
        self._emit(
            EventType.PROGRESS_STUCK,
            scene_id=scene_id,
            outcome="no_progress",
            details=details,
        )
        return self._escalate(
            EscalationKind.NO_PROGRESS,
            scene_id,
            ErrorCode.E_NO_PROGRESS,
            details,
        )

    def _stop_stuck(
        self,
        kind: EscalationKind,
        scene_id: str | None,
        code: ErrorCode,
        details: Mapping[str, Any],
    ) -> RunStatus | None:
        """Escalate a detector verdict through the bounded hybrid recovery gate."""
        if kind is EscalationKind.NO_PROGRESS:
            reason = str(details.get("reason") or "repeated action without progress")
            option_id = details.get("option_id")
            repeats = details.get("repeats")
            if option_id is not None:
                reason = f"{reason}: {option_id} ({repeats} repeats)"
            self._stop_reason_override = ("no_progress", {"reason": reason, **dict(details)})
            self._verdict = "no_progress"
        return self._escalate(kind, scene_id, code, details)

    # -- scene bookkeeping ------------------------------------------------------------------------

    def _note_scene(self, perception: Perception) -> None:
        scene = perception.scene
        if scene.scene_id != self._entered:
            if (
                self._entered is not None
                and self._entered.split(".", 1)[0] != scene.scene_id.split(".", 1)[0]
            ):
                # Failed executor metadata is only meaningful inside one scene
                # family. Do not suppress the next battle decision because a
                # prior action was interrupted by a dialog transition.
                self._recent_failed.clear()
            self._entered = scene.scene_id
            if scene.scene_id == "battle.ended":
                from pokegent.actions.battle.reader import battle_of
                from pokegent.actions.battle.state import OUTCOME_NAMES

                block = battle_of(perception.snapshot)
                outcome = OUTCOME_NAMES.get(block.outcome if block is not None else 0)
                event_type = {
                    "won": EventType.BATTLE_WON,
                    "lost": EventType.BATTLE_LOST,
                    "ran": EventType.BATTLE_FLED,
                    "caught": EventType.BATTLE_CAUGHT,
                }.get(outcome)
                if event_type is not None:
                    self._emit(
                        event_type,
                        scene_id=scene.scene_id,
                        outcome=outcome,
                        details={"battle_outcome": outcome},
                    )
            self._emit(
                EventType.SCENE_ENTERED,
                scene_id=scene.scene_id,
                details={
                    "confidence": scene.confidence,
                    "busy": scene.busy,
                    "detector": scene.detector,
                    "kind": perception.kind.value,
                },
            )
        if perception.kind not in (PerceptionKind.UNKNOWN, PerceptionKind.UNSUPPORTED):
            self._previous = scene

    # -- game clear -------------------------------------------------------------------------------

    def _observe_game_clear(self, perception: Perception) -> None:
        """``progress.game_cleared`` ONCE per run, at the first usable snapshot with
        ``FLAG_SYS_GAME_CLEAR`` set (it is set at the START of the Hall of Fame and stays set).
        ``at_start`` says the game was already cleared when this run began (a state saved after the
        ending). A broken watch is ``runtime.degraded``, never a stop."""
        watch = self._game_clear
        if watch is None or self.game_cleared:
            return
        try:
            if self._first_snapshot_seq is None:
                self._first_snapshot_seq = perception.snapshot.snapshot_seq
            fired = watch.observe(perception.snapshot)
        except Exception as exc:  # an optional fact must not stop the game
            self._game_clear = None
            self._emit(
                EventType.RUNTIME_DEGRADED,
                details={
                    "component": "game_clear_watch",
                    "reason": f"{type(exc).__name__}: {exc}"[:200],
                },
            )
            return
        if not fired:
            return
        self.game_cleared = True
        self._emit(
            EventType.PROGRESS_GAME_CLEARED,
            scene_id=perception.scene.scene_id,
            outcome="game_cleared",
            details={
                "flag": "FLAG_SYS_GAME_CLEAR",
                "snapshot_seq": perception.snapshot.snapshot_seq,
                "iteration": self.counters.iterations,
                "at_start": watch.first_snapshot_seq == self._first_snapshot_seq,
            },
        )

    # -- busy -------------------------------------------------------------------------------------

    def _handle_busy(self, perception: Perception) -> RunStatus | None:
        self.coverage.recognized(perception.scene.scene_id)
        self.counters.busy_waits += 1
        outcome = self.executor.wait_while_busy(
            self.config.busy_wait_frames, self.config.busy_wait_wall_s
        )
        if self.stop_event.is_set():
            return RunStatus.INTERRUPTED
        if outcome.satisfied:
            self._busy_streak = 0
            return None
        self._busy_streak += 1
        if self._busy_streak >= self.config.busy_stuck_limit:
            self.counters.stuck += 1
            self._emit(
                EventType.PROGRESS_STUCK,
                scene_id=perception.scene.scene_id,
                outcome="busy",
                details={
                    "busy_streak": self._busy_streak,
                    "waited_frames": outcome.frames,
                    **self._objective_ids(),
                },
            )
            self._busy_streak = 0
            return self._stop_stuck(
                EscalationKind.BUSY_STUCK,
                perception.scene.scene_id,
                ErrorCode.E_BUSY_STUCK,
                {"waited_frames": outcome.frames},
            )
        return None

    # -- unknown / unsupported / escalate-only / no options ---------------------------------------

    def _diagnostics(self, perception: Perception) -> dict[str, Any]:
        detection = perception.detection
        ui = perception.snapshot.ui
        return {
            "scene": perception.scene.scene_id,
            "evidence": dict(perception.scene.evidence),
            #: what the screen looks like, so an unknown scene can be classified without a replay
            "ui": (
                {
                    "callback": ui.callback_id,
                    "tasks": list(ui.task_signatures),
                    "menu": ui.menu_type,
                    "printer": ui.text_printer,
                    "controls_locked": ui.controls_locked,
                    "in_battle": ui.main.in_battle,
                }
                if ui is not None and ui.valid
                else None
            ),
            "claims": [
                {
                    "detector": c.detector,
                    "scene": c.result.scene_id,
                    "confidence": c.result.confidence,
                    "priority": int(c.priority),
                }
                for c in (detection.candidates if detection else ())
            ],
            "detector_errors": list(detection.errors) if detection else [],
            "confirmed_over": list(perception.confirmed_over),
            "state_hash": perception.snapshot.state_hash,
            "map": [perception.snapshot.player.map_group, perception.snapshot.player.map_num]
            if perception.snapshot.player
            else None,
        }

    def _handle_unknown(self, perception: Perception) -> RunStatus | None:
        self._unknown_streak += 1
        self.coverage.unknown(perception.scene.scene_id)
        self.counters.unknown_scenes += 1
        if self._unknown_streak == 1 or self._unknown_streak > self.config.unknown_retries:
            self._emit(
                EventType.SCENE_UNKNOWN,
                scene_id=perception.scene.scene_id,
                outcome="no_input",
                details={**self._diagnostics(perception), "streak": self._unknown_streak},
            )
        recover = getattr(self.decider, "request_unknown_scene_recovery", None)
        recovery_accepted = (
            not perception.scene.busy
            and perception.frame is not None
            and (
                self._hybrid_no_progress_active
                or self._unknown_streak > self.config.unknown_retries
            )
            and callable(recover)
            and recover()
        )
        # Only an accepted decision may bypass settles inside an active recovery window.
        # Busy fades/warps have no frame and still need the normal settle-and-reread budget.
        if not recovery_accepted and self._unknown_streak <= self.config.unknown_retries:
            self.executor.settle(self.config.settle_frames)  # may be a transition frame; no input
            return None
        self._unknown_streak = 0
        if recovery_accepted:
            if not self._hybrid_no_progress_active:
                self._hybrid_no_progress_active = True
                self._hybrid_recovery_decisions = 0
                self._progress.reset_window(now=self.clock(), iteration=self.counters.iterations)
            return self._handle_ready(perception)
        return self._escalate(
            EscalationKind.UNKNOWN_SCENE,
            perception.scene.scene_id,
            ErrorCode.E_SCENE_UNKNOWN,
            self._diagnostics(perception),
        )

    def _handle_unsupported(self, perception: Perception) -> RunStatus | None:
        no_builder = perception.kind is PerceptionKind.NO_BUILDER
        self.coverage.unsupported(perception.scene.scene_id)
        self.counters.unsupported_scenes += 1
        self._emit(
            EventType.SCENE_UNSUPPORTED,
            scene_id=perception.scene.scene_id,
            outcome="no_input",
            details={
                **self._diagnostics(perception),
                "reason": "no_builder" if no_builder else "catalog",
            },
        )
        return self._escalate(
            EscalationKind.NO_BUILDER if no_builder else EscalationKind.UNSUPPORTED_SCENE,
            perception.scene.scene_id,
            ErrorCode.E_NO_BUILDER if no_builder else ErrorCode.E_SCENE_UNSUPPORTED,
            self._diagnostics(perception),
        )

    def _handle_escalate_scene(self, perception: Perception) -> RunStatus | None:
        self.coverage.recognized(perception.scene.scene_id)
        self.counters.unknown_scenes += 1
        self._emit(
            EventType.SCENE_UNKNOWN,
            scene_id=perception.scene.scene_id,
            outcome="no_input",
            details={**self._diagnostics(perception), "reason": "escalation_only_scene"},
        )
        return self._escalate(
            EscalationKind.ESCALATE_SCENE,
            perception.scene.scene_id,
            ErrorCode.E_SCENE_UNKNOWN,
            self._diagnostics(perception),
        )

    def _handle_no_options(self, perception: Perception) -> RunStatus | None:
        frame = perception.frame
        assert frame is not None
        self.coverage.recognized(frame.scene_id)
        self._emit_options_built(perception)
        self._no_options_streak += 1
        if self._no_options_streak <= self.config.unknown_retries:
            self.executor.settle(self.config.settle_frames)
            return None
        self._no_options_streak = 0
        return self._escalate(
            EscalationKind.NO_LEGAL_OPTION,
            frame.scene_id,
            ErrorCode.E_NO_LEGAL_OPTION,
            {
                "diagnostics": [
                    {
                        "id": o.id,
                        "legality": o.legality.value,
                        **({"reason": o.facts["reason"]} if "reason" in o.facts else {}),
                    }
                    for o in frame.diagnostics
                ]
            },
        )

    # -- ready: decide and act --------------------------------------------------------------------

    def _emit_options_built(self, perception: Perception) -> None:
        frame = perception.frame
        assert frame is not None
        if frame.scene_id.startswith("battle."):
            self._last_battle_context = {
                key: frame.context[key]
                for key in ("your_pokemon", "opposing_pokemon", "your_party_able_to_battle")
                if key in frame.context
            }
        self._emit(
            EventType.OPTIONS_BUILT,
            scene_id=frame.scene_id,
            request_id=frame.decision_id,
            details={
                "chooseable": list(frame.chooseable_ids()),
                "diagnostics": [
                    {
                        "id": o.id,
                        "legality": o.legality.value,
                        **({"reason": o.facts["reason"]} if "reason" in o.facts else {}),
                    }
                    for o in frame.diagnostics
                ],
                "snapshot_seq": frame.snapshot_seq,
                "fingerprint": frame.state_fingerprint,
                "wrong_verbs": list(perception.wrong_verbs),
            },
        )

    def _publish_observation(
        self,
        frame: DecisionFrame,
        *,
        stuck_kind: str | None = None,
        details: Mapping[str, Any] | None = None,
        verification: Mapping[str, Mapping[str, Any]] | None = None,
        payload_leak: Mapping[str, str] | None = None,
    ) -> None:
        """Publish the shared sidecar from frame data already held by the runner."""
        if self._observation_path is None:
            return
        self._observation_seq += 1
        stuck = (
            StuckObservation(kind=stuck_kind, details=dict(details or {}))
            if stuck_kind is not None
            else None
        )
        try:
            loss = None
            if frame.scene_id == WHITEOUT_SCENE_ID and self._last_battle_context:
                member = self._last_battle_context.get("your_pokemon")
                opponent = self._last_battle_context.get("opposing_pokemon")
                member_match = re.search(r"Lv(\d+)", str(member))
                opponent_match = re.search(r"Lv(\d+)", str(opponent))
                loss = {
                    "reason": "e4_blackout",
                    "member": member,
                    "opponent": opponent,
                    "levels": {
                        "member": int(member_match.group(1)) if member_match else None,
                        "opponent": int(opponent_match.group(1)) if opponent_match else None,
                    },
                }
            write_observation(
                self._observation_path,
                observation_from_frame(
                    frame,
                    seq=self._observation_seq,
                    stuck=stuck,
                    verification=verification,
                    # Verification/stuck writes describe the completed action's frame.  Reusing
                    # the last OPTIONS_BUILT model would publish a stale menu after a scene or
                    # snapshot transition.
                    payload_leak=payload_leak,
                    loss=loss,
                ),
            )
        except ObservationTooLarge as exc:
            self._emit(
                EventType.RUNTIME_DEGRADED,
                scene_id=frame.scene_id,
                request_id=frame.decision_id,
                outcome=exc.diagnostic,
                details={"diagnostic": exc.diagnostic},
            )
        except OSError as exc:
            self._emit(
                EventType.RUNTIME_DEGRADED,
                scene_id=frame.scene_id,
                request_id=frame.decision_id,
                details={"component": "adapter_observation", "reason": str(exc)[:200]},
            )
        except Exception as exc:
            self.counters.observation_errors += 1
            error_type = type(exc)
            if error_type not in self._observation_logged_errors:
                self._observation_logged_errors.add(error_type)
                logging.getLogger(__name__).warning(
                    "adapter observation failed (%s): %s", error_type.__name__, str(exc)[:200]
                )
            self._emit(
                EventType.RUNTIME_DEGRADED,
                scene_id=frame.scene_id,
                request_id=frame.decision_id,
                details={"component": "adapter_observation", "reason": error_type.__name__},
            )

    def _handle_ready(self, perception: Perception) -> RunStatus | None:
        frame = perception.frame
        assert frame is not None
        self._last_frame = frame
        update_briefing = getattr(self.decider, "update_briefing", None)
        if callable(update_briefing):
            update_briefing(
                perception.snapshot,
                tables=getattr(self.perceiver, "tables", None),
                world=getattr(self.perceiver, "world", None),
                events=self._briefing_events(),
                frame=frame,
            )
        self.coverage.recognized(frame.scene_id)
        if perception.wrong_verbs:
            self.coverage.wrong_options(frame.scene_id, len(perception.wrong_verbs))
        self._emit_options_built(perception)
        # Pure waits and animation settles are observations, not agent choices.  They may run
        # without a decider; every action-bearing option still belongs to the decider.
        forced = frame.forced_option()
        if forced is not None and not perception.scene.is_unknown:
            spec = self.executor.executors.get(forced.executor_ref)
            pure_wait = (
                spec is not None and (spec.kind.value == "deterministic" or spec.autoplay_safe)
            ) or forced.facts.get("inputs") == 0
            if pure_wait:
                return self._act(
                    frame,
                    forced.id,
                    source="autoplay",
                    autoplay_reason="pure_non_decision",
                )
        with self._objective_lock:
            objective = self.objectives.objective if self.objectives is not None else None
            self._pending_objective_key = (
                (objective.objective_id, objective.revision) if objective is not None else None
            )
        chosen = self._decide(frame)
        if isinstance(chosen, RunStatus):
            return chosen
        if chosen is None:
            return None
        frame, option_id = chosen
        self.counters.decided += 1
        return self._act(frame, option_id, source="decider")

    @staticmethod
    def _player_position(snapshot: StateSnapshot) -> tuple[int, int, int, int, int] | None:
        state = snapshot.map
        player = state.player if state is not None else None
        if state is None or player is None:
            return None
        return (
            state.identity.group,
            state.identity.number,
            player.avatar_x,
            player.avatar_y,
            player.elevation,
        )

    def _guard_immediate_bike_off(self, perception: Perception) -> Perception:
        """Do not bounce straight back off a newly mounted bike on the same tile."""
        if self._last_bike_on_position is None or perception.frame is None:
            return perception
        if self._player_position(perception.snapshot) != self._last_bike_on_position:
            self._last_bike_on_position = None
            return perception
        bike_off = perception.frame.get("bike_off")
        if bike_off is None or bike_off not in perception.frame.chooseable():
            return perception
        facts = dict(bike_off.facts)
        facts["repetition"] = "bike_on was just chosen; no tile movement followed"
        blocked = bike_off.model_copy(
            update={
                "legality": Legality.UNAVAILABLE,
                "facts": facts,
                "executor_ref": None,
                "postcondition_ref": None,
            }
        )
        frame = perception.frame.model_copy(
            update={
                "options": tuple(o for o in perception.frame.options if o.id != "bike_off"),
                "diagnostics": (*perception.frame.diagnostics, blocked),
            }
        )
        return replace(
            perception,
            frame=frame,
            kind=(PerceptionKind.READY if frame.chooseable() else PerceptionKind.NO_OPTIONS),
        )

    def _briefing_events(self) -> list[dict[str, Any]]:
        """Return the incrementally maintained notable event view."""
        return list(self._briefing_public_events)

    def _decide(self, frame: DecisionFrame) -> tuple[DecisionFrame, str] | RunStatus | None:
        """The frame paired with its validated option, ``None``, or a terminal status."""
        try:
            to_system_one_payload(frame)  # fail closed: a leaky builder never reaches a provider
        except PayloadLeakError as exc:
            leak = exc.as_dict()
            logging.getLogger(__name__).warning(
                "System One payload leak: %s=%s", leak["kind"], leak["token"]
            )
            self._publish_observation(frame, payload_leak=leak)
            self._emit(
                EventType.DECISION_SYSTEMONE_INVALID,
                scene_id=frame.scene_id,
                request_id=frame.decision_id,
                outcome=ErrorCode.E_PAYLOAD_LEAK.value,
                details=leak,
            )
            return self._escalate(
                EscalationKind.DECIDER_FAILED,
                frame.scene_id,
                ErrorCode.E_PAYLOAD_LEAK,
                {"payload_leak": leak},
            )
        shown = frame
        self._emit(
            EventType.DECISION_SYSTEMONE_REQUESTED,
            scene_id=frame.scene_id,
            request_id=frame.decision_id,
            details={
                "criteria": list(shown.chooseable_ids()),
                "deadline_ms": frame.deadline_ms,
            },
        )
        result = None
        last_error: Exception | None = None
        outage_started: float | None = None
        outage_attempt = 0
        attempt = 0
        while True:
            attempt += 1
            t0 = self.clock()
            try:
                result = self.decider.decide(shown)
                elapsed_ms = (self.clock() - t0) * 1000.0
                take_frame = getattr(self.decider, "take_decided_frame", None)
                if callable(take_frame):
                    adopted = take_frame()
                    if adopted is not None:
                        shown = adopted
                        frame = adopted
                break
            except DeciderTimeout as exc:
                last_error = exc
                self._emit(
                    EventType.DECISION_SYSTEMONE_TIMEOUT,
                    scene_id=frame.scene_id,
                    request_id=frame.decision_id,
                    details={"attempt": attempt + 1},
                )
                if self.stop_event.is_set():
                    return RunStatus.INTERRUPTED
                if "interrupted" in str(exc).lower():
                    return RunStatus.INTERRUPTED
                if self._is_system1_transient(exc):
                    # ``pause`` controls the outage policy after the configured decision
                    # retries.  It must not turn a transient provider failure into a
                    # zero-retry path merely because the first attempt was slow.
                    if (
                        self.config.system1_outage == "pause"
                        and attempt < 1 + self.config.decider_retries
                        and not self._is_provider_timeout(exc)
                    ):
                        continue
                    retry, outage_started, outage_attempt = self._wait_for_system1_outage(
                        frame,
                        exc,
                        outage_started,
                        outage_attempt,
                        # A ProviderTimeout gets at least one explicit outage wait even when
                        # pause is configured; subsequent failures still honor that policy.
                        force_wait=self._is_provider_timeout(exc) and outage_attempt == 0,
                    )
                    if retry:
                        continue
                    if self.stop_event.is_set():
                        return RunStatus.INTERRUPTED
                    break
                if getattr(self.decider, "safe_pause_on_timeout", False):
                    return RunStatus.SAFE_PAUSE
                if attempt >= 1 + self.config.decider_retries:
                    break
            except PayloadLeakError as exc:
                last_error = exc
                leak = exc.as_dict()
                logging.getLogger(__name__).warning(
                    "System One payload leak: %s=%s", leak["kind"], leak["token"]
                )
                self._publish_observation(frame, payload_leak=leak)
                self._emit(
                    EventType.DECISION_SYSTEMONE_INVALID,
                    scene_id=frame.scene_id,
                    request_id=frame.decision_id,
                    outcome=ErrorCode.E_PAYLOAD_LEAK.value,
                    details=leak,
                )
                break
            except Exception as exc:  # DeciderError or a provider bug: no retry on a hard failure
                last_error = exc
                from addons.system_one.provider import ProviderConfigurationError

                if isinstance(exc, ProviderConfigurationError):
                    details = {"reason": "provider_configuration", "error": str(exc),
                               "status": exc.detail.get("status"),
                               "provider_reason": exc.detail.get("reason")}
                    self._verdict = "owner_review"
                    self._stop_reason_override = ("provider_configuration", details)
                    self._emit(EventType.RUNTIME_BLOCKED, outcome="provider_configuration",
                               details=details)
                    return RunStatus.SAFE_PAUSE
                if self.stop_event.is_set():
                    return RunStatus.INTERRUPTED
                if self._is_system1_transient(exc):
                    retry, outage_started, outage_attempt = self._wait_for_system1_outage(
                        frame, exc, outage_started, outage_attempt
                    )
                    if retry:
                        continue
                    if self.stop_event.is_set():
                        return RunStatus.INTERRUPTED
                    break
                self._emit(
                    EventType.DECISION_SYSTEMONE_INVALID,
                    scene_id=frame.scene_id,
                    request_id=frame.decision_id,
                    outcome=ErrorCode.E_DECIDER_FAILED.value,
                    details={"error": f"{type(exc).__name__}: {exc}"},
                )
                break
        if self.stop_event.is_set():
            return RunStatus.INTERRUPTED
        if result is None:
            code = (
                ErrorCode.E_DECIDER_TIMEOUT
                if isinstance(last_error, DeciderTimeout)
                else ErrorCode.E_PAYLOAD_LEAK
                if isinstance(last_error, PayloadLeakError)
                else ErrorCode.E_DECIDER_FAILED
            )
            details = (
                {"payload_leak": last_error.as_dict()}
                if isinstance(last_error, PayloadLeakError)
                else {"error": str(last_error)}
            )
            return self._escalate(EscalationKind.DECIDER_FAILED, frame.scene_id, code, details)

        problem = check_choice(frame, result, allow_all=result.provider_id in {"agent", "external"})
        if problem is not None:
            self.counters.invalid_choices += 1
            self._invalid_choice_streak += 1
            self._emit(
                EventType.DECISION_SYSTEMONE_INVALID,
                scene_id=frame.scene_id,
                request_id=frame.decision_id,
                provider=result.provider_id or None,
                outcome=problem.value,
                details={"option_id": result.option_id, "chooseable": list(frame.chooseable_ids())},
            )
            self._emit(
                EventType.OPTION_REJECTED,
                scene_id=frame.scene_id,
                request_id=frame.decision_id,
                outcome=problem.value,
                details={"option_id": result.option_id, "source": "decider"},
            )
            if self._invalid_choice_streak >= self.config.invalid_choice_limit:
                self._invalid_choice_streak = 0
                return self._escalate(
                    EscalationKind.DECIDER_FAILED,
                    frame.scene_id,
                    problem,
                    {"option_id": result.option_id},
                )
            return None
        self._invalid_choice_streak = 0
        last_decision = getattr(self.decider, "last_decision", None)
        last_decision = last_decision if isinstance(last_decision, dict) else {}
        decided_by = last_decision.get("decided_by") or (
            "agent" if result.provider_id in {"agent", "external"} else "system1"
        )
        if decided_by == "agent":
            self.counters.agent_frames += 1
        self._emit(
            EventType.DECISION_AGENT_COMPLETED
            if decided_by == "agent"
            else EventType.DECISION_SYSTEMONE_COMPLETED,
            scene_id=frame.scene_id,
            request_id=frame.decision_id,
            provider=result.provider_id or None,
            outcome="accepted",
            details={
                "option_id": result.option_id,
                "model_id": result.model_id,
                "latency_ms": result.latency_ms,
                "measured_ms": round(elapsed_ms, 3),
                "late": elapsed_ms > frame.deadline_ms,
                "confidence": result.confidence,
                "truncated": result.truncated,
                **(
                    {"decided_by": last_decision["decided_by"]}
                    if last_decision.get("decided_by")
                    else {}
                ),
                # A hybrid route-step veto replaces the System One claim; say so, and keep the
                # claim that was not followed in the event instead of a fabricated confidence.
                **(
                    {"vetoed_by": last_decision["vetoed_by"]}
                    if last_decision.get("vetoed_by")
                    else {}
                ),
                **(
                    {
                        "system1_option_id": last_decision.get("system1_option_id"),
                        "system1_confidence": last_decision.get("system1_confidence"),
                    }
                    if "system1_option_id" in last_decision
                    and (decided_by == "agent" or last_decision.get("vetoed_by"))
                    else {}
                ),
            },
        )
        if outage_attempt:
            self._emit(
                EventType.DECISION_SYSTEMONE_RECOVERED,
                scene_id=frame.scene_id,
                request_id=frame.decision_id,
                provider="system1",
                outcome="recovered",
                details={"attempts": outage_attempt},
            )
        return frame, result.option_id

    @staticmethod
    def _is_system1_transient(exc: Exception) -> bool:
        try:
            from addons.system_one.provider import CircuitOpen, ProviderTimeout, ProviderUnavailable

            transient = (CircuitOpen, ProviderTimeout, ProviderUnavailable)
        except ImportError:
            transient = ()
        return isinstance(exc, transient + (TimeoutError, ConnectionError))

    @staticmethod
    def _is_provider_timeout(exc: Exception) -> bool:
        try:
            from addons.system_one.provider import ProviderTimeout
        except ImportError:
            return False
        return isinstance(exc, ProviderTimeout)

    def _wait_for_system1_outage(
        self,
        frame: DecisionFrame,
        exc: Exception,
        started: float | None,
        attempt: int,
        *,
        force_wait: bool = False,
    ) -> tuple[bool, float, int]:
        """Wait before retrying the frozen frame; no emulator input means its fingerprint is safe.

        The emulator cannot advance without an input, so retrying this exact decision frame cannot
        select against a changed game state. A finite outage budget is checked again before sleep.
        """
        started = self.clock() if started is None else started
        attempt += 1
        elapsed = self.clock() - started
        max_outage = self.config.system1_max_outage_s
        if (self.config.system1_outage == "pause" and not force_wait) or (
            max_outage > 0 and elapsed >= max_outage
        ):
            self._emit(
                EventType.DECISION_SYSTEMONE_INVALID,
                scene_id=frame.scene_id,
                request_id=frame.decision_id,
                provider="system1",
                outcome=ErrorCode.E_DECIDER_FAILED.value,
                details={"error": f"{type(exc).__name__}: {exc}"},
            )
            return False, started, attempt
        delay = min(30.0, 2.0 ** min(attempt - 1, 5))
        if max_outage > 0:
            delay = min(delay, max_outage - elapsed)
        if delay <= 0:
            self._emit(
                EventType.DECISION_SYSTEMONE_INVALID,
                scene_id=frame.scene_id,
                request_id=frame.decision_id,
                provider="system1",
                outcome=ErrorCode.E_DECIDER_FAILED.value,
                details={"error": f"{type(exc).__name__}: outage budget exceeded"},
            )
            return False, started, attempt
        self._emit(
            EventType.DECISION_SYSTEMONE_WAITING,
            scene_id=frame.scene_id,
            request_id=frame.decision_id,
            provider="system1",
            outcome="waiting",
            public_visibility=True,
            details={
                "attempt": attempt,
                "next_retry_s": delay,
                "error": type(exc).__name__,
                "elapsed_s": round(elapsed, 3),
            },
        )
        if not self._sleep_interruptible(delay):
            if self.stop_event.is_set():
                return False, started, attempt
            self._emit(
                EventType.DECISION_SYSTEMONE_INVALID,
                scene_id=frame.scene_id,
                request_id=frame.decision_id,
                provider="system1",
                outcome=ErrorCode.E_DECIDER_FAILED.value,
                details={"error": f"{type(exc).__name__}: outage budget exceeded"},
            )
            return False, started, attempt
        return True, started, attempt

    def _sleep_interruptible(self, seconds: float) -> bool:
        deadline = self.clock() + seconds
        while True:
            if self.stop_event.is_set():
                return False
            remaining = deadline - self.clock()
            if remaining <= 0:
                return True
            self._sleep(min(0.1, remaining))

    def _goal_view(self, frame: DecisionFrame):
        """The perceiver's goal view when it is the frame's goal (``None`` otherwise)."""
        view_of = getattr(self.perceiver, "goal_view", None)
        if not callable(view_of):
            return None
        try:
            view = view_of()
        except Exception:  # a broken goal source is goal-less (see Perceiver.goal_view)
            return None
        return view if view is not None and view.goal_id == frame.goal_id else None

    def _goal_key(self, frame: DecisionFrame) -> tuple[str | None, int | None]:
        """Which objective (id, revision) a frame was built under; reset when it changes."""
        view = self._goal_view(frame)
        return frame.goal_id, (view.revision if view is not None else None)

    def _act(
        self,
        frame: DecisionFrame,
        option_id: str,
        *,
        source: str,
        autoplay_reason: str | None = None,
    ) -> RunStatus | None:
        if self.stop_event.is_set():
            return RunStatus.INTERRUPTED
        if option_id == "bike_on":
            self._last_bike_on_position = (
                self._player_position(self._last_snapshot) if self._last_snapshot else None
            )
        else:
            self._last_bike_on_position = None
        option = frame.get(option_id)
        self._emit(
            EventType.ACTION_STARTED,
            scene_id=frame.scene_id,
            request_id=frame.decision_id,
            details={
                "option_id": option_id,
                "source": source,
                "executor_ref": getattr(option, "executor_ref", None),
            },
        )
        mark_autoplay = getattr(self.decider, "mark_autoplay", None)
        if source == "autoplay" and callable(mark_autoplay):
            mark_autoplay(frame, option_id, autoplay_reason or "autoplay")
        try:
            # A pending external frame may have been rebuilt by the HTTP thread.  Keep the
            # objective session at that frame's revision while executor revalidation performs
            # its fresh perception; otherwise the field fingerprint can be compared against the
            # previous objective after the rebuilt frame has already been adopted.
            with self._objective_lock:
                if self.objectives is not None:
                    refreshed = self.objectives.refresh()
                    self._report_objective_warnings(refreshed)
                result = self.executor.execute(frame, option_id)
        except Exception as exc:  # an executor bug must not become an input or a crash loop
            result = ActionResult.rejected(
                ErrorCode.E_EMULATOR,
                f"{type(exc).__name__}: {exc}",
                decision_id=frame.decision_id,
                option_id=option_id,
            )
        if self.executor.last_emulator_frame is not None:
            self._frame_no = self.executor.last_emulator_frame
        option = frame.get(option_id)
        if self._progress_tracks_scene(frame.scene_id) or self._hybrid_no_progress_active:
            self._progress.note_decision(
                verified=(
                    result.status is ActionStatus.APPLIED
                    and result.verified_outcome is True
                    or result.progressed is True
                    or self._interrupted_by_battle(result)
                ),
            )
        self._record_dialog_block(frame, option_id, result, source=source)
        self._record_result(frame, option_id, result)
        notify = getattr(self.decider, "record_action_result", None)
        clear_autoplay_pending = getattr(self.decider, "clear_autoplay_pending", None)
        try:
            if callable(notify):
                notify(frame, result)
        finally:
            if callable(clear_autoplay_pending):
                clear_autoplay_pending()
        action_failed = result.status is not ActionStatus.APPLIED
        self._publish_observation(
            frame,
            stuck_kind="action_failed" if action_failed else None,
            details=(
                {
                    "option_id": option_id,
                    "status": result.status.value,
                    "error_code": result.error_code.value if result.error_code else None,
                    "reason": result.reason,
                    **(
                        {"failure_reason": result.failure_reason}
                        if result.failure_reason is not None
                        else {}
                    ),
                }
                if action_failed
                else None
            ),
            verification={
                option_id: {
                    "status": (
                        "passed"
                        if result.verified_outcome is True
                        else "failed"
                        if result.status is not ActionStatus.APPLIED
                        else "pending"
                    ),
                    "reason": result.reason,
                }
            },
        )

        if result.progressed is True:
            self._finish_no_progress_recovery()
        if self._hybrid_no_progress_active and source == "decider":
            self._hybrid_recovery_decisions += 1

        rejected_key = (
            (frame.scene_id, option_id, result.error_code)
            if result.status is ActionStatus.REJECTED and result.error_code in STALE_CODES
            else None
        )
        if rejected_key is not None:
            repeats = self._rejected_choice_attempts.get(rejected_key, 0) + 1
            self._rejected_choice_attempts[rejected_key] = repeats
            if repeats >= 3:
                self._rejected_choice_attempts.pop(rejected_key, None)
                self.counters.stuck += 1
                self._emit(
                    EventType.PROGRESS_STUCK,
                    scene_id=frame.scene_id,
                    request_id=frame.decision_id,
                    outcome="rejected_choice",
                    details={"option_id": option_id, "repeats": repeats, **self._objective_ids()},
                )
                self._publish_observation(
                    frame, stuck_kind="edges_exhausted", details={"option_id": option_id}
                )
                return self._stop_stuck(
                    EscalationKind.NO_PROGRESS,
                    frame.scene_id,
                    ErrorCode.E_NO_PROGRESS,
                    {"option_id": option_id, "repeats": repeats},
                )
        elif result.status is ActionStatus.APPLIED:
            for key in tuple(self._rejected_choice_attempts):
                if key[:2] == (frame.scene_id, option_id):
                    self._rejected_choice_attempts.pop(key, None)

        scope = (self._goal_key(frame), frame.context.get("map"))
        if scope != self._postcondition_scope:
            self._postcondition_scope = scope
            self._postcondition_attempts.clear()

        if result.error_code is ErrorCode.E_POSTCONDITION_NOT_MET:
            postcondition_key = (frame.scene_id, option_id)
            option = frame.get(option_id)
            limit = option.facts.get("postcondition_failure_limit") if option else None
            if self._note_postcondition_attempt(postcondition_key, limit=limit):
                self.counters.stuck += 1
                self._emit(
                    EventType.PROGRESS_STUCK,
                    scene_id=frame.scene_id,
                    request_id=frame.decision_id,
                    outcome="postcondition_failure",
                    details={
                        "option_id": option_id,
                        "repeats": self.config.no_progress_limit,
                        "scope": "objective+map",
                        **self._objective_ids(),
                    },
                )
                self._publish_observation(
                    frame,
                    stuck_kind="failed_postcondition",
                    details={"option_id": option_id},
                )
                self._postcondition_attempts.pop(postcondition_key, None)
                return self._stop_stuck(
                    EscalationKind.NO_PROGRESS,
                    frame.scene_id,
                    ErrorCode.E_NO_PROGRESS,
                    {"option_id": option_id, "repeats": self.config.no_progress_limit},
                )
        elif result.status is ActionStatus.APPLIED:
            self._postcondition_attempts.pop((frame.scene_id, option_id), None)

        if result.progressed is True:
            self._attempts.clear()
            self._progress.reset_window(now=self.clock(), iteration=self.counters.iterations)

        text_wait_changed_state = (
            option is not None
            and option.executor_ref in {"dialog.wait_for_text", "dialog.advance_text"}
            and result.status is ActionStatus.APPLIED
            and result.verified_outcome is True
            and result.before_hash is not None
            and result.after_hash is not None
            and result.before_hash != result.after_hash
        )
        if source == "decider" and result.progressed is not True and not text_wait_changed_state:
            target_key = option_target_key(option) if option is not None else option_id
            key = (frame.scene_id, option_id, target_key, frame.state_fingerprint)
            if self._note_attempt(key):
                self.counters.stuck += 1
                self._emit(
                    EventType.PROGRESS_STUCK,
                    scene_id=frame.scene_id,
                    request_id=frame.decision_id,
                    outcome="no_progress",
                    details={
                        "option_id": option_id,
                        "fingerprint": frame.state_fingerprint,
                        "repeats": self.config.no_progress_limit,
                        "window": self.config.no_progress_window,
                        **self._objective_ids(),
                    },
                )
                self._publish_observation(
                    frame, stuck_kind="edges_exhausted", details={"option_id": option_id}
                )
                self._attempts.pop(key, None)
                return self._stop_stuck(
                    EscalationKind.NO_PROGRESS,
                    frame.scene_id,
                    ErrorCode.E_NO_PROGRESS,
                    {"option_id": option_id, "repeats": self.config.no_progress_limit},
                )
        return None

    def _note_attempt(self, key: tuple[str, str, str]) -> bool:
        """Record one attempt; True when the key reached ``no_progress_limit`` inside the window."""
        cfg = self.config
        history = self._attempts.setdefault(key, deque())
        history.append(self.counters.iterations)
        while history and self.counters.iterations - history[0] >= cfg.no_progress_window:
            history.popleft()
        if len(self._attempts) > 512:  # bounded memory: drop the oldest-touched keys
            for old in list(self._attempts)[:128]:
                if old != key:
                    self._attempts.pop(old, None)
        return len(history) >= cfg.no_progress_limit

    def _note_postcondition_attempt(
        self, key: tuple[str, str], *, limit: int | None = None
    ) -> bool:
        """Count failures until this option succeeds or its objective/map scope changes."""
        count = self._postcondition_attempts.get(key, 0) + 1
        self._postcondition_attempts[key] = count
        return count >= (limit if limit is not None else self.config.no_progress_limit)

    @staticmethod
    def _is_dialog_block(result: ActionResult) -> bool:
        return (
            result.status is ActionStatus.INTERRUPTED
            and result.error_code is ErrorCode.E_INTERRUPTED_SCENE
            and result.progressed is False
            and result.dialog_fingerprint is not None
        )

    @staticmethod
    def _interrupted_by_battle(result: ActionResult) -> bool:
        """A field macro handing control to a battle made meaningful progress."""
        return (
            result.status is ActionStatus.INTERRUPTED
            and result.error_code is ErrorCode.E_INTERRUPTED_SCENE
            and (result.interrupted_scene or "").startswith("battle.")
        )

    def _record_dialog_block(
        self,
        frame: DecisionFrame,
        option_id: str,
        result: ActionResult,
        *,
        source: str,
    ) -> None:
        """Keep consecutive dialog-only decider outcomes across their forced dialog scenes."""
        if source != "decider":
            return
        if not self._is_dialog_block(result):
            return
        option = frame.get(option_id)
        record = {
            "option_id": option_id,
            "scene_id": frame.scene_id,
            "state_fingerprint": frame.state_fingerprint,
            "status": result.status.value,
            "error_code": result.error_code.value,
            "executor_ref": frame.get(option_id).executor_ref if frame.get(option_id) else None,
            "progressed": result.progressed,
            "dialog_fingerprint": result.dialog_fingerprint,
            "target_key": option_target_key(option) if option is not None else option_id,
            "goal_key": self._goal_key(frame),
            "relevant_state": relevant_state_signature(self._last_snapshot),
        }
        self._dialog_blocks.append(record)

    def _record_result(self, frame: DecisionFrame, option_id: str, result: ActionResult) -> None:
        for index, step in enumerate(result.input_trace):
            if step.kind == "tap":
                self._emit(
                    EventType.ACTION_INPUT,
                    scene_id=frame.scene_id,
                    request_id=frame.decision_id,
                    details={
                        "index": index,
                        "buttons": list(step.buttons),
                        "hold_frames": step.hold_frames,
                        "wait_frames": step.wait_frames,
                    },
                )
        base = {
            "option_id": option_id,
            "status": result.status.value,
            "error_code": result.error_code.value if result.error_code else None,
            "reason": result.reason,
            "elapsed_frames": result.elapsed_frames,
            "before_hash": result.before_hash,
            "after_hash": result.after_hash,
            "verified": result.verified_outcome,
            "progressed": result.progressed,
            "dialog_fingerprint": result.dialog_fingerprint,
            "dialog_text": list(result.dialog_text),
            "inputs": sum(1 for s in result.input_trace if s.kind == "tap"),
        }
        status = result.status
        if status is ActionStatus.APPLIED:
            self._scene_escalations.clear()
            self.counters.actions_applied += 1
            if result.verified_outcome:
                self.counters.actions_verified += 1
                self.coverage.verified(frame.scene_id)
            self._emit(
                EventType.ACTION_VERIFIED,
                scene_id=frame.scene_id,
                request_id=frame.decision_id,
                outcome=status.value,
                details=base,
            )
            return
        if not self._is_dialog_block(result):
            option = frame.get(option_id)
            record = {
                "option_id": option_id,
                "scene_id": frame.scene_id,
                "state_fingerprint": frame.state_fingerprint,
                "status": status.value,
                "error_code": result.error_code.value if result.error_code else "",
                "executor_ref": option.executor_ref if option is not None else None,
                "progressed": result.progressed,
                "dialog_fingerprint": result.dialog_fingerprint,
                "interrupted_scene": result.interrupted_scene,
                "failure_reason": result.failure_reason or result.reason,
                "target_key": option_target_key(option) if option is not None else option_id,
                "goal_key": self._goal_key(frame),
                "relevant_state": relevant_state_signature(self._last_snapshot),
            }
            if (
                frame.scene_id == "field.free"
                and option_id.startswith("warp:")
                and "map_unchanged" in str(record["failure_reason"])
            ):
                self._warp_blocks.append(record)
            else:
                self._recent_failed.append(record)
        if status is ActionStatus.REJECTED:
            self.counters.actions_rejected += 1
            if result.error_code in STALE_CODES:
                self.coverage.stale(frame.scene_id)
            self._emit(
                EventType.OPTION_REJECTED,
                scene_id=frame.scene_id,
                request_id=frame.decision_id,
                outcome=status.value,
                details=base,
            )
        elif status is ActionStatus.INTERRUPTED:
            self.counters.actions_interrupted += 1
            self._emit(
                EventType.ACTION_INTERRUPTED,
                scene_id=frame.scene_id,
                request_id=frame.decision_id,
                outcome=status.value,
                details=base,
            )
        else:
            self.counters.actions_failed += 1
            self._emit(
                EventType.ACTION_FAILED,
                scene_id=frame.scene_id,
                request_id=frame.decision_id,
                outcome=status.value,
                details=base,
            )

    # -- escalation -------------------------------------------------------------------------------

    def _escalate(
        self,
        kind: EscalationKind,
        scene_id: str | None,
        code: ErrorCode | None,
        details: Mapping[str, Any],
        *,
        terminal: bool = False,
    ) -> RunStatus | None:
        self.counters.escalations += 1
        self.coverage.escalated(scene_id or "scene.unknown")
        self._emit(
            EventType.ESCALATION_REQUESTED,
            scene_id=scene_id,
            outcome=kind.value,
            details={
                "kind": kind.value,
                "error_code": code.value if code else None,
                **details,
            },
        )
        if kind is EscalationKind.NO_PROGRESS and not terminal:
            recover = getattr(self.decider, "request_no_progress_recovery", None)
            if self._hybrid_no_progress_active:
                # A second exhausted window must stop even if a custom sink says CONTINUE.
                terminal = True
            elif callable(recover) and recover():
                self._hybrid_no_progress_active = True
                self._hybrid_recovery_decisions = 0
                self._attempts.clear()
                self._postcondition_attempts.clear()
                self._rejected_choice_attempts.clear()
                self._progress.reset_window(now=self.clock(), iteration=self.counters.iterations)
                self._stop_reason_override = None
                self._verdict = "other"
                return None
        request = EscalationRequest(kind, scene_id, self.counters.iterations, code, dict(details))
        response = self._ask_sink(request)
        if terminal:
            return RunStatus.SAFE_PAUSE
        status = self._apply_response(kind, response)
        if status is None and kind in _CANNOT_ACT_KINDS and self.config.scene_escalation_budget:
            key = scene_id or "scene.unknown"
            n = self._scene_escalations[key] = self._scene_escalations.get(key, 0) + 1
            if n >= self.config.scene_escalation_budget:
                self._emit(
                    EventType.RUNTIME_BLOCKED,
                    scene_id=scene_id,
                    outcome="scene_escalation_budget",
                    details={"kind": kind.value, "escalations": n, "scene": key},
                )
                return RunStatus.SAFE_PAUSE
        return status

    def _ask_sink(self, request: EscalationRequest) -> EscalationResponse:
        try:
            return self.escalation.escalate(request)
        except Exception as exc:  # a broken sink must not turn into inputs: pause
            self._emit(
                EventType.RUNTIME_DEGRADED,
                details={"escalation_sink": f"{type(exc).__name__}: {exc}"},
            )
            return EscalationResponse(EscalationAction.PAUSE, "sink failed")

    def _apply_response(
        self, kind: EscalationKind, response: EscalationResponse
    ) -> RunStatus | None:
        if response.action is EscalationAction.CONTINUE:
            return None
        if response.action is EscalationAction.STOP:
            self._emit(EventType.RUNTIME_BLOCKED, outcome="stopped", details={"kind": kind.value})
            return RunStatus.STOPPED
        if self._verdict == "other":
            self._verdict = "escalated"
        if self._stop_reason_override is None:
            self._stop_reason_override = ("escalated", {"kind": kind.value})
        if bool(
            getattr(self.decider, "supervisor_control", False)
            or getattr(self.decider, "external_control", False)
        ):
            self._emit(
                EventType.RUNTIME_PAUSED,
                outcome="safe_pause",
                details={"reason": "escalation", "kind": kind.value},
            )
            return RunStatus.SAFE_PAUSE
        self._emit(
            EventType.RUNTIME_BLOCKED,
            outcome="escalated",
            details={"kind": kind.value},
        )
        return RunStatus.ESCALATED

    # -- objectives (contracts v1.3) --------------------------------------------------------------

    def _objective_sink(self, event_type: str, details: Mapping[str, Any]) -> None:
        """The tracker's ``progress.*`` events go to the event log like any runner event."""
        self._emit(EventType(event_type), source="objective", details=dict(details))

    def _objective_ids(self) -> dict[str, Any]:
        obj = self.objectives.objective if self.objectives is not None else None
        return {} if obj is None else {"objective_id": obj.objective_id, "revision": obj.revision}

    def _objective_refresh(self) -> RunStatus | None:
        """Start of an iteration: read the file, report what was wrong with it, fix the goal."""
        session = self.objectives
        assert session is not None
        with self._objective_lock:
            refreshed = session.refresh()
            self._report_objective_warnings(refreshed)
        return None

    def _report_objective_warnings(self, refreshed: Any) -> None:
        for warning in refreshed.warnings:
            self.counters.objective_warnings += 1
            if is_objective_rejection(warning):
                field = warning.reasons[0].field if warning.reasons else "objective"
                print(
                    f"WARN objective rejected: {warning.code} {field}".rstrip(),
                    file=sys.stderr,
                    flush=True,
                )
            self._emit(
                EventType.RUNTIME_DEGRADED,
                source="objective",
                outcome=warning.code,
                details={
                    "component": "objective_store",
                    "code": warning.code,
                    "detail": warning.detail,
                    "revision": warning.revision,
                    "reasons": [r.as_dict() for r in warning.reasons],
                    "kept": (
                        self.objectives.objective.objective_id
                        if self.objectives is not None and self.objectives.objective
                        else None
                    ),
                },
            )

    def _refresh_pending_frame(self, frame: DecisionFrame) -> DecisionFrame | None:
        """Rebuild a pending decision against the same RAM snapshot after a goal revision."""
        with self._objective_lock:
            if self.objectives is None or self._last_snapshot is None:
                return None
            refreshed = self.objectives.refresh()
            self._report_objective_warnings(refreshed)
            objective = self.objectives.objective
            objective_key = (
                (objective.objective_id, objective.revision) if objective is not None else None
            )
            if objective_key == self._pending_objective_key:
                return None
            rebuilt = self.perceiver._perceive(
                frame.decision_id,
                self._previous,
                (),
                confirm=False,
                snapshot=self._last_snapshot,
            ).frame
            if rebuilt is not None:
                self._pending_objective_key = objective_key
            return rebuilt

    def _objective_observe(self, perception: Perception) -> RunStatus | None:
        session = self.objectives
        assert session is not None
        snapshot = perception.snapshot
        self._last_snapshot = snapshot
        with self._objective_lock:
            session.observe(
                snapshot,
                safe_boundary=self._objective_safe_boundary(perception),
                clear_completed=self.config.on_objective_done == "continue",
            )  # completion is recorded only at the safe boundary
        return None

    @staticmethod
    def _objective_safe_boundary(perception: Perception) -> bool:
        """Fresh-RAM idle scene with known non-battle controls: the only handoff boundary."""
        if perception.kind not in (PerceptionKind.READY, PerceptionKind.NO_OPTIONS):
            return False
        if perception.scene.scene_id not in IDLE_SCENE_IDS:
            return False
        snapshot = perception.snapshot
        if not snapshot.usable:
            return False
        battle = getattr(snapshot, "battle", None)
        if getattr(battle, "active", False):
            return False
        ui = snapshot.ui
        if ui is not None:
            if not ui.valid or ui.main.in_battle is not False:
                return False
        return True

    def _objective_waits(self, perception: Perception) -> bool:
        """A finished objective does not start a new free-roaming decision."""
        session = self.objectives
        if session is None or self.config.on_objective_done == "continue":
            return False
        if not session.done and session.hold_waiting_reason is None:
            return False
        if self.config.ignore_objective and session.completed:
            return False  # headless opt-out: a satisfied success probe is not a stop
        return self._objective_safe_boundary(perception)

    def _wait_for_objective(self, perception: Perception) -> RunStatus | None:
        session = self.objectives
        assert session is not None
        obj = session.objective
        info = {
            "reason": "waiting_objective",
            "objective_id": obj.objective_id if obj else None,
            "revision": obj.revision if obj else None,
            "completed": session.completed,
            "on_objective_done": self.config.on_objective_done,
        }
        if session.hold_waiting_reason is not None:
            info["reason"] = "WAITING_OBJECTIVE"
            info["detail"] = session.hold_waiting_reason
        scene_id = perception.scene.scene_id
        if self.config.on_objective_done == "pause":
            self._emit(
                EventType.RUNTIME_PAUSED,
                scene_id=scene_id,
                outcome="waiting_objective",
                details=info,
            )
            return RunStatus.WAITING_OBJECTIVE
        # hold: alive, no input, one bounded look at the file per iteration
        self.counters.objective_idle += 1
        if not self._holding:
            self._holding = True
            self._emit(EventType.RUNTIME_PAUSED, scene_id=scene_id, outcome="holding", details=info)
        if self.config.hold_idle_frames:
            self.executor.settle(self.config.hold_idle_frames)
        if self.config.hold_poll_s:
            self._sleep(self.config.hold_poll_s)
        return None

    def _leave_hold(self) -> None:
        if self._holding:
            self._holding = False
            self._emit(EventType.RUNTIME_RESUMED, details={"reason": "objective_changed"})

    # -- events -----------------------------------------------------------------------------------

    def _emit(self, type: EventType, **kw: Any) -> None:
        kw.setdefault("emulator_frame", self._frame_no)
        self.events.emit(type, **kw)
        event_type = type.value
        scene = kw.get("scene_id")
        kind = (
            event_type.removeprefix("battle.")
            if event_type in {"battle.caught", "battle.won", "battle.lost"}
            else "milestone"
            if event_type == "progress.milestone_completed"
            else "faint"
            if scene == "battle.faint"
            else "level_up"
            if scene == "battle.level_up"
            else None
        )
        if kind:
            self._briefing_public_events.append({"kind": kind, "outcome": kw.get("outcome")})
