"""Execution contract (spec 5.3, 11): ActionResult, execution kinds, steps, waits.

``ActionResult`` is the only thing the executor returns. The invariant that matters most is
enforced by the type: a ``rejected`` result has an empty ``input_trace`` (a rejection never comes
after an input) and always names an error code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pokegent.errors import ErrorCode


class ExecutionKind(str, Enum):
    """Spec 5.3."""

    ATOMIC = "atomic"  # exactly one confirmed input step
    MACRO = "macro"  # several interruptible steps with a scene check at every boundary
    INTENT = "intent"  # longer-lived goal decomposed lazily into macros (navigator, WP4)
    DETERMINISTIC = "deterministic"  # wait/animation/cursor move; no model involved


class ActionStatus(str, Enum):
    APPLIED = "applied"  # all steps ran; postcondition verified, or none declared
    REJECTED = "rejected"  # validation failed BEFORE any input; nothing was pressed
    INTERRUPTED = "interrupted"  # a macro stopped at a safe boundary (unexpected scene)
    TIMED_OUT = "timed_out"  # a bounded wait or budget ran out; inputs may have been applied
    UNCERTAIN = "uncertain"  # inputs ran but the outcome could not be established


class InputStep(BaseModel):
    """One thing the executor really did to the emulator, in order (replay material)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["tap", "advance"]
    buttons: tuple[str, ...] = ()
    hold_frames: int = 0
    wait_frames: int = 0
    frames: int = 0  # for "advance"


class ActionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: ActionStatus
    decision_id: str = ""
    option_id: str = ""
    executor_ref: str | None = None
    kind: ExecutionKind | None = None
    before_hash: str | None = None  # state_hash of the snapshot the input was based on
    after_hash: str | None = None  # state_hash after the action (None if unreadable)
    input_trace: tuple[InputStep, ...] = ()
    game_event_ids: tuple[str, ...] = ()  # reserved until the snapshot carries game events
    elapsed_frames: int = Field(default=0, ge=0)
    verified_outcome: bool | None = None  # None = no postcondition declared / not evaluated
    #: Whether game-progress state changed; distinct from a handoff postcondition such as
    #: ``field.interaction_started`` (which may succeed on a dialog-only action).
    progressed: bool | None = None
    #: Stable identity of the dialog which interrupted a field/menu macro.  This is executor
    #: evidence used to distinguish one repeatedly blocking script from unrelated interruptions.
    dialog_fingerprint: str | None = None
    #: Dialog page shown immediately before an agent-owned dialog advance/close input.
    dialog_text: tuple[str, ...] = ()
    #: Scene observed when a macro stopped at an unexpected-scene boundary.
    interrupted_scene: str | None = None
    error_code: ErrorCode | None = None
    reason: str = ""
    failure_reason: str | None = None  # additive typed adapter/executor reason

    @model_validator(mode="after")
    def _consistent(self) -> ActionResult:
        if self.status is ActionStatus.REJECTED and self.input_trace:
            raise ValueError("a rejected action must not have applied any input")
        if self.status is ActionStatus.REJECTED and self.elapsed_frames:
            raise ValueError("a rejected action must not have advanced the emulator")
        if self.status is not ActionStatus.APPLIED and self.error_code is None:
            raise ValueError(f"a {self.status.value} action needs an error_code")
        if self.status is ActionStatus.APPLIED and self.error_code is not None:
            raise ValueError("an applied action has no error_code")
        return self

    @classmethod
    def rejected(
        cls,
        code: ErrorCode,
        reason: str = "",
        *,
        decision_id: str = "",
        option_id: str = "",
        executor_ref: str | None = None,
        before_hash: str | None = None,
        failure_reason: str | None = None,
    ) -> ActionResult:
        return cls(
            status=ActionStatus.REJECTED,
            error_code=code,
            reason=reason,
            decision_id=decision_id,
            option_id=option_id,
            executor_ref=executor_ref,
            before_hash=before_hash,
            failure_reason=failure_reason,
        )

    @property
    def inputs_applied(self) -> bool:
        return any(step.kind == "tap" for step in self.input_trace)


# -- macro steps (what a registered executor function yields) -------------------------------------


@dataclass(frozen=True)
class Tap:
    """Press a button or a whitelisted chord. The actor validates it; nothing a model wrote can
    reach this (steps come from locally registered functions only)."""

    buttons: str | tuple[str, ...]
    hold_frames: int = 2
    wait_frames: int = 1
    #: Keep the button down, one frame at a time after ``hold_frames``, until this predicate holds
    #: on a fresh Observation or ``max_hold_frames`` frames passed; ``wait_frames`` then run with
    #: the button up. The game reads its buttons once per main-loop iteration and a lagging
    #: iteration can span more frames than a short hold, so the press is then never read (M4 run 1:
    #: 9 of 80 item uses; see ``the runtime contracts`` section 7 row 12). The
    #: predicate asks the game itself, e.g. ``gMain.heldKeys``, never the clock. One step, one
    #: input: the trace records the frames held.
    until: Callable[[Any], bool] | None = None
    #: Optional source-side hint that can stop the hold without decoding a snapshot. The next
    #: macro boundary still verifies ``until`` and every postcondition on a full observation.
    until_hint: tuple[str, object] | None = None
    #: Optional cheap gate before the post-input full observation. It only coalesces the full
    #: boundary read with the wait the executor would perform while the scene is busy.
    settle_hint: tuple[str, object] | None = None
    max_hold_frames: int = 8


@dataclass(frozen=True)
class Advance:
    """Run frames with no input change."""

    frames: int


@dataclass(frozen=True)
class Interrupt:
    """Stop the owning macro at the current safe boundary without sending another input."""

    reason: str


@dataclass(frozen=True)
class WaitUntil:
    """Run frames until the predicate holds on a fresh observation (frame and wall bounds)."""

    predicate: Callable[[Any], bool]
    max_frames: int = 120
    max_wall_s: float | None = None
    check_every: int = 2
    #: Action-specific reason returned when this gate times out.  This keeps a failed macro's
    #: result actionable ("party target did not become input-ready"), rather than reporting only
    #: the generic clock that expired.
    failure_reason: str | None = None


Step = Tap | Advance | Interrupt | WaitUntil


@dataclass(frozen=True)
class WaitOutcome:
    satisfied: bool
    reason: str  # "satisfied" | "frame_limit" | "wall_limit"
    frames: int
    wall_s: float
    last: Any = None  # last Observation

    @property
    def error_code(self) -> ErrorCode | None:
        if self.satisfied:
            return None
        return (
            ErrorCode.E_WAIT_TIMEOUT_WALL
            if self.reason == "wall_limit"
            else ErrorCode.E_WAIT_TIMEOUT_FRAMES
        )
