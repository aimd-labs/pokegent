"""Deterministic offline providers: :class:`MockProvider` (scripted) and :class:`NullProvider`.

``MockProvider`` answers from a strategy (``first``, ``last``, a callable) or a per-call script
whose items are an option key, an exception to raise, or a callable. No network, no randomness;
the delay is an ``asyncio.sleep`` so timeout paths can be tested. It validates nothing the real
provider would not: the key it returns is checked by the caller like any other.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from pokegent.options.models import ChoiceResult

from .provider import ProviderChoice, ProviderHealth, ProviderUnavailable
from .questions import PROMPT_SCHEMA_VERSION

Strategy = str | Callable[[dict, dict], str]


class MockProvider:
    provider_id = "mock"
    provider_version = "mock/1"
    model_id = "mock"

    def __init__(
        self,
        strategy: Strategy = "first",
        *,
        script: Iterable[Any] = (),
        delay_s: float = 0.0,
        with_probabilities: bool = True,
        truncated: bool = False,
    ) -> None:
        self.strategy = strategy
        self._script = list(script)
        self.delay_s = delay_s
        self.with_probabilities = with_probabilities
        self.truncated = truncated
        self.calls: list[dict[str, Any]] = []

    def _pick(self, state: dict, question: dict) -> str:
        keys: Sequence[str] = list(question["criteria"])
        if self._script:
            item = self._script.pop(0)
            if isinstance(item, BaseException):
                raise item
            if callable(item):
                return item(state, question)
            return item
        if callable(self.strategy):
            return self.strategy(state, question)
        return keys[-1] if self.strategy == "last" else keys[0]

    async def decide(
        self, *, state: dict, question: dict, request_id: str, timeout_s: float
    ) -> ChoiceResult:
        self.calls.append(
            {"state": state, "question": question, "request_id": request_id, "timeout_s": timeout_s}
        )
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        key = self._pick(state, question)
        keys = list(question["criteria"])
        probs: dict[str, float] = {}
        confidence = None
        if self.with_probabilities and key in keys:
            rest = [k for k in keys if k != key]
            top = 1.0 if not rest else 0.7
            probs = {key: top, **{k: (1.0 - top) / len(rest) for k in rest}}
            confidence = top
        return ProviderChoice(
            option_id=key,
            probabilities=probs,
            confidence=confidence,
            model_id=self.model_id,
            provider_id=self.provider_id,
            truncated=self.truncated,
            raw_response_ref=f"mock:{request_id}",
            meta={"attempts": 1},
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            ok=True,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            model_id=self.model_id,
            prompt_schema_version=PROMPT_SCHEMA_VERSION,
            requests=len(self.calls),
        )


class NullProvider:
    """System One disabled: every call fails, so the runner escalates instead of guessing."""

    provider_id = "null"
    provider_version = "null/1"
    model_id = ""

    async def decide(
        self, *, state: dict, question: dict, request_id: str, timeout_s: float
    ) -> ChoiceResult:
        raise ProviderUnavailable("System One is disabled in the configuration")

    async def health(self) -> ProviderHealth:
        return ProviderHealth(ok=False, state="closed", provider_id=self.provider_id)
