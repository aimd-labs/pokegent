"""Route provenance in the existing viewer decision consumer (#132).

The events and frames are synthetic; the runner, event log and viewer consumer are real.
"""

import json
from types import SimpleNamespace

from test_hybrid_route_first import _decider, _draft, _frame
from test_probe_primitives import snapshot

from pokegent.actions._exec.models import ActionResult
from pokegent.dashboard.state import KEEP_DECISIONS, ViewerState
from pokegent.dashboard.telemetry import build_telemetry
from pokegent.events import EventType, MemoryEventLog
from pokegent.loop import Runner, RunnerConfig
from pokegent.options.models import ChoiceResult
from pokegent.pipeline import Perception, PerceptionKind
from pokegent.state.scenes import SceneResult


def _viewer():
    viewer = ViewerState(run_id="synthetic", decider="hybrid", max_steps=1, realtime=False)
    events = MemoryEventLog("synthetic")
    events.add_listener(viewer.on_event)
    return viewer, events


def _observe(viewer, frame):
    """Unusable RAM here: this slice is about the frame/event, not map data."""
    viewer.on_perception(
        SimpleNamespace(snapshot=SimpleNamespace(usable=False, emulator_frame=0), frame=frame)
    )


def _runner(frame, decider, events, viewer):
    def perceive(decision_id, *_args):
        current = frame.model_copy(update={"decision_id": decision_id})
        perception = Perception(
            PerceptionKind.READY,
            snapshot(),
            SceneResult(scene_id=frame.scene_id, confidence=1, detector="invented"),
            current,
        )
        _observe(viewer, current)
        return perception

    def execute(current, option_id):
        return ActionResult(
            status="applied", option_id=option_id, progressed=True, verified_outcome=True
        )

    executor = SimpleNamespace(perceiver=True, last_emulator_frame=None, execute=execute)
    runner = Runner(
        decider=decider,
        executor=executor,
        events=events,
        perceiver=SimpleNamespace(perceive=perceive),
        config=RunnerConfig(max_steps=1, no_progress_limit=2),
    )
    return runner


def _route_event(events, frame, *, option_id="warp:0:0:0"):
    return events.emit(
        EventType.DECISION_ROUTE,
        scene_id=frame.scene_id,
        request_id=frame.decision_id,
        provider="route",
        outcome="accepted",
        details={"option_id": option_id, "decided_by": "route"},
    )


def test_real_route_event_becomes_a_route_decision_with_action_linkage(monkeypatch):
    frame = _frame(_draft(wall=True))
    viewer, events = _viewer()
    runner = _runner(frame, _decider(monkeypatch), events, viewer)

    runner.run()

    route_events = events.of_type(EventType.DECISION_ROUTE)
    assert len(route_events) == 1
    assert not events.of_type(EventType.DECISION_SYSTEMONE_REQUESTED)
    view = viewer.view()
    assert len(view["decisions"]) == 1
    decision = view["decisions"][0]
    # real request_id correlation, not a synthetic id
    assert decision["id"] == route_events[0].request_id
    assert decision["source"] == "route"
    assert decision["decided_by"] == "route"
    assert decision["selected"] == "warp:0:0:0"
    assert decision["provider"] == "route"
    # no model call happened: no model identity/frame, no confidence, no latency
    assert decision["model"] is None
    assert decision["model_view"] is None
    assert decision["confidence"] is None
    assert decision["latency_ms"] is None
    # the later verified action updates the same bounded history entry
    assert decision["status"] == "done"
    assert decision["result"]["status"] == "applied"
    action = next(a for a in view["actions"] if a["id"] == decision["id"])
    assert action["source"] == "route"
    assert action["status"] == "done"
    assert view["stats"]["decisions"] == 1
    assert view["stats"]["decider_errors"] == 0
    assert view["stats"]["latency_ms_total"] == 0.0
    assert view["stats"]["latency_ms_last"] is None
    assert "executor_ref" not in json.dumps(decision)
    telemetry = build_telemetry(view)
    assert telemetry["pokegent"]["decision"]["source"] == "route"
    assert telemetry["pokegent"]["decision"]["source_label"] == "ROUTE"
    assert telemetry["pokegent"]["decision"]["latency_ms"] is None
    assert telemetry["run_stats"]["decisions"] == 1
    assert telemetry["pokegent"]["metrics"]["latency_ms_last"] is None
    assert telemetry["pokegent"]["metrics"]["latency_ms_mean"] is None


def test_route_history_is_bounded_and_never_double_counted():
    template = _frame(_draft(wall=True))
    viewer, events = _viewer()
    for i in range(KEEP_DECISIONS + 2):
        frame = template.model_copy(update={"decision_id": f"route-{i}"})
        _observe(viewer, frame)
        _route_event(events, frame)
    view = viewer.view()
    expected = [f"route-{i}" for i in range(2, KEEP_DECISIONS + 2)]
    assert [d["id"] for d in view["decisions"]] == expected
    assert view["stats"]["decisions"] == KEEP_DECISIONS + 2
    picks = list(view["picks"])

    # A repeated envelope for the same request_id updates nothing and is not counted twice.
    _route_event(events, template.model_copy(update={"decision_id": expected[-1]}))
    view = viewer.view()
    assert [d["id"] for d in view["decisions"]] == expected
    assert view["stats"]["decisions"] == KEEP_DECISIONS + 2
    assert view["picks"] == picks


def test_route_event_without_its_frame_records_no_decision():
    viewer, events = _viewer()
    _route_event(events, _frame(_draft(wall=True)))
    view = viewer.view()
    assert view["decisions"] == []
    assert view["stats"]["decisions"] == 0
    assert view["events"][-1]["type"] == EventType.DECISION_ROUTE.value


def _decider_call(viewer, frame, *, elapsed_ms=12.0, error=False):
    _observe(viewer, frame)
    viewer.on_decider_start(frame)
    viewer.on_decider_call(
        frame,
        None
        if error
        else ChoiceResult(option_id="warp:0:0:0", provider_id="system1", model_id="mock"),
        RuntimeError("synthetic failure") if error else None,
        elapsed_ms,
    )


def test_mixed_route_and_model_mean_counts_only_measured_decider_calls():
    template = _frame(_draft(wall=True))
    viewer, events = _viewer()
    route_frame = template.model_copy(update={"decision_id": "route-1"})
    _observe(viewer, route_frame)
    _route_event(events, route_frame)
    _decider_call(viewer, template.model_copy(update={"decision_id": "model-1"}), elapsed_ms=12.0)

    view = viewer.view()
    metrics = build_telemetry(view)["pokegent"]["metrics"]
    assert metrics["decisions"] == 2
    assert metrics["latency_ms_mean"] == 12.0
    assert view["stats"]["latency_samples"] == 1


def test_decider_only_mean_is_unchanged():
    viewer, _events = _viewer()
    _decider_call(
        viewer, _frame(_draft(wall=True)).model_copy(update={"decision_id": "model-1"})
    )
    metrics = build_telemetry(viewer.view())["pokegent"]["metrics"]
    assert metrics["decisions"] == 1
    assert metrics["latency_ms_mean"] == 12.0


def test_failed_decider_is_not_a_latency_sample():
    viewer, _events = _viewer()
    _decider_call(
        viewer,
        _frame(_draft(wall=True)).model_copy(update={"decision_id": "failed-1"}),
        elapsed_ms=5.0,
        error=True,
    )
    _decider_call(
        viewer, _frame(_draft(wall=True)).model_copy(update={"decision_id": "model-1"})
    )
    view = viewer.view()
    assert view["stats"]["decisions"] == 2
    assert view["stats"]["decider_errors"] == 1
    assert view["stats"]["latency_samples"] == 1
    assert build_telemetry(view)["pokegent"]["metrics"]["latency_ms_mean"] == 12.0


def test_legacy_view_without_latency_samples_keeps_the_old_denominator():
    viewer, _events = _viewer()
    _decider_call(
        viewer, _frame(_draft(wall=True)).model_copy(update={"decision_id": "model-1"})
    )
    view = viewer.view()
    view["stats"].pop("latency_samples")
    assert build_telemetry(view)["pokegent"]["metrics"]["latency_ms_mean"] == 12.0


def test_route_and_system_one_records_keep_separate_provenance():
    template = _frame(_draft(wall=True))
    viewer, events = _viewer()
    route_frame = template.model_copy(update={"decision_id": "route-1"})
    _observe(viewer, route_frame)
    _route_event(events, route_frame)

    system1_frame = template.model_copy(update={"decision_id": "system1-1"})
    _observe(viewer, system1_frame)
    viewer.on_decider_start(system1_frame)
    viewer.on_decider_call(
        system1_frame,
        ChoiceResult(option_id="warp:0:0:0", provider_id="system1", model_id="mock"),
        None,
        12.0,
    )
    events.emit(
        EventType.DECISION_SYSTEMONE_COMPLETED,
        request_id="system1-1",
        outcome="accepted",
        details={"decided_by": "system1"},
    )

    records = {record["id"]: record for record in viewer.view()["decisions"]}
    route, system1 = records["route-1"], records["system1-1"]
    assert (route["source"], route["decided_by"]) == ("route", "route")
    assert (route["provider"], route["model"], route["latency_ms"]) == ("route", None, None)
    assert (system1["source"], system1["decided_by"]) == ("hybrid", "system1")
    assert (system1["provider"], system1["model"], system1["latency_ms"]) == (
        "system1",
        "mock",
        12.0,
    )
