"""Perceive: snapshot -> scene -> frame. The one place that wires reader, detectors and builders.

Both the runner (to decide) and the executor (to re-validate right before an input) call the same
:class:`Perceiver`, so "is this decision still valid" is answered by the same code that created
the decision, never by a second, drifting implementation.

``perceive`` never presses anything. It may advance a few frames through ``settle`` to confirm a
scene over stable snapshots (spec 4.4), which is observation, not input.

Transient invalid reads (WP1c): the game decrypts a
party member in place while a party or summary screen draws, so the reader reports
``party/mon_bad_checksum`` for one or two frames. Every read the Perceiver makes goes through
:meth:`Perceiver._read`, which retries such a read one settled frame later, at most
``transient_retries`` (default 4) times in a row. Only a snapshot whose errors are ALL in
``transient_codes`` is retried; any other invalid reason is returned at once. A snapshot that
stays invalid is returned unchanged (the runner then pauses), nothing is repaired or substituted,
and no input is ever sent on an invalid read: the retry is observation (``settle``), never input.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.goals.source import GoalSource, GoalView
from pokegent.options.models import DecisionFrame, Option, freeze
from pokegent.options.ranking import rank_field_options
from pokegent.options.registry import BuildContext, BuilderRegistry
from pokegent.state.catalog import PAUSE_SCENE_IDS, SceneCatalog
from pokegent.state.scenes import (
    ConfirmPolicy,
    Detection,
    DetectorRegistry,
    SceneCoverageReport,
    SceneResult,
    confirm_scene,
    readable,
)


class SnapshotSource(Protocol):
    """Anything that yields a fresh, atomic :class:`StateSnapshot` (never a stale cached one)."""

    def read_snapshot(self) -> StateSnapshot: ...


class IncrementalSnapshotSource(SnapshotSource, Protocol):
    """A source that can cheaply say that decision-relevant RAM did not change.

    ``None`` means the last fully decoded snapshot is still current for executor polling.  A
    returned value is always a newly decoded, full snapshot; callers must never use the cheap
    probe itself for a decision or a verification.
    """

    def read_snapshot_if_changed(self, *, mode: str = "wait") -> StateSnapshot | None: ...


#: Read errors (``subsystem``, ``code``) that can be observed while FireRed relocates or
#: updates its save blocks. Validation still rejects these values; this only permits a bounded
#: observation retry before the runner treats the read as persistent corruption.
TRANSIENT_INVALID_CODES: frozenset[tuple[str, str]] = frozenset(
    {
        ("party", "mon_bad_checksum"),
        ("save_blocks", "pointer_changed"),
        ("player", "money_out_of_range"),
        ("player", "coins_out_of_range"),
        ("bag", "bag_slot_invalid"),
        ("bag", "pc_item_slot_invalid"),
    }
)
#: Consecutive invalid frames tolerated before the read counts as real (WP1c: longest seen is 2).
DEFAULT_TRANSIENT_RETRIES = 4
DEFAULT_MEANINGFUL_ALTERNATIVES_CAP = 15
logger = logging.getLogger(__name__)


def is_transient_invalid(
    snapshot: StateSnapshot,
    codes: frozenset[tuple[str, str]] = TRANSIENT_INVALID_CODES,
) -> bool:
    """True for an invalid snapshot whose every read error is a known transient one."""
    if snapshot.usable:
        return False
    errors = snapshot.health.read_errors
    return bool(errors) and all((e.subsystem, e.code) in codes for e in errors)


class LiveSnapshotSource:
    """Adapt the FireRed ``LiveReader`` to the :class:`SnapshotSource` protocol."""

    def __init__(self, live_reader: Any) -> None:
        self._live = live_reader

    def read_snapshot(self) -> StateSnapshot:
        return self._live.read().snapshot


class PerceptionKind(str, Enum):
    INVALID = "invalid"  # snapshot unusable -> SAFE_PAUSE, no input
    PAUSE = "pause"  # scene whose only answer is a controlled pause (unsupported ROM)
    UNKNOWN = "unknown"  # no/ambiguous/unstable evidence -> diagnostics, no input
    UNSUPPORTED = "unsupported"  # recognized, deliberately not implemented (P2 etc.)
    ESCALATE = "escalate"  # scene whose catalog vocabulary is escalation only
    BUSY = "busy"  # game not processing input -> bounded wait
    NO_BUILDER = "no_builder"  # recognized, but no option builder is registered for its family
    NO_OPTIONS = "no_options"  # builder ran, nothing chooseable
    READY = "ready"  # frame with at least one chooseable option


@dataclass(frozen=True)
class Observation:
    snapshot: StateSnapshot
    scene: SceneResult


@dataclass(frozen=True)
class Perception:
    kind: PerceptionKind
    snapshot: StateSnapshot
    scene: SceneResult
    frame: DecisionFrame | None = None
    detection: Detection | None = None
    #: option verbs the builder emitted that the scene's catalog vocabulary does not contain
    wrong_verbs: tuple[str, ...] = ()
    confirmed_over: tuple[str, ...] = ()


def _option_sort_key(option: Any) -> tuple[int, int, str]:
    """Preserve structural UI order: travel before field prompts, Yes before No."""
    facts = option.facts
    target = facts.get("target")
    target_kind = target.get("kind") if isinstance(target, Mapping) else None
    transition = target_kind in {"warp", "edge", "ladder", "elevator", "door", "stairs"}
    affirmative = facts.get("prompt_kind") == "yes_no" and facts.get("affirmative") is True
    return (0 if transition else 1, 0 if affirmative else 1, option.id)


class Perceiver:
    def __init__(
        self,
        source: SnapshotSource,
        detectors: DetectorRegistry,
        builders: BuilderRegistry,
        *,
        run_id: str = "",
        goal_id: str | None = None,
        goal: GoalSource | None = None,
        catalog: SceneCatalog | None = None,
        confirm: ConfirmPolicy | None = None,
        settle: Callable[[int], None] | None = None,
        coverage: SceneCoverageReport | None = None,
        transient_retries: int = DEFAULT_TRANSIENT_RETRIES,
        transient_codes: frozenset[tuple[str, str]] = TRANSIENT_INVALID_CODES,
        max_visible_options: int = DEFAULT_MEANINGFUL_ALTERNATIVES_CAP,
        tables: Any = None,
        world: Any = None,
        pre_game_callbacks: Collection[str] | None = None,
        allow_unknown_scene: bool = False,
    ) -> None:
        if transient_retries < 0:
            raise ValueError("transient_retries must be >= 0")
        self.source = source
        self.detectors = detectors
        self.builders = builders
        self.run_id = run_id
        #: the fixed goal id of the pre-v1.3 call sites; a ``goal`` source wins while it has a goal
        self.goal_id = goal_id
        #: asked ONCE per perception (contracts v1.3): the goal can change while the run goes on
        self.goal_source = goal
        self.catalog = catalog if catalog is not None else detectors.catalog
        self.confirm = confirm
        self.settle = settle  # bound by the executor (the only frame advancer) if left None
        self.coverage = coverage
        self.transient_retries = transient_retries
        self.transient_codes = frozenset(transient_codes)
        if (
            isinstance(max_visible_options, bool)
            or not isinstance(max_visible_options, int)
            or not 1 <= max_visible_options <= 50
        ):
            raise ValueError("max_visible_options must be an integer from 1 to 50")
        self.max_visible_options = max_visible_options
        self.tables = tables
        self.world = world
        self._pre_game_mode = pre_game_callbacks is not None
        self._pre_game_active = self._pre_game_mode
        self._pre_game_callbacks = frozenset(pre_game_callbacks or ())
        self.allow_unknown_scene = allow_unknown_scene
        #: diagnostics: one-frame settles spent on transient invalid reads, reads that recovered,
        #: and the longest run of consecutive transient invalid frames that recovered
        self.transient_settled = 0
        self.transient_recovered = 0
        self.transient_longest_run = 0
        #: read-only taps (the viewer): each is called with every :class:`Perception` this
        #: perceiver returns, after it is built; an exception in one is swallowed
        self.observers: list[Callable[[Perception], None]] = []

    def bind_settle(self, settle: Callable[[int], None]) -> None:
        if self.settle is None:
            self.settle = settle

    def bind_goal(self, goal: GoalSource) -> None:
        if self.goal_source is None:
            self.goal_source = goal

    # -- the goal of THIS perception --------------------------------------------------------

    def goal_view(self, snapshot: StateSnapshot | None = None) -> GoalView | None:
        """The goal a frame built now carries: the goal source's, else the fixed ``goal_id``.
        A broken source is goal-less for that perception, never a crash."""
        if self.goal_source is not None:
            current_for = getattr(self.goal_source, "current_for", None)
            if snapshot is not None and callable(current_for):
                try:
                    return current_for(snapshot)
                except Exception:
                    return None
            try:
                view = self.goal_source.current()
            except Exception:
                view = None
            if view is not None:
                return view
        return GoalView(self.goal_id) if self.goal_id else None

    # -- the one read path ------------------------------------------------------------------

    def _read(self) -> StateSnapshot:
        """Read a snapshot; look again one frame later while it is a TRANSIENT invalid read.

        At most ``transient_retries`` settles, so at most ``transient_retries + 1`` reads; the
        snapshot returned is always a real read (never a cached or repaired one). Without a bound
        ``settle`` (a standalone Perceiver) there is no way to advance, so nothing is retried.
        """
        snapshot = self.source.read_snapshot()
        tries = 0
        while (
            tries < self.transient_retries
            and self.settle is not None
            and is_transient_invalid(snapshot, self.transient_codes)
        ):
            self.settle(1)  # observation, never input: the executor advances the frame
            self.transient_settled += 1
            tries += 1
            snapshot = self.source.read_snapshot()
        if tries and snapshot.usable:
            self.transient_recovered += 1
            self.transient_longest_run = max(self.transient_longest_run, tries)
        return snapshot

    def _read_if_changed(self, *, mode: str = "wait") -> StateSnapshot | None:
        """Use an optional source-side RAM probe, falling back to an ordinary full read."""
        reader = getattr(self.source, "read_snapshot_if_changed", None)
        if not callable(reader):
            return self._read()
        snapshot = reader() if mode == "wait" else reader(mode=mode)
        if snapshot is None:
            return None
        # A transient invalid candidate still gets the same bounded retry policy as every other
        # perception read.  This is rare, so the extra full read is preferable to duplicating the
        # source's incremental state machine here.
        if is_transient_invalid(snapshot, self.transient_codes):
            return self._read()
        return snapshot

    # -- observe: snapshot + scene, nothing else --------------------------------------------

    def _detect_all(
        self, snapshot: StateSnapshot, previous: SceneResult | None = None
    ) -> Detection:
        if snapshot.usable and self._pre_game_active:
            self._pre_game_active = False
        callbacks = None
        if self._pre_game_mode:
            callbacks = self._pre_game_callbacks if self._pre_game_active else frozenset()
        return self.detectors.detect_all(snapshot, previous, pre_game_callbacks=callbacks)

    def _detect(self, snapshot: StateSnapshot, previous: SceneResult | None = None) -> SceneResult:
        return self._detect_all(snapshot, previous).result

    def observe(self, previous: SceneResult | None = None) -> Observation:
        snapshot = self._read()
        return Observation(snapshot, self._detect(snapshot, previous))

    def observe_if_changed(
        self, previous: SceneResult | None = None, *, mode: str = "wait"
    ) -> Observation | None:
        """Return a full observation only when the source's cheap RAM probe changed.

        Sources without the optional probe retain the original behavior and always return a full
        observation.  The executor periodically forces :meth:`observe` as a safety refresh.
        """
        snapshot = self._read_if_changed(mode=mode)
        if snapshot is None:
            return None
        return Observation(snapshot, self._detect(snapshot, previous))

    def probe_hint_matches(self, hint: tuple[str, object]) -> bool | None:
        """Ask an optional source-side probe whether a macro polling hint is satisfied."""
        probe = getattr(self.source, "probe_hint_matches", None)
        return probe(hint) if callable(probe) else None

    # -- perceive: the full router front half ----------------------------------------------

    def perceive(
        self,
        decision_id: str,
        previous: SceneResult | None = None,
        recent_failed: tuple[Mapping[str, Any], ...] = (),
        *,
        confirm: bool = True,
    ) -> Perception:
        perception = self._perceive(decision_id, previous, recent_failed, confirm=confirm)
        for observer in self.observers:
            try:
                observer(perception)
            except Exception:  # noqa: BLE001 - observation never influences the router
                pass
        return perception

    def _perceive(
        self,
        decision_id: str,
        previous: SceneResult | None,
        recent_failed: tuple[Mapping[str, Any], ...],
        *,
        confirm: bool,
        snapshot: StateSnapshot | None = None,
    ) -> Perception:
        snapshot = self._read() if snapshot is None else snapshot
        detection = self._detect_all(snapshot, previous)
        scene = detection.result
        # an invalid core is a normal scene only when a detector that accepts it claimed valid UI
        # evidence (the reboot after the credits, docs/contracts.md v1.8); never otherwise
        if scene.is_invalid or not readable(snapshot, scene):
            return Perception(PerceptionKind.INVALID, snapshot, scene, detection=detection)
        if scene.is_unknown and (not self.allow_unknown_scene or scene.busy):
            return Perception(PerceptionKind.UNKNOWN, snapshot, scene, detection=detection)
        if scene.scene_id in PAUSE_SCENE_IDS:
            return Perception(PerceptionKind.PAUSE, snapshot, scene, detection=detection)
        catalog = self.catalog
        if catalog is not None:
            if catalog.is_unsupported(scene.scene_id):
                return Perception(PerceptionKind.UNSUPPORTED, snapshot, scene, detection=detection)
            spec = catalog.get(scene.scene_id)
            if spec is not None and spec.escalation_only:
                return Perception(PerceptionKind.ESCALATE, snapshot, scene, detection=detection)
        if scene.busy:
            return Perception(PerceptionKind.BUSY, snapshot, scene, detection=detection)

        confirmed: tuple[str, ...] = ()
        if confirm and self.confirm is not None and self.confirm.needs(scene):
            settle_frames = self.confirm.settle_frames
            result = confirm_scene(
                snapshot,
                scene,
                read=self._read,
                detect=lambda s: self._detect(s, scene),
                settle=(lambda: self.settle(settle_frames)) if self.settle else None,
                required=self.confirm.required,
                max_reads=self.confirm.max_reads,
            )
            snapshot, scene, confirmed = result.snapshot, result.scene, result.observed
            if not result.stable:
                return Perception(
                    PerceptionKind.UNKNOWN,
                    snapshot,
                    scene,
                    detection=detection,
                    confirmed_over=confirmed,
                )
            if not readable(snapshot, scene):
                return Perception(
                    PerceptionKind.INVALID,
                    snapshot,
                    SceneResult.invalid(
                        [f"{e.subsystem}/{e.code}" for e in snapshot.health.read_errors]
                    ),
                    detection=detection,
                    confirmed_over=confirmed,
                )

        goal = self.goal_view(snapshot)
        ctx = BuildContext(
            snapshot=snapshot,
            scene=scene,
            run_id=self.run_id,
            goal_id=goal.goal_id if goal else None,
            goal_revision=goal.revision if goal else None,
            recent_failed=recent_failed,
            objective=(goal.summary or None) if goal else None,
            objective_kind=goal.kind if goal else None,
            objective_item=goal.target_item if goal else None,
            objective_quantity=goal.target_quantity if goal else None,
            forbid=goal.forbid if goal else (),
        )
        frame = self.builders.build(ctx, decision_id)
        if frame is None:
            return Perception(
                PerceptionKind.NO_BUILDER,
                snapshot,
                scene,
                detection=detection,
                confirmed_over=confirmed,
            )
        frame = self._with_item_storage_fact(frame, snapshot)
        wrong = self._wrong_verbs(frame)
        kind = PerceptionKind.READY if frame.chooseable() else PerceptionKind.NO_OPTIONS
        if scene.is_unknown:
            kind = PerceptionKind.UNKNOWN
        return Perception(
            kind,
            snapshot,
            scene,
            frame=frame,
            detection=detection,
            wrong_verbs=wrong,
            confirmed_over=confirmed,
        )

    def _with_item_storage_fact(
        self, frame: DecisionFrame, snapshot: StateSnapshot
    ) -> DecisionFrame:
        """Attach the live bag-capacity fact used by PC options."""
        if not snapshot.usable:
            return frame
        item_storage_reason = next(
            (
                f"{pocket.pocket} pocket is full"
                for pocket in snapshot.bag
                if len(pocket.items) >= pocket.capacity
            ),
            None,
        )

        def with_item_storage_fact(option: Option) -> Option:
            target = option.facts.get("target")
            if not (
                item_storage_reason and isinstance(target, Mapping) and target.get("kind") == "pc"
            ):
                return option
            return option.model_copy(
                update={
                    "facts": freeze(
                        {**dict(option.facts), "item_storage_reason": item_storage_reason}
                    )
                }
            )

        options, all_options = self._meaningful_alternatives(
            frame.scene_id,
            tuple(with_item_storage_fact(option) for option in frame.options),
            tuple(with_item_storage_fact(option) for option in frame.all_options),
            max_visible_options=getattr(
                self, "max_visible_options", DEFAULT_MEANINGFUL_ALTERNATIVES_CAP
            ),
        )
        return frame.model_copy(
            update={
                "options": options,
                "all_options": all_options,
            }
        )

    @staticmethod
    def _meaningful_alternatives(
        scene_id,
        options,
        all_options,
        *,
        max_visible_options=DEFAULT_MEANINGFUL_ALTERNATIVES_CAP,
    ):
        """Apply the count cap while preserving the builders' non-policy order."""
        if not (scene_id.startswith("field.") or scene_id == "dialog.multichoice"):
            return options, all_options
        combined = tuple({option.id: option for option in (*options, *all_options)}.values())
        if scene_id == "dialog.multichoice" or any(
            option.facts.get("menu") in {"pc_menu", "pc_storage"} for option in combined
        ):
            return combined, ()
        return rank_field_options(
            scene_id,
            combined,
            None,
            max_visible_options=max_visible_options,
        )

    def _wrong_verbs(self, frame: DecisionFrame) -> tuple[str, ...]:
        if self.catalog is None:
            return ()
        spec = self.catalog.get(frame.scene_id)
        if spec is None or not spec.verbs:
            return ()  # composite/descriptive rows (battle.wild ...) list no own vocabulary
        allowed = set(spec.verbs)
        return tuple(
            o.id
            for o in (*frame.options, *frame.diagnostics)
            if o.verb is not None and o.verb not in allowed
        )
