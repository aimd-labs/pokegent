"""Local registries: the model can never name code (spec 5.1, 0.2 "Plan != Action").

``ExecutorRegistry`` maps an ``executor_ref`` string (written by option builders) to a locally
registered function; ``VerifierRegistry`` maps a ``postcondition_ref`` to a predicate. An option
whose reference is not registered is rejected before any input. Nothing in either registry is
ever derived from model output, and a model only ever returns an option *id*.

An executor function takes a :class:`StepContext` and returns a :class:`Step`, an iterable of
steps, or a generator that yields steps and receives an ``Observation`` after each one (so a macro
can adapt to the cursor it reads). It never touches the emulator itself.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pokegent.actions._exec.models import ExecutionKind, Step
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.options.models import REF_RE, DecisionFrame, Option
from pokegent.state.scenes import SceneResult


@dataclass(frozen=True)
class Arg:
    """Declared executor argument. Builders produce args; the executor still checks them."""

    type: type
    required: bool = True
    min: int | None = None
    max: int | None = None
    choices: tuple[Any, ...] | None = None


@dataclass(frozen=True)
class StepContext:
    option: Option
    args: Mapping[str, Any]
    frame: DecisionFrame
    snapshot: StateSnapshot  # the fresh snapshot the action was re-validated against
    scene: SceneResult


@dataclass(frozen=True)
class ExecutorSpec:
    ref: str
    kind: ExecutionKind
    fn: Callable[[StepContext], Step | Iterable[Step]]
    args: Mapping[str, Arg] = field(default_factory=dict)
    #: Scenes a macro may pass through between steps. ``None`` = only the option's own scene.
    #: Busy phases are always tolerated (bounded wait). Anything else interrupts the macro.
    allowed_scenes: frozenset[str] | None = None
    max_inputs: int | Callable[[StepContext], int] = 16
    max_frames: int = 1800
    #: Bounded wait for the game to take input again after each step (frames).
    settle_frames: int = 300
    #: Wall-clock bound of that same wait, seconds. ``None`` = the executor's ``busy_wait_wall_s``.
    #: Raise it together with ``settle_frames`` for long animations (Pokemon Center heal) so a
    #: loaded host cannot turn a correct press into ``timed_out`` (contracts v1.1, additive).
    settle_wall_s: float | None = None
    #: Safe to run without a decider in external ``autoplay: off`` mode.  This is executor
    #: metadata, not a scene special case: text advancement and animation waits can opt in while
    #: movement, battle, and menu actions remain agent-controlled.
    autoplay_safe: bool = False
    #: Best-effort bounded cleanup after an input-bearing failure.  The callback must verify the
    #: resulting scene itself; it is intentionally opt-in for macros that own recoverable UI.
    failure_cleanup: Callable[[Any, Any, StepContext], None] | None = None
    #: Some macros own a menu handoff whose detector is busy during its fade/load phase.  Those
    #: macros express the input-ready boundary with their own WaitUntil RAM gate, so the generic
    #: post-step busy wait must pass the observation into the generator instead of pre-empting it.
    macro_manages_busy: bool = False

    def __post_init__(self) -> None:
        if not REF_RE.match(self.ref):
            raise ValueError(f"executor ref must look like 'namespace.name': {self.ref!r}")
        if self.kind is ExecutionKind.ATOMIC and self.max_inputs != 1:
            object.__setattr__(self, "max_inputs", 1)
        if (
            (not callable(self.max_inputs) and self.max_inputs < 1)
            or self.max_frames < 1
            or self.settle_frames < 1
        ):
            raise ValueError("budgets must be positive")
        if self.settle_wall_s is not None and self.settle_wall_s <= 0:
            raise ValueError("settle_wall_s must be positive")
        if self.macro_manages_busy and self.kind is not ExecutionKind.MACRO:
            raise ValueError("only a macro may manage its own busy handoffs")

    def check_args(self, args: Mapping[str, Any]) -> str | None:
        """``None`` if the arguments match the declaration, else a reason."""
        unknown = sorted(set(args) - set(self.args))
        if unknown:
            return f"unknown arguments {unknown}"
        for name, arg in self.args.items():
            if name not in args:
                if arg.required:
                    return f"missing argument {name!r}"
                continue
            value = args[name]
            if isinstance(value, bool) and arg.type is not bool:
                return f"argument {name!r} must be {arg.type.__name__}"
            if not isinstance(value, arg.type):
                return f"argument {name!r} must be {arg.type.__name__}"
            if arg.min is not None and value < arg.min:
                return f"argument {name!r} below {arg.min}"
            if arg.max is not None and value > arg.max:
                return f"argument {name!r} above {arg.max}"
            if arg.choices is not None and value not in arg.choices:
                return f"argument {name!r} not one of {list(arg.choices)}"
        return None


class ExecutorRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ExecutorSpec] = {}

    def register(self, spec: ExecutorSpec) -> ExecutorSpec:
        if spec.ref in self._specs:
            raise ValueError(f"executor {spec.ref!r} is already registered")
        self._specs[spec.ref] = spec
        return spec

    def get(self, ref: str | None) -> ExecutorSpec | None:
        return self._specs.get(ref) if ref else None

    def refs(self) -> frozenset[str]:
        return frozenset(self._specs)


@dataclass(frozen=True)
class VerifyContext:
    before: StateSnapshot  # the snapshot the input was based on
    after: StateSnapshot  # the latest observation
    scene: SceneResult  # scene of ``after``
    option: Option
    frame: DecisionFrame
    args: Mapping[str, Any]


@dataclass(frozen=True)
class VerifierSpec:
    """A postcondition predicate with its own bounded wait (spec 11.3)."""

    ref: str
    predicate: Callable[[VerifyContext], bool]
    max_frames: int = 120
    max_wall_s: float = 5.0
    check_every: int = 2
    failure_reason: Callable[[VerifyContext], str | None] | None = None

    def __post_init__(self) -> None:
        if not REF_RE.match(self.ref):
            raise ValueError(f"verifier ref must look like 'namespace.name': {self.ref!r}")
        if self.max_frames < 1 or self.max_wall_s <= 0 or self.check_every < 1:
            raise ValueError("verifier bounds must be positive")


class VerifierRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, VerifierSpec] = {}

    def register(self, spec: VerifierSpec) -> VerifierSpec:
        if spec.ref in self._specs:
            raise ValueError(f"verifier {spec.ref!r} is already registered")
        self._specs[spec.ref] = spec
        return spec

    def get(self, ref: str | None) -> VerifierSpec | None:
        return self._specs.get(ref) if ref else None

    def refs(self) -> frozenset[str]:
        return frozenset(self._specs)
