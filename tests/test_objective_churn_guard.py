"""Default-off objective churn detection over real tracker + session snapshots."""

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
from pokegent.goals.source import ObjectiveSession
from pokegent.goals.store import ObjectiveStore
from pokegent.goals.tracker import STUCK, ObjectiveTracker


def goal(revision, objective_id="churn"):
    return dict(
        objective_id=objective_id,
        revision=revision,
        kind="hold",
        set_by="operator",
        success_probes=[dict(kind="money_at_least", amount=99)],
        forbid=["sell"],
    )


def snapshot(frame, seq=1, *, valid=True):
    return StateSnapshot.create(
        rom_profile="synthetic",
        snapshot_seq=seq,
        emulator_frame=frame,
        health=Health(
            valid=valid,
            read_errors=()
            if valid
            else (ReadError(subsystem="test", code="invalid", message="synthetic"),),
        ),
        player=Player(
            name="Test",
            gender="male",
            tid=0,
            sid=0,
            map_group=0,
            map_num=0,
            map_layout_id=0,
            x=0,
            y=0,
            money=0,
            coins=0,
        )
        if valid
        else None,
        flags=FlagSet() if valid else None,
        vars=VarTable() if valid else None,
    )


def build(tmp_path, limit):
    store = ObjectiveStore(tmp_path / "objective.json")
    assert store.propose(goal(1)).accepted
    session = ObjectiveSession(store, churn_limit=limit)
    session.refresh()
    return session, store


def churn_events(events):
    return [
        event
        for event in events
        if event.type == STUCK and event.details.get("reason") == "objective_churn"
    ]


def switch(session, store, revision, snap):
    assert store.propose(goal(revision), expected_revision=revision - 1).accepted
    assert session.refresh().changed
    return session.observe(snap)


def test_opt_in_emits_one_typed_stuck_after_frozen_revision_churn(tmp_path):
    session, store = build(tmp_path, 2)
    assert session.tracker.churn_limit == 2
    fixed = snapshot(100, 1)
    assert not churn_events(session.observe(fixed))
    assert not churn_events(switch(session, store, 2, snapshot(100, 2)))
    events = switch(session, store, 3, snapshot(100, 3))
    stuck = churn_events(events)
    assert len(stuck) == 1
    details = stuck[0].details
    assert details["objective_id"] == "churn"
    assert details["revision"] == 3
    assert details["churn_changes"] == 2
    assert details["emulator_frame"] == 100
    assert isinstance(details["churn_changes"], int)
    assert isinstance(details["emulator_frame"], int)
    # The incident is reported once, and the active objective is preserved.
    for revision in (4, 5):
        assert not churn_events(switch(session, store, revision, snapshot(100, revision)))
    assert session.objective.revision == 5 and not session.completed


def test_default_off_and_same_goal_updates_keep_legacy_behavior(tmp_path):
    session, store = build(tmp_path, 0)
    assert session.tracker.churn_limit == 0
    assert not churn_events(session.observe(snapshot(100, 1)))
    for revision in (2, 3, 4):
        assert not churn_events(switch(session, store, revision, snapshot(100, revision)))
    # Repeating an update of the same goal is not a real revision change.
    repeated = [session.observe(snapshot(100, 9)) for _ in range(5)]
    assert all(not churn_events(events) for events in repeated)


def test_same_goal_updates_do_not_count_toward_churn(tmp_path):
    session, store = build(tmp_path, 2)
    session.observe(snapshot(100, 1))
    assert not churn_events(switch(session, store, 2, snapshot(100, 2)))
    for seq in range(3, 8):
        assert not churn_events(session.observe(snapshot(100, seq)))
    assert len(churn_events(switch(session, store, 3, snapshot(100, 8)))) == 1


def test_frame_advance_resets_and_a_new_incident_can_fire(tmp_path):
    session, store = build(tmp_path, 3)
    session.observe(snapshot(100, 1))
    assert not churn_events(switch(session, store, 2, snapshot(100, 2)))
    # Advance alone is progress: it clears the one frozen change.
    assert not churn_events(session.observe(snapshot(101, 3)))
    assert not churn_events(switch(session, store, 3, snapshot(101, 4)))
    assert not churn_events(switch(session, store, 4, snapshot(101, 5)))
    assert len(churn_events(switch(session, store, 5, snapshot(101, 6)))) == 1
    # A later advance re-arms a fresh incident.
    assert not churn_events(session.observe(snapshot(102, 7)))
    assert not churn_events(switch(session, store, 6, snapshot(102, 8)))
    assert not churn_events(switch(session, store, 7, snapshot(102, 9)))
    assert len(churn_events(switch(session, store, 8, snapshot(102, 10)))) == 1


@pytest.mark.parametrize("mode", ["unknown", "unusable"])
def test_unknown_or_unusable_frames_cannot_prove_a_freeze(tmp_path, mode):
    session, store = build(tmp_path, 2)
    session.observe(snapshot(100, 1))
    for revision, seq in ((2, 2), (3, 3), (4, 4)):
        during = (snapshot(None, seq) if mode == "unknown"
                  else snapshot(100, seq, valid=False))
        assert not churn_events(switch(session, store, revision, during))
    assert session.objective.revision == 4


def test_frozen_frame_after_unknown_gap_starts_a_fresh_incident(tmp_path):
    session, store = build(tmp_path, 2)
    session.observe(snapshot(100, 1))
    assert not churn_events(switch(session, store, 2, snapshot(100, 2, valid=False)))
    assert not churn_events(switch(session, store, 3, snapshot(100, 3, valid=False)))
    # The first usable snapshot after the gap is only a baseline.
    assert not churn_events(session.observe(snapshot(100, 4)))
    assert not churn_events(switch(session, store, 4, snapshot(100, 5)))
    assert len(churn_events(switch(session, store, 5, snapshot(100, 6)))) == 1


@pytest.mark.parametrize("value", [True, False, -1, 1, 1.5, "2", None])
def test_invalid_churn_limits_rejected(value):
    from pokegent.app import AppError, config_from_mapping

    with pytest.raises(ValueError, match="objective_churn_limit"):
        ObjectiveTracker(churn_limit=value)
    with pytest.raises(AppError, match="objective_churn_limit"):
        config_from_mapping({"run": {"objective_churn_limit": value}})


def test_app_config_and_session_binding(tmp_path):
    from pokegent.app import AppConfig, config_from_mapping, make_objective_session

    assert AppConfig().objective_churn_limit == 0
    cfg = config_from_mapping({"run": {"objective_churn_limit": 3}})
    assert cfg.objective_churn_limit == 3
    session = make_objective_session(tmp_path / "objective.json",
                                     churn_limit=cfg.objective_churn_limit)
    assert session.tracker.churn_limit == 3
    tracker = ObjectiveTracker(churn_limit=5)
    supplied = ObjectiveSession(
        ObjectiveStore(tmp_path / "supplied.json"), tracker=tracker, churn_limit=2
    )
    assert supplied.tracker is tracker and tracker.churn_limit == 5
    assert make_objective_session(supplied, churn_limit=2) is supplied
    assert tracker.churn_limit == 5


def test_churn_reuses_stuck_review_opt_in_without_action_authority(tmp_path):
    store = ObjectiveStore(tmp_path / "objective.json")

    def reviewed(revision):
        data = goal(revision)
        data["kind"] = "train"
        data["next_review_on"] = ["stuck"]
        return data

    assert store.propose(reviewed(1)).accepted
    session = ObjectiveSession(store, churn_limit=2)
    session.refresh()
    session.observe(snapshot(100, 1))
    for revision in (2, 3):
        assert store.propose(reviewed(revision), expected_revision=revision - 1).accepted
        session.refresh()
        events = session.observe(snapshot(100, revision))
    assert len(churn_events(events)) == 1
    reviews = session.take_reviews()
    assert len(reviews) == 1
    assert reviews[0].objective_id == "churn" and reviews[0].revision == 3
    assert reviews[0].details["reason"] == "objective_churn"
    assert reviews[0].details["churn_changes"] == 2
    assert reviews[0].details["emulator_frame"] == 100
    assert session.review_pending and session.objective.revision == 3


def test_idle_stuck_still_works_when_churn_is_enabled(tmp_path):
    store = ObjectiveStore(tmp_path / "objective.json")
    data = goal(1)
    data["kind"] = "train"
    data["success_probes"] = [dict(kind="party_level", min_level=99)]
    assert store.propose(data).accepted
    session = ObjectiveSession(store, stuck_after=2, churn_limit=2)
    session.refresh()
    fixed = snapshot(100, 1)
    assert not session.observe(fixed)
    assert not session.observe(fixed)
    events = session.observe(fixed)
    stuck = [event for event in events if event.type == STUCK]
    assert len(stuck) == 1 and "reason" not in stuck[0].details
    assert stuck[0].details["idle_updates"] == 2


def test_completion_semantics_are_unchanged_when_churn_is_enabled(tmp_path):
    store = ObjectiveStore(tmp_path / "objective.json")
    data = goal(1)
    data["kind"] = "train"
    data["success_probes"] = [dict(kind="money_at_least", amount=0)]
    assert store.propose(data).accepted
    session = ObjectiveSession(store, churn_limit=2)
    session.refresh()
    events = session.observe(snapshot(100, 1))
    milestones = [event for event in events if event.type == "progress.milestone_completed"]
    assert len(milestones) == 1 and session.completed
    assert not churn_events(session.observe(snapshot(100, 2)))
    assert session.completed


def test_build_runner_passes_app_config_churn_limit(tmp_path, monkeypatch):
    import pokegent.app as app_module

    class FreshReader:
        def __init__(self, *args, **kwargs):
            pass

        def read(self):
            raise AssertionError("no live frame in this wiring control")

    class DecisionSource:
        def __init__(self, *args, **kwargs):
            pass

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
        config=app_module.AppConfig(objective_churn_limit=4),
        sink=SimpleNamespace(),
    )
    assert app.objectives.tracker.churn_limit == 4
