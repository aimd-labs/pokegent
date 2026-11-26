"""Synthetic offline checks for the calibration JSONL report; no provider or game input."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from addons.system_one.telemetry import CalibrationLog
from tools.calibration_report import (
    MAX_LINE_BYTES,
    ReportError,
    build_report,
    main,
    scene_eligibility_report,
)

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "calibration_report.py"
STRATUM_KEYS = {
    "source", "run_id", "scene_id", "provider_id", "provider_version", "model_id",
    "prompt_schema_version", "option_order", "records", "outcomes", "attempts_total",
    "attempts_missing", "truncated", "measured_ms", "identity_missing", "identity_mixed",
    "distinct_stage_models",
}
REPORT_KEYS = {
    "tool", "version", "calibration_schema", "outcome_semantics", "sources", "records",
    "no_records", "strata", "sequence_diagnostics", "labels", "accuracy", "disagreement",
    "verified_progress", "eligibility", "note",
}


def record(**overrides):
    base = {
        "decision_id": "d1",
        "run_id": "run-a",
        "snapshot_seq": 1,
        "scene_id": "route-1",
        "goal_id": "goal",
        "option_set_hash": "hash",
        "option_count": 2,
        "state": {"PLANTED_STATE": "STATE_SECRET"},
        "instructions": "PROMPT_SECRET",
        "criteria": {"a": "CRITERIA_SECRET"},
        "plan": {"PLANTED_PLAN": "PLAN_SECRET"},
        "option_order": "frame",
        "sent_order": ["a", "b"],
        "stages": [{
            "name": "root", "request_id": "d1", "path": [], "criteria_keys": ["a", "b"],
            "measured_ms": 1.25, "choice": "CHOICE_SECRET", "probabilities": {"a": 1.0},
            "confidence": 0.9, "truncated": False, "model_id": "m1",
            "raw_response_ref": "", "attempts": 1,
        }],
        "choice": "CHOICE_SECRET",
        "probabilities": {"a": 1.0},
        "confidence": 0.9,
        "model_id": "m1",
        "provider_id": "local",
        "provider_version": "pv1",
        "prompt_schema_version": "ps1",
        "truncated": False,
        "state_reduced": False,
        "server_truncated": False,
        "usage_in": 1,
        "usage_out": 2,
        "attempts": 1,
        "gate": "disabled",
        "outcome": "ok",
        "error_code": None,
        "error": "ERROR_SECRET",
        "measured_ms": 10.0,
    }
    base.update(overrides)
    return base


def produced(path, records):
    log = CalibrationLog(path)
    try:
        for item in records:
            log.append(item)
    finally:
        log.close()


def raw(path, records):
    path.write_text("".join(json.dumps(r, allow_nan=True) + "\n" for r in records),
                    encoding="utf-8")


def run_cli(*paths):
    return subprocess.run([sys.executable, str(TOOL), *map(str, paths)],
                          capture_output=True, text=True, cwd=ROOT, check=False)


def report_of(result):
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return json.loads(result.stdout)


def test_empty_input_reports_zero_without_crash(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_bytes(b"")

    report = report_of(run_cli(path))

    assert report["records"] == 0
    assert report["no_records"] is True
    assert report["strata"] == []
    assert report["labels"] == "none"
    assert report["accuracy"] is None
    assert report["verified_progress"] is None
    assert report["eligibility"] == "not_evaluated"


def test_two_runs_and_strata_keep_their_own_percentiles(tmp_path):
    path = tmp_path / "synthetic.jsonl"
    produced(path, [
        record(measured_ms=10.0, outcome="ok", attempts=1),
        record(measured_ms=20.0, outcome="ok", attempts=None),
        record(measured_ms=30.0, outcome="timeout", attempts=2),
        record(measured_ms=40.0, outcome="invalid", attempts=3, truncated=True),
        record(run_id="run-b", scene_id="route-2", provider_id="remote",
               provider_version="pv2", model_id="m2", prompt_schema_version="ps2",
               option_order="shuffle", measured_ms=100.0),
        record(run_id="run-b", scene_id="route-2", provider_id="remote",
               provider_version="pv2", model_id="m2", prompt_schema_version="ps2",
               option_order="shuffle", measured_ms=200.0),
    ])

    report = report_of(run_cli(path))

    assert report["records"] == 6
    assert report["no_records"] is False
    assert len(report["strata"]) == 2
    first, second = sorted(report["strata"], key=lambda s: s["run_id"])
    assert first["run_id"] == "run-a"
    assert first["records"] == 4
    assert first["outcomes"] == {"ok": 2, "timeout": 1, "invalid": 1, "error": 0}
    assert first["attempts_total"] == 6
    assert first["attempts_missing"] == 1
    assert first["truncated"] == 1
    assert first["measured_ms"] == {"count": 4, "p50": 20.0, "p95": 40.0}
    assert second["run_id"] == "run-b"
    assert second["measured_ms"] == {"count": 2, "p50": 100.0, "p95": 200.0}


@pytest.mark.parametrize(("mutate", "field"), [
    (lambda r: r.update(schema=2), "schema"),
    (lambda r: r.pop("schema"), "schema"),
    (lambda r: r.update(record_seq=True), "record_seq"),
    (lambda r: r.update(measured_ms=True), "measured_ms"),
    (lambda r: r.update(measured_ms=float("nan")), "measured_ms"),
    (lambda r: r.update(measured_ms=float("inf")), "measured_ms"),
    (lambda r: r.update(measured_ms=-1.0), "measured_ms"),
    (lambda r: r.update(attempts=True), "attempts"),
    (lambda r: r.update(attempts=-1), "attempts"),
    (lambda r: r.update(outcome="maybe"), "outcome"),
    (lambda r: r.update(truncated=1), "truncated"),
    (lambda r: r.pop("scene_id"), "scene_id"),
    (lambda r: r.update(scene_id="x" * 257), "scene_id"),
])
def test_invalid_and_unsupported_records_fail_without_output(tmp_path, mutate, field):
    item = record(schema=1, record_seq=1)
    mutate(item)
    path = tmp_path / "bad.jsonl"
    raw(path, [item])

    result = run_cli(path)

    assert result.returncode != 0
    assert result.stdout == ""
    assert f"field {field}" in result.stderr


def test_malformed_json_names_line_and_does_not_echo_record(tmp_path):
    path = tmp_path / "torn.jsonl"
    path.write_text('{"raw": "TORN_SECRET"\n', encoding="utf-8")

    result = run_cli(path)

    assert result.returncode != 0
    assert result.stdout == ""
    assert f"{path}:1:" in result.stderr
    assert "malformed JSON" in result.stderr
    assert "TORN_SECRET" not in result.stderr


def test_line_record_and_strata_limits_fail(tmp_path):
    path = tmp_path / "synthetic.jsonl"
    produced(path, [record(measured_ms=float(i)) for i in range(3)])

    with pytest.raises(ReportError, match="record limit"):
        build_report([path], max_records=2)
    other = tmp_path / "strata.jsonl"
    produced(other, [record(scene_id=f"scene-{i}") for i in range(3)])
    with pytest.raises(ReportError, match="strata limit"):
        build_report([other], max_strata=2)


def test_oversized_line_fails_before_parsing(tmp_path):
    path = tmp_path / "long.jsonl"
    path.write_bytes(b"x" * (MAX_LINE_BYTES + 1) + b"\n")

    result = run_cli(path)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "line exceeds" in result.stderr


def test_duplicate_sequence_is_scoped_per_file_and_run(tmp_path):
    first = tmp_path / "one.jsonl"
    raw(first, [record(schema=1, record_seq=1), record(schema=1, record_seq=1),
                record(schema=1, record_seq=0)])
    second = tmp_path / "two.jsonl"
    raw(second, [record(schema=1, record_seq=1)])

    report = report_of(run_cli(first, second))

    assert report["records"] == 4
    assert len(report["strata"]) == 2
    assert report["sequence_diagnostics"] == [{
        "source": 0, "run_id": "run-a", "duplicate_count": 1, "non_monotonic_count": 1,
    }]


def test_null_and_mixed_stage_identities_are_counted_truthfully(tmp_path):
    path = tmp_path / "identity.jsonl"
    produced(path, [
        record(model_id=None, stages=[{"model_id": None}], measured_ms=1.0),
        record(model_id="leaf", measured_ms=2.0,
               stages=[{"model_id": "m1"}, {"model_id": "m2"}]),
        record(run_id="run-b", model_id="leaf-only", stages=[], measured_ms=3.0),
    ])

    report = report_of(run_cli(path))

    by_run = {s["run_id"]: s for s in report["strata"]}
    null_leaf = [s for s in report["strata"] if s["model_id"] is None][0]
    assert null_leaf["identity_missing"] == 1
    assert null_leaf["identity_mixed"] == 0
    assert by_run["run-a"]["identity_mixed"] == 1
    assert by_run["run-b"]["identity_missing"] == 1
    assert by_run["run-a"]["distinct_stage_models"] == 2


def test_top_level_identity_gaps_and_conflicts_are_disclosed(tmp_path):
    path = tmp_path / "identity-top.jsonl"
    produced(path, [
        record(model_id=None, stages=[{"model_id": "m1"}], measured_ms=1.0),
        record(run_id="run-conflict", model_id="m3", stages=[{"model_id": "m1"}],
               measured_ms=2.0),
        record(run_id="run-hier", model_id="m2", stages=[{"model_id": "m1"},
                                                         {"model_id": "m2"}],
               measured_ms=3.0),
        record(run_id="run-flat", model_id="m1", stages=[{"model_id": "m1"}],
               measured_ms=4.0),
    ])

    report = report_of(run_cli(path))

    by_run = {stratum["run_id"]: stratum for stratum in report["strata"]}
    assert (by_run["run-a"]["identity_missing"], by_run["run-a"]["identity_mixed"]) == (1, 0)
    assert (by_run["run-conflict"]["identity_missing"],
            by_run["run-conflict"]["identity_mixed"]) == (0, 1)
    assert (by_run["run-hier"]["identity_missing"],
            by_run["run-hier"]["identity_mixed"]) == (0, 1)
    assert (by_run["run-flat"]["identity_missing"],
            by_run["run-flat"]["identity_mixed"]) == (0, 0)


def test_huge_int_measured_ms_is_rejected_as_a_field_error(tmp_path):
    value = 10 ** 400
    path = tmp_path / "huge-int.jsonl"
    raw(path, [record(schema=1, record_seq=1, measured_ms=value)])

    result = run_cli(path)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "field measured_ms" in result.stderr
    assert "Traceback" not in result.stderr
    assert "OverflowError" not in result.stderr
    assert str(value) not in result.stderr


def test_output_has_allowlisted_fields_and_no_planted_secrets(tmp_path):
    path = tmp_path / "synthetic.jsonl"
    produced(path, [record(future_additive={"ignored": "ADDITIVE_SECRET"})])
    before = sorted(p.name for p in tmp_path.iterdir())
    original = path.read_bytes()

    result = run_cli(path)
    report = report_of(result)

    assert set(report) == REPORT_KEYS
    assert all(set(stratum) == STRATUM_KEYS for stratum in report["strata"])
    assert "ADDITIVE_SECRET" not in result.stdout
    for secret in ("STATE_SECRET", "PROMPT_SECRET", "CRITERIA_SECRET", "PLAN_SECRET",
                   "CHOICE_SECRET", "ERROR_SECRET"):
        assert secret not in result.stdout
    for forbidden in ('"state":', '"instructions":', '"criteria":', '"plan":', '"choice":',
                      '"option_set_hash":', '"error_code":'):
        assert forbidden not in result.stdout
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert path.read_bytes() == original


def test_missing_file_is_a_nonzero_error_without_stdout(tmp_path):
    result = run_cli(tmp_path / "absent.jsonl")

    assert result.returncode != 0
    assert result.stdout == ""
    assert "absent.jsonl" in result.stderr


def test_main_returns_zero_for_valid_file_and_nonzero_for_bad_file(tmp_path, capsys):
    good = tmp_path / "good.jsonl"
    produced(good, [record()])

    assert main([str(good)]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["records"] == 1
    assert out.err == ""

    bad = tmp_path / "bad.jsonl"
    bad.write_text("{\n", encoding="utf-8")
    assert main([str(bad)]) != 0
    out = capsys.readouterr()
    assert out.out == ""
    assert "malformed JSON" in out.err


def test_scene_eligibility_labels_eligible_not_eligible_unknown():
    data = {"route-1": (1, 1, 0, 0, [10.0]), "shaky": (2, 1, 1, 0, [10.0, 20.0]),
            "slow": (2, 2, 0, 0, [50.0, 2000.0]), "thin": (0, 0, 0, 0, []),
            "opaque": (2, 1, 0, 1, [10.0, 20.0])}
    scenes = {name: {"scene_id": name, "records": rec, "valid": v, "invalid": i,
                     "unknown": u, "samples": samp}
              for name, (rec, v, i, u, samp) in data.items()}
    report = scene_eligibility_report(scenes, (1, 0.8, 100.0))
    got = {s["scene_id"]: (s["eligible"], s["reason"], s["p50_ms"], s["p95_ms"])
           for s in report["scenes"]}
    assert report["policy"] == {"min_samples": 1, "min_valid_rate": 0.8, "max_p95_ms": 100.0}
    assert (report["accuracy"], report["no_progress"], report["authority_granted"]) \
        == (None, None, False)
    assert got["route-1"] == (True, "eligible", 10.0, 10.0)
    assert got["shaky"] == (False, "below_valid_rate", 10.0, 20.0)
    assert got["slow"] == (False, "over_latency_cap", 50.0, 2000.0)
    assert got["thin"] == ("unknown", "below_min_samples", None, None)
    assert got["opaque"] == ("unknown", "unknown_choices", 10.0, 20.0)


def test_scene_eligibility_rejects_inconsistent_scene_counts():
    base = {"scene_id": "s", "records": 2, "valid": 1, "invalid": 1, "unknown": 0,
            "samples": [10.0, 20.0]}
    for change in ({"valid": 0, "invalid": 0, "unknown": 0}, {"samples": []},
                   {"samples": [10.0]}, {"unknown": 1}):
        with pytest.raises(ValueError, match="do not match records"):
            scene_eligibility_report({"s": {**base, **change}}, (1, 0.5, 100.0))
