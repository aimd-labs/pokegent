"""Opt-in, frame-bound legal-choice SDK facade for an already admitted plugin.

This module performs local validation only. It never reads a snapshot, calls a backend, executor,
callback or game input, and it exposes no raw snapshot, hidden option, executor argument or
redaction/title-neutral projection claim. Disabled unless the caller explicitly opts in.
"""

from __future__ import annotations

from dataclasses import dataclass

from pokegent.games.observation import (
    GameObservation,
    TransitionRejected,
    validate_transition,
)
from pokegent.games.plugin import PluginSelection
from pokegent.options.models import ChoiceResult, DecisionFrame, check_choice


class ChoiceSdkRejected(ValueError):
    """The SDK refused an observation, offer or submission."""


@dataclass(frozen=True, slots=True)
class OfferedChoice:
    """One visible, chooseable option as the SDK exposes it."""

    option_id: str
    label: str


@dataclass(frozen=True, slots=True)
class ChoiceOffer:
    """The validated offer bound to one observation generation."""

    decision_id: str
    snapshot_seq: int
    restore_epoch: int
    state_fingerprint: str
    options: tuple[OfferedChoice, ...]


@dataclass(frozen=True, slots=True)
class _Pending:
    frame: DecisionFrame
    offer: ChoiceOffer


class ChoiceSdk:
    """Bind caller-supplied frames and results to one selected plugin and observation."""

    def __init__(self, selection: PluginSelection, *, enabled: bool = False) -> None:
        if not isinstance(selection, PluginSelection):
            raise TypeError("selection must be an admitted PluginSelection")
        if type(enabled) is not bool:
            raise TypeError("enabled must be exactly bool")
        self._selection = selection
        self._enabled = enabled
        self._observation: GameObservation | None = None
        self._pending: _Pending | None = None

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise ChoiceSdkRejected("choice SDK is opt-in; construct it with enabled=True")

    def observe(self, observation: GameObservation) -> None:
        """Accept a strictly newer observation and invalidate any pending frame."""
        self._require_enabled()
        if not isinstance(observation, GameObservation):
            raise ChoiceSdkRejected("observation must be a GameObservation")
        descriptor = self._selection.descriptor
        if (
            observation.game_id != descriptor.game
            or observation.profile_id != descriptor.profile_id
        ):
            raise ChoiceSdkRejected("observation identity does not match the selected plugin")
        if self._observation is not None:
            try:
                validate_transition(self._observation, observation)
            except TransitionRejected as exc:
                raise ChoiceSdkRejected(f"observation transition rejected: {exc}") from exc
        self._observation = observation
        self._pending = None

    def offer(self, frame: DecisionFrame) -> ChoiceOffer:
        """Offer the current observation's visible choices; any prior offer is replaced."""
        self._require_enabled()
        self._pending = None
        if not isinstance(frame, DecisionFrame):
            raise ChoiceSdkRejected("offer requires a DecisionFrame")
        current = self._observation
        if current is None:
            raise ChoiceSdkRejected("no observation has been accepted")
        if not current.usable:
            raise ChoiceSdkRejected("current observation is unusable")
        if type(frame.snapshot_seq) is not int:
            raise ChoiceSdkRejected("frame snapshot_seq must be a plain int")
        if frame.snapshot_seq != current.snapshot_seq:
            raise ChoiceSdkRejected("frame is stale for the current observation")
        if not frame.decision_id.strip():
            raise ChoiceSdkRejected("frame decision_id must be non-empty")
        if not frame.state_fingerprint.strip():
            raise ChoiceSdkRejected("frame state_fingerprint must be non-empty")
        offer = ChoiceOffer(
            decision_id=frame.decision_id,
            snapshot_seq=frame.snapshot_seq,
            restore_epoch=current.restore_epoch,
            state_fingerprint=frame.state_fingerprint,
            options=tuple(
                OfferedChoice(option_id=option.id, label=option.label)
                for option in frame.chooseable()
            ),
        )
        self._pending = _Pending(frame=frame, offer=offer)
        return offer

    def submit(self, result: ChoiceResult) -> ChoiceResult:
        """Validate a result against the pending frame, then consume that frame on success."""
        self._require_enabled()
        pending = self._pending
        self._pending = None
        if not isinstance(result, ChoiceResult):
            raise ChoiceSdkRejected("submission must be a ChoiceResult")
        if pending is None:
            raise ChoiceSdkRejected("no pending frame; offer a fresh frame first")
        if result.decision_id != pending.offer.decision_id:
            raise ChoiceSdkRejected("result decision_id does not match the pending frame")
        error = check_choice(pending.frame, result, allow_all=False)
        if error is not None:
            raise ChoiceSdkRejected(f"choice rejected: {error.value}")
        return result
