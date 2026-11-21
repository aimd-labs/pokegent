import json
from pathlib import Path

import pytest

from pokegent.ledger import open_ledger
from pokegent.ledger_ingest import IngestError, ingest_run


def envelope(seq=1, *, run="upstream", kind="runtime.started", details=None):
    return {
        "schema_rev": 1,
        "id": f"{run}:{seq}",
        "run_id": run,
        "seq": seq,
        "observed_at": "2026-11-21T12:00:00+00:00",
        "type": kind,
        "source": "runner",
        "details": details or {},
    }


def line(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


@pytest.fixture
def ledger(tmp_path):
    db = open_ledger(tmp_path / "ledger.sqlite")
    with db:
        db.execute("INSERT INTO saves(id,source_ref) VALUES('save','caller')")
    yield db
    db.close()


def ingest(db, paths, **kw):
    with db:
        return ingest_run(
            db, save_id="save", segment_id="segment", run_id="upstream", event_paths=paths, **kw
        )


def test_partial_append_retries_and_rotated_dedup(ledger, tmp_path):
    active = tmp_path / "events.jsonl"
    rotated = tmp_path / "events-000001.jsonl"
    active.write_bytes(line(envelope())[:-1])
    assert ingest(ledger, [active]) == 0
    assert ledger.execute("SELECT byte_offset FROM ingest_sources").fetchone()[0] == 0
    active.write_bytes(active.read_bytes() + b"\n" + line(envelope(2)))
    assert ingest(ledger, [active]) == 2
    rotated.write_bytes(line(envelope()))
    active.write_bytes(b"")
    assert ingest(ledger, [rotated, active]) == 1  # duplicate ID, same canonical content
    assert ledger.execute("SELECT count(*) FROM ingested_events").fetchone()[0] == 2


def test_conflicting_duplicate_and_invalid_complete_line_roll_back(ledger, tmp_path):
    source = tmp_path / "events.jsonl"
    source.write_bytes(line(envelope()))
    assert ingest(ledger, [source]) == 1
    other = tmp_path / "events-000001.jsonl"
    other.write_bytes(line(envelope(details={"changed": True})))
    with pytest.raises(IngestError, match="content conflict"):
        ingest(ledger, [other])
    assert ledger.execute("SELECT count(*) FROM ingest_sources").fetchone()[0] == 1

    malformed = tmp_path / "bad-events.jsonl"
    malformed.write_bytes(b"not-json\n")
    with pytest.raises(IngestError, match="malformed complete"):
        ingest(ledger, [malformed])
    assert ledger.execute("SELECT byte_offset FROM ingest_sources").fetchone()[0] == len(
        line(envelope())
    )


def test_duplicate_only_rotation_boundary_rewrite_is_detected(ledger, tmp_path):
    original = envelope(details={"padding": "a" * 5000})
    active = tmp_path / "events.jsonl"
    rotated = tmp_path / "events-000001.jsonl"
    active.write_bytes(line(original))
    assert ingest(ledger, [active]) == 1
    rotated.write_bytes(line(original))
    assert ingest(ledger, [rotated]) == 1
    changed = envelope(details={"padding": "a" * 4999 + "b"})
    rotated.write_bytes(line(changed))
    with pytest.raises(IngestError, match="boundary was rewritten"):
        ingest(ledger, [rotated])


def test_boundary_rewrite_and_truncation_are_detected(ledger, tmp_path):
    source = tmp_path / "events.jsonl"
    original = line(envelope()) + line(envelope(2))
    source.write_bytes(original)
    assert ingest(ledger, [source], max_lines=1) == 1
    with source.open("r+b") as fh:
        fh.seek(0)
        fh.write(line(envelope(details={"rewritten": True})))
    with pytest.raises(IngestError, match="prefix was rewritten|boundary was rewritten"):
        ingest(ledger, [source], max_lines=1)

    source.write_bytes(b"")
    assert ingest(ledger, [source]) == 0
    assert ledger.execute("SELECT count(*) FROM ingest_sources").fetchone()[0] == 2


def test_summary_exit_reconciliation_is_idempotent_and_missing_is_unknown(ledger, tmp_path):
    source = tmp_path / "events.jsonl"
    source.write_bytes(line(envelope(kind="battle.won")))
    summary = tmp_path / "summary.json"
    exit_record = tmp_path / "exit.json"
    summary.write_text(
        json.dumps(
            {
                "run_id": "upstream",
                "verdict": "clear",
                "stop_reason": "completed",
                "wall_seconds": 9.5,
                "state": "done",
                "objective": {
                    "active": {
                        "objective_id": "summary_goal",
                        "revision": 3,
                        "kind": "reach",
                        "set_by": "operator",
                    }
                },
            }
        )
    )
    exit_record.write_text(
        json.dumps(
            {
                "run_id": "upstream",
                "verdict": "clear",
                "reason": "completed",
                "state_path": "/inert/ref",
            }
        )
    )
    assert ingest(ledger, [source], summary_path=summary, exit_path=exit_record) == 1
    assert ingest(ledger, [source], summary_path=summary, exit_path=exit_record) == 0
    assert ledger.execute(
        "SELECT verdict,exit_reason,wall_seconds,ended_at FROM runs"
    ).fetchone() == ("clear", "completed", 9.5, None)
    assert ledger.execute("SELECT opponent,turns,result FROM battles").fetchone() == (
        None,
        None,
        "won",
    )
    assert ledger.execute("SELECT count(*) FROM incidents").fetchone()[0] == 0
    assert ledger.execute("SELECT objective_id,accepted FROM objectives").fetchone() == (
        "summary_goal",
        1,
    )
    assert not Path("/inert/ref").exists()


def test_wrong_run_identity_and_untrusted_event_fields_fail(ledger, tmp_path):
    source = tmp_path / "events.jsonl"
    source.write_bytes(line(envelope(run="other")))
    with pytest.raises(IngestError, match="run_id"):
        ingest(ledger, [source])
    source.write_bytes(line({**envelope(), "observed_at": "2026-11-21T12:00:00"}))
    with pytest.raises(IngestError, match="timezone"):
        ingest(ledger, [source])
    source.write_bytes(line({**envelope(), "observed_at": None}))
    with pytest.raises(IngestError, match="timestamp string"):
        ingest(ledger, [source])
    source.write_bytes(line({**envelope(), "producer_extension": True}))
    with pytest.raises(IngestError, match="producer_extension.*schema compatibility"):
        ingest(ledger, [source])


def test_event_budget_leaves_remainder_for_next_call(ledger, tmp_path):
    source = tmp_path / "events.jsonl"
    source.write_bytes(b"".join(line(envelope(i)) for i in range(1, 4)))
    assert ingest(ledger, [source], max_lines=2) == 2
    assert ingest(ledger, [source], max_lines=2) == 1
    assert ledger.execute("SELECT count(*) FROM ingested_events").fetchone()[0] == 3


def test_objective_projection_and_utc_start(ledger, tmp_path):
    source = tmp_path / "events.jsonl"
    goal = envelope(
        1,
        kind="progress.goal_changed",
        details={
            "objective_id": "reach_lobby",
            "revision": 2,
            "kind": "reach",
            "set_by": "agent",
        },
    )
    started = envelope(2, kind="runtime.started")
    started["observed_at"] = "2026-11-21T14:00:00+02:00"
    done = envelope(
        3,
        kind="progress.milestone_completed",
        details={
            "objective_id": "reach_lobby",
            "revision": 2,
            "kind": "reach",
            "satisfied": 1,
            "probes": 1,
            "completed": True,
        },
    )
    source.write_bytes(line(goal) + line(started) + line(done))
    assert ingest(ledger, [source]) == 3
    assert (
        ledger.execute("SELECT started_at FROM runs").fetchone()[0] == "2026-11-21T12:00:00+00:00"
    )
    assert ledger.execute(
        "SELECT revision,objective_id,accepted,done FROM objectives"
    ).fetchone() == (2, "reach_lobby", 1, 1)


def test_summary_first_objective_reconciles_with_later_event(ledger, tmp_path):
    source = tmp_path / "events.jsonl"
    started = envelope(1, kind="runtime.started")
    goal = envelope(
        2,
        kind="progress.goal_changed",
        details={
            "objective_id": "summary_goal",
            "revision": 3,
            "kind": "reach",
            "set_by": "operator",
        },
    )
    source.write_bytes(line(started) + line(goal))
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "run_id": "upstream",
                "objective": {
                    "active": {
                        "objective_id": "summary_goal",
                        "revision": 3,
                        "kind": "reach",
                        "set_by": "operator",
                    }
                },
            }
        )
    )
    assert ingest(ledger, [source], summary_path=summary, max_lines=1) == 1
    before = ledger.execute("SELECT accepted_at,source_event_id FROM objectives").fetchone()
    assert before == (None, None)
    assert ingest(ledger, [source], summary_path=summary, max_lines=1) == 1
    after = ledger.execute(
        "SELECT accepted_at,source_event_id,source_ref FROM objectives"
    ).fetchone()
    assert after == ("2026-11-21T12:00:00+00:00", "upstream:2", "events.jsonl")


def test_summary_unknown_setter_is_enriched_by_event(ledger, tmp_path):
    source = tmp_path / "events.jsonl"
    source.write_bytes(
        line(envelope(1, kind="runtime.started"))
        + line(
            envelope(
                2,
                kind="progress.goal_changed",
                details={
                    "objective_id": "summary_goal",
                    "revision": 3,
                    "kind": "reach",
                    "set_by": "agent",
                },
            )
        )
    )
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "run_id": "upstream",
                "objective": {
                    "active": {
                        "objective_id": "summary_goal",
                        "revision": 3,
                        "kind": "reach",
                        "set_by": None,
                    }
                },
            }
        )
    )
    assert ingest(ledger, [source], summary_path=summary, max_lines=1) == 1
    assert ingest(ledger, [source], summary_path=summary, max_lines=1) == 1
    assert ledger.execute("SELECT set_by,accepted_at FROM objectives").fetchone() == (
        "agent",
        "2026-11-21T12:00:00+00:00",
    )


def test_known_objective_setter_conflict_rolls_back_event_cursor(ledger, tmp_path):
    source = tmp_path / "events.jsonl"
    source.write_bytes(
        line(envelope(1, kind="runtime.started"))
        + line(
            envelope(
                2,
                kind="progress.goal_changed",
                details={
                    "objective_id": "summary_goal",
                    "revision": 3,
                    "kind": "reach",
                    "set_by": "operator",
                },
            )
        )
    )
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "run_id": "upstream",
                "objective": {
                    "active": {
                        "objective_id": "summary_goal",
                        "revision": 3,
                        "kind": "reach",
                        "set_by": "agent",
                    }
                },
            }
        )
    )
    assert ingest(ledger, [source], summary_path=summary, max_lines=1) == 1
    cursor = ledger.execute("SELECT byte_offset FROM ingest_sources").fetchone()[0]
    with pytest.raises(IngestError, match="objective source conflicts"):
        ingest(ledger, [source], summary_path=summary, max_lines=1)
    assert ledger.execute("SELECT byte_offset FROM ingest_sources").fetchone()[0] == cursor


def test_stuck_incident_requires_busy_evidence_and_end_mappings(ledger, tmp_path):
    source = tmp_path / "events.jsonl"
    idle = envelope(1, kind="progress.stuck", details={"objective_id": "goal", "revision": 1})
    idle["outcome"] = "no_progress"
    busy = envelope(2, kind="progress.stuck", details={"busy_streak": 20})
    busy["outcome"] = "busy"
    escalation = envelope(3, kind="escalation.requested", details={"kind": "no_progress"})
    stopped = envelope(4, kind="runtime.stopped", details={"stop_reason": "max_steps"})
    stopped["outcome"] = "max_steps"
    source.write_bytes(b"".join(line(e) for e in (idle, busy, escalation, stopped)))
    assert ingest(ledger, [source]) == 4
    assert ledger.execute("SELECT kind FROM incidents ORDER BY kind").fetchall() == [
        ("busy_stuck",),
        ("escalation",),
    ]
    assert ledger.execute("SELECT ended_at,exit_reason FROM runs").fetchone() == (
        "2026-11-21T12:00:00+00:00",
        "max_steps",
    )


def test_nonfinite_summary_rolls_back_all_ingest_writes(ledger, tmp_path):
    source = tmp_path / "events.jsonl"
    source.write_bytes(line(envelope()))
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"run_id": "upstream", "wall_seconds": float("nan")}))
    with pytest.raises(IngestError, match="wall_seconds"):
        ingest(ledger, [source], summary_path=summary)
    assert ledger.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    assert ledger.execute("SELECT count(*) FROM ingest_sources").fetchone()[0] == 0
    assert ledger.execute("SELECT count(*) FROM ingested_events").fetchone()[0] == 0


def test_outer_transaction_rolls_back_event_fact_and_cursor(ledger, tmp_path):
    source = tmp_path / "events.jsonl"
    source.write_bytes(line(envelope(kind="battle.won")))
    with pytest.raises(RuntimeError):
        with ledger:
            assert (
                ingest_run(
                    ledger,
                    save_id="save",
                    segment_id="rollback",
                    run_id="upstream",
                    event_paths=[source],
                )
                == 1
            )
            raise RuntimeError("simulate caller failure")
    assert ledger.execute("SELECT count(*) FROM runs WHERE id='rollback'").fetchone()[0] == 0
    assert ledger.execute("SELECT count(*) FROM battles").fetchone()[0] == 0
    assert ledger.execute("SELECT count(*) FROM ingested_events").fetchone()[0] == 0
    assert ledger.execute("SELECT count(*) FROM ingest_sources").fetchone()[0] == 0


def test_missing_summary_values_preserve_known_runtime_facts(ledger, tmp_path):
    from pokegent.ledger_ingest import ingest_run

    source = tmp_path / "events.jsonl"
    source.write_bytes(line(envelope(kind="runtime.stopped")))
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps(
            {"run_id": "upstream", "verdict": "clear", "stop_reason": "done", "wall_seconds": 12.5}
        )
    )
    with ledger:
        ingest_run(
            ledger,
            save_id="save",
            segment_id="segment",
            run_id="upstream",
            event_paths=[source],
            summary_path=summary,
        )
    summary.write_text(
        json.dumps(
            {"run_id": "upstream", "verdict": None, "stop_reason": None, "wall_seconds": None}
        )
    )
    with ledger:
        ingest_run(
            ledger,
            save_id="save",
            segment_id="segment",
            run_id="upstream",
            event_paths=[source],
            summary_path=summary,
        )
    assert ledger.execute("SELECT verdict,exit_reason,wall_seconds FROM runs").fetchone() == (
        "clear",
        "done",
        12.5,
    )


def test_event_ingest_allowlist_tracks_envelope_fields():
    import ast
    import inspect

    from pokegent.events import EventEnvelope
    from pokegent.ledger_ingest import _event

    tree = ast.parse(inspect.getsource(_event))
    allowed = next(n for n in ast.walk(tree) if isinstance(n, ast.Set))
    assert {ast.literal_eval(x) for x in allowed.elts} == set(EventEnvelope.model_fields)
