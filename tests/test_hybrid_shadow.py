"""Opt-in zero-authority hybrid Shadow: observation only, agent owns the choice."""

import threading
import time
from types import SimpleNamespace

import pytest

from addons.system_one.hybrid import HybridDecider
from addons.system_one.provider import ProviderConfigurationError, ProviderUnavailable
from pokegent import app, server
from pokegent.events import EventType, MemoryEventLog
from pokegent.loop import Runner, RunStatus
from pokegent.options.models import (
    AgentHandoffTimeout,
    ChoiceResult,
    DecisionFrame,
    Legality,
    Option,
    Risk,
    StaleDecision,
)
from pokegent.user_config import load_user_config


def _fingerprint(digit="0"):
    return "sha256:" + digit * 64


def frame(*, scene="field.free", decision_id="dec-1", fingerprint="0", goal_id=None):
    return DecisionFrame(
        decision_id=decision_id,
        run_id="synthetic",
        snapshot_seq=1,
        scene_id=scene,
        scene_confidence=1,
        state_fingerprint=_fingerprint(fingerprint),
        goal_id=goal_id,
        options=tuple(
            Option(id=key, label=key, legality=Legality.LEGAL, executor_ref="test.action")
            for key in ("sign", "walk")
        ),
    )


def _option(option_id, *, executor_ref, postcondition_ref=None, facts=None, risk=Risk.NORMAL):
    return Option(
        id=option_id,
        label=option_id,
        legality=Legality.LEGAL,
        executor_ref=executor_ref,
        postcondition_ref=postcondition_ref,
        facts=facts or {},
        risk=risk,
    )


def _route_frame():
    step = _option(
        "warp:1:59:3",
        executor_ref="field.enter_warp",
        postcondition_ref="field.warp_done",
        facts={
            "target": {"kind": "warp"},
            "route_relation": "next route step",
            "route_step": True,
            "next_route_step": True,
            "on_route": True,
            "serves_goal": True,
        },
    )
    face = _option(
        "face:down",
        executor_ref="field.face",
        facts={"target": {"kind": "face"}, "steps": 0},
        risk=Risk.LOW,
    )
    return DecisionFrame(
        decision_id="route-1",
        run_id="synthetic",
        snapshot_seq=1,
        scene_id="field.free",
        scene_confidence=1,
        state_fingerprint=_fingerprint("1"),
        goal_id="goal",
        options=(step, face),
    )


class Bridge:
    """Capture the offer and stand in for the external agent transport."""

    port = 19760

    def __init__(self, *, choice="walk", expire=False, fail=None, refreshed=None,
                 probabilities=None):
        self.choice = choice
        self.expire = expire
        self.fail = fail
        self.refreshed = refreshed
        self.probabilities = probabilities or {}
        self.offers = []

    def bind_agent_request(self, callback):
        self.callback = callback

    def bind_pending_frame(self, refresh):
        self.refresh = refresh

    def bind_decision_valid(self, valid):
        self.valid = valid

    def offer(self, offered, **kwargs):
        self.offers.append((offered, kwargs))
        if self.expire:
            raise AgentHandoffTimeout("external choice timed out")
        if self.fail is not None:
            raise self.fail
        return ChoiceResult(
            option_id=self.choice,
            probabilities=self.probabilities,
            provider_id="agent",
            decision_id=(self.refreshed or offered).decision_id,
        )

    def take_decided_frame(self):
        return self.refreshed

    def discard_stale_decision(self):
        pass


def _system1(*, option_id="sign", confidence=0.1, model_id="model-x"):
    return SimpleNamespace(
        decide=lambda _frame: ChoiceResult(
            option_id=option_id,
            confidence=confidence,
            provider_id="system1",
            model_id=model_id,
        )
    )


def _raising(exc):
    def decide(_frame):
        raise exc

    return SimpleNamespace(decide=decide)


def hybrid(monkeypatch, *, system1=None, bridge=None, events=None, **settings):
    bridge = bridge if bridge is not None else Bridge()
    monkeypatch.setattr("addons.system_one.hybrid.ExternalDecider", lambda *a, **k: bridge)
    decider = HybridDecider(
        system1 if system1 is not None else _system1(),
        port=19760,
        external_timeout=1,
        events=events,
        **settings,
    )
    return decider, bridge


def runner(decider, events):
    executor = SimpleNamespace(perceiver=object(), bind_perceiver=lambda _p: None)
    return Runner(decider=decider, executor=executor, events=events, perceiver=object())


@pytest.fixture
def no_sockets(monkeypatch):
    monkeypatch.setattr(
        server,
        "ThreadingHTTPServer",
        lambda *args: SimpleNamespace(
            server_address=("127.0.0.1", 19760),
            serve_forever=lambda: None,
            shutdown=lambda: None,
            server_close=lambda: None,
        ),
    )


def test_shadow_provider_call_is_private_and_agent_choice_is_returned(monkeypatch):
    calls = []

    def decide(offered):
        calls.append(offered)
        return ChoiceResult(
            option_id="sign", confidence=0.99, provider_id="system1", model_id="model-x"
        )

    decider, bridge = hybrid(
        monkeypatch,
        system1=SimpleNamespace(decide=decide),
        shadow=True,
        override_window_s=30.0,
    )
    result = decider.decide(frame())

    assert result.option_id == "walk" and result.provider_id == "agent"
    assert len(calls) == 1
    assert len(bridge.offers) == 1
    _offered, offer = bridge.offers[0]
    assert offer["metadata"] is None
    assert offer["default"] is None
    assert offer["handoff"] == {"ownership": "shadow", "timeout_s": 300.0}
    assert offer["wait_timeout"] == 300.0 and offer["interruptible_wait"] is True
    assert decider.last_decision["decided_by"] == "agent"
    shadow = decider.last_decision["shadow"]
    assert shadow["outcome"] == "ok"
    assert shadow["option_id"] == "sign"
    assert shadow["provider_id"] == "system1"
    assert shadow["model_id"] == "model-x"
    assert shadow["confidence"] == 0.99
    assert shadow["measured_ms"] >= 0
    assert shadow["decision_id"] == "dec-1"


def test_shadow_disables_deterministic_route_preemption(monkeypatch):
    ordinary, _ = hybrid(monkeypatch, shadow=False)
    chosen = ordinary.deterministic_route_step(_route_frame())
    assert chosen is not None and chosen.option_id == "warp:1:59:3"

    shadowed, bridge = hybrid(
        monkeypatch,
        system1=_system1(option_id="warp:1:59:3"),
        bridge=Bridge(choice="face:down"),
        shadow=True,
    )
    assert shadowed.deterministic_route_step(_route_frame()) is None
    result = shadowed.decide(_route_frame())
    assert result.option_id == "face:down"
    assert bridge.offers[0][1]["default"] is None
    assert bridge.offers[0][1]["metadata"] is None


def test_runner_emits_one_shadow_observation_and_agent_acceptance(monkeypatch):
    events = MemoryEventLog("synthetic")
    decider, _ = hybrid(monkeypatch, system1=_system1(), events=events, shadow=True)
    assert runner(decider, events)._decide(frame())[1] == "walk"

    completed = events.of_type(EventType.DECISION_SYSTEMONE_COMPLETED)
    assert len(completed) == 1
    event = completed[0]
    assert event.outcome == "observed"
    assert event.provider == "system1"
    assert event.request_id == "dec-1"
    assert event.details["option_id"] == "sign"
    assert event.details["model_id"] == "model-x"
    assert event.details["provider_id"] == "system1"
    assert isinstance(event.details["measured_ms"], float)
    assert event.details["accepted"] is False
    assert event.details["authority"] == "shadow"
    assert event.details["agreement"] is False
    assert event.details["accuracy"] == "unknown"
    assert event.details["agent_option_id"] == "walk"
    assert not any(
        item.type == EventType.DECISION_SYSTEMONE_COMPLETED and item.outcome == "accepted"
        for item in events.events
    )
    agent = events.of_type(EventType.DECISION_AGENT_COMPLETED)
    assert len(agent) == 1 and agent[0].outcome == "accepted"


def test_shadow_provider_failure_still_lets_the_agent_choose(monkeypatch):
    events = MemoryEventLog("synthetic")
    decider, _ = hybrid(
        monkeypatch,
        system1=_raising(ProviderUnavailable("synthetic outage")),
        events=events,
        shadow=True,
    )
    assert runner(decider, events)._decide(frame())[1] == "walk"

    completed = events.of_type(EventType.DECISION_SYSTEMONE_COMPLETED)
    assert len(completed) == 1
    event = completed[0]
    assert event.outcome == "failed"
    assert event.details["accepted"] is False
    assert event.details["authority"] == "shadow"
    assert event.details["agreement"] is None
    assert event.details["accuracy"] == "unknown"
    assert event.details["option_id"] is None
    assert event.details["agent_option_id"] == "walk"
    assert decider.last_decision["shadow"]["error_class"] == "ProviderUnavailable"
    assert len(events.of_type(EventType.DECISION_AGENT_COMPLETED)) == 1


def test_shadow_agent_timeout_keeps_observation_without_agreement(monkeypatch):
    events = MemoryEventLog("synthetic")
    decider, _ = hybrid(
        monkeypatch,
        system1=_system1(),
        bridge=Bridge(expire=True),
        events=events,
        shadow=True,
    )
    assert runner(decider, events)._decide(frame()) is RunStatus.SAFE_PAUSE

    completed = events.of_type(EventType.DECISION_SYSTEMONE_COMPLETED)
    assert len(completed) == 1
    assert completed[0].outcome == "observed"
    assert completed[0].details["agreement"] is None
    assert completed[0].details["accepted"] is False
    assert completed[0].details["authority"] == "shadow"
    assert len(events.of_type(EventType.DECISION_AGENT_EXPIRED)) == 1
    assert not events.of_type(EventType.DECISION_AGENT_COMPLETED)
    assert not events.of_type(EventType.DECISION_SYSTEMONE_TIMEOUT)


def test_shadow_agent_failure_keeps_observation_without_agreement(monkeypatch):
    events = MemoryEventLog("synthetic")
    decider, _ = hybrid(
        monkeypatch,
        system1=_system1(),
        bridge=Bridge(fail=OSError("synthetic transport")),
        events=events,
        shadow=True,
    )
    assert runner(decider, events)._decide(frame()) is RunStatus.SAFE_PAUSE

    completed = events.of_type(EventType.DECISION_SYSTEMONE_COMPLETED)
    assert len(completed) == 1
    assert completed[0].outcome == "observed"
    assert completed[0].details["agreement"] is None
    assert completed[0].details["accepted"] is False
    assert completed[0].details["authority"] == "shadow"
    assert not events.of_type(EventType.DECISION_AGENT_COMPLETED)


def test_shadow_provider_malformed_probabilities_is_a_failed_observation(monkeypatch):
    def decide(_frame):
        return ChoiceResult(
            option_id="sign",
            probabilities={"bogus": 0.5},
            provider_id="system1",
            model_id="model-x",
        )

    decider, _ = hybrid(
        monkeypatch, system1=SimpleNamespace(decide=decide), shadow=True
    )
    assert decider.decide(frame()).option_id == "walk"
    shadow = decider.last_decision["shadow"]
    assert shadow["outcome"] == "failed"
    assert shadow["error_class"] == "InvalidResponse"
    assert shadow["option_id"] is None


def test_shadow_agent_stale_keeps_one_observation(monkeypatch):
    events = MemoryEventLog("synthetic")
    bridge = Bridge()
    monkeypatch.setattr("addons.system_one.hybrid.ExternalDecider", lambda *a, **k: bridge)

    class StalingHybrid(HybridDecider):
        def decide(self, offered):
            super().decide(offered)
            raise StaleDecision("synthetic stale")

    decider = StalingHybrid(
        _system1(), port=19760, external_timeout=1, events=events, shadow=True
    )
    assert runner(decider, events)._decide(frame()) is None

    completed = events.of_type(EventType.DECISION_SYSTEMONE_COMPLETED)
    assert len(completed) == 1
    assert completed[0].outcome == "observed"
    assert completed[0].details["agreement"] is None
    assert completed[0].details["accepted"] is False
    assert completed[0].details["authority"] == "shadow"
    assert not events.of_type(EventType.DECISION_AGENT_COMPLETED)


def test_shadow_malformed_agent_choice_keeps_one_observation(monkeypatch):
    events = MemoryEventLog("synthetic")
    decider, _ = hybrid(
        monkeypatch,
        system1=_system1(),
        bridge=Bridge(probabilities={"bogus": 0.5}),
        events=events,
        shadow=True,
    )
    assert runner(decider, events)._decide(frame()) is None

    completed = events.of_type(EventType.DECISION_SYSTEMONE_COMPLETED)
    assert len(completed) == 1
    assert completed[0].outcome == "observed"
    assert completed[0].details["agreement"] is None
    assert completed[0].details["accepted"] is False
    assert completed[0].details["authority"] == "shadow"
    assert not events.of_type(EventType.DECISION_AGENT_COMPLETED)


def test_shadow_refreshed_frame_has_unknown_agreement(monkeypatch):
    events = MemoryEventLog("synthetic")
    refreshed = frame(decision_id="dec-2", fingerprint="2")
    decider, _ = hybrid(
        monkeypatch,
        system1=_system1(),
        bridge=Bridge(choice="walk", refreshed=refreshed),
        events=events,
        shadow=True,
    )
    chosen_frame, option_id = runner(decider, events)._decide(frame())

    assert option_id == "walk" and chosen_frame.decision_id == "dec-2"
    completed = events.of_type(EventType.DECISION_SYSTEMONE_COMPLETED)
    assert len(completed) == 1
    assert completed[0].request_id == "dec-1"
    assert completed[0].details["agreement"] is None
    agent = events.of_type(EventType.DECISION_AGENT_COMPLETED)
    assert len(agent) == 1 and agent[0].request_id == "dec-2"


def test_shadow_unknown_scene_skips_the_provider(monkeypatch):
    calls = []
    decider, bridge = hybrid(
        monkeypatch,
        system1=SimpleNamespace(decide=lambda offered: calls.append(offered)),
        shadow=True,
    )
    assert decider.decide(frame(scene="scene.unknown")).option_id == "walk"
    assert calls == []
    assert "shadow" not in decider.last_decision
    assert bridge.offers[0][1]["metadata"] == {
        "escalated": True,
        "escalation_reason": "unknown_scene",
    }
    assert bridge.offers[0][1]["handoff"]["ownership"] == "recovery_window"


def test_shadow_provider_configuration_error_remains_terminal(monkeypatch):
    events = MemoryEventLog("synthetic")
    decider, bridge = hybrid(
        monkeypatch,
        system1=_raising(ProviderConfigurationError("synthetic rejected", status=401)),
        events=events,
        shadow=True,
    )
    assert runner(decider, events)._decide(frame()) is RunStatus.SAFE_PAUSE
    assert bridge.offers == []
    assert not events.of_type(EventType.DECISION_SYSTEMONE_COMPLETED)
    assert events.of_type(EventType.RUNTIME_BLOCKED)[0].outcome == "provider_configuration"


def test_user_config_shadow_is_strict_bool_default_false(tmp_path):
    empty = tmp_path / "absent.yaml"
    assert load_user_config(empty, environ={})["hybrid"]["shadow"] is False
    good = load_user_config(empty, environ={}, cli={"hybrid": {"shadow": True}})
    assert good["hybrid"]["shadow"] is True
    for bad in (1, "true"):
        with pytest.raises(ValueError, match="hybrid.shadow"):
            load_user_config(empty, environ={}, cli={"hybrid": {"shadow": bad}})
    with pytest.raises(ValueError, match="requires decider hybrid"):
        load_user_config(
            empty, environ={}, cli={"decider": "agent", "hybrid": {"shadow": True}}
        )


def test_app_factory_passes_shadow_and_rejects_missing_provider(monkeypatch, no_sockets):
    monkeypatch.delenv("SYSTEM1_BASE_URL", raising=False)
    with pytest.raises(app.AppError, match="hybrid.shadow"):
        app.build_decider(
            "hybrid",
            config_raw={"hybrid": {"shadow": True}},
            events=MemoryEventLog("synthetic"),
            out_dir=None,
        )

    config = {
        "system_one": {
            "provider_url": "http://127.0.0.1:19799/systemone",
            "protocol": "systemone",
            "request_model": "synthetic",
            "api_key": "synthetic",
        },
        "hybrid": {"shadow": True},
    }
    decider, _, close = app.build_decider(
        "hybrid", config_raw=config, events=MemoryEventLog("synthetic"), out_dir=None
    )
    try:
        assert decider.shadow is True
    finally:
        close()


def test_app_factory_rejects_shadow_when_system_one_disabled(monkeypatch, no_sockets):
    monkeypatch.delenv("SYSTEM1_BASE_URL", raising=False)
    with pytest.raises(app.AppError, match="system_one.enabled"):
        app.build_decider(
            "hybrid",
            config_raw={
                "system_one": {
                    "enabled": False,
                    "provider_url": "http://127.0.0.1:19799/systemone",
                },
                "hybrid": {"shadow": True},
            },
            events=MemoryEventLog("synthetic"),
            out_dir=None,
        )


def test_real_served_agent_payload_has_no_shadow_metadata(no_sockets):
    decider = HybridDecider(
        _system1(), port=19760, external_timeout=1, agent_handoff_timeout_s=2, shadow=True
    )
    chosen = []
    worker = threading.Thread(target=lambda: chosen.append(decider.decide(frame())), daemon=True)
    worker.start()
    served = None
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with decider.external._condition:
            if decider.external._frame is not None:
                served = dict(decider.external._frame)
                break
            decider.external._condition.wait(0.05)
    assert served is not None
    decider.external.choose({"frame_id": served["frame_id"], "option_id": "walk"})
    worker.join(2)

    assert not worker.is_alive()
    assert chosen[0].option_id == "walk"
    assert "system1" not in served
    assert "_hybrid_metadata" not in served
    assert "_hybrid_metadata" not in served["state"]
    assert "system1" not in served["state"]
    assert served["handoff"]["ownership"] == "shadow"
