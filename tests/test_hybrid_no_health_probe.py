"""HybridDecider decides from the decision outcome; it never probes provider health."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from addons.system_one import hybrid
from addons.system_one.http_provider import HttpProvider
from addons.system_one.hybrid import HybridDecider
from addons.system_one.policy import SystemOneConfig
from addons.system_one.provider import (
    CircuitOpen,
    ProviderConfigurationError,
    ProviderTimeout,
    ProviderUnavailable,
)
from pokegent.options.models import ChoiceResult, DecisionFrame, Legality, Option


def frame() -> DecisionFrame:
    return DecisionFrame(
        decision_id="dec-1",
        run_id="synthetic",
        snapshot_seq=1,
        scene_id="field.free",
        scene_confidence=1,
        state_fingerprint="sha256:" + "0" * 64,
        options=tuple(
            Option(id=key, label=key, legality=Legality.LEGAL, executor_ref="test.action")
            for key in ("sign", "walk")
        ),
    )


def claim(option_id: str = "sign", *, confidence: float) -> ChoiceResult:
    return ChoiceResult(
        option_id=option_id,
        probabilities={option_id: confidence},
        confidence=confidence,
        model_id="system1",
        provider_id="system1",
        decision_id="dec-1",
    )


class HealthSpy:
    """A System One stand-in whose health method is a test failure if decide calls it."""

    def __init__(self, *, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.decisions = 0
        self.health_calls = 0

    def decide(self, offered: DecisionFrame) -> ChoiceResult:
        self.decisions += 1
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result

    def health(self):
        self.health_calls += 1
        raise AssertionError("HybridDecider.decide must not query provider health")


class Bridge:
    """Capture the escalated offer and answer as an agent, without a server or socket."""

    port = 19760

    def __init__(self, *args, **kwargs) -> None:
        self.offers = []

    def bind_agent_request(self, callback) -> None:
        self.callback = callback

    def offer(self, offered: DecisionFrame, **kwargs) -> ChoiceResult:
        self.offers.append(kwargs)
        return ChoiceResult(
            option_id="walk",
            probabilities={"walk": 1.0},
            confidence=1.0,
            model_id="agent",
            provider_id="agent",
            decision_id=offered.decision_id,
        )


@pytest.fixture
def build(monkeypatch):
    monkeypatch.setattr(hybrid, "ExternalDecider", Bridge)

    def make(system1: HealthSpy) -> HybridDecider:
        return HybridDecider(system1, port=19760, external_timeout=10.0)

    return make


def test_normal_high_confidence_choice_needs_no_health_probe(build):
    system1 = HealthSpy(result=claim(confidence=0.9))
    decider = build(system1)

    result = decider.decide(frame())

    assert result.option_id == "sign"
    assert decider.last_decision == {"decided_by": "system1", "escalated": False}
    assert system1.health_calls == 0
    assert decider.external.offers == []


def test_low_confidence_escalation_needs_no_health_probe(build):
    system1 = HealthSpy(result=claim(confidence=0.2))
    decider = build(system1)

    result = decider.decide(frame())

    assert result.provider_id == "agent"
    assert system1.health_calls == 0
    assert decider.external.offers[-1]["metadata"]["escalation_reason"] == "low_confidence"


def test_circuit_open_error_keeps_circuit_attribution(build):
    system1 = HealthSpy(error=CircuitOpen("breaker open"))
    decider = build(system1)

    result = decider.decide(frame())

    assert result.provider_id == "agent"
    assert system1.health_calls == 0
    assert decider.external.offers[-1]["metadata"]["escalation_reason"] == "circuit_open"


@pytest.mark.parametrize(
    ("error", "label"),
    [
        (ProviderUnavailable("connection failure or 429/5xx"), "provider_unavailable"),
        (ProviderTimeout("slow provider"), "provider_timeout"),
        (RuntimeError("unrelated failure"), "generic_error"),
    ],
    ids=["provider_unavailable", "provider_timeout", "generic_error"],
)
def test_non_circuit_failures_are_not_called_circuit_open(build, error, label):
    system1 = HealthSpy(error=error)
    decider = build(system1)

    result = decider.decide(frame())

    assert result.provider_id == "agent", label
    assert system1.health_calls == 0, label
    assert (
        decider.external.offers[-1]["metadata"]["escalation_reason"]
        == "system1_unavailable"
    ), label


def test_strict_response_model_mismatch_still_rejected_without_health() -> None:
    class Client:
        url_for_log = "synthetic"

        def request(self, payload, *, request_id, deadline):
            body = {
                "model": "other-model",
                "answers": {"action": {"type": "choice", "choice": "sign"}},
            }
            return SimpleNamespace(
                body=json.dumps(body).encode(),
                headers={},
                attempts=1,
                truncated_header=None,
            )

        def cancel(self) -> None:
            pass

    config = SystemOneConfig(
        provider_url="http://127.0.0.1:1",
        protocol="systemone",
        request_model="selected-model",
        model="pinned-model",
        strict_model_pin=True,
    )
    provider = HttpProvider(config, client=Client())
    question = {
        "name": "action",
        "type": "choice",
        "instructions": "pick one",
        "criteria": ["sign", "walk"],
    }
    try:
        with pytest.raises(ProviderConfigurationError) as raised:
            asyncio.run(
                provider.decide(
                    state={"synthetic": True},
                    question=question,
                    request_id="dec-1",
                    timeout_s=1.0,
                )
            )
        assert "differs from pinned model" in str(raised.value)
    finally:
        provider.close()
