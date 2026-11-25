"""Hybrid route-first admission: one proven warp/edge step skips the provider."""

from types import SimpleNamespace

import pytest
from test_field_targets_hidden_flags import _state
from test_planner_fossil_step import _FakeGraph
from test_probe_primitives import snapshot

from addons.system_one import hybrid
from addons.system_one.hybrid import HybridDecider
from pokegent.actions._exec.models import ActionResult
from pokegent.actions.field.builder import FieldBuilder
from pokegent.events import EventType, MemoryEventLog
from pokegent.games.gen3.firered.maps.map_schema import WarpInfo
from pokegent.goals.models import Objective
from pokegent.goals.validate import ResolvedObjective, ResolvedTarget
from pokegent.loop import Runner, RunnerConfig, RunStatus
from pokegent.navigation.adapter import NavigationAdapter
from pokegent.navigation.models import (
    NavigationEdge,
    NavigationNode,
    NavigationPlan,
    NavigationTarget,
)
from pokegent.options.models import ChoiceResult, DecisionFrame, Legality, Option, Risk, fingerprint
from pokegent.options.registry import BuildContext
from pokegent.pipeline import Perception, PerceptionKind
from pokegent.state.scenes import SceneResult

PARTY = (
    SimpleNamespace(slot=0, personality=1, species=1, moves=(), hp=10, max_hp=10),
    SimpleNamespace(slot=1, personality=2, species=4, moves=(), hp=10, max_hp=10),
)


def _warp(x=2, y=1, index=0):
    return WarpInfo(
        index=index,
        x=x,
        y=y,
        elevation=0,
        dest_group=1,
        dest_number=2,
        dest_name="DestMap",
        dest_warp_id=0,
        dynamic=False,
        tile_behavior=0,
        tile_kind="normal",
        trigger="step",
        trigger_direction=None,
    )


def _draft(*, party=PARTY, wall=False, objects=()):
    """A frame from the actual default FieldBuilder + NavigationAdapter."""
    state = _state(objects)
    if wall:
        grid = list(state.grid.blocks)
        grid[(3 + 7) * 19 + 2 + 7] = 0xC00
        state = state.model_copy(
            update={"grid": state.grid.model_copy(update={"blocks": tuple(grid)})}
        )
    warp = _warp()
    state = state.model_copy(update={"warps": (warp,)})
    objective = Objective(
        objective_id="reach_dest", revision=1, kind="reach", target={"map": "DestMap"}
    )
    resolved = ResolvedObjective(objective, ResolvedTarget(group=1, number=2), ())
    start = NavigationNode(0, 0, state.player.avatar_x, state.player.avatar_y)
    step = NavigationNode(0, 0, warp.x, warp.y)
    dest = NavigationNode(1, 2, 3, 3)
    route = []
    if (warp.x, warp.y) != (start.x, start.y):
        route.append(
            NavigationEdge(f"walk:{warp.x}:{warp.y}", start, step, "walk", 1, direction="N")
        )
    route.append(
        NavigationEdge(
            "warp:0",
            step,
            dest,
            "warp",
            2,
            executor_target=(("kind", "warp"), ("warp", warp.index)),
        )
    )
    plan = NavigationPlan(start, NavigationTarget(1, 2), 3, tuple(route))
    graph = _FakeGraph(names={(0, 0): "DemoMap", (1, 2): "DestMap"})
    graph.scripted_warp_arrival = lambda *_a, **_k: None
    adapter = NavigationAdapter(graph)
    adapter.plan = lambda *_a, **_k: plan
    builder = FieldBuilder(field_menu=True, navigation=adapter, objectives=lambda _: resolved)
    snap = snapshot().model_copy(update={"map": state, "party": tuple(party)})
    return builder.build(
        BuildContext(
            snapshot=snap,
            scene=SceneResult(scene_id="field.free", confidence=1, detector="invented"),
            goal_id=objective.objective_id,
        )
    )


def _frame(
    draft=None, *, options=(), goal_id="reach_dest", scene_id="field.free", decision_id="d-1"
):
    draft = _draft() if draft is None else draft
    raw = (*draft.options, *options)
    return DecisionFrame.from_options(
        raw,
        decision_id=decision_id,
        run_id="synthetic",
        snapshot_seq=1,
        scene_id=scene_id,
        scene_confidence=1.0,
        state_fingerprint=fingerprint(scene_id, options=[option.id for option in raw]),
        goal_id=goal_id,
        goal_revision=1,
        context=draft.context,
    )


def _tactical() -> Option:
    return Option(
        id="talk:7",
        label="Talk to the trainer",
        legality=Legality.LEGAL,
        facts={"target": {"kind": "trainer"}, "route_relation": "off route", "serves_goal": False},
        executor_ref="field.talk",
        postcondition_ref="field.talk_done",
        risk=Risk.HIGH,
    )


def _item() -> Option:
    return Option(
        id="item:14",
        label="Pick up the item",
        legality=Legality.LEGAL,
        facts={"target": {"kind": "item"}, "route_relation": "off route", "serves_goal": False},
        executor_ref="field.pickup_item",
        postcondition_ref="field.item_obtained",
        risk=Risk.NORMAL,
    )


def _unknown_target() -> Option:
    return Option(
        id="mystery:1",
        label="Use the unknown object",
        legality=Legality.LEGAL,
        facts={"target": {"kind": "mystery"}, "route_relation": "off route", "serves_goal": False},
        executor_ref="field.mystery",
        postcondition_ref="field.mystery_done",
    )


def _second_step() -> Option:
    return Option(
        id="warp:9:9:9",
        label="Take the other ladder",
        legality=Legality.LEGAL,
        facts={
            "target": {"kind": "warp", "map": (9, 9), "index": 9},
            "route_relation": "next route step",
            "next_route_step": True,
            "on_route": True,
            "serves_goal": True,
        },
        executor_ref="field.enter_warp",
        postcondition_ref="field.warp_done",
    )


def _replace(frame, option_id, **changes):
    options = tuple(
        option.model_copy(update=changes) if option.id == option_id else option
        for option in frame.options
    )
    return frame.model_copy(update={"options": options})


class FakeSystem1:
    def __init__(self, option_id="warp:0:0:0", confidence=0.9):
        self.frames = []
        self.option_id = option_id
        self.confidence = confidence

    def decide(self, frame):
        self.frames.append(frame)
        return ChoiceResult(
            option_id=self.option_id,
            probabilities={self.option_id: self.confidence},
            confidence=self.confidence,
            model_id="mock",
            provider_id="system1",
            decision_id=frame.decision_id,
        )

    def health(self):
        return SimpleNamespace(state="closed")


class FakeExternal:
    def __init__(self, port, timeout, *, autoplay="off"):
        self.port = port
        self.results = []

    def bind_agent_request(self, callback):
        self.callback = callback

    def bind_pending_frame(self, refresh):
        pass

    def bind_decision_valid(self, valid):
        pass

    def take_decided_frame(self):
        return None

    def discard_stale_decision(self):
        pass

    def update_briefing(self, *args, **kwargs):
        pass

    def offer(self, frame, **kwargs):
        return ChoiceResult(option_id="talk:7", provider_id="agent", decision_id=frame.decision_id)

    def record_action_result(self, frame, result, *, decided_by=None):
        self.results.append(decided_by)

    def clear_autoplay_pending(self):
        pass


def _decider(monkeypatch, *, system1=None, **kwargs):
    monkeypatch.setattr(hybrid, "ExternalDecider", FakeExternal)
    return HybridDecider(
        system1 if system1 is not None else FakeSystem1(),
        port=0,
        external_timeout=1,
        **kwargs,
    )


def _runner(frame, decider, events, *, progressed=False, max_steps=1, no_progress_limit=2):
    actions = []

    def perceive(decision_id, *_args):
        current = frame.model_copy(update={"decision_id": decision_id})
        return Perception(
            PerceptionKind.READY,
            snapshot(),
            SceneResult(scene_id=frame.scene_id, confidence=1, detector="invented"),
            current,
        )

    def execute(current, option_id):
        actions.append(option_id)
        return ActionResult(
            status="applied",
            option_id=option_id,
            progressed=progressed,
            verified_outcome=True,
        )

    executor = SimpleNamespace(perceiver=True, last_emulator_frame=None, execute=execute)
    runner = Runner(
        decider=decider,
        executor=executor,
        events=events,
        perceiver=SimpleNamespace(perceive=perceive),
        config=RunnerConfig(max_steps=max_steps, no_progress_limit=no_progress_limit),
    )
    return runner, actions


def test_actual_default_producer_frame_qualifies_for_route_first(monkeypatch):
    """Normal party + face/swap UI: the unique warp step is admitted with no provider call."""
    frame = _frame(_draft(wall=True))  # field_menu=True, party=2 -> swap + face + warp
    assert {"warp:0:0:0", "swap:00000002", "face:S"} <= set(frame.chooseable_ids())
    system1 = FakeSystem1()
    decider = _decider(monkeypatch, system1=system1)

    choice = decider.deterministic_route_step(frame)

    assert choice is not None
    assert choice.option_id == "warp:0:0:0"
    assert choice.provider_id == "route"
    assert choice.confidence is None
    assert choice.probabilities == {}
    assert choice.decision_id == frame.decision_id
    assert system1.frames == []
    assert decider.last_decision == {"decided_by": "route", "escalated": False}
    assert decider._decision_id == frame.decision_id


def test_runner_executes_route_step_once_with_route_provenance(monkeypatch):
    frame = _frame(_draft(wall=True))
    events = MemoryEventLog("synthetic")
    system1 = FakeSystem1()
    decider = _decider(monkeypatch, system1=system1)
    runner, actions = _runner(frame, decider, events, progressed=True)

    summary = runner.run()

    assert summary.status is RunStatus.MAX_STEPS
    assert actions == ["warp:0:0:0"]
    assert system1.frames == []
    assert not events.of_type(EventType.DECISION_SYSTEMONE_REQUESTED)
    assert not events.of_type(EventType.DECISION_SYSTEMONE_COMPLETED)
    route_events = events.of_type(EventType.DECISION_ROUTE)
    assert len(route_events) == 1
    assert route_events[0].details["decided_by"] == "route"
    started = events.of_type(EventType.ACTION_STARTED)
    assert started[0].details["source"] == "route"
    # Route actions keep the normal decider history responsibility.
    assert len(runner.history._entries) == 1


@pytest.mark.parametrize("flag", ["_no_progress_recovery", "_stuck"])
def test_recovery_ownership_wins(monkeypatch, flag):
    frame = _frame(_draft(wall=True))
    decider = _decider(monkeypatch)
    setattr(decider, flag, True)

    assert decider.deterministic_route_step(frame) is None


def test_override_window_wins(monkeypatch):
    frame = _frame(_draft(wall=True))
    decider = _decider(monkeypatch, override_window_s=0.5)

    assert decider.deterministic_route_step(frame) is None


def test_forced_agent_repeat_wins(monkeypatch):
    frame = _frame(_draft(wall=True))
    decider = _decider(monkeypatch)
    decider._repeat_key = (frame.scene_id, frame.state_fingerprint, "warp:0:0:0")
    decider._repeat_count = decider.force_agent_after_repeats

    assert decider.deterministic_route_step(frame) is None


@pytest.mark.parametrize(
    "kw",
    [
        {"goal_id": None},
        {"scene_id": "scene.unknown"},
        {"scene_id": "busy.animation"},
    ],
)
def test_no_goal_unknown_or_busy_scene_refuses(monkeypatch, kw):
    frame = _frame(_draft(wall=True), **kw)
    decider = _decider(monkeypatch)

    assert decider.deterministic_route_step(frame) is None


_STEP_FACTS = {
    "target": {"kind": "warp", "map": (0, 0), "index": 0},
    "route_relation": "next route step",
    "next_route_step": True,
    "on_route": True,
    "serves_goal": True,
}


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"risk": Risk.HIGH}, "high"),
        ({"facts": {**_STEP_FACTS, "irreversible": True}}, "irreversible"),
        ({"facts": {**_STEP_FACTS, "serves_goal": False}}, "not serves_goal"),
        ({"facts": {**_STEP_FACTS, "route_needs": ["item"]}}, "unmet"),
        ({"facts": {**_STEP_FACTS, "blocked_choice": "warp:9:9:9"}}, "blocked"),
        ({"facts": {**_STEP_FACTS, "selection_reason": "repeated_failure"}}, "repeated"),
    ],
)
def test_high_irreversible_unmet_or_repeated_step_refuses(monkeypatch, changes, expected):
    frame = _frame(_draft(wall=True))
    frame = _replace(frame, "warp:0:0:0", **changes)
    decider = _decider(monkeypatch)

    assert decider.deterministic_route_step(frame) is None


def test_ambiguous_route_steps_refuse(monkeypatch):
    frame = _frame(_draft(wall=True), options=(_second_step(),))
    decider = _decider(monkeypatch)

    assert decider.deterministic_route_step(frame) is None


def _exit(**extra) -> Option:
    facts = {
        "target": {"kind": "warp", "map": (1, 1), "index": 1},
        "route_relation": "off route",
        "serves_goal": False,
    }
    facts.update(extra)
    return Option(
        id="warp:1:1:1",
        label="Take the other exit",
        legality=Legality.LEGAL,
        facts=facts,
        executor_ref="field.enter_warp",
        postcondition_ref="field.warp_done",
    )


def test_ordinary_off_route_warp_alternative_does_not_block(monkeypatch):
    frame = _frame(_draft(wall=True), options=(_exit(),))
    decider = _decider(monkeypatch)

    choice = decider.deterministic_route_step(frame)

    assert choice is not None and choice.option_id == "warp:0:0:0"


def test_competing_on_route_warp_alternative_blocks(monkeypatch):
    frame = _frame(_draft(wall=True), options=(_exit(on_route=True, serves_goal=True),))
    decider = _decider(monkeypatch)

    assert decider.deterministic_route_step(frame) is None


@pytest.mark.parametrize("extra", [_item(), _tactical(), _unknown_target()])
def test_meaningful_or_unknown_alternative_refuses(monkeypatch, extra):
    frame = _frame(_draft(wall=True), options=(extra,))
    decider = _decider(monkeypatch)

    assert decider.deterministic_route_step(frame) is None


def test_meaningful_tactical_alternative_returns_to_the_decider(monkeypatch):
    frame = _frame(_draft(wall=True), options=(_tactical(),))
    events = MemoryEventLog("synthetic")
    system1 = FakeSystem1(option_id="warp:0:0:0")
    decider = _decider(monkeypatch, system1=system1)
    runner, actions = _runner(frame, decider, events, progressed=True)

    runner.run()

    assert decider.deterministic_route_step(frame) is None
    assert len(system1.frames) == 1
    assert actions == ["warp:0:0:0"]
    assert not events.of_type(EventType.DECISION_ROUTE)
    assert events.of_type(EventType.DECISION_SYSTEMONE_REQUESTED)


def test_non_hybrid_decider_keeps_the_original_path():
    events = MemoryEventLog("synthetic")
    frame = _frame(_draft(wall=True))

    class PlainDecider:
        last_decision = {"decided_by": "system1"}

        def __init__(self):
            self.frames = []

        def decide(self, shown):
            self.frames.append(shown)
            return ChoiceResult(
                option_id="warp:0:0:0", provider_id="system1", decision_id=shown.decision_id
            )

    decider = PlainDecider()
    runner, actions = _runner(frame, decider, events, progressed=True)

    runner.run()

    assert len(decider.frames) == 1
    assert actions == ["warp:0:0:0"]
    assert not events.of_type(EventType.DECISION_ROUTE)


def test_stale_route_result_is_not_executed(monkeypatch):
    """Runner re-validates the route claim against the current frame, then falls back."""
    frame = _frame(_draft(wall=True))
    events = MemoryEventLog("synthetic")
    system1 = FakeSystem1(option_id="warp:0:0:0")
    decider = _decider(monkeypatch, system1=system1)

    def stale(frame):
        choice = HybridDecider.deterministic_route_step(decider, frame)
        return choice.model_copy(update={"decision_id": "older-frame"}) if choice else None

    decider.deterministic_route_step = stale
    runner, actions = _runner(frame, decider, events, progressed=True)

    runner.run()

    assert len(system1.frames) == 1
    assert actions == ["warp:0:0:0"]
    assert not events.of_type(EventType.DECISION_ROUTE)


def test_stale_objective_revision_skips_route(monkeypatch):
    """A frame built for an older objective revision never gets pre-provider admission."""
    frame = _frame(_draft(wall=True))
    events = MemoryEventLog("synthetic")
    system1 = FakeSystem1(option_id="warp:0:0:0")
    decider = _decider(monkeypatch, system1=system1)
    runner, actions = _runner(frame, decider, events, progressed=True)
    runner.objectives = SimpleNamespace(
        objective=SimpleNamespace(objective_id="reach_dest", revision=99),
        refresh=lambda: SimpleNamespace(warnings=()),
    )

    runner._handle_ready(
        Perception(
            PerceptionKind.READY,
            snapshot(),
            SceneResult(scene_id="field.free", confidence=1, detector="invented"),
            frame,
        )
    )

    assert len(system1.frames) == 1
    assert actions == ["warp:0:0:0"]
    assert not events.of_type(EventType.DECISION_ROUTE)


def test_route_owned_failures_reach_the_normal_no_progress_stop(monkeypatch):
    frame = _frame(_draft(wall=True))
    events = MemoryEventLog("synthetic")
    system1 = FakeSystem1()
    decider = _decider(monkeypatch, system1=system1)
    runner, actions = _runner(frame, decider, events, progressed=False, max_steps=10)

    summary = runner.run()

    assert summary.status is RunStatus.SAFE_PAUSE
    assert actions == ["warp:0:0:0", "warp:0:0:0"]
    assert system1.frames == []
    stuck = events.of_type(EventType.PROGRESS_STUCK)
    assert any(event.outcome == "no_progress" for event in stuck)


def test_route_failure_evidence_survives_record_action_result(monkeypatch):
    frame = _frame(_draft(wall=True))
    decider = _decider(monkeypatch)
    choice = decider.deterministic_route_step(frame)
    assert choice is not None

    decider.record_action_result(
        frame,
        ActionResult(
            status="applied",
            option_id=choice.option_id,
            progressed=False,
            verified_outcome=True,
        ),
    )

    assert decider._repeat_count == 1
    assert decider._repeat_key == (frame.scene_id, frame.state_fingerprint, choice.option_id)
    assert decider.external.results == ["route"]
