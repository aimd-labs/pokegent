"""Scene detection contract (spec 4.4, 6.0, 6.K): results, detectors, priority and conflict rules.

Detectors are small, pure functions of one :class:`StateSnapshot`. They never press anything and
never talk to the emulator. :class:`DetectorRegistry` combines their claims into exactly one
:class:`SceneResult` by the rules of spec 6.0:

1. an invalid snapshot is ``system.invalid`` (priority class SYSTEM) and nothing else is run. One
   deliberate exception (v1.8, the reboot after the credits): a detector that declares
   ``accepts_invalid_core = True`` runs on an invalid snapshot whose UI evidence is valid, and
   only those detectors run. A claim by them (same ranking rules among themselves) is a scene
   marked ``core_invalid``; nothing claimed stays ``system.invalid``;
2. candidates are ranked by priority class (6.0 items 1 to 8), then by specificity (a specific
   scene id beats a generic one), then by confidence; registration order breaks exact ties so the
   result is deterministic;
3. contradictory evidence resolves conservatively to ``scene.unknown``: a tie between equally
   specific, equally confident claims (unless the previous confirmed scene says which successor is
   expected), a top claim below ``min_confidence``, a weak top claim (below
   ``strong_confidence``) against a more confident claim of a lower class, a detector that raises,
   or a scene id that is not in the catalog;
4. a confident BUSY-class claim next to a ready scene marks the result ``busy`` (wait, do not
   press): "ready" only if the game really processes input (spec 6.0, last sentence).

No randomness, no wall clock.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from pokegent.frozen import FrozenMap
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.state.catalog import (
    RESERVED_SCENE_IDS,
    SCENE_INVALID,
    SCENE_UNKNOWN,
    SceneCatalog,
)


class PriorityClass(IntEnum):
    """Spec 6.0 items 1 to 8. A LOWER number wins."""

    SYSTEM = 1  # emulator/ROM/RAM invalid
    MODAL = 2  # naming, move-learn/evolution, YES/NO, PC context, shop quantity, save, quest log
    BATTLE = 3  # decided by the active controller phase
    MENU = 4  # start, bag, TM case, party, summary, pokedex, PC, shop, ferry, elevator, fly
    FIELD_DIALOG = 5  # field dialog / script interaction (even with controls not locked)
    FIELD = 6  # controllable overworld and field moves
    BUSY = 7  # animation, fade, warp phases
    UNKNOWN = 8


class SceneResult(BaseModel):
    """One detector's claim, or the registry's verdict. Immutable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scene_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: FrozenMap = Field(default_factory=dict)
    #: True while the game is not processing input (animation, fade, script movement).
    busy: bool = False
    #: Name of the detector that produced it ("" for registry verdicts).
    detector: str = ""
    #: True when the core reader was invalid and a detector that declares ``accepts_invalid_core``
    #: claimed the scene from valid UI evidence alone (the game just rebooted; contracts.md v1.8).
    core_invalid: bool = False

    @classmethod
    def unknown(cls, reason: str, **evidence: Any) -> SceneResult:
        return cls(scene_id=SCENE_UNKNOWN, confidence=0.0, evidence={"reason": reason, **evidence})

    @classmethod
    def invalid(cls, reasons: Iterable[str]) -> SceneResult:
        return cls(scene_id=SCENE_INVALID, confidence=1.0, evidence={"read_errors": list(reasons)})

    @property
    def is_unknown(self) -> bool:
        return self.scene_id == SCENE_UNKNOWN

    @property
    def is_invalid(self) -> bool:
        return self.scene_id == SCENE_INVALID


@runtime_checkable
class SceneDetector(Protocol):
    name: str
    priority: PriorityClass
    specificity: int  # higher = more specific scene id (dialog.starter_choice > dialog.yes_no)

    def detect(self, snapshot: StateSnapshot) -> SceneResult | None:
        """The claim for this snapshot, or ``None`` if the detector does not apply."""
        ...


def accepts_invalid_core(detector: object) -> bool:
    """Optional detector attribute ``accepts_invalid_core`` (default False): the detector needs only
    valid UI evidence and may claim a scene while the core snapshot is invalid (a soft reset clears
    the save-block pointers). Only ``True`` counts, nothing truthy."""
    return getattr(detector, "accepts_invalid_core", False) is True


def accepts_pre_game_core(detector: object) -> bool:
    """Whether ``detector`` may claim a mapped cold-boot scene before an avatar exists."""
    return getattr(detector, "accepts_pre_game_core", False) is True


def readable(snapshot: StateSnapshot, scene: SceneResult) -> bool:
    """The snapshot may back an action: a usable one, or an invalid core with a ``core_invalid``
    claim (valid UI evidence + a detector that accepts it). The one rule the perceiver, the
    executor's re-validation and its step boundaries share."""
    return snapshot.usable or (scene.core_invalid and not scene.is_invalid)


class FunctionDetector:
    """Adapter: a plain function as a :class:`SceneDetector` (tests, small detectors)."""

    def __init__(
        self,
        name: str,
        priority: PriorityClass,
        fn: Callable[[StateSnapshot], SceneResult | None],
        *,
        specificity: int = 0,
    ) -> None:
        self.name = name
        self.priority = priority
        self.specificity = specificity
        self._fn = fn

    def detect(self, snapshot: StateSnapshot) -> SceneResult | None:
        return self._fn(snapshot)


@dataclass(frozen=True)
class Candidate:
    detector: str
    priority: PriorityClass
    specificity: int
    result: SceneResult


@dataclass(frozen=True)
class Detection:
    """Registry verdict plus everything that led to it (diagnostics for events)."""

    result: SceneResult
    candidates: tuple[Candidate, ...] = ()
    errors: tuple[str, ...] = ()


class DetectorRegistry:
    def __init__(
        self,
        detectors: Iterable[SceneDetector] = (),
        *,
        catalog: SceneCatalog | None = None,
        min_confidence: float = 0.6,
        strong_confidence: float = 0.8,
        ambiguity_margin: float = 0.1,
        successors: Mapping[str, Collection[str]] | None = None,
    ) -> None:
        self._detectors: list[SceneDetector] = []
        self.catalog = catalog
        self.min_confidence = min_confidence
        self.strong_confidence = strong_confidence
        self.ambiguity_margin = ambiguity_margin
        self.successors = {k: frozenset(v) for k, v in (successors or {}).items()}
        for d in detectors:
            self.register(d)

    def register(self, detector: SceneDetector) -> None:
        if any(d.name == detector.name for d in self._detectors):
            raise ValueError(f"detector {detector.name!r} is already registered")
        self._detectors.append(detector)

    @property
    def detectors(self) -> tuple[SceneDetector, ...]:
        return tuple(self._detectors)

    # -- detection --------------------------------------------------------------------------

    def detect(
        self,
        snapshot: StateSnapshot,
        previous: SceneResult | None = None,
        *,
        pre_game_callbacks: Collection[str] | None = None,
    ) -> SceneResult:
        return self.detect_all(snapshot, previous, pre_game_callbacks=pre_game_callbacks).result

    def detect_all(
        self,
        snapshot: StateSnapshot,
        previous: SceneResult | None = None,
        *,
        pre_game_callbacks: Collection[str] | None = None,
    ) -> Detection:
        if not snapshot.usable:
            return self._detect_invalid_core(snapshot, previous, pre_game_callbacks)
        return self._detect(snapshot, previous, self._detectors)

    def _detect_invalid_core(
        self,
        snapshot: StateSnapshot,
        previous: SceneResult | None,
        pre_game_callbacks: Collection[str] | None,
    ) -> Detection:
        """Invalid core: ``system.invalid`` unless the UI block is valid and a detector that
        ``accepts_invalid_core`` claims a scene from it (then that scene, marked ``core_invalid``).
        """
        reasons = [f"{e.subsystem}/{e.code}" for e in snapshot.health.read_errors]
        ui = snapshot.ui
        if pre_game_callbacks is None:
            eligible = [d for d in self._detectors if accepts_invalid_core(d)]
        else:
            callback = (
                ui.main.callback2.name if ui is not None and ui.main.callback2 is not None else None
            )
            eligible = (
                [d for d in self._detectors if accepts_pre_game_core(d)]
                if callback in pre_game_callbacks
                else []
            )
        if not eligible or ui is None or not ui.valid:
            return Detection(SceneResult.invalid(reasons))
        detection = self._detect(snapshot, previous, eligible)
        if detection.result.is_unknown or detection.result.is_invalid:
            return Detection(SceneResult.invalid(reasons), detection.candidates, detection.errors)
        flag = {"core_invalid": True}
        evidence = {**detection.result.evidence, "core_read_errors": tuple(reasons)}
        return Detection(
            detection.result.model_copy(update={**flag, "evidence": evidence}),
            tuple(
                Candidate(c.detector, c.priority, c.specificity, c.result.model_copy(update=flag))
                for c in detection.candidates
            ),
            detection.errors,
        )

    def _detect(
        self,
        snapshot: StateSnapshot,
        previous: SceneResult | None,
        detectors: Iterable[SceneDetector],
    ) -> Detection:
        candidates: list[Candidate] = []
        errors: list[str] = []
        for detector in detectors:
            try:
                claim = detector.detect(snapshot)
            except Exception as exc:  # a crashing detector is evidence of nothing: fail closed
                errors.append(f"{detector.name}: {type(exc).__name__}: {exc}")
                continue
            if claim is None:
                continue
            if claim.scene_id == SCENE_INVALID:
                return Detection(
                    claim,
                    (Candidate(detector.name, detector.priority, detector.specificity, claim),),
                )
            known = claim.scene_id in RESERVED_SCENE_IDS or (
                self.catalog is None or claim.scene_id in self.catalog
            )
            if not known:
                errors.append(f"{detector.name}: scene id {claim.scene_id!r} is not in the catalog")
                continue
            candidates.append(
                Candidate(
                    detector.name,
                    detector.priority,
                    detector.specificity,
                    claim.model_copy(update={"detector": detector.name}),
                )
            )

        cands = tuple(candidates)
        if errors:
            return Detection(
                SceneResult.unknown(
                    "detector_error", errors=errors, claims=[c.result.scene_id for c in cands]
                ),
                cands,
                tuple(errors),
            )
        if not cands:
            return Detection(SceneResult.unknown("no_detector_matched"), cands)

        # stable sort: registration order is the last tie-break
        ranked = sorted(cands, key=lambda c: (c.priority, -c.specificity, -c.result.confidence))
        top = ranked[0]
        rivals = [c for c in ranked[1:] if c.result.scene_id != top.result.scene_id]

        if top.result.confidence < self.min_confidence:
            return Detection(self._unknown("low_confidence", ranked), cands)

        # equal rank, equal specificity, nearly equal confidence: contradictory evidence
        ties = [
            c
            for c in rivals
            if c.priority == top.priority
            and c.specificity == top.specificity
            and top.result.confidence - c.result.confidence < self.ambiguity_margin
        ]
        if ties:
            pick = self._by_expected_successor([top, *ties], previous)
            if pick is None:
                return Detection(self._unknown("ambiguous", ranked), cands)
            top = pick
            rivals = [c for c in ranked if c.result.scene_id != top.result.scene_id]

        # a WEAK higher-priority claim against a more confident lower-priority one: the priority
        # order alone is not enough to override evidence that points elsewhere
        if top.result.confidence < self.strong_confidence:
            for c in rivals:
                if (
                    c.priority > top.priority
                    and c.priority != PriorityClass.BUSY
                    and c.result.confidence >= self.min_confidence
                    and c.result.confidence > top.result.confidence
                ):
                    return Detection(self._unknown("contradictory_evidence", ranked), cands)

        result = top.result
        if not result.busy and any(
            c.priority == PriorityClass.BUSY and c.result.confidence >= self.min_confidence
            for c in rivals
        ):
            result = result.model_copy(
                update={"busy": True, "evidence": {**result.evidence, "busy_claim": True}}
            )
        return Detection(result, cands)

    # -- helpers ----------------------------------------------------------------------------

    def _by_expected_successor(
        self, group: list[Candidate], previous: SceneResult | None
    ) -> Candidate | None:
        if previous is None:
            return None
        expected = self.successors.get(previous.scene_id, frozenset())
        hits = [c for c in group if c.result.scene_id in expected]
        return hits[0] if len(hits) == 1 else None

    @staticmethod
    def _unknown(reason: str, ranked: list[Candidate]) -> SceneResult:
        return SceneResult.unknown(
            reason,
            claims=[
                {
                    "scene": c.result.scene_id,
                    "confidence": c.result.confidence,
                    "detector": c.detector,
                    "priority": int(c.priority),
                }
                for c in ranked
            ],
        )


# -- confirmation over stable snapshots (spec 4.4: "2-3 stabile Snapshots") --------------------


@dataclass(frozen=True)
class ConfirmPolicy:
    """Which scenes must be seen identically N times before anything is pressed."""

    scene_ids: frozenset[str] = frozenset()
    required: int = 2
    max_reads: int = 3
    settle_frames: int = 2

    def __post_init__(self) -> None:
        if not 1 <= self.required <= self.max_reads:
            raise ValueError("need 1 <= required <= max_reads")

    def needs(self, scene: SceneResult) -> bool:
        return scene.scene_id in self.scene_ids and not scene.busy


@dataclass(frozen=True)
class Confirmation:
    scene: SceneResult
    snapshot: StateSnapshot
    stable: bool
    observed: tuple[str, ...]


def confirm_scene(
    first_snapshot: StateSnapshot,
    first: SceneResult,
    *,
    read: Callable[[], StateSnapshot],
    detect: Callable[[StateSnapshot], SceneResult],
    settle: Callable[[], None] | None = None,
    required: int = 2,
    max_reads: int = 3,
) -> Confirmation:
    """Read again (at most ``max_reads`` snapshots in total) until the last ``required``
    observations name the same, non-busy scene. Unstable means ``scene.unknown``.

    ``settle`` (optional) advances a few frames between reads; it must not press anything.
    The returned snapshot is the last one read, so a frame built from it is current.
    """
    if not 1 <= required <= max_reads:
        raise ValueError("need 1 <= required <= max_reads")
    snapshot, scene = first_snapshot, first
    observed = [scene.scene_id]
    seen: list[SceneResult] = [scene]

    def stable() -> bool:
        tail = seen[-required:]
        return len(tail) == required and all(
            s.scene_id == tail[0].scene_id and not s.busy for s in tail
        )

    while not stable() and len(seen) < max_reads:
        if settle is not None:
            settle()
        snapshot = read()
        scene = detect(snapshot)
        observed.append(scene.scene_id)
        seen.append(scene)
    if stable():
        confidence = min(s.confidence for s in seen[-required:])
        return Confirmation(
            scene.model_copy(update={"confidence": confidence}), snapshot, True, tuple(observed)
        )
    return Confirmation(
        SceneResult.unknown("unstable_scene", observed=observed), snapshot, False, tuple(observed)
    )


# -- coverage report (spec 6.K.6) --------------------------------------------------------------

COVERAGE_COUNTERS = (
    "recognized",
    "unknown",
    "unsupported",
    "wrong_options",
    "stale",
    "verified",
    "escalated",
)


@dataclass
class SceneCoverageReport:
    """Counters per test or soak run. A report is a measurement of one run, not a promise that
    the scene catalog is covered (the catalog is a specification, spec 6.K.6)."""

    counts: Counter = field(default_factory=Counter)
    by_scene: dict[str, Counter] = field(default_factory=dict)

    def _bump(self, counter: str, scene_id: str, n: int = 1) -> None:
        if counter not in COVERAGE_COUNTERS:
            raise KeyError(counter)
        self.counts[counter] += n
        self.by_scene.setdefault(scene_id, Counter())[counter] += n

    def recognized(self, scene_id: str) -> None:
        self._bump("recognized", scene_id)

    def unknown(self, scene_id: str = SCENE_UNKNOWN) -> None:
        self._bump("unknown", scene_id)

    def unsupported(self, scene_id: str) -> None:
        self._bump("unsupported", scene_id)

    def wrong_options(self, scene_id: str, n: int = 1) -> None:
        self._bump("wrong_options", scene_id, n)

    def stale(self, scene_id: str) -> None:
        self._bump("stale", scene_id)

    def verified(self, scene_id: str) -> None:
        self._bump("verified", scene_id)

    def escalated(self, scene_id: str) -> None:
        self._bump("escalated", scene_id)

    def merge(self, other: SceneCoverageReport) -> None:
        self.counts.update(other.counts)
        for scene_id, counter in other.by_scene.items():
            self.by_scene.setdefault(scene_id, Counter()).update(counter)

    def to_dict(self) -> dict[str, Any]:
        return {
            "totals": {name: self.counts.get(name, 0) for name in COVERAGE_COUNTERS},
            "by_scene": {
                sid: {name: c.get(name, 0) for name in COVERAGE_COUNTERS if c.get(name, 0)}
                for sid, c in sorted(self.by_scene.items())
            },
        }


def scene_coverage_report(report: SceneCoverageReport) -> dict[str, Any]:
    """The per-run output of spec 6.K.6: recognized, unknown, unsupported, wrong options, stale,
    action verified, escalated; totals and per scene. Tests and soaks print/store this dict."""
    return report.to_dict()
