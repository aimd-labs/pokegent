"""Already-satisfied reach admission through the real store/propose API."""

import importlib
import json
from types import SimpleNamespace

import pytest

from pokegent.games.gen3.firered.reader.schemas import (
    FlagSet,
    Health,
    Player,
    ReadError,
    StateSnapshot,
    VarTable,
)
from pokegent.goals.store import ObjectiveStore
from pokegent.goals.tracker import GOAL_CHANGED, MILESTONE_COMPLETED, ObjectiveTracker
from pokegent.goals.validate import MapFacts

MAP = "PewterCity"
MAP_KEY = (3, 7)


class Directory:
    def describe(self, group, number):
        return MapFacts(10, 10, 1, frozenset({1}))

    def resolve_item(self, name):
        return None

    def leader_for_badge(self, badge):
        return None


def snapshot(*, usable=True, map_key=MAP_KEY, x=4, y=5, flags=()):
    return StateSnapshot.create(
        rom_profile="synthetic",
        snapshot_seq=1,
        emulator_frame=0,
        health=Health(
            valid=usable,
            read_errors=() if usable else (ReadError("test", "invalid", "synthetic"),),
        ),
        player=Player(
            name="Test",
            gender="male",
            tid=0,
            sid=0,
            map_group=map_key[0],
            map_num=map_key[1],
            map_layout_id=0,
            x=x,
            y=y,
            money=0,
            coins=0,
        )
        if usable
        else None,
        flags=FlagSet(set_ids=tuple(sorted(flags))) if usable else None,
        vars=VarTable() if usable else None,
    )


def reach(revision, *, target=None, objective_id=None):
    return {
        "objective_id": objective_id or f"reach{revision}",
        "revision": revision,
        "kind": "reach",
        "target": target or {"map": MAP},
    }


def hold(revision=1):
    return {"objective_id": f"hold{revision}", "revision": revision, "kind": "hold"}


@pytest.fixture(autouse=True)
def map_resolution(monkeypatch):
    module = importlib.import_module("pokegent.goals.validate")
    monkeypatch.setattr(
        module, "resolve_map", lambda name: None if name == "Nowhere" else MAP_KEY
    )
    monkeypatch.setattr(module.map_names, "map_name", lambda group, number: "MAP_PEWTER_CITY")


def store_with_active(tmp_path):
    store = ObjectiveStore(tmp_path / "objective.json", directory=Directory())
    assert store.propose(hold(1)).accepted
    return store


def assert_rejected_cleanly(store, result):
    assert result.accepted is False
    assert [reason.code for reason in result.reasons] == ["objective_already_satisfied"]
    assert store.current().objective_id == "hold1"
    assert store.current().revision == 1
    assert store.revision() == 1
    assert json.loads(store.path.read_text())["revision"] == 1
    assert [row["event"] for row in store.history()] == ["accepted", "rejected"]


def test_already_on_map_reach_is_rejected_not_accepted(tmp_path):
    store = store_with_active(tmp_path)
    store.bind_snapshot(lambda: snapshot())

    result = store.propose(reach(2))

    assert_rejected_cleanly(store, result)


def test_already_on_explicit_tile_reach_is_rejected(tmp_path):
    store = store_with_active(tmp_path)
    store.bind_snapshot(lambda: snapshot(x=3, y=2))

    result = store.propose(reach(2, target={"map": MAP, "x": 3, "y": 2}))

    assert_rejected_cleanly(store, result)


def test_same_map_different_tile_reach_is_still_accepted(tmp_path):
    store = store_with_active(tmp_path)
    store.bind_snapshot(lambda: snapshot(x=3, y=2))

    result = store.propose(reach(2, target={"map": MAP, "x": 1, "y": 1}))

    assert result.accepted is True
    assert store.current().revision == 2


def test_reach_on_different_map_is_still_accepted(tmp_path):
    store = store_with_active(tmp_path)
    store.bind_snapshot(lambda: snapshot(map_key=(9, 9)))

    result = store.propose(reach(2))

    assert result.accepted is True
    assert store.current().revision == 2


def test_unreadable_unknown_or_absent_snapshot_keeps_existing_acceptance(tmp_path):
    unreadable = store_with_active(tmp_path)
    unreadable.bind_snapshot(lambda: snapshot(usable=False))
    assert unreadable.propose(reach(2)).accepted is True

    unknown = ObjectiveStore(tmp_path / "unknown.json", directory=Directory())
    unknown.bind_snapshot(lambda: None)
    assert unknown.propose(reach(1)).accepted is True

    failed = ObjectiveStore(tmp_path / "failed.json", directory=Directory())
    failed.bind_snapshot(lambda: (_ for _ in ()).throw(RuntimeError("native refused")))
    assert failed.propose(reach(1)).accepted is True

    absent = ObjectiveStore(tmp_path / "absent.json", directory=Directory())
    assert absent.propose(reach(1)).accepted is True


def test_rejection_is_silent_for_tracker_and_preserves_active(tmp_path):
    store = store_with_active(tmp_path)
    tracker = ObjectiveTracker(store=store)
    current = snapshot()
    assert [event.type for event in tracker.update(current)] == [GOAL_CHANGED]
    store.bind_snapshot(lambda: current)

    result = store.propose(reach(2))

    assert result.accepted is False
    assert tracker.update(current) == ()
    assert tracker.objective.objective_id == "hold1"
    assert tracker.objective.revision == 1
    assert MILESTONE_COMPLETED not in {event.type for event in tracker.update(current)}


def test_validation_and_other_kinds_keep_existing_behavior(tmp_path):
    store = store_with_active(tmp_path)
    store.bind_snapshot(lambda: snapshot())

    unknown_map = store.propose(reach(2, target={"map": "Nowhere"}))
    assert unknown_map.accepted is False
    assert unknown_map.reasons[0].code == "unknown_map"

    stale = store.propose(reach(1))
    assert stale.accepted is False
    assert stale.reasons[0].code == "stale_revision"

    talk = {
        "objective_id": "talk2",
        "revision": 2,
        "kind": "talk",
        "target": {"map": MAP, "npc": 1},
        "success_probes": [{"kind": "flag", "flag": 1}],
    }
    talk_store = ObjectiveStore(tmp_path / "talk.json", directory=Directory())
    talk_store.bind_snapshot(lambda: snapshot(flags=(1,)))
    assert talk_store.propose(talk).accepted is True

    heal_store = ObjectiveStore(tmp_path / "heal.json", directory=Directory())
    heal_store.bind_snapshot(lambda: snapshot())
    heal = {"objective_id": "heal2", "revision": 1, "kind": "heal", "target": {"map": MAP}}
    assert heal_store.propose(heal).accepted is True


def test_reach_with_unsatisfied_explicit_probe_is_still_accepted(tmp_path):
    store = store_with_active(tmp_path)
    store.bind_snapshot(lambda: snapshot())

    result = store.propose(
        {
            "objective_id": "probe2",
            "revision": 2,
            "kind": "reach",
            "target": {"map": MAP},
            "success_probes": [{"kind": "flag", "flag": 1}],
        }
    )

    assert result.accepted is True
    assert store.current().objective_id == "probe2"


def test_server_objective_propose_returns_typed_rejection(tmp_path):
    from pokegent.server import ExternalDecider

    store = store_with_active(tmp_path)
    store.bind_snapshot(lambda: snapshot())
    owner = object.__new__(ExternalDecider)
    owner._objective_store = store

    response = owner.objective_propose(reach(2))

    assert response["accepted"] is False
    assert response["proposal"]["reasons"][0]["code"] == "objective_already_satisfied"
    assert store.current().revision == 1


def test_build_runner_binds_fresh_snapshot_not_the_decision_source(tmp_path, monkeypatch):
    app_module = importlib.import_module("pokegent.app")
    reads = []
    decision_reads = []
    current = snapshot()

    class FreshReader:
        def __init__(self, source, profile, **kwargs):
            self.source = source

        def read(self):
            reads.append(1)
            return SimpleNamespace(snapshot=current)

    class DecisionSource:
        def __init__(self, *args, **kwargs):
            pass

        def read_snapshot(self):
            decision_reads.append(1)
            raise AssertionError("decision snapshot must not feed admission")

    class FakeRunner:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self._last_snapshot = SimpleNamespace(usable=False)

    fake_registries = SimpleNamespace(
        detectors=object(), builders=object(), executors=object(), verifiers=object()
    )
    fake_bundle = SimpleNamespace(plugins={}, sidecar=None, confirm_policy=lambda catalog: None)
    monkeypatch.setattr(app_module, "load_scene_catalog", lambda: object())
    monkeypatch.setattr(app_module.Registries, "new", lambda catalog: fake_registries)
    monkeypatch.setattr(app_module, "register_families", lambda *args, **kwargs: fake_bundle)
    monkeypatch.setattr(app_module, "CompositeSnapshotSource", DecisionSource)
    monkeypatch.setattr(app_module, "Perceiver", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(
        app_module, "Executor", lambda *args, **kwargs: SimpleNamespace(perceiver=None)
    )
    monkeypatch.setattr(app_module, "Runner", FakeRunner)
    monkeypatch.setattr(
        "pokegent.games.gen3.firered.reader.menu_rom.FireRedMenuResolver",
        lambda actor, profile: None,
    )
    monkeypatch.setattr("pokegent.games.gen3.firered.reader.live.LiveReader", FreshReader)
    profile = SimpleNamespace(profile_id="synthetic", rom=SimpleNamespace(sha1="a" * 40))

    app = app_module.build_runner(
        actor=object(),
        profile=profile,
        events=object(),
        decider=SimpleNamespace(),
        objectives=tmp_path / "objective.json",
        objective_directory=Directory(),
        sink=SimpleNamespace(),
    )
    store = app.objectives.store
    assert store.propose(hold(1)).accepted is True
    assert reads == []

    result = store.propose(reach(2))

    assert result.reasons[0].code == "objective_already_satisfied"
    assert reads == [1]
    assert decision_reads == []
