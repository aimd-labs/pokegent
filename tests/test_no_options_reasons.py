"""A NO_OPTIONS pause carries the producer's own sanitized diagnostic reasons.

The fixture is the real ``FieldBuilder`` unknown-read branch (no map block), injected through a
fake perceiver into the real Runner. ``route_needs``/``blocked_route`` are never read, so no
prerequisite or cause is invented; budget prose is only shown when a producer wrote it verbatim.
"""

from test_objective_budget_reuse import SearchGraph, _resolved, _snapshot, _state
from test_probe_primitives import snapshot

import pokegent.navigation.planner as planner_module
import pokegent.options.diagnostics as diagnostics
from pokegent.actions.field.builder import FieldBuilder
from pokegent.dashboard.state import EVENT_DETAILS, event_details
from pokegent.events import MemoryEventLog
from pokegent.loop import (
    EscalationAction,
    EscalationResponse,
    Runner,
    RunnerConfig,
    RunStatus,
    stop_message,
)
from pokegent.navigation.adapter import NavigationAdapter
from pokegent.options.models import DecisionFrame, Legality, Option, fingerprint
from pokegent.options.registry import BuildContext
from pokegent.pipeline import Perception, PerceptionKind
from pokegent.state.scenes import SceneResult


def _frame_of(options, *, scene="field.free", seq=1):
    return DecisionFrame.from_options(
        options,
        decision_id="dec-synthetic",
        run_id="synthetic",
        snapshot_seq=seq,
        scene_id=scene,
        scene_confidence=1.0,
        state_fingerprint=fingerprint(scene, seq=seq),
    )


def _diagnostic(key, reason=..., legality=Legality.UNKNOWN, **fields):
    facts = {} if reason is ... else {"reason": reason}
    return Option(id=f"diag:{key}", label=key, legality=legality, facts=facts, **fields)


def _map_unreadable():
    """The real builder branch: a usable snapshot without a map block."""
    snap = snapshot()
    assert snap.map is None
    draft = FieldBuilder(field_menu=False).build(
        BuildContext(snapshot=snap, scene=SceneResult(scene_id="field.free", confidence=1))
    )
    frame = DecisionFrame.from_options(
        draft.options,
        decision_id="dec-map",
        run_id="synthetic",
        snapshot_seq=snap.snapshot_seq,
        scene_id="field.free",
        scene_confidence=1.0,
        state_fingerprint=draft.state_fingerprint,
    )
    # This is the pipeline's real predicate: a frame with no chooseable option is NO_OPTIONS.
    assert frame.options == ()
    assert [option.facts.get("reason") for option in frame.diagnostics] == ["map_unreadable"]
    return snap, frame


class _NoInput:
    """Executor stand-in: a pause must never reach the input path."""

    perceiver = True
    last_emulator_frame = None

    def settle(self, frames):
        return None

    def execute(self, *_args, **_kwargs):
        raise AssertionError("a NO_OPTIONS pause must not execute an option")


def _run(snap, frame=None, *, scene_id="field.free", kind=PerceptionKind.NO_OPTIONS,
         sink=None, max_steps=10):
    class Perceiver:
        def perceive(self, _decision_id, *_args, **_kwargs):
            return Perception(kind, snap, SceneResult(scene_id=scene_id, confidence=1), frame)

    decider = type("NoDecider", (), {"decide": lambda self, _frame: None})()
    return Runner(
        decider=decider,
        executor=_NoInput(),
        events=MemoryEventLog("synthetic"),
        perceiver=Perceiver(),
        escalation=sink,
        config=RunnerConfig(unknown_retries=0, max_steps=max_steps),
    ).run()


def test_real_no_options_pause_carries_the_producer_reason():
    snap, frame = _map_unreadable()
    summary = _run(snap, frame)

    assert summary.status is RunStatus.ESCALATED
    assert summary.stop_reason == "escalated"
    assert summary.stop_details == {
        "kind": "no_legal_option",
        "reasons": ["map_unreadable"],
        "agent_frames": 0,
    }
    message = stop_message(summary.stop_reason, summary.stop_details)
    assert message == "stopped: escalated (no_legal_option: map_unreadable)"
    assert "prerequisite" not in message and "root cause" not in message
    # The live operator card never sees the new key and the CLI line keeps its shape.
    assert "reasons" not in EVENT_DETAILS
    assert event_details(summary.stop_details) == {"kind": "no_legal_option"}


def test_missing_and_refused_reasons_stay_absent():
    snap = snapshot()
    frame = _frame_of(
        (_diagnostic("unknown_effect"), _diagnostic("pointer", "read from 0x08000000 failed"))
    )

    assert diagnostics.bounded_pause_reasons(frame) == ()
    summary = _run(snap, frame)
    assert summary.status is RunStatus.ESCALATED
    assert summary.stop_details == {"kind": "no_legal_option", "agent_frames": 0}
    assert stop_message(summary.stop_reason, summary.stop_details) == (
        "stopped: escalated (no_legal_option)"
    )


def test_exhausted_navigation_budget_is_none_and_never_invents_surf(monkeypatch):
    # The real bounded adapter abandons the search as an exact UNKNOWN; there is no plan whose
    # ``unmet``/``blocked_route_fact`` could be turned into a reason.
    monkeypatch.setattr(planner_module, "OBJECTIVE_FRAME_PLAN_BUDGET", 1)
    state = _state()
    adapter = NavigationAdapter(SearchGraph(state))
    assert adapter.plan(_snapshot(state), _resolved(), objective_budget=1) is None

    hostile = Option(
        id="diag:route_budget",
        label="route budget",
        legality=Legality.UNAVAILABLE,
        facts={
            "reason": "objective route search budget exhausted",
            "route_needs": ("surf",),
        },
    )
    reasons = diagnostics.bounded_pause_reasons(_frame_of((hostile,)))
    assert reasons == ("objective route search budget exhausted",)
    assert "Surf" not in reasons[0] and "needs Surf" not in reasons[0]


def test_multiple_reasons_dedupe_and_stop_at_the_pause_bounds():
    options = (
        FieldBuilder._diagnostic("first", "First", Legality.UNKNOWN, "reason one"),
        FieldBuilder._diagnostic("dup", "Dup", Legality.UNKNOWN, "reason one"),
        FieldBuilder._diagnostic("second", "Second", Legality.UNKNOWN, "reason two"),
        FieldBuilder._diagnostic("third", "Third", Legality.UNAVAILABLE, "reason three"),
        FieldBuilder._diagnostic("fourth", "Fourth", Legality.UNKNOWN, "reason four"),
        FieldBuilder._diagnostic("long", "Long", Legality.UNKNOWN, "y" * 500),
    )
    reasons = diagnostics.bounded_pause_reasons(_frame_of(options))
    assert reasons == ("reason one", "reason two", "reason three")
    assert len("; ".join(reasons)) <= 240

    # The shared helper already truncated the overlong row; this projection never slices again.
    long_only = _frame_of((options[-1],))
    served = diagnostics.served_diagnostics(long_only)["items"][0]["reason"]
    (long_reason,) = diagnostics.bounded_pause_reasons(long_only)
    assert long_reason == served
    assert long_reason.endswith("...") and len(long_reason) == 240

    # A 240-character whole-string budget stops before a reason that would overflow it.
    wide = tuple(
        FieldBuilder._diagnostic(f"wide_{index}", "Wide", Legality.UNKNOWN, char * 100)
        for index, char in enumerate("abc")
    )
    assert diagnostics.bounded_pause_reasons(_frame_of(wide)) == ("a" * 100, "b" * 100)


def test_reason_identity_is_redacted_without_a_second_redactor():
    frame = _frame_of(
        (
            Option(
                id="diag:token",
                label="token",
                legality=Legality.UNAVAILABLE,
                facts={"reason": "cannot use secret.executor here"},
                executor_ref="secret.executor",
            ),
            _diagnostic("opaque", "diag:opaque is unavailable"),
        )
    )
    reasons = diagnostics.bounded_pause_reasons(frame)
    assert reasons == (
        "cannot use [internal reference removed] here",
        "[non-selectable option] is unavailable",
    )

    summary = _run(snapshot(), frame)
    rendered = "; ".join(summary.stop_details["reasons"])
    assert "secret.executor" not in rendered and "diag:opaque" not in rendered
    assert "[internal reference removed]" in rendered and "[non-selectable option]" in rendered


def test_continue_leaves_no_pause_details_and_a_later_scene_pause_is_unrelated():
    class ContinueSink:
        def __init__(self):
            self.requests = []

        def escalate(self, request):
            self.requests.append(request)
            return EscalationResponse(EscalationAction.CONTINUE)

    snap, frame = _map_unreadable()
    sink = ContinueSink()
    summary = _run(snap, frame, sink=sink, max_steps=3)

    assert summary.status is RunStatus.MAX_STEPS
    assert summary.stop_reason == "max_steps"
    assert len(sink.requests) >= 1
    assert "reasons" not in summary.stop_details
    assert "no_legal_option" not in summary.stop_details.values()

    unknown = _run(snapshot(), scene_id="scene.unknown", kind=PerceptionKind.UNKNOWN)
    assert unknown.stop_details == {"kind": "unknown_scene", "agent_frames": 0}


def test_extracted_served_diagnostics_matches_the_base_capture():
    """Same-code golden captured from base 15c2050f before the move."""
    missing = _frame_of(
        (
            _diagnostic("unknown_effect"),
            Option(
                id="diag:filtered",
                label="filtered",
                legality=Legality.UNAVAILABLE,
                facts={"reason": "the exit is closed", "selection_reason": "route_focus"},
            ),
        )
    )
    assert diagnostics.served_diagnostics(missing) == {
        "total": 2,
        "items": [
            {"legality": "UNKNOWN", "reason": None},
            {"legality": "UNAVAILABLE", "reason": "the exit is closed"},
        ],
    }

    verbatim = _frame_of(
        (
            _diagnostic("map_unreadable", "map state is unreadable"),
            _diagnostic(
                "route_budget",
                "objective route search budget exhausted",
                legality=Legality.UNAVAILABLE,
            ),
        )
    )
    assert diagnostics.served_diagnostics(verbatim) == {
        "total": 2,
        "items": [
            {"legality": "UNKNOWN", "reason": "map state is unreadable"},
            {
                "legality": "UNAVAILABLE",
                "reason": "objective route search budget exhausted",
            },
        ],
    }

    secrets = _frame_of(
        (
            Option(
                id="diag:token",
                label="token",
                legality=Legality.UNAVAILABLE,
                facts={"reason": "cannot use secret.executor here"},
                executor_ref="secret.executor",
            ),
            _diagnostic("pointer", "read from 0x08000000 failed"),
        )
    )
    assert diagnostics.served_diagnostics(secrets) == {
        "total": 2,
        "items": [
            {
                "legality": "UNAVAILABLE",
                "reason": "cannot use [internal reference removed] here",
            },
            {"legality": "UNKNOWN", "reason": None},
        ],
    }

    hidden = _frame_of(
        (
            _diagnostic("opaque", "diag:opaque is unavailable"),
            _diagnostic("other", "diag:opaque blocks diag:other"),
        )
    )
    assert diagnostics.served_diagnostics(hidden) == {
        "total": 2,
        "items": [
            {"legality": "UNKNOWN", "reason": "[non-selectable option] is unavailable"},
            {
                "legality": "UNKNOWN",
                "reason": "[non-selectable option] blocks [non-selectable option]",
            },
        ],
    }

    rows = _frame_of(tuple(_diagnostic(f"row_{index}", "x" * 500) for index in range(40)))
    document = diagnostics.served_diagnostics(rows)
    assert document["total"] == 40
    assert len(document["items"]) == 32
    assert all(len(item["reason"]) == 240 for item in document["items"])
    assert all(item["reason"].endswith("...") for item in document["items"])
