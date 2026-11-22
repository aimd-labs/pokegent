"""Bounded hybrid agent handoff: real deadline expiry, typed pause, unchanged controls."""

import math
import time
from types import SimpleNamespace

import pytest

from addons.system_one.hybrid import HybridDecider
from pokegent import server
from pokegent.events import EventType, MemoryEventLog
from pokegent.loop import Runner, RunStatus
from pokegent.options.models import (
    AgentHandoffTimeout,
    ChoiceResult,
    DeciderTimeout,
    DecisionFrame,
    Legality,
    Option,
)
from pokegent.user_config import load_user_config


def frame(scene="field.free", options=("sign", "walk")):
    return DecisionFrame(
        decision_id="dec-1",
        run_id="synthetic",
        snapshot_seq=1,
        scene_id=scene,
        scene_confidence=1,
        state_fingerprint="sha256:" + "0" * 64,
        options=tuple(
            Option(id=key, label=key, legality=Legality.LEGAL, executor_ref="test.action")
            for key in options
        ),
    )


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


def test_external_decider_deadline_expiry_is_typed_safe_pause(no_sockets):
    external = server.ExternalDecider()
    started = time.monotonic()
    with pytest.raises(AgentHandoffTimeout) as raised:
        external.offer(
            frame(), wait_timeout=0.02, interruptible_wait=True, handoff_timeout=True
        )
    assert time.monotonic() - started < 1.0
    assert str(raised.value) == "external choice timed out"
    assert external._status == "safe_pause"
    assert external._stop_reason == "external choice timed out"


def test_shared_external_decider_default_timeout_is_unchanged(no_sockets):
    external = server.ExternalDecider()
    with pytest.raises(DeciderTimeout) as raised:
        external.offer(frame(), wait_timeout=0.02, interruptible_wait=True)
    assert type(raised.value) is DeciderTimeout
    assert isinstance(raised.value, AgentHandoffTimeout) is False


class Bridge:
    """Capture offers; optionally expire the bounded handoff."""

    port = 19760

    def __init__(self, *args, expire=False, **kwargs):
        self.offers = []
        self.expire = expire

    def bind_agent_request(self, callback):
        self.callback = callback

    def offer(self, offered, **kwargs):
        self.offers.append(kwargs)
        if self.expire:
            raise AgentHandoffTimeout("external choice timed out")
        return kwargs.get("default") or ChoiceResult(option_id="sign", provider_id="agent")


def hybrid(monkeypatch, *, expire=False, events=None, override_window_s=0.0, **settings):
    bridge = Bridge(expire=expire)
    monkeypatch.setattr("addons.system_one.hybrid.ExternalDecider", lambda *a, **k: bridge)
    system1 = SimpleNamespace(
        decide=lambda f: ChoiceResult(option_id="sign", confidence=0.1, provider_id="system1")
    )
    decider = HybridDecider(
        system1, port=19760, external_timeout=1, events=events,
        override_window_s=override_window_s, **settings,
    )
    return decider, bridge


def test_both_hybrid_agent_paths_are_bounded(monkeypatch):
    decider, bridge = hybrid(monkeypatch)
    assert decider.decide(frame(scene="scene.unknown")).option_id == "sign"
    assert decider.decide(frame()).option_id == "sign"
    assert len(bridge.offers) == 2
    assert all(offer["wait_timeout"] == 300.0 for offer in bridge.offers)
    assert all(offer["handoff_timeout"] is True for offer in bridge.offers)
    assert all(offer["interruptible_wait"] is True for offer in bridge.offers)


def test_configured_bound_is_used(monkeypatch):
    decider, bridge = hybrid(monkeypatch, agent_handoff_timeout_s=7.5)
    decider.decide(frame())
    assert bridge.offers[0]["wait_timeout"] == 7.5


def test_non_escalated_override_window_is_unchanged(monkeypatch):
    decider, bridge = hybrid(monkeypatch, override_window_s=5.0)
    decider.system1 = SimpleNamespace(
        decide=lambda f: ChoiceResult(option_id="sign", confidence=0.9, provider_id="system1")
    )
    result = decider.decide(frame())
    assert result.option_id == "sign"
    offer = bridge.offers[0]
    assert offer["wait_timeout"] == 5.0
    assert offer["handoff_timeout"] is False
    assert offer["interruptible_wait"] is False
    assert offer["default"] is not None


def test_recovery_expiry_emits_one_event_and_never_returns_a_choice(monkeypatch):
    events = MemoryEventLog("synthetic")
    decider, bridge = hybrid(monkeypatch, expire=True, events=events)
    with pytest.raises(AgentHandoffTimeout):
        decider.decide(frame(scene="scene.unknown"))
    assert len(bridge.offers) == 1
    expired = [e for e in events.events if e.type == EventType.DECISION_AGENT_EXPIRED]
    assert len(expired) == 1
    assert expired[0].details["reason"] == "unknown_scene"
    assert expired[0].details["ownership"] == "recovery_window"
    assert expired[0].details["timeout_s"] == 300.0
    assert expired[0].details["stop_reason"] == "external choice timed out"
    assert expired[0].public_visibility is True


def test_escalated_expiry_does_not_reuse_rejected_system_one_choice(monkeypatch):
    events = MemoryEventLog("synthetic")
    decider, bridge = hybrid(monkeypatch, expire=True, events=events)
    decider.system1 = SimpleNamespace(
        decide=lambda f: ChoiceResult(option_id="sign", confidence=0.1, provider_id="system1")
    )
    with pytest.raises(AgentHandoffTimeout):
        decider.decide(frame())
    assert len(bridge.offers) == 1
    assert bridge.offers[0]["default"] is None
    assert [e.type for e in events.events].count(EventType.DECISION_AGENT_EXPIRED) == 1


def test_runner_pauses_once_on_typed_handoff_expiry():
    class Expiring:
        def decide(self, decider_frame):
            raise AgentHandoffTimeout("external choice timed out")

    events = MemoryEventLog("synthetic")
    executor = SimpleNamespace(perceiver=object(), bind_perceiver=lambda perceiver: None)
    runner = Runner(decider=Expiring(), executor=executor, events=events, perceiver=object())
    assert runner._decide(frame()) is RunStatus.SAFE_PAUSE
    assert not any(e.type == EventType.DECISION_SYSTEMONE_TIMEOUT for e in events.events)


def test_handoff_timeout_config_default_and_validation(tmp_path):
    empty = tmp_path / "absent.yaml"
    config = load_user_config(empty, environ={})
    assert config["hybrid"]["agent_handoff_timeout_s"] == 300.0
    for bad in (0, -1, True, math.nan, math.inf):
        with pytest.raises(ValueError):
            load_user_config(
                empty,
                environ={},
                cli={"hybrid": {"agent_handoff_timeout_s": bad}},
            )
    good = load_user_config(
        empty, environ={"POKEGENT_HYBRID_AGENT_HANDOFF_TIMEOUT_S": "12.5"}
    )
    assert good["hybrid"]["agent_handoff_timeout_s"] == 12.5
