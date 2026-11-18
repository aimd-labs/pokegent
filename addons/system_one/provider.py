"""System One provider contract and the synchronous ``Decider`` adapter (spec 8.1).

* :class:`SystemOneProvider` is the async interface a backend (SYSTEM1, mock, later a TypeSafe
  adapter) implements: ``decide(state, question, request_id, timeout_s)`` and ``health()``.
  A provider knows nothing about frames, option ids semantics or the runner: it answers one
  Choice question and validates that the answer is one of the criteria it was sent.
* :class:`SyncDecider` implements the frozen synchronous ``Decider`` of ``docs/contracts.md``:
  frame -> question plan (state budget, hierarchy) -> provider call(s) under ONE hard deadline
  -> ``ChoiceResult`` with ``decision_id`` echo. Failures map to the frozen error codes
  (``E_DECIDER_TIMEOUT``, ``E_DECIDER_FAILED``, ``E_CHOICE_*``, ``E_PAYLOAD_LEAK``). Nothing
  here ever picks an option on its own: no answer means an exception and the runner escalates.

The adapter owns one background event loop thread, so it works from synchronous code and from
inside a running loop alike. ``decide`` blocks the caller for at most ``timeout_s``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from collections.abc import Coroutine
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from pokegent.errors import ErrorCode
from pokegent.options.models import (
    ChoiceResult,
    DeciderError,
    DeciderTimeout,
    DecisionFrame,
)

from .questions import (
    OPTION_ORDERS,
    PROMPT_SCHEMA_VERSION,
    DecisionPlan,
    QuestionLimits,
    plan_decision,
)
from .redact import Scrubber
from .telemetry import DecisionTelemetry

# -- errors -------------------------------------------------------------------------------------


class ProviderError(DeciderError):
    """A provider could not produce a valid answer. ``code`` is a frozen ``ErrorCode``."""

    code = ErrorCode.E_DECIDER_FAILED
    retryable = False

    def __init__(self, message: str, *, code: ErrorCode | None = None, **detail: Any) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.detail = detail


class ProviderTimeout(ProviderError, DeciderTimeout):
    code = ErrorCode.E_DECIDER_TIMEOUT
    retryable = True


class ProviderUnavailable(ProviderError):
    """Connection failure, 429 or 5xx (after bounded retries), or System One disabled."""

    retryable = True


class CircuitOpen(ProviderError):
    """The breaker is open: no request was made."""


class RateBudgetExceeded(ProviderError):
    """The local request budget is used up: no request was made."""


class ProviderRejected(ProviderError):
    """A 4xx answer: our request is wrong or unauthorized. Never retried."""


class ProviderConfigurationError(ProviderRejected):
    """Explicit request/auth rejection or failed response pin; owner review is required."""


class InvalidResponse(ProviderError):
    """The server answered, but the answer is unusable. No input may follow from it.

    ``E_CHOICE_NOT_IN_FRAME`` for a key outside the criteria sent, ``E_CHOICE_MALFORMED`` for
    NaN/inf, bad probabilities or confidence, broken JSON."""

    def __init__(self, message: str, *, code: ErrorCode = ErrorCode.E_CHOICE_MALFORMED, **d: Any):
        super().__init__(message, code=code, **d)


class ProviderChoice(ChoiceResult):
    """A ``ChoiceResult`` that also carries call metadata for the calibration log (attempts, usage,
    server truncation details, reported model). It is a ``ChoiceResult`` for every consumer."""

    meta: dict[str, Any] = Field(default_factory=dict)


# -- health -------------------------------------------------------------------------------------


class ProviderHealth(BaseModel):
    """Local view of a provider (no network call): breaker state, error rates, latency."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    state: str = "closed"  # breaker: closed | open | half_open
    provider_id: str
    provider_version: str = ""
    model_id: str = ""
    prompt_schema_version: str = PROMPT_SCHEMA_VERSION
    requests: int = Field(default=0, ge=0)
    failures: int = Field(default=0, ge=0)
    invalid: int = Field(default=0, ge=0)
    truncated: int = Field(default=0, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    latency_p99_ms: float | None = None
    last_error: str | None = None


@runtime_checkable
class SystemOneProvider(Protocol):
    async def decide(
        self, *, state: dict, question: dict, request_id: str, timeout_s: float
    ) -> ChoiceResult:
        """Answer one Choice ``question`` (``name``, ``type``, ``instructions``, ``criteria``).

        The returned ``option_id`` is guaranteed to be a key of ``question["criteria"]``;
        otherwise the provider raises :class:`InvalidResponse`."""
        ...

    async def health(self) -> ProviderHealth: ...


# -- background loop ----------------------------------------------------------------------------


class _LoopThread:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def _ensure(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def run() -> None:
                asyncio.set_event_loop(loop)
                loop.call_soon(ready.set)
                loop.run_forever()

            thread = threading.Thread(target=run, name="system-one-loop", daemon=True)
            thread.start()
            ready.wait(5.0)
            self._loop, self._thread = loop, thread
            return loop

    def submit(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self._ensure())

    def close(self) -> None:
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is not None and thread is not None:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(2.0)
            if not thread.is_alive():
                loop.close()


# -- the Decider --------------------------------------------------------------------------------


class SyncDecider:
    """Frozen synchronous ``Decider`` over an async :class:`SystemOneProvider`.

    ``expose_latency`` puts the measured wall time into ``ChoiceResult.latency_ms``; by default
    it stays 0.0 so the runner's event stream carries wall time only in ``measured_ms`` (the one
    non-replayable field, contracts rule 11). The real figure always goes to the calibration log.
    """

    def __init__(
        self,
        provider: SystemOneProvider,
        *,
        limits: QuestionLimits | None = None,
        timeout_s: float = 8.0,
        telemetry: DecisionTelemetry | None = None,
        expose_latency: bool = False,
        clock=time.monotonic,
        run_id: str = "",
        goal_id: str | None = None,
        option_order: str = "frame",
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if option_order not in OPTION_ORDERS:
            raise ValueError(f"option_order must be one of {OPTION_ORDERS}, got {option_order!r}")
        self.provider = provider
        self.limits = limits or QuestionLimits()
        self.timeout_s = float(timeout_s)
        self.telemetry = telemetry or DecisionTelemetry()
        self.expose_latency = expose_latency
        self.run_id = run_id
        #: ``frame`` | ``shuffle``: the order the criteria are sent in (an experiment switch)
        self.option_order = option_order
        self._clock = clock
        self._loop = _LoopThread()
        self._future_lock = threading.Lock()
        self._future: concurrent.futures.Future | None = None
        self._cancelled = False
        self._scrub: Scrubber = self.telemetry.scrubber

    # -- Decider ------------------------------------------------------------------------------

    def decide(self, frame: DecisionFrame) -> ChoiceResult:
        if not frame.chooseable():
            raise DeciderError("frame has no chooseable option; nothing to ask")
        # PayloadLeakError (E_PAYLOAD_LEAK) propagates
        plan = plan_decision(frame, self.limits, option_order=self.option_order)
        only = frame.forced_option()
        if only is not None:  # nothing to decide: never spend a request on a single option
            return ChoiceResult(
                option_id=only.id,
                confidence=1.0,
                model_id="forced",
                provider_id="local",
                decision_id=frame.decision_id,
            )
        self.telemetry.requested(frame, plan.audit())
        started = self._clock()
        deadline = started + self.timeout_s
        trace: list[dict[str, Any]] = []
        future = self._loop.submit(self._walk(frame, plan, deadline, trace))
        with self._future_lock:
            if self._cancelled:
                future.cancel()
            else:
                self._future = future
        error: BaseException | None = None
        outcome: ChoiceResult | None = None
        try:
            outcome = future.result(timeout=self.timeout_s)
        except concurrent.futures.TimeoutError:
            future.cancel()
            error = DeciderTimeout(f"System One decision exceeded {self.timeout_s:g}s")
        except (concurrent.futures.CancelledError, asyncio.CancelledError):
            error = DeciderTimeout("System One decision was cancelled")
        except DeciderError as exc:
            error = exc
        except Exception as exc:  # a provider bug is a failed decision, never an input
            msg = self._scrub.text(f"provider raised {type(exc).__name__}: {exc}")
            error = DeciderError(msg)
        finally:
            with self._future_lock:
                if self._future is future:
                    self._future = None
        measured_ms = round((self._clock() - started) * 1000.0, 3)
        record = self._record(frame, plan, trace, outcome, error, measured_ms)
        self.telemetry.record(record)
        if error is not None:
            if isinstance(error, DeciderTimeout):
                self.telemetry.timeout(frame, record)
            else:
                self.telemetry.invalid(frame, record)
            raise error
        assert outcome is not None
        self.telemetry.completed(frame, record)
        if self.expose_latency:
            outcome = outcome.model_copy(update={"latency_ms": measured_ms})
        return outcome

    def manifest(self) -> dict[str, Any]:
        """Per-run manifest entry: the provider's plus the presentation order of the criteria."""
        return {**provider_manifest(self.provider), "option_order": self.option_order}

    def health(self) -> ProviderHealth:
        return asyncio.run_coroutine_threadsafe(
            self.provider.health(), self._loop._ensure()
        ).result(timeout=5.0)

    def close(self) -> None:
        self.cancel()
        self._loop.close()
        closer = getattr(self.provider, "close", None)
        if callable(closer):
            closer()
        self.telemetry.close()

    def cancel(self) -> None:
        """Permanently cancel this run's in-flight decision and its blocking transport."""
        with self._future_lock:
            self._cancelled = True
            future = self._future
        if future is not None:
            future.cancel()
        cancel = getattr(self.provider, "cancel", None)
        if callable(cancel):
            cancel()

    def __enter__(self) -> SyncDecider:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals ----------------------------------------------------------------------------

    async def _walk(
        self, frame: DecisionFrame, plan: DecisionPlan, deadline: float, trace: list[dict[str, Any]]
    ) -> ChoiceResult:
        node, path = plan.root, []
        results: list[ChoiceResult] = []
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise DeciderTimeout("System One decision budget used up")
            question = plan.stage_question(node, len(path))
            request_id = (
                frame.decision_id
                if not plan.hierarchical
                else f"{frame.decision_id}.s{len(results) + 1}"
            )
            stage: dict[str, Any] = {
                "name": question["name"],
                "request_id": request_id,
                "path": list(path),
                "criteria_keys": list(question["criteria"]),  # in the order they were sent
            }
            trace.append(stage)
            t0 = self._clock()
            res = await self.provider.decide(
                state=plan.stage_state(path),
                question=question,
                request_id=request_id,
                timeout_s=remaining,
            )
            stage["measured_ms"] = round((self._clock() - t0) * 1000.0, 3)
            meta = dict(getattr(res, "meta", None) or {})
            stage.update(
                {
                    **meta,
                    "choice": res.option_id,
                    "probabilities": dict(res.probabilities),
                    "confidence": res.confidence,
                    "truncated": res.truncated,
                    "model_id": res.model_id,
                    "raw_response_ref": res.raw_response_ref,
                }
            )
            if res.option_id not in node.criteria:  # defense in depth; providers check this too
                raise InvalidResponse(
                    "answer is not one of the criteria sent",
                    code=ErrorCode.E_CHOICE_NOT_IN_FRAME,
                )
            results.append(res)
            if node.leaf:
                break
            node = node.children[res.option_id]
            path.append(res.option_id)
        return self._combine(frame, plan, results)

    def _combine(
        self, frame: DecisionFrame, plan: DecisionPlan, results: list[ChoiceResult]
    ) -> ChoiceResult:
        leaf = results[-1]
        probs: dict[str, float] = dict(leaf.probabilities)
        if len(results) > 1 and probs:
            # partial joint: P(category chain) x P(option | category); the mass of categories that
            # were not entered is not known and is not invented. Full stage data is in the log.
            scale = 1.0
            for r in results[:-1]:
                p = r.probabilities.get(r.option_id)
                scale = scale * p if p is not None else 0.0
            probs = {k: v * scale for k, v in probs.items()} if scale > 0 else {}
        confs = [r.confidence for r in results if r.confidence is not None]
        return ChoiceResult(
            option_id=leaf.option_id,
            probabilities=probs,
            confidence=min(confs) if confs else None,  # weakest link; logged, never gating
            model_id=leaf.model_id,
            provider_id=leaf.provider_id,
            latency_ms=0.0,
            truncated=plan.truncated or any(r.truncated for r in results),
            raw_response_ref=";".join(r.raw_response_ref for r in results if r.raw_response_ref),
            decision_id=frame.decision_id,
        )

    def _record(
        self,
        frame: DecisionFrame,
        plan: DecisionPlan,
        trace: list[dict[str, Any]],
        outcome: ChoiceResult | None,
        error: BaseException | None,
        measured_ms: float,
    ) -> dict[str, Any]:
        code = getattr(error, "code", ErrorCode.E_DECIDER_FAILED) if error else None
        server_truncated = any(bool(s.get("truncated")) for s in trace)
        if error is None:
            label = "ok"
        elif isinstance(error, DeciderTimeout):
            label = "timeout"
        elif code in (ErrorCode.E_CHOICE_NOT_IN_FRAME, ErrorCode.E_CHOICE_MALFORMED):
            label = "invalid"
        else:
            label = "error"
        return {
            "decision_id": frame.decision_id,
            "run_id": frame.run_id,
            "snapshot_seq": frame.snapshot_seq,
            "scene_id": frame.scene_id,
            "goal_id": frame.goal_id,
            "option_set_hash": plan.option_set_hash,
            "option_count": plan.option_count,
            "state": dict(plan.state),
            "instructions": plan.instructions,
            "criteria": dict(plan.criteria),
            "plan": plan.audit(),
            "option_order": plan.option_order,
            # ids in the order the (last) question was sent in: the leaf question of a hierarchical
            # plan, where option ids are sent; the planned flat order if no request left
            "sent_order": list(trace[-1]["criteria_keys"]) if trace else list(plan.root.criteria),
            "stages": trace,
            "choice": outcome.option_id if outcome else None,
            "probabilities": dict(outcome.probabilities) if outcome else None,
            "confidence": outcome.confidence if outcome else None,
            "model_id": outcome.model_id if outcome else None,
            "provider_id": getattr(self.provider, "provider_id", "provider"),
            "provider_version": getattr(self.provider, "provider_version", ""),
            "prompt_schema_version": PROMPT_SCHEMA_VERSION,
            "truncated": bool(plan.truncated or server_truncated),
            "state_reduced": plan.truncated,
            "server_truncated": server_truncated,
            "usage_in": sum(int(s.get("usage_in") or 0) for s in trace) or None,
            "usage_out": sum(int(s.get("usage_out") or 0) for s in trace) or None,
            "attempts": sum(int(s.get("attempts") or 0) for s in trace) or None,
            "gate": "disabled",  # observe-first: confidence is logged, never used to gate
            "outcome": label,
            "error_code": code.value if code is not None else None,
            "error": self._scrub.text(str(error)) if error is not None else None,
            "measured_ms": measured_ms,
        }


def provider_manifest(provider: object, model_id: str = "") -> dict[str, Any]:
    """Per-run manifest entry (spec 8.1): provider_version, model_id, prompt_schema_version."""
    own = getattr(provider, "manifest", None)
    if callable(own):
        return dict(own())
    return {
        "provider_id": str(getattr(provider, "provider_id", "provider")),
        "provider_version": str(getattr(provider, "provider_version", "")),
        "model_id": model_id or str(getattr(provider, "model_id", "")),
        "prompt_schema_version": PROMPT_SCHEMA_VERSION,
    }
