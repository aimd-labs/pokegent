"""System One addon (spec chapter 8): the SYSTEM1 decision provider behind the frozen ``Decider``.

Map: ``provider.py`` (protocol, ``SyncDecider``, errors) | ``http_provider.py``
(HTTP client, breaker,
budget, validation) | ``questions.py`` (frame -> question plan, state budget, hierarchy) |
``policy.py`` (config) | ``telemetry.py`` (events, calibration JSONL) | ``mock.py`` |
``factory.py`` (config -> decider) | ``check.py`` (``pokegent provider check``).
"""

from .policy import ConfigError, SystemOneConfig, load_config
from .provider import (
    CircuitOpen,
    InvalidResponse,
    ProviderError,
    ProviderHealth,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
    RateBudgetExceeded,
    SyncDecider,
    SystemOneProvider,
)
from .questions import PROMPT_SCHEMA_VERSION, QuestionLimits, plan_decision

__all__ = [
    "PROMPT_SCHEMA_VERSION",
    "CircuitOpen",
    "ConfigError",
    "InvalidResponse",
    "ProviderError",
    "ProviderHealth",
    "ProviderRejected",
    "ProviderTimeout",
    "ProviderUnavailable",
    "QuestionLimits",
    "RateBudgetExceeded",
    "SyncDecider",
    "SystemOneConfig",
    "SystemOneProvider",
    "load_config",
    "plan_decision",
]
