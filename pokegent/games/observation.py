"""Opt-in generic observation envelope. Internal SDK data only: not a provider
payload and not a redaction claim, so projected fields are not safe by default."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

SCHEMA_VERSION = 1
MAX_ID_LENGTH = 64
MAX_KEY_LENGTH = 64
MAX_STRING_LENGTH = 512
MAX_PAYLOAD_FIELDS = 32

Scalar = str | int | float | bool | None


class ObservationRejected(ValueError):
    """An observation or creation request violates the envelope contract."""


class TransitionRejected(ObservationRejected):
    """A candidate observation is not a valid successor."""


def _identity(value: object, field_name: str) -> None:
    if type(value) is not str or not value.strip() or len(value) > MAX_ID_LENGTH:
        raise ObservationRejected(f"{field_name} must be a bounded non-empty string")


def _counter(value: object, field_name: str) -> None:
    if type(value) is not int or value < 0:
        raise ObservationRejected(f"{field_name} must be a non-negative plain int")


def _frozen_payload(payload: object) -> Mapping[str, Scalar]:
    if not isinstance(payload, Mapping):
        raise ObservationRejected("payload must be a flat mapping of JSON scalars")
    if len(payload) > MAX_PAYLOAD_FIELDS:
        raise ObservationRejected("payload has too many fields")
    copied: dict[str, Scalar] = {}
    for key, value in payload.items():
        if type(key) is not str or not key or len(key) > MAX_KEY_LENGTH:
            raise ObservationRejected("payload keys must be non-empty bounded strings")
        if type(value) is str and len(value) > MAX_STRING_LENGTH:
            raise ObservationRejected("payload strings are bounded")
        if type(value) is float and not math.isfinite(value):
            raise ObservationRejected("payload floats must be finite")
        if value is not None and type(value) not in (bool, int, str, float):
            raise ObservationRejected("payload values must be JSON scalars")
        copied[key] = value
    return MappingProxyType(copied)


@dataclass(frozen=True, slots=True)
class GameObservation:
    """Versioned immutable observation; `usable=False` means state is unknown."""

    schema_version: int
    game_id: str
    profile_id: str
    snapshot_seq: int
    restore_epoch: int
    usable: bool
    payload: Mapping[str, Scalar]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ObservationRejected(f"schema_version must be {SCHEMA_VERSION}")
        _identity(self.game_id, "game_id")
        _identity(self.profile_id, "profile_id")
        _counter(self.snapshot_seq, "snapshot_seq")
        _counter(self.restore_epoch, "restore_epoch")
        if type(self.usable) is not bool:
            raise ObservationRejected("usable must be an exact bool")
        object.__setattr__(self, "payload", _frozen_payload(self.payload))

    def require_usable(self) -> GameObservation:
        if not self.usable:
            raise ObservationRejected("observation is unusable; its state is unknown")
        return self


def create_observation(
    *,
    enabled: bool = False,
    game_id: str,
    profile_id: str,
    snapshot_seq: int,
    restore_epoch: int,
    usable: bool,
    payload: Mapping[str, Scalar],
    schema_version: int = SCHEMA_VERSION,
) -> GameObservation:
    if type(enabled) is not bool:
        raise ObservationRejected("enabled must be an exact bool")
    if not enabled:
        raise ObservationRejected("observation envelope is opt-in; pass enabled=True")
    return GameObservation(
        schema_version, game_id, profile_id, snapshot_seq, restore_epoch, usable, payload
    )


def validate_transition(previous: GameObservation, current: GameObservation) -> str:
    if not isinstance(previous, GameObservation) or not isinstance(current, GameObservation):
        raise ObservationRejected("transition operands must be GameObservation values")
    if previous.game_id != current.game_id or previous.profile_id != current.profile_id:
        raise TransitionRejected("game and profile identity must match")
    if current.restore_epoch < previous.restore_epoch:
        raise TransitionRejected("restore_epoch cannot move backwards")
    if current.restore_epoch > previous.restore_epoch:
        return "epoch_advanced"
    if current.snapshot_seq <= previous.snapshot_seq:
        raise TransitionRejected("snapshot_seq must increase within a restore_epoch")
    return "sequence_advanced"
