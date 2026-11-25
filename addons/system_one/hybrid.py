"""Hybrid System One/agent handoff."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from typing import Any

from pokegent.actions._exec.models import ActionStatus
from pokegent.events import EventSink, EventType
from pokegent.options.models import (
    OBJECTIVE_RELATION_NOT_SERVING,
    AgentHandoffTimeout,
    ChoiceResult,
    DecisionFrame,
    Legality,
    Option,
    Risk,
)
from pokegent.server import ExternalDecider

from .provider import CircuitOpen, ProviderConfigurationError

#: Escalation reason used when more than one option claims the next route step.
ROUTE_STEP_ESCALATION = "multiple_route_steps"

#: Escalation reason used when System One's pick would confirm an irreversible action.
IRREVERSIBLE_ESCALATION = "irreversible_option"

#: Option-id prefixes that name a movement target the route can substitute for a pick.
NAVIGATION_ID_PREFIXES = frozenset({"warp", "edge", "fly", "tile"})

#: ``facts.target.kind`` values that name the same movement targets (doors and ladders are
#: warp options). Everything else (items, bicycle, NPC, heal, trainer, dialog) is not a
#: navigation choice, so its off-route marking never justifies a route-step replacement.
NAVIGATION_TARGET_KINDS = frozenset({"warp", "edge", "fly", "tile"})

#: Only a door/ladder warp or a map edge is a proven immediate route step for pre-provider
#: admission; fly and tile-walk options still wait for the decider. Each kind must also own the
#: exact producer executor/postcondition pair.
ROUTE_STEP_SHAPES = {
    "warp": ("field.enter_warp", "field.warp_done"),
    "edge": ("field.travel_edge", "field.edge_done"),
}

#: Escalation reason used when the only route step is high risk but not irreversible.
ROUTE_STEP_RISK_ESCALATION = "high_risk_route_step"


def _off_route(option: Option) -> bool:
    """Whether the frame's own facts put this option off the active route."""
    facts = option.facts
    return (
        facts.get("objective_relation") == OBJECTIVE_RELATION_NOT_SERVING
        or facts.get("route_relation") == "off route"
    )


def _next_route_step(option: Option) -> bool:
    """Whether the frame's own facts mark this option as the next route step."""
    facts = option.facts
    return (
        facts.get("next_route_step") is True
        or facts.get("route_step") is True
        or facts.get("route_relation") == "next route step"
    )


def _navigation(option: Option) -> bool:
    """Whether this option is a movement target (warp, edge, fly, door, ladder, tile walk)."""
    target = option.facts.get("target")
    kind = target.get("kind") if isinstance(target, Mapping) else None
    return (
        option.id.split(":", 1)[0] in NAVIGATION_ID_PREFIXES
        or kind in NAVIGATION_TARGET_KINDS
    )


def _route_step_kind(option: Option) -> str | None:
    """The exact producer kind only when the id prefix and target kind agree."""
    target = option.facts.get("target")
    kind = target.get("kind") if isinstance(target, Mapping) else None
    prefix = option.id.split(":", 1)[0]
    return kind if prefix == kind and kind in ROUTE_STEP_SHAPES else None


def _warp_or_edge(option: Option) -> bool:
    """Whether the option is a consistently shaped door/ladder warp or map edge."""
    return _route_step_kind(option) is not None


def _route_step_shape(option: Option) -> bool:
    """The option owns the exact executor/postcondition pair for its route step kind."""
    kind = _route_step_kind(option)
    if kind is None:
        return False
    executor, postcondition = ROUTE_STEP_SHAPES[kind]
    return option.executor_ref == executor and option.postcondition_ref == postcondition


def _non_competing(option: Option) -> bool:
    """A default face/swap control or a plain off-route warp/edge exit; never route-selected.

    The face/swap exemption is only the exact default-producer shape: legal, low risk,
    reversible and owning its documented executor.
    """
    if option.legality is not Legality.LEGAL or option.risk is Risk.HIGH or _irreversible(option):
        return False
    target = option.facts.get("target")
    kind = target.get("kind") if isinstance(target, Mapping) else None
    steps = option.facts.get("steps")
    if option.risk is Risk.LOW and (
        (
            option.id.startswith("face:")
            and kind == "face"
            and isinstance(steps, int)
            and not isinstance(steps, bool)
            and steps == 0
            and option.executor_ref == "field.face"
        )
        or (
            option.id.startswith("swap:")
            and option.facts.get("party_order") is True
            and option.executor_ref == "field.swap_party"
        )
    ):
        return True
    return (
        _route_step_shape(option)
        and _off_route(option)
        and not _next_route_step(option)
        and option.facts.get("on_route") is not True
        and option.facts.get("serves_goal") is not True
        and option.facts.get("route_needs") is None
        and option.facts.get("blocked_choice") is None
    )


def _irreversible(option: Option) -> bool:
    """Whether the option's model fields or facts mark it irreversible.

    The ``Option.irreversible`` flag needs prompt and effect text; families that carry the
    evidence in ``facts`` (fossil choices, item use with a lasting effect) set it there.
    """
    return option.irreversible is True or option.facts.get("irreversible") is True


def _route_step_choice(result: ChoiceResult, step: Option) -> ChoiceResult:
    """The replacement choice for a route step.

    The replacement is the planner's step, not a System One answer: it has no probability and
    no confidence, and the vetoed claim stays in the caller's metadata instead of being
    overwritten by a fabricated 1.0.
    """
    return result.model_copy(
        update={
            "option_id": step.id,
            "probabilities": {},
            "confidence": None,
            "model_id": "route-step-veto",
            "provider_id": "hybrid",
        }
    )


class HybridDecider:
    """System One first, with an HTTP override/escalation lane on the same server."""

    supervisor_control = True

    def __init__(
        self,
        system1: Any,
        *,
        port: int,
        external_timeout: float,
        agent_handoff_timeout_s: float = 300.0,
        escalate_below_confidence: float | Mapping[str, float] = 0.35,
        override_window_s: float = 0.0,
        escalate_on_no_progress: bool = True,
        escalate_unknown_scene: bool = True,
        force_agent_after_repeats: int = 2,
        autoplay: str = "off",
        events: EventSink | None = None,
    ) -> None:
        self._thresholds = self._validate_thresholds(escalate_below_confidence)
        if override_window_s < 0:
            raise ValueError("hybrid.override_window_s must be non-negative")
        if (
            isinstance(agent_handoff_timeout_s, bool)
            or not isinstance(agent_handoff_timeout_s, (int, float))
            or not math.isfinite(float(agent_handoff_timeout_s))
            or agent_handoff_timeout_s <= 0
        ):
            raise ValueError("hybrid.agent_handoff_timeout_s must be a positive finite number")
        if not isinstance(escalate_on_no_progress, bool):
            raise ValueError("hybrid.escalate_on_no_progress must be a boolean")
        self.escalate_on_no_progress = escalate_on_no_progress
        if not isinstance(escalate_unknown_scene, bool):
            raise ValueError("hybrid.escalate_unknown_scene must be a boolean")
        self.escalate_unknown_scene = escalate_unknown_scene
        self._no_progress_recovery = False
        if (
            isinstance(force_agent_after_repeats, bool)
            or not isinstance(force_agent_after_repeats, int)
            or force_agent_after_repeats < 1
        ):
            raise ValueError("hybrid.force_agent_after_repeats must be a positive integer")
        self.force_agent_after_repeats = force_agent_after_repeats
        self._repeat_key: tuple[str, str, str] | None = None
        self._repeat_count = 0
        self._decision_id: str | None = None
        self.system1 = system1
        self.agent = ExternalDecider(port, external_timeout, autoplay=autoplay)
        # Compatibility attribute; the bridge module is now server.
        self.external = self.agent
        self.escalate_below_confidence = escalate_below_confidence
        self.override_window_s = override_window_s
        self.agent_handoff_timeout_s = float(agent_handoff_timeout_s)
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

    def bind_decision_valid(self, valid: Any) -> None:
        bind = getattr(self.external, "bind_decision_valid", None)
        if callable(bind):
            bind(valid)

    def discard_stale_decision(self) -> None:
        self.external.discard_stale_decision()

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

    def request_unknown_scene_recovery(self) -> bool:
        """Use the same runner-owned bound even when no-progress recovery is disabled."""
        if not self.escalate_unknown_scene:
            return False
        self._no_progress_recovery = True
        return True

    def decide(self, frame: DecisionFrame) -> ChoiceResult:
        self._decision_id = frame.decision_id
        unknown = self.escalate_unknown_scene and frame.scene_id == "scene.unknown"
        if self._no_progress_recovery or unknown:
            reason = "unknown_scene" if unknown else "no_progress"
            self.last_decision = {
                "decided_by": "agent", "escalated": True, "escalation_reason": reason,
            }
            try:
                return self.external.offer(
                    frame,
                    metadata={"escalated": True, "escalation_reason": reason},
                    wait_timeout=self.agent_handoff_timeout_s,
                    interruptible_wait=True,
                    handoff_timeout=True,
                    handoff={
                        "ownership": "recovery_window",
                        "escalation_reason": reason,
                        "timeout_s": self.agent_handoff_timeout_s,
                    },
                )
            except AgentHandoffTimeout as exc:
                self._emit_handoff_expired(frame, reason, "recovery_window", exc)
                raise
        scope = (frame.scene_id, frame.state_fingerprint)
        # Offering or discarding a choice is not an executed action. Only result accounting
        # updates the streak, including after a forced agent choice has actually completed.
        force_agent = (self._repeat_key is not None and self._repeat_key[:2] == scope
                       and self._repeat_count >= self.force_agent_after_repeats)
        started = time.monotonic()
        system_result: ChoiceResult | None = None
        system_error: Exception | None = None
        try:
            if not force_agent:
                system_result = self.system1.decide(frame)
        except ProviderConfigurationError:
            raise
        except Exception as exc:  # provider and breaker failures both escalate
            system_error = exc

        system_claim = system_result
        veto_result, veto_reason = self._route_step_veto(frame, system_claim)
        # The choice that would actually be applied: the vetoed route step when there is one.
        served = veto_result if veto_result is not None else system_claim
        vetoed = veto_result is not None or veto_reason is not None
        confidence = system_claim.confidence if system_claim is not None else None
        # Confidence is not evidence about an irreversible action; the post-veto choice decides.
        irreversible = self._irreversible_choice(frame, served)
        # The decision's own exception is the availability evidence; no health probe. Only a
        # breaker-open failure is circuit-open: an unavailable provider (connection/429/5xx) is
        # an outage, not a breaker. ``SyncDecider.health()`` stays for status calls only.
        circuit_open = isinstance(system_error, CircuitOpen)
        stuck = self._stuck
        self._stuck = False
        threshold = self._thresholds.get(frame.scene_id, self._thresholds["default"])
        escalate = (
            system_claim is None
            or circuit_open
            or stuck
            or irreversible
            or veto_reason is not None
            or (veto_result is None and (confidence is None or confidence <= threshold))
        )
        if not escalate and self.override_window_s <= 0:
            self.last_decision = {"decided_by": "system1", "escalated": False}
            if vetoed and system_claim is not None:
                self.last_decision.update(
                    vetoed_by="route_step",
                    system1_option_id=system_claim.option_id,
                    system1_confidence=confidence,
                )
            return served

        metadata: dict[str, Any] = {
            "system1": {
                "option_id": system_claim.option_id if system_claim else None,
                "probabilities": dict(system_claim.probabilities) if system_claim else {},
                "confidence": confidence,
            },
            "escalated": escalate,
            "escalation_reason": (
                "repeated_option"
                if force_agent
                else "stuck"
                if stuck
                else "circuit_open"
                if circuit_open
                else "system1_error"
                if system_error
                else IRREVERSIBLE_ESCALATION
                if irreversible
                else veto_reason
                if veto_reason is not None
                else "low_confidence"
                if confidence is not None
                else "system1_unavailable"
            ),
        }
        if vetoed and system_claim is not None:
            # The served frame records why System One's claim was not followed; the original
            # claim stays in the ``system1`` block and in this explicit copy.
            metadata["vetoed_by"] = "route_step"
            metadata["vetoed_option"] = {
                "option_id": system_claim.option_id,
                "confidence": confidence,
            }
        if system_claim is None and system_error is not None:
            metadata["escalation_reason"] = (
                "circuit_open" if circuit_open else "system1_unavailable"
            )
        if escalate and not force_agent and self._events is not None:
            self._events.emit(
                EventType.DECISION_SYSTEMONE_COMPLETED,
                scene_id=frame.scene_id,
                request_id=frame.decision_id,
                provider="system1",
                outcome="rejected" if system_claim is not None else "failed",
                details={
                    "option_id": system_claim.option_id if system_claim else None,
                    "choice": system_claim.option_id if system_claim else None,
                    "confidence": confidence,
                    "measured_ms": round((time.monotonic() - started) * 1000, 3),
                    "accepted": False,
                    "escalation_reason": metadata["escalation_reason"],
                    **(
                        {
                            "vetoed_by": metadata["vetoed_by"],
                            "vetoed_option": metadata["vetoed_option"],
                        }
                        if "vetoed_option" in metadata
                        else {}
                    ),
                },
            )
        self.last_decision = {
            "decided_by": "agent" if escalate else "system1",
            "escalated": escalate,
            "escalation_reason": metadata["escalation_reason"],
        }
        offer_kwargs: dict[str, Any] = {
            "metadata": metadata,
            "default": served if not escalate else None,
            "wait_timeout": (
                self.agent_handoff_timeout_s if escalate else self.override_window_s
            ),
            "interruptible_wait": escalate,
            "handoff_timeout": escalate,
        }
        if escalate:
            offer_kwargs["handoff"] = {
                "ownership": "escalated",
                "escalation_reason": metadata["escalation_reason"],
                "timeout_s": self.agent_handoff_timeout_s,
            }
        try:
            result = self.external.offer(frame, **offer_kwargs)
        except AgentHandoffTimeout as exc:
            self._emit_handoff_expired(
                frame, metadata["escalation_reason"], "escalated", exc
            )
            raise
        decided_by = "agent" if result.provider_id in {"agent", "external"} else "system1"
        self.last_decision = {
            "decided_by": decided_by,
            "escalated": escalate,
            "escalation_reason": metadata["escalation_reason"],
        }
        if decided_by == "agent" and system_claim is not None:
            self.last_decision.update(
                system1_option_id=system_claim.option_id,
                system1_confidence=system_claim.confidence,
            )
        if vetoed and system_claim is not None:
            self.last_decision.update(
                vetoed_by="route_step",
                system1_option_id=system_claim.option_id,
                system1_confidence=confidence,
            )
        return result

    def deterministic_route_step(self, frame: DecisionFrame) -> ChoiceResult | None:
        """Admit one uniquely proven warp/edge step before the provider; every residual case
        keeps the existing decider/veto path. The shortcut never selects or reclassifies the
        generic face/swap UI options and never fabricates a confidence.
        """
        if self._no_progress_recovery or self._stuck or self.override_window_s > 0:
            return None
        scope = (frame.scene_id, frame.state_fingerprint)
        if (
            self._repeat_key is not None
            and self._repeat_key[:2] == scope
            and self._repeat_count >= self.force_agent_after_repeats
        ):
            return None
        if frame.goal_id is None or frame.scene_id != "field.free":
            return None
        navigation = frame.context.get("navigation")
        if isinstance(navigation, Mapping) and navigation.get("unmet"):
            return None
        if frame.context.get("team_needs"):
            return None
        chooseable = frame.chooseable()
        if any(
            option.facts.get("route_needs") is not None
            or option.facts.get("blocked_choice") is not None
            or option.facts.get("selection_reason") == "repeated_failure"
            or option.risk is Risk.HIGH
            or _irreversible(option)
            for option in chooseable
        ):
            return None
        steps = [
            option
            for option in chooseable
            if _next_route_step(option) and _warp_or_edge(option)
        ]
        if len(steps) != 1:
            return None
        step = steps[0]
        if (
            step.legality is not Legality.LEGAL
            or step.facts.get("on_route") is not True
            or step.facts.get("serves_goal") is not True
            or not _route_step_shape(step)
        ):
            return None
        for option in chooseable:
            if option.id == step.id:
                continue
            if (
                _next_route_step(option)
                or option.facts.get("on_route") is True
                or option.facts.get("serves_goal") is True
                or not _non_competing(option)
            ):
                return None
        self._decision_id = frame.decision_id
        self.last_decision = {"decided_by": "route", "escalated": False}
        return ChoiceResult(
            option_id=step.id,
            probabilities={},
            confidence=None,
            model_id="route-step",
            provider_id="route",
            decision_id=frame.decision_id,
        )

    @staticmethod
    def _route_step_veto(
        frame: DecisionFrame, result: ChoiceResult | None
    ) -> tuple[ChoiceResult | None, str | None]:
        """Veto a System One pick that the frame's route facts put off the objective.

        Return ``(replacement, escalate_reason)``. A System One answer is a claim, and an
        off-route claim is followed literally: on Mt Moon 1F the frame marked the warp back
        out to Route 4 as not serving the objective while the planner's next route step was
        the B2F ladder, so accepting it looped the agent in and out of the cave. When the
        claim is off route and the frame names exactly one next route step, that step
        replaces the claim (the model's confidence is not evidence about a different
        option), and the replacement carries no confidence of its own. A single route step
        that is irreversible goes to the caller's irreversible gate, and one that is only
        high risk escalates (``ROUTE_STEP_RISK_ESCALATION``) rather than being applied.
        Several candidate route steps are an ambiguity the facts do not resolve, so the frame
        escalates (``ROUTE_STEP_ESCALATION``) instead of the veto choosing one. Only
        navigation targets (warp, edge, fly, door, ladder, tile walk) are vetoed: an item,
        bicycle, NPC, heal or trainer pick keeps the ordinary confidence gate even when it is
        off route. An on-route claim, a frame without an objective or route facts and a choice
        outside the frame are left unchanged.
        """
        if result is None or frame.goal_id is None:
            # No objective, no route to defend.
            return None, None
        chooseable = frame.chooseable()
        chosen = next((option for option in chooseable if option.id == result.option_id), None)
        if chosen is None or not _navigation(chosen) or not _off_route(chosen):
            return None, None
        route_steps = [option for option in chooseable if _next_route_step(option)]
        if not route_steps or any(option.id == chosen.id for option in route_steps):
            return None, None
        if len(route_steps) > 1:
            return None, ROUTE_STEP_ESCALATION
        step = route_steps[0]
        if _irreversible(step):
            # The caller's irreversible gate hands the replacement to the agent, with no
            # default; the veto itself never applies a step that cannot be undone.
            return _route_step_choice(result, step), None
        if step.risk is Risk.HIGH:
            # A high-risk step (a route trainer battle) is the agent's call, not a silent
            # substitution for System One's off-route claim.
            return None, ROUTE_STEP_RISK_ESCALATION
        return _route_step_choice(result, step), None

    @staticmethod
    def _irreversible_choice(frame: DecisionFrame, result: ChoiceResult | None) -> bool:
        """Whether System One's pick is an option marked irreversible.

        Confidence is not evidence about an action that cannot be undone, so such a pick is
        always handed to the agent instead of being applied; frames without an irreversible
        pick are unchanged.
        """
        if result is None:
            return False
        chosen = next(
            (option for option in frame.chooseable() if option.id == result.option_id), None
        )
        return chosen is not None and _irreversible(chosen)

    def _emit_handoff_expired(
        self, frame: DecisionFrame, reason: str, ownership: str, exc: Exception
    ) -> None:
        """Record one bounded handoff expiry without inventing a fallback choice."""
        if self._events is None:
            return
        self._events.emit(
            EventType.DECISION_AGENT_EXPIRED,
            scene_id=frame.scene_id,
            request_id=frame.decision_id,
            provider="agent",
            outcome="expired",
            public_visibility=True,
            details={
                "decision_id": frame.decision_id,
                "reason": reason,
                "ownership": ownership,
                "timeout_s": self.agent_handoff_timeout_s,
                "stop_reason": str(exc),
            },
        )

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
        changed = result.progressed is True or (
            result.progressed is None
            and result.status is ActionStatus.APPLIED
            and result.verified_outcome is True
            and result.before_hash is not None
            and result.after_hash is not None
            and result.before_hash != result.after_hash
        )
        if (
            frame.decision_id != self._decision_id
            or decided_by not in {"system1", "route"}
            or changed
        ):
            self._repeat_key = None
            self._repeat_count = 0
        else:
            key = (frame.scene_id, frame.state_fingerprint, result.option_id)
            self._repeat_count = self._repeat_count + 1 if key == self._repeat_key else 1
            self._repeat_key = key
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
