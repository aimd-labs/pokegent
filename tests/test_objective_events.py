"""Explicit offline counts of emitted objective progress events (no inference)."""

import json

import pytest

from pokegent.events import MemoryEventLog
from pokegent.games.gen3.firered.reader.schemas import (
    FlagSet,
    Health,
    Player,
    StateSnapshot,
    VarTable,
)
from pokegent.goals.source import ObjectiveSession
from pokegent.goals.store import ObjectiveStore
from pokegent.goals.tracker import event_sink_adapter
from tools.objective_events import ReportError, build_report, main


def goal(revision, objective_id="route", amount=99, kind="train"):
    return dict(objective_id=objective_id, revision=revision, kind=kind, set_by="agent",
                success_probes=[dict(kind="money_at_least", amount=amount)], forbid=["sell"])


def snapshot(money=0, frame=1):
    return StateSnapshot.create(
        rom_profile="synthetic", snapshot_seq=1, emulator_frame=frame,
        health=Health(valid=True),
        player=Player(name="Test", gender="male", tid=0, sid=0, map_group=0, map_num=0,
                      map_layout_id=0, x=0, y=0, money=money, coins=0),
        flags=FlagSet(), vars=VarTable(),
    )


def write_jsonl(path, records):
    path.write_text("".join(json.dumps(row) + "\n" for row in records))
    return str(path)


def record(event_type, details, run_id="run-a"):
    return {"run_id": run_id, "type": event_type, "details": details}


def test_real_tracker_events_project_through_memory_log(tmp_path):
    log = MemoryEventLog("run-a")
    store = ObjectiveStore(tmp_path / "objective.json")
    assert store.propose(goal(1)).accepted
    session = ObjectiveSession(store, sink=event_sink_adapter(log), stuck_after=2)
    session.refresh()
    for _ in range(3):
        session.observe(snapshot())
    assert store.propose(goal(2, amount=0), expected_revision=1).accepted
    session.refresh()
    session.observe(snapshot())
    session.tracker.set_objective(None)
    source = tmp_path / "events.jsonl"
    write_jsonl(source, [event.model_dump(mode="json") for event in log.events])

    report = build_report([str(source)])
    assert report["records"] == len(log.events)
    rows = {(row["run_id"], row["objective_id"], row["revision"]): row for row in report["strata"]}
    first = rows[("run-a", "route", 1)]
    assert first["goal_changed_events"] == 1
    assert first["stuck_events"] == 1
    assert first["milestone_completed_events"] == 0
    assert first["stuck_reason"] == {"known": 0, "unknown": 1}
    second = rows[("run-a", "route", 2)]
    assert second["goal_changed_events"] == 1
    assert second["milestone_completed_events"] == 1
    assert second["completed"] == {"true": 1, "false": 0, "unknown": 0}
    cleared = rows[("run-a", None, 0)]
    assert cleared["goal_changed_events"] == 1
    assert report["source_event_counts_only"] is True
    assert report["count_semantics"] == "emitted_events_not_episodes_or_denominator"
    assert report["run_completion"] == "not_inferred"
    assert report["campaign_completion"] == "not_inferred"
    assert report["eligibility"] == "not_evaluated"


def test_equal_names_different_runs_and_revisions_are_separate_strata(tmp_path):
    path = tmp_path / "events.jsonl"
    source = write_jsonl(path, [
        record("runtime.started", {"secret": "TOPSECRET"}, run_id="run-a"),
        record("progress.goal_changed", {"objective_id": "g", "revision": 1}),
        record("progress.goal_changed", {"objective_id": "g", "revision": 2}),
        record("progress.goal_changed", {"objective_id": "g", "revision": 1}, run_id="run-b"),
        record("progress.goal_changed", {"objective_id": None, "revision": 0}),
        record("progress.milestone_completed",
               {"objective_id": "g", "revision": 2, "completed": False}),
        record("progress.milestone_completed",
               {"objective_id": "g", "revision": 1, "completed": None}),
        record("progress.stuck", {"objective_id": "g", "revision": 1, "reason": "anything"}),
        record("progress.stuck", {"objective_id": "g", "revision": 1}),
    ])

    report = build_report([source])
    rows = {(row["run_id"], row["objective_id"], row["revision"]): row for row in report["strata"]}
    assert len(rows) == 4
    assert rows[("run-a", "g", 2)]["milestone_completed_events"] == 1
    assert rows[("run-a", "g", 2)]["completed"] == {"true": 0, "false": 1, "unknown": 0}
    assert rows[("run-a", "g", 1)]["completed"] == {"true": 0, "false": 0, "unknown": 1}
    assert rows[("run-a", "g", 1)]["stuck_reason"] == {"known": 1, "unknown": 1}
    assert rows[("run-a", None, 0)]["goal_changed_events"] == 1
    assert "TOPSECRET" not in json.dumps(report)


@pytest.mark.parametrize("event_type,details", [
    ("progress.stuck", {"revision": 1}),
    ("progress.milestone_completed", {"revision": 1, "completed": True}),
    ("progress.goal_changed", {"objective_id": None, "revision": 3}),
    ("progress.goal_changed", {"objective_id": "g", "revision": True}),
    ("progress.goal_changed", {"objective_id": "g", "revision": "1"}),
    ("progress.goal_changed", {"objective_id": "g", "revision": -1}),
    ("progress.milestone_completed", {"objective_id": "g", "revision": 1, "completed": "true"}),
    ("progress.stuck", {"objective_id": "g", "revision": 1, "reason": 7}),
])
def test_missing_or_invalid_identity_and_completed_are_rejected(tmp_path, event_type, details):
    source = write_jsonl(tmp_path / "events.jsonl", [record(event_type, details)])
    with pytest.raises(ReportError):
        build_report([source])


def test_secret_marker_is_never_echoed_in_errors(tmp_path):
    source = write_jsonl(tmp_path / "events.jsonl", [
        record("progress.goal_changed", {"objective_id": "TOPSECRET" * 100, "revision": 1}),
    ])
    with pytest.raises(ReportError) as caught:
        build_report([source])
    assert "TOPSECRET" not in str(caught.value)


def test_empty_blank_only_input_has_no_records(tmp_path):
    source = tmp_path / "events.jsonl"
    source.write_text("\n \n")
    report = build_report([str(source)])
    assert report["scanned"] == 0
    assert report["records"] == 0
    assert report["no_records"] is True
    assert report["strata"] == []


@pytest.mark.parametrize("kwargs", [
    {"max_records": 1},
    {"max_strata": 1},
    {"max_line_bytes": 20},
])
def test_bounds_are_enforced(tmp_path, kwargs):
    source = write_jsonl(tmp_path / "events.jsonl", [
        record("progress.goal_changed", {"objective_id": "a", "revision": 1}),
        record("progress.goal_changed", {"objective_id": "b", "revision": 1}),
    ])
    with pytest.raises(ReportError):
        build_report([source], **kwargs)


def test_cli_one_path_explicit_report_and_error_without_stdout(tmp_path, capsys):
    source = write_jsonl(tmp_path / "events.jsonl", [
        record("progress.stuck", {"objective_id": "g", "revision": 1}),
    ])
    assert main([source]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["records"] == 1

    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"type":"progress.stuck","details":{"objective_id":"TOPSECRET"}}\n')
    assert main([str(bad)]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "TOPSECRET" not in out.err
