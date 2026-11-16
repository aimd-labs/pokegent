"""Hybrid System One/agent handoff."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from typing import Any

from pokegent.events import EventSink, EventType
from pokegent.options.models import ChoiceResult, DecisionFrame
from pokegent.server import ExternalDecider


class HybridDecider:
    """System One first, with an HTTP override/escalation lane on the same server."""

    supervisor_control = True

    def __init__(
        self,
        system1: Any,
        *,
        port: int,
        external_timeout: float,
        escalate_below_confidence: float | Mapping[str, float] = 0.35,
        override_window_s: float = 0.0,
        escalate_on_no_progress: bool = True,
        autoplay: str = "off",
        events: EventSink | None = None,
    ) -> None:
        self._thresholds = self._validate_thresholds(escalate_below_confidence)
        if override_window_s < 0:
            raise ValueError("hybrid.override_window_s must be non-negative")
        if not isinstance(escalate_on_no_progress, bool):
            raise ValueError("hybrid.escalate_on_no_progress must be a boolean")
        self.escalate_on_no_progress = escalate_on_no_progress
        self._no_progress_recovery = False
        self.system1 = system1
        self.agent = ExternalDecider(port, external_timeout, autoplay=autoplay)
        # Compatibility attribute; the bridge module is now server.
        self.external = self.agent
        self.escalate_below_confidence = escalate_below_confidence
        self.override_window_s = override_window_s
        self.last_decision: dict[str, Any] = {}
        self._stuck = False
        self._events = events
        self.external.bind_agent_request(self._agent_requested)

    @property
    def port(self) -> int:
        return self.external.port

    def attach_screen_source(self, source: Any) -> None:
        self.external.attach_screen_source(source)

    def bind_planning(self, store: Any) -> None:
        self.external.bind_planning(store)

    def bind_pending_frame(self, refresh: Any) -> None:
        """Use the runner's same-snapshot refresh for escalated agent choices."""
        self.external.bind_pending_frame(refresh)

    def take_decided_frame(self) -> DecisionFrame | None:
        """Adopt the frame paired with a choice refreshed while the agent waited."""
        return self.external.take_decided_frame()

    def update_briefing(
        self,
        snapshot: Any,
        tables: Any = None,
        world: Any = None,
        events: Any = None,
        frame: Any = None,
    ) -> None:
        self.external.update_briefing(snapshot, tables, world, events, frame)

    def request_no_progress_recovery(self) -> bool:
        """Reserve one agent-owned recovery window; the runner owns its bound."""
        if not self.escalate_on_no_progress:
            return False
        self._no_progress_recovery = True
        return True

    def notify_progress(self) -> None:
        self._no_progress_recovery = False

    def decide(self, frame: DecisionFrame) -> ChoiceResult:
        if self._no_progress_recovery:
            self.last_decision = {
                "decided_by": "agent", "escalated": True, "escalation_reason": "no_progress",
            }
            return self.external.offer(
                frame,
                metadata={"escalated": True, "escalation_reason": "no_progress"},
                wait_timeout=None,
                interruptible_wait=True,
            )
        started = time.monotonic()
        system_result: ChoiceResult | None = None
        system_error: Exception | None = None
        try:
            system_result = self.system1.decide(frame)
        except Exception as exc:  # provider and breaker failures both escalate
            system_error = exc

        confidence = system_result.confidence if system_result is not None else None
        health = getattr(self.system1, "health", None)
        circuit_open = False
        if callable(health):
            try:
                state = health().state
                circuit_open = state == "open"
            except Exception:
                circuit_open = True
        stuck = self._stuck
        self._stuck = False
        threshold = self._thresholds.get(frame.scene_id, self._thresholds["default"])
        escalate = (
            system_result is None
            or circuit_open
            or stuck
            or confidence is None
            or confidence <= threshold
        )
        if not escalate and self.override_window_s <= 0:
            self.last_decision = {"decided_by": "system1", "escalated": False}
            return system_result

        metadata: dict[str, Any] = {
            "system1": {
                "option_id": system_result.option_id if system_result else None,
                "probabilities": dict(system_result.probabilities) if system_result else {},
                "confidence": confidence,
            },
            "escalated": escalate,
            "escalation_reason": (
                "stuck"
                if stuck
                else "system1_error"
                if system_error
                else "circuit_open"
                if circuit_open
                else "low_confidence"
                if confidence is not None
                else "system1_unavailable"
            ),
        }
        if system_result is None and system_error is not None:
            metadata["escalation_reason"] = "system1_unavailable"
        if escalate and self._events is not None:
            self._events.emit(
                EventType.DECISION_SYSTEMONE_COMPLETED,
                scene_id=frame.scene_id,
                request_id=frame.decision_id,
                provider="system1",
                outcome="rejected" if system_result is not None else "failed",
                details={
                    "option_id": system_result.option_id if system_result else None,
                    "choice": system_result.option_id if system_result else None,
                    "confidence": confidence,
                    "measured_ms": round((time.monotonic() - started) * 1000, 3),
                    "accepted": False,
                    "escalation_reason": metadata["escalation_reason"],
                },
            )
        self.last_decision = {
            "decided_by": "agent" if escalate else "system1",
            "escalated": escalate,
            "escalation_reason": metadata["escalation_reason"],
        }
        result = self.external.offer(
            frame,
            metadata=metadata,
            default=system_result if not escalate else None,
            wait_timeout=None if escalate else self.override_window_s,
            interruptible_wait=escalate,
        )
        decided_by = "agent" if result.provider_id in {"agent", "external"} else "system1"
        self.last_decision = {
            "decided_by": decided_by,
            "escalated": escalate,
            "escalation_reason": metadata["escalation_reason"],
        }
        if decided_by == "agent" and system_result is not None:
            self.last_decision.update(
                system1_option_id=system_result.option_id,
                system1_confidence=system_result.confidence,
            )
        return result

    def _agent_requested(self, frame_id: str, frame: DecisionFrame) -> None:
        if self._events is None:
            return
        self._events.emit(
            EventType.DECISION_AGENT_REQUESTED,
            scene_id=frame.scene_id,
            request_id=frame.decision_id,
            provider="agent",
            outcome="waiting",
            details={
                "frame_id": frame_id,
                "escalation_reason": self.last_decision.get("escalation_reason", "low_confidence"),
            },
        )

    @staticmethod
    def _validate_thresholds(value: float | Mapping[str, float]) -> dict[str, float]:
        if isinstance(value, Mapping):
            thresholds: dict[str, float] = {"default": 0.35}
            for scene_id, threshold in value.items():
                if not isinstance(scene_id, str):
                    raise ValueError(
                        "hybrid.escalate_below_confidence mapping keys must be strings"
                    )
                if scene_id != "default" and not scene_id:
                    raise ValueError("hybrid.escalate_below_confidence scene ids must not be empty")
                if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
                    raise ValueError(
                        "hybrid.escalate_below_confidence values must be numbers between 0 and 1"
                    )
                threshold = float(threshold)
                if not math.isfinite(threshold) or not 0 <= threshold <= 1:
                    raise ValueError(
                        "hybrid.escalate_below_confidence values must be between 0 and 1"
                    )
                thresholds[scene_id] = threshold
            return thresholds
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("hybrid.escalate_below_confidence must be a number or mapping")
        value = float(value)
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("hybrid.escalate_below_confidence must be between 0 and 1")
        return {"default": value}

    def record_action_result(self, frame: DecisionFrame, result: Any) -> None:
        decided_by = self.last_decision.get("decided_by") or "system1"
        self.external.record_action_result(
            frame,
            result,
            decided_by=decided_by,
        )

    def notify_stuck(self) -> None:
        """Make the next frame an external escalation after the runner's progress detector fires."""
        self._stuck = True

    def mark_autoplay(self, frame: DecisionFrame, option_id: str, reason: str) -> None:
        self.external.mark_autoplay(frame, option_id, reason, decided_by="system1")

    def interrupt_requested(self) -> bool:
        return self.external.interrupt_requested()

    def clear_interrupt(self) -> None:
        self.external.clear_interrupt()

    def macro_started(self) -> None:
        self.external.macro_started()

    def macro_finished(self) -> None:
        self.external.macro_finished()

    def clear_autoplay_pending(self) -> None:
        self.external.clear_autoplay_pending()

    def set_autoplay(self, mode: str) -> None:
        self.external.set_autoplay(mode)

    def cancel(self) -> None:
        """Wake either half of a hybrid decision during process shutdown."""
        self.external.cancel()
        cancel = getattr(self.system1, "cancel", None)
        if callable(cancel):
            cancel()

    def close(self) -> None:
        self.external.close()
        close = getattr(self.system1, "close", None)
        if callable(close):
            close()

    def health(self) -> Any:
        return self.system1.health()

    def manifest(self) -> dict[str, Any]:
        return {"provider_id": "hybrid", "system1": getattr(self.system1, "manifest", lambda: {})()}
