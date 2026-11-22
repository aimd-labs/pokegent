"""Bounded hybrid escalations carry one versioned handoff record on the served frame."""

import hashlib
import json
import threading
import time
from types import SimpleNamespace

import pytest

from addons.system_one.hybrid import HybridDecider
from pokegent import server
from pokegent.options.models import (
    ChoiceResult,
    DecisionFrame,
    Legality,
    Option,
    assert_payload_clean,
    fingerprint,
    to_system_one_payload,
)
from pokegent.options.registry import BuildContext, BuilderRegistry, FrameDraft

HANDOFF_TIMEOUT = 5.0


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


def _option(key, label=None):
    return Option(
        id=key,
        label=label or key,
        legality=Legality.LEGAL,
        executor_ref="test.action",
    )


def _frame(
    *,
    decision_id="dec-1",
    seq=7,
    goal_id="objective-1",
    goal_revision=None,
    options=("sign", "walk"),
    all_options=(),
    state_fingerprint=None,
):
    fields = {
        "decision_id": decision_id,
        "run_id": "synthetic",
        "snapshot_seq": seq,
        "scene_id": "field.free",
        "scene_confidence": 1.0,
        "state_fingerprint": state_fingerprint or fingerprint("field.free"),
        "goal_id": goal_id,
        "options": tuple(_option(key) for key in options),
        "all_options": tuple(all_options),
    }
    if goal_revision is not None:
        fields["goal_revision"] = goal_revision
    return DecisionFrame(**fields)


def _handoff_context(reason="low_confidence", ownership="escalated"):
    return {
        "ownership": ownership,
        "escalation_reason": reason,
        "timeout_s": HANDOFF_TIMEOUT,
    }


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached")


def _start_offer(external, frame, **kwargs):
    outcome = {}

    def run():
        try:
            outcome["result"] = external.offer(frame, **kwargs)
        except BaseException as exc:  # surfaced by the test, never swallowed
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    _wait_until(
        lambda: external._frame is not None and external._status == "waiting_for_choice"
    )
    return thread, outcome


def _served(external):
    status, body = external.frame_response(all_options=True, wait_timeout=0.0)
    assert status == 200
    return body


def _canonical_union_hash(body):
    rendered = sorted(
        (
            [item["id"], item["criterion"]]
            for item in (*body["options"], *body.get("all_options", ()))
        ),
        key=lambda entry: entry[0],
    )
    canonical = json.dumps(
        {"scope": "selectable_union", "options": rendered},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _finish(external, body, thread, outcome, option_id=None):
    external.choose(
        {
            "frame_id": body["frame_id"],
            "option_id": option_id or body["options"][0]["id"],
        }
    )
    thread.join(2)
    assert not thread.is_alive()
    assert "error" not in outcome, outcome.get("error")


def test_escalated_frame_carries_versioned_handoff_record(no_sockets):
    external = server.ExternalDecider()
    frame = _frame(
        goal_revision=3,
        all_options=(_option("hidden:pc", "use the PC"),),
    )
    thread, outcome = _start_offer(
        external,
        frame,
        wait_timeout=HANDOFF_TIMEOUT,
        interruptible_wait=True,
        handoff_timeout=True,
        handoff=_handoff_context(),
    )
    body = _served(external)
    record = body["handoff"]

    assert record["schema"] == "handoff/1"
    assert record["controller"] == "AGENT_PENDING"
    assert record["ownership"] == "escalated"
    assert record["decision_id"] == frame.decision_id
    assert record["frame_id"] == body["frame_id"]
    assert record["snapshot"] == {
        "seq": frame.snapshot_seq,
        "fingerprint": frame.state_fingerprint,
    }
    assert record["objective"] == {"id": "objective-1", "revision": 3}
    assert record["escalation_reason"] == "low_confidence"
    assert record["option_scope"] == "selectable_union"
    assert record["option_set_hash"] == _canonical_union_hash(body)
    assert record["option_set_hash"] != hashlib.sha256(
        json.dumps(
            [item["id"] for item in body["options"]],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    assert record["deadline"]["policy"] == "safe_pause"
    assert record["deadline"]["timeout_s"] == HANDOFF_TIMEOUT
    assert 0 < record["deadline"]["remaining_s"] <= HANDOFF_TIMEOUT

    # The sidecar is agent-only: the provider payload remains the guarded three-key payload
    # and never carries the record, the state fingerprint or the revision.
    payload = to_system_one_payload(frame)
    assert_payload_clean(payload, frame)
    rendered_payload = json.dumps(payload, sort_keys=True)
    assert "handoff" not in rendered_payload
    assert frame.state_fingerprint not in rendered_payload
    assert "revision" not in rendered_payload

    _finish(external, body, thread, outcome)
    assert outcome["result"].option_id == "sign"


def test_handoff_hash_is_canonical_and_covers_extended_options(no_sockets):
    external = server.ExternalDecider()
    base = _frame(
        goal_revision=1,
        options=("sign", "walk"),
        all_options=(_option("hidden:pc", "use the PC"),),
    )
    thread, outcome = _start_offer(
        external,
        base,
        wait_timeout=HANDOFF_TIMEOUT,
        interruptible_wait=True,
        handoff_timeout=True,
        handoff=_handoff_context(),
    )
    first = _served(external)
    first_hash = first["handoff"]["option_set_hash"]
    _finish(external, first, thread, outcome)

    reordered = _frame(
        goal_revision=1,
        options=("walk", "sign"),
        all_options=(_option("hidden:pc", "use the PC"),),
    )
    thread, outcome = _start_offer(
        external,
        reordered,
        wait_timeout=HANDOFF_TIMEOUT,
        interruptible_wait=True,
        handoff_timeout=True,
        handoff=_handoff_context(),
    )
    second = _served(external)
    assert second["handoff"]["option_set_hash"] == first_hash
    _finish(external, second, thread, outcome)

    changed = _frame(
        goal_revision=1,
        options=("sign", "walk"),
        all_options=(_option("hidden:pc", "use the PC carefully"),),
    )
    thread, outcome = _start_offer(
        external,
        changed,
        wait_timeout=HANDOFF_TIMEOUT,
        interruptible_wait=True,
        handoff_timeout=True,
        handoff=_handoff_context(),
    )
    third = _served(external)
    assert third["handoff"]["option_set_hash"] != first_hash
    _finish(external, third, thread, outcome)


def test_unknown_objective_identity_stays_unknown(no_sockets):
    external = server.ExternalDecider()
    frame = _frame(goal_id=None, goal_revision=None)
    thread, outcome = _start_offer(
        external,
        frame,
        wait_timeout=HANDOFF_TIMEOUT,
        interruptible_wait=True,
        handoff_timeout=True,
        handoff=_handoff_context(),
    )
    record = _served(external)["handoff"]
    assert record["objective"] == {"id": None, "revision": None}
    _finish(external, _served(external), thread, outcome)


def test_refresh_updates_record_atomically_and_keeps_original_deadline(no_sockets):
    external = server.ExternalDecider()
    original = _frame(goal_revision=1, options=("sign", "walk"))
    refreshed = _frame(goal_revision=2, options=("sign", "talk"))
    served_updates = []

    def refresh(frame):
        if served_updates:
            return None
        served_updates.append(frame)
        return refreshed

    thread, outcome = _start_offer(
        external,
        original,
        wait_timeout=HANDOFF_TIMEOUT,
        interruptible_wait=True,
        handoff_timeout=True,
        handoff=_handoff_context(),
    )
    first = _served(external)
    first_record = first["handoff"]
    assert first_record["objective"]["revision"] == 1

    external.bind_pending_frame(refresh)
    _wait_until(
        lambda: external._frame is not None
        and external._frame.get("handoff", {}).get("objective", {}).get("revision") == 2
    )
    second = _served(external)
    second_record = second["handoff"]

    assert second["frame_id"] != first["frame_id"]
    assert second_record["frame_id"] == second["frame_id"]
    assert second_record["objective"] == {"id": "objective-1", "revision": 2}
    assert second_record["option_set_hash"] == _canonical_union_hash(second)
    assert second_record["option_set_hash"] != first_record["option_set_hash"]
    # A refresh is not a new wait: the remaining budget only shrinks.
    assert second_record["deadline"]["remaining_s"] < first_record["deadline"][
        "remaining_s"
    ]

    with pytest.raises(LookupError):
        external.choose({"frame_id": first["frame_id"], "option_id": "sign"})
    _finish(external, second, thread, outcome, option_id="talk")
    assert outcome["result"].decision_id == refreshed.decision_id


def test_plain_agent_and_override_frames_have_no_handoff_record(no_sockets):
    external = server.ExternalDecider()
    frame = _frame(goal_revision=2)
    thread, outcome = _start_offer(
        external, frame, wait_timeout=HANDOFF_TIMEOUT, interruptible_wait=True
    )
    body = _served(external)
    assert "handoff" not in body
    _finish(external, body, thread, outcome)


class _Bridge:
    port = 19760

    def __init__(self):
        self.offers = []

    def bind_agent_request(self, callback):
        self.callback = callback

    def offer(self, frame, **kwargs):
        self.offers.append(kwargs)
        return ChoiceResult(option_id="sign", provider_id="agent")


def _hybrid(monkeypatch, *, override_window_s=0.0):
    bridge = _Bridge()
    monkeypatch.setattr("addons.system_one.hybrid.ExternalDecider", lambda *a, **k: bridge)
    system1 = SimpleNamespace(
        decide=lambda frame: ChoiceResult(
            option_id="sign", confidence=0.1, provider_id="system1"
        )
    )
    decider = HybridDecider(
        system1,
        port=19760,
        external_timeout=1,
        agent_handoff_timeout_s=HANDOFF_TIMEOUT,
        override_window_s=override_window_s,
    )
    return decider, bridge


def test_hybrid_passes_handoff_context_only_for_bounded_offers(monkeypatch):
    decider, bridge = _hybrid(monkeypatch)
    # Ordinary escalation and unknown-scene recovery both carry identity.
    decider.decide(_frame())
    unknown = _frame().model_copy(update={"scene_id": "scene.unknown"})
    decider.decide(unknown)
    assert bridge.offers[0]["handoff"] == {
        "ownership": "escalated",
        "escalation_reason": "low_confidence",
        "timeout_s": HANDOFF_TIMEOUT,
    }
    assert bridge.offers[1]["handoff"] == {
        "ownership": "recovery_window",
        "escalation_reason": "unknown_scene",
        "timeout_s": HANDOFF_TIMEOUT,
    }

    override, override_bridge = _hybrid(monkeypatch, override_window_s=5.0)
    override.system1 = SimpleNamespace(
        decide=lambda frame: ChoiceResult(
            option_id="sign", confidence=0.9, provider_id="system1"
        )
    )
    override.decide(_frame())
    assert "handoff" not in override_bridge.offers[0]


def test_registry_binds_goal_revision_from_the_frame_build_context():
    class Builder:
        def build(self, ctx):
            return FrameDraft(
                options=(_option("sign"),),
                state_fingerprint=fingerprint("field.free"),
            )

    registry = BuilderRegistry()
    registry.register_scenes(("field.free",), Builder())
    snapshot = SimpleNamespace(run_id="synthetic", snapshot_seq=1)
    scene = SimpleNamespace(scene_id="field.free", confidence=1.0)

    bound = registry.build(
        BuildContext(
            snapshot=snapshot,
            scene=scene,
            goal_id="objective-1",
            goal_revision=4,
        ),
        "dec-1",
    )
    assert bound is not None and bound.goal_revision == 4

    unknown = registry.build(
        BuildContext(snapshot=snapshot, scene=scene, goal_id="objective-1"),
        "dec-2",
    )
    assert unknown is not None and unknown.goal_revision is None
