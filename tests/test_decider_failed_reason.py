"""A lost decider result names its fixed cause at the operator stop.

``_decide`` already knows why no result came back; before this change the public stop line was
only ``escalated (decider_failed)``. These tests use the real Runner with synthetic frames and a
fake clock: they never touch a provider, network, emulator or wall clock, and never execute an
option. The stop carries one fixed token; exception messages, URLs, credentials and arbitrary
attributes stay out of the public reason.
"""

from test_probe_primitives import snapshot

from addons.system_one.provider import CircuitOpen, ProviderTimeout, ProviderUnavailable
from pokegent.dashboard.state import EVENT_DETAILS, event_details
from pokegent.errors import ErrorCode
from pokegent.events import EventType, MemoryEventLog
from pokegent.loop import (
    EscalationAction,
    EscalationResponse,
    PauseOnEscalation,
    Runner,
    RunnerConfig,
    RunStatus,
    stop_message,
)
from pokegent.options.models import (
    DeciderTimeout,
    DecisionFrame,
    Legality,
    Option,
    PayloadLeakError,
    fingerprint,
)
from pokegent.pipeline import Perception, PerceptionKind
from pokegent.state.scenes import SceneResult


class _Clock:
    """A monotonic clock whose sleeps advance it instead of blocking."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _RaisingDecider:
    supervisor_control = False
    external_control = False

    def __init__(self, error):
        self.error = error
        self.calls = 0

    def decide(self, _frame):
        self.calls += 1
        raise self.error


class _NoInput:
    """Executor stand-in: a decider failure must never reach the input path."""

    perceiver = True
    last_emulator_frame = None

    def settle(self, _frames):
        return None

    def execute(self, _frame, _option):
        raise AssertionError("a decider failure must not execute an option")


class _Perceiver:
    def __init__(self, kind=PerceptionKind.READY, scene_id="field.free"):
        self.kind = kind
        self.scene_id = scene_id

    def perceive(self, decision_id, *_args, **_kwargs):
        snap = snapshot()
        frame = DecisionFrame(
            decision_id=str(decision_id),
            run_id="synthetic",
            snapshot_seq=snap.snapshot_seq,
            scene_id=self.scene_id,
            scene_confidence=1,
            state_fingerprint=fingerprint(self.scene_id, seq=snap.snapshot_seq),
            options=(
                Option(id="walk", label="walk", legality=Legality.LEGAL,
                       executor_ref="test.action"),
                Option(id="talk", label="talk", legality=Legality.LEGAL,
                       executor_ref="test.action"),
            ),
        )
        return Perception(
            self.kind, snap, SceneResult(scene_id=self.scene_id, confidence=1),
            frame if self.kind is PerceptionKind.READY else None,
        )


def _run(error, *, config=None, sink=None, kind=PerceptionKind.READY, scene_id="field.free"):
    clock = _Clock()
    events = MemoryEventLog("synthetic")
    decider = _RaisingDecider(error)
    runner = Runner(
        decider=decider,
        executor=_NoInput(),
        events=events,
        perceiver=_Perceiver(kind, scene_id),
        escalation=sink if sink is not None else PauseOnEscalation(),
        clock=clock,
        sleep=clock.sleep,
        config=config or RunnerConfig(max_steps=3, decider_retries=0,
                                      system1_outage="pause"),
    )
    return runner.run(), events, decider


def _public(summary):
    return summary.stop_details, stop_message(summary.stop_reason, summary.stop_details)


def test_provider_unavailable_names_only_its_fixed_reason():
    secret = "https://provider.invalid/v1?api_key=sk-invented-credential"
    summary, events, decider = _run(ProviderUnavailable(secret))

    details, message = _public(summary)
    assert summary.status is RunStatus.ESCALATED
    assert details == {
        "kind": "decider_failed",
        "reasons": ["provider_unavailable"],
        "agent_frames": 0,
    }
    assert message == "stopped: escalated (decider_failed: provider_unavailable)"
    published = repr(details) + message
    assert secret not in published and "sk-invented-credential" not in published
    assert "provider.invalid" not in published
    assert event_details(details) == {"kind": "decider_failed"}
    assert "reasons" not in EVENT_DETAILS
    assert decider.calls == 1

    # The private escalation event keeps its existing details untouched.
    requested = events.of_type(EventType.ESCALATION_REQUESTED)
    assert requested[-1].details["error_code"] == ErrorCode.E_DECIDER_FAILED.value
    assert requested[-1].details["error"].startswith("https://provider.invalid/v1?api_key=")
    assert "sk-invented-credential" not in requested[-1].details["error"]


def test_provider_timeout_keeps_its_code_and_waits_on_the_existing_clock():
    summary, events, decider = _run(
        ProviderTimeout("slow provider"),
        config=RunnerConfig(
            max_steps=3, decider_retries=0, system1_outage="pause",
            system1_max_outage_s=0.5,
        ),
    )

    details, message = _public(summary)
    assert summary.status is RunStatus.ESCALATED
    assert details["reasons"] == ["provider_timeout"]
    assert message == "stopped: escalated (decider_failed: provider_timeout)"
    # Existing timeout classification is unchanged; only the public reason is additive.
    requested = events.of_type(EventType.ESCALATION_REQUESTED)
    assert requested[-1].details["error_code"] == ErrorCode.E_DECIDER_TIMEOUT.value
    assert decider.calls == 2  # one bounded outage retry, via the fake clock


def test_circuit_open_names_its_fixed_reason():
    summary, _, _ = _run(CircuitOpen("breaker open"))

    details, message = _public(summary)
    assert details == {
        "kind": "decider_failed",
        "reasons": ["circuit_open"],
        "agent_frames": 0,
    }
    assert message == "stopped: escalated (decider_failed: circuit_open)"


def test_unknown_exception_falls_back_without_reading_hostile_attributes():
    class HostileError(Exception):
        def __init__(self):
            super().__init__("https://secret.invalid/token=invented")
            self.code = "E_ATTACKER_CONTROLLED"

    summary, _, _ = _run(HostileError())

    details, message = _public(summary)
    assert details == {
        "kind": "decider_failed",
        "reasons": [ErrorCode.E_DECIDER_FAILED.value],
        "agent_frames": 0,
    }
    assert message == "stopped: escalated (decider_failed: E_DECIDER_FAILED)"
    published = repr(details) + message
    assert "E_ATTACKER_CONTROLLED" not in published
    assert "secret.invalid" not in published and "invented" not in published


def test_plain_decider_timeout_keeps_code_and_derives_reason_from_it():
    summary, events, _ = _run(DeciderTimeout("deadline"))

    details, message = _public(summary)
    assert details["reasons"] == [ErrorCode.E_DECIDER_TIMEOUT.value]
    assert message == "stopped: escalated (decider_failed: E_DECIDER_TIMEOUT)"
    requested = events.of_type(EventType.ESCALATION_REQUESTED)
    assert requested[-1].details["error_code"] == ErrorCode.E_DECIDER_TIMEOUT.value


def test_payload_leak_keeps_its_code_and_leak_details():
    leak = PayloadLeakError("payload exposed an executor reference", kind="executor_ref",
                            token="test.action")
    summary, events, _ = _run(leak)

    details, message = _public(summary)
    assert details["reasons"] == [ErrorCode.E_PAYLOAD_LEAK.value]
    assert message == "stopped: escalated (decider_failed: E_PAYLOAD_LEAK)"
    requested = events.of_type(EventType.ESCALATION_REQUESTED)
    assert requested[-1].details["error_code"] == ErrorCode.E_PAYLOAD_LEAK.value
    assert requested[-1].details["payload_leak"] == leak.as_dict()


def test_continue_leaves_no_stale_stop_override():
    class ContinueSink:
        def escalate(self, _request):
            return EscalationResponse(EscalationAction.CONTINUE, "continue")

    summary, _, decider = _run(ProviderUnavailable("down"), sink=ContinueSink(),
                               config=RunnerConfig(max_steps=1, decider_retries=0,
                                                   system1_outage="pause"))

    assert summary.status is RunStatus.MAX_STEPS
    assert summary.stop_reason == "max_steps"
    assert "reasons" not in summary.stop_details
    assert summary.stop_details["max_steps"] == 1
    assert decider.calls >= 1


def test_other_escalation_kinds_keep_their_stop_shape():
    summary, _, _ = _run(RuntimeError("unused"), kind=PerceptionKind.UNKNOWN,
                         config=RunnerConfig(max_steps=3, unknown_retries=0))

    details, message = _public(summary)
    assert summary.status is RunStatus.ESCALATED
    assert details == {"kind": "unknown_scene", "agent_frames": 0}
    assert message == "stopped: escalated (unknown_scene)"
    assert "reasons" not in details
