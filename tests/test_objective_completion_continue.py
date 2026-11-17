"""Synthetic objective lifecycle with real store, perception, executor and runner."""

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from types import SimpleNamespace

import pytest

from addons.system_one.hybrid import HybridDecider
from pokegent.actions import Registries
from pokegent.actions._exec.executor import Executor
from pokegent.actions.unknown import UnknownBuilder, register_unknown_recovery
from pokegent.app import config_from_mapping
from pokegent.dashboard.state import ViewerState
from pokegent.events import EventType, MemoryEventLog
from pokegent.games.gen3.firered.reader.schemas import (
    FlagSet,
    Health,
    Player,
    ReadError,
    StateSnapshot,
    VarTable,
)
from pokegent.games.gen3.firered.reader.ui_schemas import (
    FieldEvidence,
    MainEvidence,
    MenuEvidence,
    ScriptEvidence,
    TaskEvidence,
    TextEvidence,
    UiEvidence,
)
from pokegent.goals.source import ObjectiveSession, ScriptedObjectiveSource
from pokegent.goals.store import ObjectiveStore, atomic_write_json
from pokegent.loop import Runner, RunnerConfig, RunStatus
from pokegent.options.models import ChoiceResult, fingerprint
from pokegent.pipeline import Perceiver
from pokegent.server import ExternalDecider
from pokegent.state.scenes import FunctionDetector, PriorityClass, SceneResult


def objective(revision=1, *, objective_id="first", set_by="agent", amount=1):
    return dict(objective_id=objective_id, revision=revision, kind="hold", set_by=set_by,
                success_probes=[dict(kind="money_at_least", amount=amount)], forbid=["sell"])


@pytest.fixture
def harness(tmp_path):
    def build(*, set_by="agent", policy="continue", amount=1, plan=None):
        store = ObjectiveStore(tmp_path / "objective.json")
        script = ScriptedObjectiveSource(store, plan) if plan else None
        if script is None:
            assert store.propose(objective(set_by=set_by, amount=amount)).accepted
        session = ObjectiveSession(store, script=script)
        state = SimpleNamespace(frame=0, seq=0, actions=0, money=0, valid=True,
                                scene="field.free", busy=False, battle=False)
        seen, results = [], []
        events = MemoryEventLog("synthetic")
        viewer = ViewerState(run_id="synthetic", decider="first", max_steps=6, realtime=False)
        events.add_listener(viewer.on_event)

        class Source:
            def read_snapshot(self):
                state.seq += 1
                section = dict(valid=True, confidence=1)
                ui = UiEvidence(
                    valid=True, main=MainEvidence(**section, in_battle=state.battle),
                    tasks=TaskEvidence(**section), text=TextEvidence(**section),
                    script=ScriptEvidence(**section), menu=MenuEvidence(**section),
                    field=FieldEvidence(**section), controls_locked=False,
                )
                return StateSnapshot.create(
                    rom_profile="synthetic", snapshot_seq=state.seq, emulator_frame=state.frame,
                    health=Health(valid=state.valid, read_errors=() if state.valid else (
                        ReadError(subsystem="test", code="invalid", message="synthetic failure"),
                    )),
                    player=Player(name="Test", gender="male", tid=0, sid=0, map_group=0,
                                  map_num=0, map_layout_id=0, x=0, y=0,
                                  money=state.money, coins=0) if state.valid else None,
                    flags=FlagSet() if state.valid else None,
                    vars=VarTable() if state.valid else None,
                ).with_ui(ui)

        class Actor:
            def advance_frames(self, frames):
                state.frame += frames
                return frames

            def tap(self, buttons, hold_frames=2, wait_frames=1):
                state.actions += 1
                state.money += 1
                state.frame += hold_frames + wait_frames
                return hold_frames

            def release_all(self):
                pass

            def save_state(self, *args):
                pytest.fail("objective transitions must not save/restart the emulator")

        class Builder(UnknownBuilder):
            def build(self, ctx):
                draft = super().build(ctx)
                return replace(draft, state_fingerprint=fingerprint(
                    draft.state_fingerprint, goal=ctx.goal_id, objective=ctx.objective))

        registries = Registries.new()
        register_unknown_recovery(registries)
        registries.builders.register_scenes(("field.free", "dialog.end"), Builder())
        registries.detectors.register(FunctionDetector(
            "synthetic", PriorityClass.FIELD_DIALOG,
            lambda s: SceneResult(scene_id=state.scene, confidence=1, busy=state.busy),
        ))
        source, actor = Source(), Actor()
        perceiver = Perceiver(source, registries.detectors, registries.builders, run_id="synthetic")
        executor = Executor(actor, registries.executors, registries.verifiers, perceiver)

        def decide(frame):
            seen.append((session.objective, frame))
            return ChoiceResult(option_id="tap:b")

        decider = SimpleNamespace(decide=decide,
                                  record_action_result=lambda f, r: results.append(r))
        runner = Runner(decider=decider, executor=executor, perceiver=perceiver, events=events,
                        objectives=session, config=RunnerConfig(
                            run_id="synthetic", max_steps=6, no_progress_limit=20,
                            on_objective_done=policy, hold_idle_frames=0, hold_poll_s=0,
                            invalid_retries=0))
        return SimpleNamespace(**locals())
    return build


@pytest.mark.parametrize("set_by", ["agent", "operator"])
def test_completion_clears_and_another_verified_action_uses_same_run(harness, set_by):
    h = harness(set_by=set_by)
    identity = (os.getpid(), h.runner, h.actor, h.perceiver, h.viewer)
    summary = h.runner.run()
    assert summary.status is RunStatus.MAX_STEPS
    assert identity == (os.getpid(), h.runner, h.actor, h.perceiver, h.viewer)
    assert h.state.actions >= 2
    assert h.seen[0][0].objective_id == "first"
    assert all(obj is None for obj, _ in h.seen[1:])
    assert all(frame.run_id == "synthetic" for _, frame in h.seen)
    assert all(r.verified_outcome is True for r in h.results)
    assert not h.store.path.exists() and h.store.current() is None
    assert h.session.current() is None and summary.objective["active"] is None
    assert h.store.revision() == 1
    assert [r["event"] for r in h.store.history()].count("completed") == 1
    assert [r["event"] for r in h.store.history()].count("completion_cleared") == 1
    assert len(h.events.of_type(EventType.RUNTIME_STARTED)) == 1
    assert not h.events.of_type(EventType.RUNTIME_PAUSED)
    assert not h.runner.stop_event.is_set()


@pytest.mark.parametrize("policy", ["hold", "pause"])
def test_existing_policies_keep_completed_objective(harness, policy):
    h = harness(policy=policy)
    assert h.runner._iteration() is None
    assert h.runner._iteration() is (RunStatus.WAITING_OBJECTIVE if policy == "pause" else None)
    assert h.runner._holding is (policy == "hold")
    assert h.state.actions == 1 and h.store.path.exists()
    assert h.session.completed
    assert not any(row["event"] == "completion_cleared" for row in h.store.history())
    restarted = ObjectiveStore(h.store.path)
    assert restarted.current() == h.session.objective
    assert restarted.revision() == 1
    assert RunnerConfig().on_objective_done == "hold"


@pytest.mark.parametrize("unsafe", ["busy", "battle", "menu", "invalid"])
def test_completion_waits_for_fresh_safe_boundary(harness, unsafe):
    h = harness()
    h.session.refresh()
    h.state.money = 1
    h.state.busy = unsafe == "busy"
    h.state.battle = unsafe == "battle"
    h.state.scene = "menu.pause" if unsafe == "menu" else "field.free"
    h.state.valid = unsafe != "invalid"
    perception = h.runner._perceive()
    h.runner._objective_observe(perception)
    assert h.store.current() is not None and h.store.path.exists()
    assert not any(row["event"] == "completed" for row in h.store.history())
    h.state.busy = h.state.battle = False
    h.state.scene, h.state.valid, h.state.money = "field.free", True, 0
    h.runner._objective_observe(h.runner._perceive())
    assert h.store.current() is not None  # prior completion is not fresh evidence
    h.state.money = 1
    h.runner._objective_observe(h.runner._perceive())
    assert h.store.current() is None


def test_completion_id_and_revision_cas_preserves_newer_objective(tmp_path):
    path = tmp_path / "objective.json"
    store = ObjectiveStore(path)
    assert store.propose(objective()).accepted
    assert not store.commit_completion(objective_id="other", expected_revision=1,
                                       clear_completed=True)
    other = ObjectiveStore(path)
    assert other.propose(objective(2, objective_id="second")).accepted
    assert not store.commit_completion(objective_id="first", expected_revision=1,
                                       clear_completed=True)
    assert store.current().objective_id == "second" and path.exists()
    assert not any(row["event"] == "completed" for row in store.history())


def test_concurrent_stores_record_completion_once_and_manual_clear_keeps_authority(tmp_path):
    store = ObjectiveStore(tmp_path / "objective.json")
    assert store.propose(objective(set_by="operator")).accepted
    other = ObjectiveStore(store.path)
    for current in (store, other):
        assert current.commit_completion(objective_id="first", expected_revision=1)
        assert current.clear(expected_revision=1)["reason"] == "forbid_dropped"
    assert other.commit_completion(objective_id="first", expected_revision=1,
                                   clear_completed=True)
    assert sum(row["event"] == "completed" for row in store.history()) == 1


def test_restart_and_restored_old_file_cannot_resurrect_completion(tmp_path):
    store = ObjectiveStore(tmp_path / "objective.json")
    assert store.propose(objective()).accepted
    assert store.commit_completion(objective_id="first", expected_revision=1,
                                   clear_completed=True)
    assert ObjectiveStore(store.path).current() is None
    atomic_write_json(store.path, objective())  # e.g. crash before unlink or stale restoration
    restarted = ObjectiveStore(store.path)
    assert restarted.current() is None and restarted.revision() == 1
    assert not restarted.propose(objective()).accepted
    assert restarted.propose(objective(2, objective_id="second")).accepted


def test_scripted_successor_survives_completion_clear(harness):
    h = harness(plan=[objective(set_by="script"),
                      objective(2, objective_id="second", set_by="script", amount=99)])
    summary = h.runner.run()
    assert summary.status is RunStatus.MAX_STEPS
    assert [obj.objective_id for obj, _ in h.seen] == ["first"] + ["second"] * 4
    assert h.store.current().objective_id == "second"
    assert h.script.status()["completed_ids"] == ["first"]
    assert not any(row["event"] == "completion_cleared" for row in h.store.history())


def test_exhausted_script_clears_last_goal_without_restarting(harness):
    h = harness(plan=[objective(set_by="script")])
    assert h.runner.run().status is RunStatus.MAX_STEPS
    assert h.script.status()["completed_ids"] == ["first"]
    assert h.store.current() is None and h.state.actions >= 2
    restarted = ScriptedObjectiveSource(ObjectiveStore(h.store.path), h.script.plan)
    assert restarted.start() is None and restarted.status()["completed_ids"] == ["first"]


def test_completed_queue_predecessor_cannot_replay_below_revision_floor(harness):
    first = objective(set_by="script")
    second = objective(2, objective_id="second", set_by="script", amount=2)
    h = harness(plan=[first, second])
    assert h.runner.run().status is RunStatus.MAX_STEPS
    assert h.script.status()["completed_ids"] == ["first", "second"]
    assert h.store.current() is None
    assert [(row["objective_id"], row["revision"]) for row in h.store.history()
            if row["event"] == "completion_cleared"] == [("second", 2)]

    atomic_write_json(h.store.path, first)
    restarted = ObjectiveStore(h.store.path)
    assert restarted.current() is None and restarted.revision() == 2
    assert restarted.last_warning().code == "stale_revision"
    assert restarted.propose(objective(3, objective_id="third")).accepted


def test_history_failure_does_not_clear_without_durable_record(harness, monkeypatch):
    h = harness()
    h.session.refresh()
    h.state.money = 1
    monkeypatch.setattr(h.store, "_log", lambda *args: False)
    h.runner._objective_observe(h.runner._perceive())
    assert h.store.current() is not None and h.store.path.exists()


def test_config_supports_continue_and_preserves_default():
    assert config_from_mapping({}).runner_config().on_objective_done == "hold"
    config = config_from_mapping({"run": {"on_objective_done": "continue"}})
    assert config.runner_config().on_objective_done == "continue"


@pytest.mark.parametrize("mode", ["agent", "hybrid"])
def test_pending_choice_rebuild_rejects_stale_frame_and_preserves_legality(harness, mode,
                                                                        monkeypatch):
    monkeypatch.setattr("pokegent.server.ThreadingHTTPServer", lambda *args: SimpleNamespace(
        server_address=("127.0.0.1", 19760), serve_forever=lambda: None))
    h = harness(amount=99)
    h.session.refresh()
    frame = h.runner._perceive().frame
    h.runner._pending_objective_key = ("first", 1)
    external = ExternalDecider(port=19760)
    if mode == "hybrid":
        decider = HybridDecider(
            SimpleNamespace(decide=lambda f: ChoiceResult(option_id="tap:b", confidence=0)),
            port=19760, external_timeout=1,
        )
        decider.agent = decider.external = external
    else:
        decider = external
    decider.bind_pending_frame(h.runner._refresh_pending_frame)
    requests, errors, choices = [], [], []

    def requested(frame_id, offered):
        requests.append((frame_id, offered))
        try:
            if len(requests) == 1:
                assert h.store.propose(objective(2, objective_id="second", amount=99)).accepted
            else:
                with pytest.raises(LookupError, match="stale frame"):
                    external.choose(dict(frame_id=requests[0][0], option_id="tap:b"))
                with pytest.raises(ValueError, match="not in the current frame"):
                    external.choose(dict(frame_id=frame_id, option_id="release:mon"))
                choices.append(external.choose(dict(frame_id=frame_id, option_id="tap:b")))
        except BaseException as exc:
            errors.append(exc)
            external._stop_requested.set()  # no unbounded wait on a failed assertion

    external.bind_agent_request(requested)
    result = decider.decide(frame)
    assert not errors and choices[0]["accepted"]
    assert len(requests) == 2 and requests[0][0] != requests[1][0]
    rebuilt = decider.take_decided_frame()
    assert rebuilt.goal_id == "second" and h.session.objective.revision == 2
    assert rebuilt.snapshot_seq == frame.snapshot_seq
    assert result.option_id in rebuilt.chooseable_ids()
    assert h.state.actions == 0 and not h.runner.stop_event.is_set()
    executed = h.executor.execute(rebuilt, result.option_id)
    assert executed.verified_outcome is True and h.state.actions == 1


@pytest.mark.parametrize("same_id", [False, True])
def test_concurrent_completion_and_proposal_never_delete_successor(tmp_path, same_id):
    store = ObjectiveStore(tmp_path / "objective.json")
    assert store.propose(objective()).accepted
    writer = ObjectiveStore(store.path)
    barrier = Barrier(2)

    def complete():
        barrier.wait(timeout=2)
        return store.commit_completion(objective_id="first", expected_revision=1,
                                       clear_completed=True)

    def propose():
        barrier.wait(timeout=2)
        return writer.propose(objective(2, objective_id="first" if same_id else "second"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        completed, proposed = pool.submit(complete), pool.submit(propose)
        completed.result(timeout=3)
        assert proposed.result(timeout=3).accepted
    assert store.current().revision == 2 and store.path.exists()
    assert ObjectiveStore(store.path).current().revision == 2


def test_hot_reload_and_invalid_writes_preserve_same_runner(harness):
    h = harness(amount=99)
    assert h.runner._iteration() is None
    assert h.store.propose(objective(2, objective_id="second", amount=99)).accepted
    assert h.runner._iteration() is None
    h.store.path.write_text('{"malformed":')
    assert h.runner._iteration() is None
    atomic_write_json(h.store.path, objective())
    assert h.runner._iteration() is None
    assert [obj.objective_id for obj, _ in h.seen] == ["first"] + ["second"] * 3
    assert h.state.actions == 4 and h.session.revisions_seen == [1, 2]
    assert not h.runner.stop_event.is_set()
    assert not h.events.of_type(EventType.RUNTIME_PAUSED)


def test_stale_choice_is_revalidated_before_any_input(harness):
    h = harness(amount=99)
    h.session.refresh()
    stale = h.runner._perceive().frame
    assert h.store.propose(objective(2, objective_id="second", amount=99)).accepted
    h.session.refresh()
    result = h.executor.execute(stale, "tap:b")
    assert result.status.value == "rejected" and h.state.actions == 0


def test_cli_continue_reaches_app(monkeypatch, tmp_path):
    from pokegent import cli

    called = []
    monkeypatch.setattr("pokegent.app.run_offline", lambda **kwargs:
                        called.append(kwargs) or SimpleNamespace(exit_code=0))
    monkeypatch.setenv("POKEGENT_FIELD_MENU", "1")
    with pytest.raises(SystemExit) as result:
        cli.main(["run", "--new-game", "--config", str(tmp_path / "absent.yaml"),
                  "--on-objective-done", "continue"])
    assert result.value.code == 0 and called[0]["on_objective_done"] == "continue"


def test_completion_tombstone_survives_crash_before_unlink(tmp_path, monkeypatch):
    store = ObjectiveStore(tmp_path / "objective.json")
    assert store.propose(objective()).accepted

    def crash():
        raise RuntimeError("simulated crash before unlink")

    monkeypatch.setattr(store, "_remove_active", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.commit_completion(objective_id="first", expected_revision=1, clear_completed=True)
    assert store.path.exists()
    restarted = ObjectiveStore(store.path)
    assert restarted.current() is None and restarted.revision() == 1


def test_preexisting_writer_cannot_reuse_cleared_revision(tmp_path):
    store = ObjectiveStore(tmp_path / "objective.json")
    writer = ObjectiveStore(store.path)
    assert store.propose(objective()).accepted
    assert store.commit_completion(objective_id="first", expected_revision=1,
                                   clear_completed=True)
    assert not writer.propose(objective()).accepted
    assert writer.propose(objective(2, objective_id="second")).accepted


def test_hot_reload_during_one_run_never_stops(harness):
    h = harness(amount=99)

    def publish(iteration):
        if iteration == 1:
            assert h.store.propose(objective(2, objective_id="second", amount=99)).accepted

    h.runner.after_iteration.append(publish)
    assert h.runner.run().status is RunStatus.MAX_STEPS
    assert [obj.objective_id for obj, _ in h.seen] == ["first"] + ["second"] * 5
    assert all(r.verified_outcome is True for r in h.results)
    assert len(h.events.of_type(EventType.RUNTIME_STARTED)) == 1
    assert not h.events.of_type(EventType.RUNTIME_PAUSED) and not h.runner.stop_event.is_set()
