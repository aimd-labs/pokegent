"""Authoritative executor (spec 0.2, 5.3, 11): the only code path that sends input.

``Executor.execute(frame, option_id)`` applies a chosen option through the
:class:`~pokegent.emulator.EmulatorActor` and only after re-validation:

1. the option is in the frame, chooseable, and its ``executor_ref`` / ``postcondition_ref`` are
   registered locally and its arguments match the declaration;
2. a FRESH snapshot is read and perceived again: usable, not older than the frame, same scene id,
   not busy, same ``state_fingerprint``, the option still offered (LEGAL or CONDITIONAL) with the
   same executor reference and arguments;
3. only then inputs are sent, step by step, within hard budgets. A macro is checked at every step
   boundary: an unexpected scene stops it ("do not finish the remaining presses", spec 11.2);
4. the postcondition (if declared) is verified with a bounded wait.

Any failure in steps 1 and 2 returns ``rejected`` with an error code and an EMPTY input trace.
All waits are bounded in frames and in wall-clock seconds, tracked independently (spec 11.1).
Nothing here is random and nothing parses a model's text into a key or a duration.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Protocol

from pokegent.actions._exec.models import (
    ActionResult,
    ActionStatus,
    Advance,
    ExecutionKind,
    InputStep,
    Interrupt,
    Step,
    Tap,
    WaitOutcome,
    WaitUntil,
)
from pokegent.actions._exec.registry import (
    ExecutorRegistry,
    ExecutorSpec,
    StepContext,
    VerifierRegistry,
    VerifyContext,
)
from pokegent.actions.menu.system_flow import evidence_endgame
from pokegent.emulator import InputRejected
from pokegent.errors import ErrorCode
from pokegent.options.models import DecisionFrame, Legality
from pokegent.pipeline import Observation, Perception, PerceptionKind
from pokegent.state.scenes import readable


class InputPort(Protocol):
    """The part of ``EmulatorActor`` the executor uses (the real actor satisfies it)."""

    def tap(self, buttons: Any, hold_frames: int = 2, wait_frames: int = 1) -> int: ...
    def advance_frames(self, frames: int) -> int: ...
    def release_all(self) -> None: ...


def _progress_state_changed(before: Any, after: Any) -> bool:
    """Compare durable game progress, excluding dialog and player-position-only changes."""
    if before is None or after is None:
        return False
    before_player = getattr(before, "player", None)
    after_player = getattr(after, "player", None)
    if (
        getattr(before_player, "map_group", None),
        getattr(before_player, "map_num", None),
    ) != (
        getattr(after_player, "map_group", None),
        getattr(after_player, "map_num", None),
    ):
        return True
    before_party = getattr(before, "party", ())
    after_party = getattr(after, "party", ())
    if len(before_party) != len(after_party) or before_party != after_party:
        return True
    if getattr(before, "flags", None) != getattr(after, "flags", None):
        return True
    if getattr(before, "bag", None) != getattr(after, "bag", None):
        return True
    if getattr(before_player, "money", None) != getattr(after_player, "money", None):
        return True
    before_storage = getattr(before, "storage", None)
    after_storage = getattr(after, "storage", None)
    if getattr(before_storage, "boxes", None) != getattr(after_storage, "boxes", None):
        return True
    return getattr(before, "battle", None) is None and getattr(after, "battle", None) is not None


def _ram_player_position(snapshot: Any) -> tuple[object, ...] | None:
    """Return the RAM-backed player placement used to prove grass movement."""
    state = getattr(snapshot, "map", None)
    player = getattr(state, "player", None)
    identity = getattr(state, "identity", None)
    if player is None or identity is None:
        return None
    return (
        getattr(identity, "group", None),
        getattr(identity, "number", None),
        getattr(player, "avatar_x", None),
        getattr(player, "avatar_y", None),
    )


def _dialog_fingerprint(perceiver: Any, obs: Observation | None) -> str | None:
    """Return the same-message identity at an interrupted dialog boundary, if available."""
    if obs is None or not obs.scene.scene_id.startswith("dialog."):
        return None
    sidecar = getattr(getattr(perceiver, "source", None), "sidecar", None)
    identity = sidecar.identity(obs.snapshot) if sidecar is not None else None
    if identity is None:
        return None
    digest, page = identity
    return f"{digest}:{page}"


_DIALOG_PAGE_SCENES = frozenset({"dialog.advance", "dialog.end", "dialog.item_receive"})


def _dialog_page_text(perceiver: Any, snapshot: Any, scene_id: str) -> tuple[str, str]:
    """Return ``(page_key, text)`` for the proven visible page, never a stale guess."""
    sidecar = getattr(getattr(perceiver, "source", None), "sidecar", None)
    text = sidecar.get(snapshot) if sidecar is not None else None
    if text is None or not text.pages:
        return ("unreadable", "unreadable")
    # At a paragraph-clear prompt the printer pointer has already crossed the
    # separator, while the preceding page is what remains visible until A.
    original_index = (
        text.page_index - 1 if scene_id == "dialog.advance" and text.page_index else text.page_index
    )
    try:
        page_index = text.page_indices.index(original_index)
    except ValueError:
        return ("unreadable", "unreadable")
    page = text.pages[page_index]
    if not page:
        return ("unreadable", "unreadable")
    return (f"{text.digest}:{original_index}", page)


class Perceives(Protocol):
    def perceive(
        self,
        decision_id: str,
        previous: Any = None,
        recent_failed: Any = (),
        *,
        confirm: bool = True,
    ) -> Perception: ...
    def observe(self, previous: Any = None) -> Observation: ...
    def observe_if_changed(
        self, previous: Any = None, *, mode: str = "wait"
    ) -> Observation | None: ...
    def probe_hint_matches(self, hint: tuple[str, object]) -> bool | None: ...
    def bind_settle(self, settle: Callable[[int], None]) -> None: ...


class _Stop(Exception):
    """Internal: unwinds a run with a final result."""

    def __init__(
        self,
        status: ActionStatus,
        code: ErrorCode,
        reason: str,
        *,
        failure_reason: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.status, self.code, self.reason = status, code, reason
        self.failure_reason = failure_reason


class Executor:
    def __init__(
        self,
        actor: InputPort,
        executors: ExecutorRegistry,
        verifiers: VerifierRegistry | None = None,
        perceiver: Perceives | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        busy_wait_frames: int = 600,
        busy_wait_wall_s: float = 20.0,
        wait_chunk_frames: int = 2,
        full_poll_frames: int = 30,
        interrupt_requested: Callable[[], bool] | None = None,
        clear_interrupt: Callable[[], None] | None = None,
        macro_started: Callable[[], None] | None = None,
        macro_finished: Callable[[], None] | None = None,
    ) -> None:
        self.actor = actor
        self.executors = executors
        self.verifiers = verifiers or VerifierRegistry()
        self.perceiver: Perceives | None = None
        self._clock = clock
        self.busy_wait_frames = busy_wait_frames
        self.busy_wait_wall_s = busy_wait_wall_s
        self.wait_chunk_frames = wait_chunk_frames
        if full_poll_frames < 1:
            raise ValueError("full_poll_frames must be >= 1")
        self.full_poll_frames = full_poll_frames
        self._interrupt_requested = interrupt_requested
        self._clear_interrupt = clear_interrupt
        self._macro_started = macro_started
        self._macro_finished = macro_finished
        self.frames_advanced = 0  # total frames this executor ran (inputs + waits + settles)
        self.last_emulator_frame: int | None = None
        if perceiver is not None:
            self.bind_perceiver(perceiver)

    def bind_perceiver(self, perceiver: Perceives) -> None:
        """Late binding (the perceiver may be built by the runner). Also lets the perceiver
        advance frames to confirm scenes, through this executor and nobody else."""
        self.perceiver = perceiver
        perceiver.bind_settle(self.settle)

    def bind_interrupt(
        self,
        requested: Callable[[], bool] | None,
        clear: Callable[[], None] | None = None,
        *,
        macro_started: Callable[[], None] | None = None,
        macro_finished: Callable[[], None] | None = None,
    ) -> None:
        """Connect an external stop request to macro safe boundaries."""
        self._interrupt_requested = requested
        self._clear_interrupt = clear
        self._macro_started = macro_started
        self._macro_finished = macro_finished

    def _interrupt(self) -> bool:
        if self._interrupt_requested is None:
            return False
        try:
            requested = bool(self._interrupt_requested())
        except Exception:
            requested = False
        if requested and self._clear_interrupt is not None:
            try:
                self._clear_interrupt()
            except Exception:
                pass
        return requested

    # -- bounded frame helpers -----------------------------------------------------------------

    def settle(self, frames: int = 2) -> None:
        """Advance a few frames with no input (observation settle). Bounded by the actor."""
        self.actor.advance_frames(frames)
        self.frames_advanced += frames

    def wait_until(
        self,
        predicate: Callable[[Observation], bool],
        *,
        max_frames: int,
        max_wall_s: float | None = None,
        chunk: int | None = None,
        initial: Observation | None = None,
    ) -> WaitOutcome:
        """Observe, test, advance ``chunk`` frames, repeat; stop on the first limit hit.

        The predicate is tested before the first frame runs. ``max_frames`` and ``max_wall_s`` are
        independent limits; the result says which one ended the wait.
        """
        if max_frames < 1:
            raise ValueError("max_frames must be >= 1")
        perceiver = self._need_perceiver()
        if max_wall_s is not None:
            wall_limit = max_wall_s
        else:
            # A macro's frame budget is the semantic bound.  Derive a generous host-time
            # watchdog from it so loaded runners do not turn a frame-bounded wait into a
            # shorter wall-clock wait, while still stopping a genuinely hung emulator.
            min_fps = float(getattr(self.actor, "min_fps", 20.0))
            wall_slack_s = float(getattr(self.actor, "wall_slack_s", 5.0))
            wall_limit = max_frames / min_fps + wall_slack_s
        step = max(1, chunk or self.wait_chunk_frames)
        started = self._clock()
        frames = 0
        obs = initial if initial is not None else perceiver.observe()
        self.last_emulator_frame = obs.snapshot.emulator_frame
        last_full_frame = 0
        fresh_observation = True
        while True:
            if fresh_observation and predicate(obs):
                return WaitOutcome(True, "satisfied", frames, self._clock() - started, obs)
            if frames >= max_frames:
                return WaitOutcome(False, "frame_limit", frames, self._clock() - started, obs)
            if self._clock() - started >= wall_limit:
                return WaitOutcome(False, "wall_limit", frames, self._clock() - started, obs)
            n = min(step, max_frames - frames)
            self.actor.advance_frames(n)
            self.frames_advanced += n
            frames += n
            elapsed = self._clock() - started
            force_full = (
                frames >= max_frames
                or elapsed >= wall_limit
                or frames - last_full_frame >= self.full_poll_frames
            )
            if force_full:
                obs = perceiver.observe()
                self.last_emulator_frame = obs.snapshot.emulator_frame
                last_full_frame = frames
                fresh_observation = True
                continue
            observe_if_changed = getattr(perceiver, "observe_if_changed", None)
            candidate = (
                observe_if_changed() if callable(observe_if_changed) else perceiver.observe()
            )
            if candidate is not None:
                obs = candidate
                self.last_emulator_frame = obs.snapshot.emulator_frame
                last_full_frame = frames
                fresh_observation = True
            else:
                fresh_observation = False

    def wait_while_busy(
        self,
        max_frames: int | None = None,
        max_wall_s: float | None = None,
        *,
        initial: Observation | None = None,
    ) -> WaitOutcome:
        """Bounded wait until the game takes input again (or the snapshot is unusable)."""
        return self.wait_until(
            lambda obs: not obs.scene.busy,
            max_frames=max_frames or self.busy_wait_frames,
            max_wall_s=max_wall_s if max_wall_s is not None else self.busy_wait_wall_s,
            initial=initial,
        )

    def _need_perceiver(self) -> Perceives:
        if self.perceiver is None:
            raise RuntimeError("Executor has no perceiver; call bind_perceiver() first")
        return self.perceiver

    def _press(self, step: Tap, hold: int) -> int:
        """Press ``step`` and return the frames the button stayed down."""
        if step.until is None:
            self.actor.tap(step.buttons, hold, step.wait_frames)
            return hold
        perceiver = self._need_perceiver()
        held = 0
        while True:
            frames = hold if held == 0 else 1
            self.actor.tap(step.buttons, frames, 0)  # back to back: the button never comes up
            held += frames
            if held >= step.max_hold_frames:
                break
            try:
                probe_hint_matches = getattr(perceiver, "probe_hint_matches", None)
                if step.until_hint is not None and callable(probe_hint_matches):
                    matched = probe_hint_matches(step.until_hint)
                    if matched is True:
                        break
                    if matched is False:
                        continue
                observe_if_changed = getattr(perceiver, "observe_if_changed", None)
                candidate = (
                    observe_if_changed(mode="input")
                    if callable(observe_if_changed)
                    else perceiver.observe()
                )
                if candidate is not None and step.until(candidate):
                    break
            except Exception:  # a broken predicate must not keep a button down
                break
        if step.wait_frames:
            self.actor.advance_frames(step.wait_frames)
        return held

    def _settle_on_hint(
        self,
        perceiver: Perceives,
        hint: tuple[str, object],
        *,
        max_frames: int,
        max_wall_s: float,
    ) -> int:
        """Advance cheaply until a source hint says the post-input animation settled."""
        probe = getattr(perceiver, "probe_hint_matches", None)
        if not callable(probe) or max_frames < 1:
            return 0
        started = self._clock()
        frames = 0
        while frames < max_frames and self._clock() - started < max_wall_s:
            matched = probe(hint)
            if matched is None or matched:
                break
            n = min(self.wait_chunk_frames, max_frames - frames)
            self.actor.advance_frames(n)
            self.frames_advanced += n
            frames += n
        return frames

    # -- execute ---------------------------------------------------------------------------------

    def execute(self, frame: DecisionFrame, option_id: str) -> ActionResult:
        base = {"decision_id": frame.decision_id, "option_id": option_id}
        option = frame.get(option_id)
        if option is None:
            return ActionResult.rejected(
                ErrorCode.E_UNKNOWN_OPTION, f"{option_id!r} is not in the frame", **base
            )
        base["executor_ref"] = option.executor_ref
        if not option.chooseable or option_id not in frame.all_chooseable_ids():
            return ActionResult.rejected(
                ErrorCode.E_NOT_CHOOSEABLE, f"{option_id!r} is {option.legality.value}", **base
            )
        spec = self.executors.get(option.executor_ref)
        if spec is None:
            return ActionResult.rejected(
                ErrorCode.E_UNKNOWN_EXECUTOR_REF,
                f"executor {option.executor_ref!r} is not registered",
                **base,
            )
        problem = spec.check_args(option.executor_args)
        if problem:
            return ActionResult.rejected(ErrorCode.E_BAD_ARGS, problem, **base)
        verifier = None
        if option.postcondition_ref:
            verifier = self.verifiers.get(option.postcondition_ref)
            if verifier is None:
                return ActionResult.rejected(
                    ErrorCode.E_UNKNOWN_POSTCONDITION,
                    f"postcondition {option.postcondition_ref!r} is not registered",
                    **base,
                )

        perceiver = self._need_perceiver()
        try:
            # The decision-producing perception already passed any scene confirmation gate.
            # Revalidation must only read current state: a stale rejection sends no input and
            # must not advance the emulator behind the caller's back.
            fresh = perceiver.perceive(decision_id=f"{frame.decision_id}/revalidate", confirm=False)
        except Exception as exc:  # the reader/actor failed: no input without a fresh view
            return ActionResult.rejected(
                ErrorCode.E_EMULATOR, f"{type(exc).__name__}: {exc}", **base
            )
        before_hash = fresh.snapshot.state_hash if readable(fresh.snapshot, fresh.scene) else None
        self.last_emulator_frame = fresh.snapshot.emulator_frame
        base["before_hash"] = before_hash
        rejection = self._revalidate(frame, option_id, fresh)
        if rejection is not None:
            code, reason = rejection
            return ActionResult.rejected(code, reason, **base)

        ctx = StepContext(
            option=option,
            args=dict(option.executor_args),
            frame=frame,
            snapshot=fresh.snapshot,
            scene=fresh.scene,
        )
        lifecycle = spec.kind in (ExecutionKind.MACRO, ExecutionKind.INTENT)
        if lifecycle and self._macro_started is not None:
            self._macro_started()
        try:
            return self._run(spec, verifier, ctx, base)
        finally:
            if lifecycle and self._macro_finished is not None:
                self._macro_finished()

    # -- re-validation (zero input on any failure) -------------------------------------------------

    @staticmethod
    def _revalidate(
        frame: DecisionFrame, option_id: str, fresh: Perception
    ) -> tuple[ErrorCode, str] | None:
        if fresh.kind is PerceptionKind.INVALID or not readable(fresh.snapshot, fresh.scene):
            return ErrorCode.E_SNAPSHOT_INVALID, "fresh snapshot is not usable"
        if fresh.snapshot.snapshot_seq < frame.snapshot_seq:
            return ErrorCode.E_STALE_SNAPSHOT, (
                f"fresh snapshot_seq {fresh.snapshot.snapshot_seq} < frame {frame.snapshot_seq}"
            )
        if fresh.scene.scene_id != frame.scene_id:
            return ErrorCode.E_SCENE_CHANGED, (
                f"scene is now {fresh.scene.scene_id!r}, decided in {frame.scene_id!r}"
            )
        if fresh.kind is PerceptionKind.BUSY or fresh.scene.busy:
            return ErrorCode.E_SCENE_BUSY, "game is not processing input"
        if fresh.frame is None:
            return (
                ErrorCode.E_SCENE_CHANGED,
                f"scene {frame.scene_id!r} yields no frame ({fresh.kind.value})",
            )
        if fresh.frame.state_fingerprint != frame.state_fingerprint:
            return ErrorCode.E_FINGERPRINT_CHANGED, "decision-relevant state changed"
        now = fresh.frame.get(option_id)
        if now is None or option_id not in fresh.frame.all_chooseable_ids():
            if now is not None and now.legality in (
                Legality.UNAVAILABLE,
                Legality.UNKNOWN,
                Legality.UNSUPPORTED,
            ):
                return ErrorCode.E_NOT_CHOOSEABLE, f"option is {now.legality.value} now"
            return ErrorCode.E_OPTION_GONE, "option is not offered by the rebuilt frame"
        original = frame.get(option_id)
        assert original is not None
        if (now.executor_ref, dict(now.executor_args)) != (
            original.executor_ref,
            dict(original.executor_args),
        ) or now.action_contract != original.action_contract:
            return (
                ErrorCode.E_OPTION_CHANGED,
                "executor reference, arguments, or action contract differ now",
            )
        return None

    # -- the run -----------------------------------------------------------------------------------

    def _run(
        self, spec: ExecutorSpec, verifier, ctx: StepContext, base: dict[str, Any]
    ) -> ActionResult:
        trace: list[InputStep] = []
        spent = 0  # frames this action ran
        last_obs: Observation | None = None
        interrupted_obs: Observation | None = None
        dialog_fingerprint: str | None = None
        verified: bool | None = None
        grass_position_changed = False
        perceiver = self._need_perceiver()
        captured_dialog_text: list[str] = []
        captured_dialog_pages: set[str] = set()

        def capture_dialog_page(obs: Observation) -> None:
            if obs.scene.scene_id not in _DIALOG_PAGE_SCENES:
                return
            page_key, page = _dialog_page_text(perceiver, obs.snapshot, obs.scene.scene_id)
            if page_key in captured_dialog_pages:
                return
            captured_dialog_pages.add(page_key)
            captured_dialog_text.append(page)

        capture_dialog_page(Observation(ctx.snapshot, ctx.scene))
        previous_ram_position = _ram_player_position(ctx.snapshot)
        target_facts = ctx.option.facts.get("target")
        is_grass = isinstance(target_facts, Mapping) and target_facts.get("kind") == "grass"

        def result(**kw: Any) -> ActionResult:
            if last_obs is not None:
                self.last_emulator_frame = last_obs.snapshot.emulator_frame
            after = (
                last_obs.snapshot.state_hash
                if last_obs and readable(last_obs.snapshot, last_obs.scene)
                else None
            )
            progress_obs = interrupted_obs if dialog_fingerprint is not None else last_obs
            progressed = (
                _progress_state_changed(
                    ctx.snapshot,
                    progress_obs.snapshot,
                )
                if progress_obs and readable(progress_obs.snapshot, progress_obs.scene)
                else None
            )
            if (
                not progressed
                and progress_obs is not None
                and readable(progress_obs.snapshot, progress_obs.scene)
                and evidence_endgame.wait_is_progress(progress_obs.snapshot)
            ):
                progressed = True
            if is_grass and grass_position_changed:
                progressed = True
            return ActionResult(
                kind=spec.kind,
                input_trace=tuple(trace),
                elapsed_frames=spent,
                after_hash=after,
                verified_outcome=verified,
                progressed=progressed,
                dialog_fingerprint=dialog_fingerprint,
                dialog_text=tuple(captured_dialog_text),
                interrupted_scene=(interrupted_obs.scene.scene_id if interrupted_obs else None),
                **base,
                **kw,
            )

        gen = self._steps(spec, ctx)

        def clean_after_failure(reason: str) -> str:
            """Run an owning macro's bounded cleanup and surface any failure to the caller."""
            nonlocal last_obs
            if not trace or spec.failure_cleanup is None:
                return reason
            try:
                spec.failure_cleanup(self.actor, perceiver, ctx)
                last_obs = perceiver.observe()
            except Exception as exc:
                return f"{reason}; cleanup failed: {type(exc).__name__}: {exc}"
            return reason

        allowed = (
            spec.allowed_scenes
            if spec.allowed_scenes is not None
            else frozenset({ctx.frame.scene_id})
        )
        contract = ctx.option.action_contract
        if contract is not None:
            allowed = allowed | frozenset(contract.expected_outcome_scenes)
        settles_invalid_boundary = bool(
            contract is not None
            and any(expected.kind == "map_changed" for expected in contract.expected)
        )
        inputs = 0
        input_limit = spec.max_inputs(ctx) if callable(spec.max_inputs) else spec.max_inputs
        if input_limit < 1:
            return result(
                status=ActionStatus.REJECTED,
                error_code=ErrorCode.E_BAD_ARGS,
                reason=f"invalid input budget {input_limit} for {spec.ref}",
            )
        try:
            step = self._next(gen, None, first=True)
            while step is not None:
                if last_obs is not None:
                    capture_dialog_page(last_obs)
                if isinstance(step, Tap):
                    inputs += 1
                    if inputs > input_limit:
                        raise _Stop(
                            ActionStatus.TIMED_OUT,
                            ErrorCode.E_BUDGET_EXCEEDED,
                            f"more than {input_limit} inputs for {spec.ref}",
                        )
                    hold = step.hold_frames
                    names = (
                        (step.buttons,) if isinstance(step.buttons, str) else tuple(step.buttons)
                    )
                    # logged BEFORE the call: a timeout may still have pressed
                    trace.append(
                        InputStep(
                            kind="tap",
                            buttons=names,
                            hold_frames=hold,
                            wait_frames=step.wait_frames,
                        )
                    )
                    try:
                        hold = self._press(step, hold)
                    except InputRejected:
                        trace.pop()  # refused by the actor's whitelist: nothing was pressed
                        raise
                    if hold != trace[-1].hold_frames:  # held on until the game read the button
                        trace[-1] = trace[-1].model_copy(update={"hold_frames": hold})
                    spent += hold + step.wait_frames
                    self.frames_advanced += hold + step.wait_frames
                elif isinstance(step, Advance):
                    if spent + step.frames > spec.max_frames:
                        raise _Stop(
                            ActionStatus.TIMED_OUT,
                            ErrorCode.E_BUDGET_EXCEEDED,
                            f"frame budget {spec.max_frames} exhausted",
                        )
                    trace.append(InputStep(kind="advance", frames=step.frames))
                    self.actor.advance_frames(step.frames)
                    spent += step.frames
                    self.frames_advanced += step.frames
                elif isinstance(step, Interrupt):
                    raise _Stop(
                        ActionStatus.INTERRUPTED,
                        ErrorCode.E_INTERRUPTED_SCENE,
                        step.reason,
                    )
                elif isinstance(step, WaitUntil):
                    room = spec.max_frames - spent
                    if room < 1:
                        raise _Stop(
                            ActionStatus.TIMED_OUT,
                            ErrorCode.E_BUDGET_EXCEEDED,
                            f"frame budget {spec.max_frames} exhausted",
                        )
                    outcome = self.wait_until(
                        step.predicate,
                        max_frames=min(step.max_frames, room),
                        max_wall_s=step.max_wall_s,
                        chunk=step.check_every,
                        initial=last_obs,
                    )
                    spent += outcome.frames
                    if outcome.frames:
                        trace.append(InputStep(kind="advance", frames=outcome.frames))
                    if not outcome.satisfied:
                        raise _Stop(
                            ActionStatus.TIMED_OUT,
                            outcome.error_code,
                            step.failure_reason or f"wait ended: {outcome.reason}",
                            failure_reason=step.failure_reason,
                        )
                else:
                    raise _Stop(
                        ActionStatus.UNCERTAIN, ErrorCode.E_BAD_ARGS, f"unknown step {step!r}"
                    )
                if spent > spec.max_frames:
                    raise _Stop(
                        ActionStatus.TIMED_OUT,
                        ErrorCode.E_BUDGET_EXCEEDED,
                        f"frame budget {spec.max_frames} exhausted",
                    )

                if isinstance(step, Tap) and step.settle_hint is not None:
                    settled_frames = self._settle_on_hint(
                        perceiver,
                        step.settle_hint,
                        max_frames=min(30, spec.max_frames - spent),
                        max_wall_s=(
                            spec.settle_wall_s
                            if spec.settle_wall_s is not None
                            else self.busy_wait_wall_s
                        ),
                    )
                    spent += settled_frames

                # -- safe boundary: settle, observe, let the macro adapt, check the scene ---------
                last_obs = perceiver.observe()
                capture_dialog_page(last_obs)
                if is_grass:
                    current_ram_position = _ram_player_position(last_obs.snapshot)
                    if (
                        previous_ram_position is not None
                        and current_ram_position is not None
                        and current_ram_position != previous_ram_position
                    ):
                        grass_position_changed = True
                    previous_ram_position = current_ram_position
                if last_obs.scene.busy and not spec.macro_manages_busy:
                    waited = self.wait_while_busy(
                        spec.settle_frames, spec.settle_wall_s, initial=last_obs
                    )
                    spent += waited.frames
                    last_obs = waited.last or last_obs
                    capture_dialog_page(last_obs)
                    if not waited.satisfied:
                        raise _Stop(
                            ActionStatus.TIMED_OUT,
                            waited.error_code,
                            "game stayed busy after a step",
                        )
                if not readable(last_obs.snapshot, last_obs.scene):
                    if not settles_invalid_boundary:
                        raise _Stop(
                            ActionStatus.INTERRUPTED,
                            ErrorCode.E_SNAPSHOT_INVALID,
                            "snapshot invalid at a boundary",
                        )
                    # Map loads can invalidate observations exactly at the action boundary.
                    # Settle without input; the action's postcondition can then recognize a
                    # completed transition. A failed settle remains an interruption.
                    settled = self.wait_until(
                        lambda obs: readable(obs.snapshot, obs.scene),
                        max_frames=spec.settle_frames,
                        max_wall_s=spec.settle_wall_s,
                    )
                    spent += settled.frames
                    last_obs = settled.last or last_obs
                    if not settled.satisfied:
                        raise _Stop(
                            ActionStatus.INTERRUPTED,
                            ErrorCode.E_SNAPSHOT_INVALID,
                            "snapshot invalid at a boundary",
                        )
                if self._interrupt_at_safe_boundary(last_obs):
                    raise _Stop(
                        ActionStatus.INTERRUPTED,
                        ErrorCode.E_INTERRUPTED_SCENE,
                        "external interrupt at safe boundary",
                    )
                nxt = self._next(gen, last_obs)
                if nxt is None:
                    break
                if isinstance(nxt, Interrupt):
                    raise _Stop(
                        ActionStatus.INTERRUPTED,
                        ErrorCode.E_INTERRUPTED_SCENE,
                        nxt.reason,
                    )
                if last_obs.scene.scene_id not in allowed:
                    raise _Stop(
                        ActionStatus.INTERRUPTED,
                        ErrorCode.E_INTERRUPTED_SCENE,
                        f"unexpected scene {last_obs.scene.scene_id!r} after step {len(trace)}",
                    )
                step = nxt
        except _Stop as stop:
            if stop.code is ErrorCode.E_INTERRUPTED_SCENE:
                interrupted_obs = last_obs
                dialog_fingerprint = _dialog_fingerprint(perceiver, interrupted_obs)
                if (
                    verifier is not None
                    and interrupted_obs is not None
                    and readable(interrupted_obs.snapshot, interrupted_obs.scene)
                ):
                    try:
                        verification = VerifyContext(
                            before=ctx.snapshot,
                            after=interrupted_obs.snapshot,
                            scene=interrupted_obs.scene,
                            option=ctx.option,
                            frame=ctx.frame,
                            args=ctx.args,
                        )
                        if verifier.predicate(verification):
                            verified = True
                            self._release()
                            return result(
                                status=ActionStatus.APPLIED,
                                reason="applied and verified at expected follow-up scene",
                            )
                    except Exception:
                        pass
            reason = clean_after_failure(stop.reason)
            self._release()
            return result(
                status=stop.status,
                error_code=stop.code,
                reason=reason,
                failure_reason=stop.failure_reason,
            )
        except InputRejected as exc:
            reason = clean_after_failure(str(exc))
            self._release()
            if not trace:  # the actor refused before pressing anything
                return ActionResult.rejected(ErrorCode.E_INPUT_REJECTED, reason, **base)
            return result(
                status=ActionStatus.UNCERTAIN,
                error_code=ErrorCode.E_INPUT_REJECTED,
                reason=reason,
            )
        except Exception as exc:  # actor stopped/timed out, buggy executor function ...
            reason = clean_after_failure(f"{type(exc).__name__}: {exc}")
            self._release()
            failure_reason = getattr(exc, "failure_reason", None)
            if not trace:
                return ActionResult.rejected(
                    ErrorCode.E_EMULATOR,
                    reason,
                    failure_reason=failure_reason,
                    **base,
                )
            return result(
                status=ActionStatus.UNCERTAIN,
                error_code=ErrorCode.E_EMULATOR,
                reason=reason,
                failure_reason=failure_reason,
            )

        # -- postcondition (spec 11.3) ------------------------------------------------------------
        if last_obs is None:
            last_obs = self._need_perceiver().observe()
        if contract is not None and last_obs.scene.scene_id in contract.expected_outcome_scenes:
            # The declared scene proves the action was applied, but it is not the action's
            # ordinary postcondition; leave verified_outcome unknown for downstream accounting.
            return result(status=ActionStatus.APPLIED)
        if verifier is not None:
            notes: list[str] = []
            failure_reasons: list[str] = []

            def holds(obs: Observation) -> bool:
                if not readable(obs.snapshot, obs.scene):
                    return False
                verification = VerifyContext(
                    before=ctx.snapshot,
                    after=obs.snapshot,
                    scene=obs.scene,
                    option=ctx.option,
                    frame=ctx.frame,
                    args=ctx.args,
                )
                try:
                    matched = bool(verifier.predicate(verification))
                    if not matched and verifier.failure_reason is not None:
                        reason = verifier.failure_reason(verification)
                        if isinstance(reason, str) and reason:
                            failure_reasons.append(reason)
                    return matched
                except Exception as exc:  # a buggy predicate is "not verified", never a crash
                    notes.append(f"{type(exc).__name__}: {exc}")
                    typed = getattr(exc, "failure_reason", None)
                    if isinstance(typed, str):
                        failure_reasons.append(typed)
                    return False

            try:
                outcome = self.wait_until(
                    holds,
                    max_frames=verifier.max_frames,
                    max_wall_s=verifier.max_wall_s,
                    chunk=verifier.check_every,
                    initial=last_obs,
                )
            except Exception as exc:
                return result(
                    status=ActionStatus.UNCERTAIN,
                    error_code=ErrorCode.E_EMULATOR,
                    reason=f"verification failed: {type(exc).__name__}: {exc}",
                )
            spent += outcome.frames
            last_obs = outcome.last or last_obs
            verified = outcome.satisfied
            if not verified:
                detail = failure_reasons[-1] if failure_reasons else (notes[-1] if notes else None)
                return result(
                    status=ActionStatus.UNCERTAIN,
                    error_code=ErrorCode.E_POSTCONDITION_NOT_MET,
                    reason=f"{verifier.ref} not met ({outcome.reason})"
                    + (f"; {detail}" if detail else ""),
                    failure_reason=failure_reasons[-1] if failure_reasons else None,
                )
        return result(status=ActionStatus.APPLIED, reason=str(ctx.args.get("_not_executed", "")))

    # -- helpers -----------------------------------------------------------------------------------

    def _release(self) -> None:
        try:
            self.actor.release_all()
        except Exception:  # best effort; the actor also releases keys when a command ends
            pass

    def _interrupt_at_safe_boundary(self, obs: Observation) -> bool:
        """Only consume an interrupt when no modal UI would be left open.

        A macro can expose a menu or a text printer immediately after an input.  Stopping at
        that point would return control with a half-completed interaction, so leave the request
        pending until the macro reaches a readable, non-modal boundary.
        """
        ui = getattr(obs.snapshot, "ui", None)
        if ui is not None and ui.valid:
            text = ui.text
            if text is not None and (
                text.any_active is True or text.printing is True or text.waiting_for_button is True
            ):
                return False
            menu = ui.menu
            if menu is not None:
                yes_no = menu.yes_no
                if yes_no is not None and (yes_no.window_in_use or yes_no.task_active is True):
                    return False
                if menu.multichoice is not None and menu.multichoice.window_in_use:
                    return False
                if menu.list_menu is not None and menu.list_menu.window_in_use:
                    return False
                if menu.start_menu is not None and menu.start_menu.task_active is True:
                    return False
                if menu.party is not None and menu.party.active_hint is True:
                    return False
                if menu.bag is not None and menu.bag.active_hint is True:
                    return False
                if menu.summary_screen_open is True:
                    return False
        return self._interrupt()

    @staticmethod
    def _steps(spec: ExecutorSpec, ctx: StepContext):
        produced = spec.fn(ctx)
        if inspect.isgenerator(produced):
            return produced
        if isinstance(produced, (Tap, Advance, WaitUntil)):
            return iter((produced,))
        return iter(produced)

    @staticmethod
    def _next(gen: Iterator[Step], obs: Observation | None, *, first: bool = False) -> Step | None:
        try:
            if first or not inspect.isgenerator(gen):
                return next(gen)
            return gen.send(obs)
        except StopIteration:
            return None
