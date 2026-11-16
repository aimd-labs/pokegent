"""Config -> ready ``SyncDecider`` (the one place that wires provider, telemetry and limits)."""

from __future__ import annotations

import os
import time
from collections.abc import Callable

from pokegent.events import EventSink

from .http_provider import HttpProvider
from .mock import MockProvider, NullProvider
from .policy import RUN_ID_PLACEHOLDER, SystemOneConfig
from .provider import SyncDecider, SystemOneProvider
from .questions import QuestionLimits
from .redact import Scrubber
from .telemetry import CalibrationLog, DecisionTelemetry

# The public provider identifier is neutral; the hybrid decider is implemented in
# ``addons.system_one.hybrid`` alongside this optional provider.


def build_provider(
    config: SystemOneConfig, *, scrubber: Scrubber | None = None
) -> SystemOneProvider:
    if not config.enabled:
        return NullProvider()
    if config.primary == "mock":
        return MockProvider()
    return HttpProvider(config, scrubber=scrubber)


def build_decider(
    config: SystemOneConfig,
    *,
    events: EventSink | None = None,
    emit_events: bool = False,
    calibration_path: str | os.PathLike[str] | None = None,
    provider: SystemOneProvider | None = None,
    clock: Callable[[], float] = time.monotonic,
    expose_latency: bool = False,
) -> SyncDecider:
    """Build the decider for ``config``.

    ``emit_events`` stays False when a Runner drives the decider (it emits ``decision.systemone.*``
    itself); set it for tools that call the decider directly.
    """
    scrubber = Scrubber([config.api_key_value() or ""])
    path = calibration_path or config.calibration.log_path
    if path and RUN_ID_PLACEHOLDER in os.fspath(path):
        # never create a directory literally named "<run-id>": the caller resolves it first
        # (`policy.resolve_log_path`; `pokegent.app.build_decider` does)
        raise ValueError(
            f"the calibration path contains {RUN_ID_PLACEHOLDER!r}; resolve it with "
            "addons.system_one.policy.resolve_log_path() and pass calibration_path="
        )
    telemetry = DecisionTelemetry(
        events=events,
        calibration=CalibrationLog(path, scrubber=scrubber) if path else None,
        emit_events=emit_events,
        scrubber=scrubber,
        provider_id=config.primary,
    )
    return SyncDecider(
        provider or build_provider(config, scrubber=scrubber),
        limits=QuestionLimits(
            max_state_tokens=config.max_state_tokens,
            max_options_per_question=config.max_options_per_question,
        ),
        timeout_s=config.timeout_seconds,
        option_order=config.option_order,
        telemetry=telemetry,
        expose_latency=expose_latency,
        clock=clock,
    )
