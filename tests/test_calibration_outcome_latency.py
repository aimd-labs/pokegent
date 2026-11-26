"""Opt-in per-outcome latency in the offline calibration report; synthetic only."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.calibration_report import ReportError, build_report

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "calibration_report.py"


def record(**overrides):
    base = {"schema": 1, "record_seq": 1, "measured_ms": 10.0, "truncated": False,
            "outcome": "ok", "stages": [], "run_id": "run-a", "scene_id": "scene-1",
            "provider_id": "local", "provider_version": "pv1", "model_id": "m1",
            "prompt_schema_version": "ps1", "option_order": "frame", "attempts": 1}
    base.update(overrides)
    return base


def raw(path, records):
    path.write_text("".join(json.dumps(r, allow_nan=True) + "\n" for r in records),
                    encoding="utf-8")
    return path


def run_cli(*args):
    return subprocess.run([sys.executable, str(TOOL), *map(str, args)],
                          capture_output=True, text=True, cwd=ROOT, check=False)


class PoisonPaths(list):
    def __iter__(self):
        raise AssertionError("input opened before flag validation")


def test_multi_outcome_nearest_rank_empty_category_and_totals(tmp_path):
    path = raw(tmp_path / "multi.jsonl", [
        record(measured_ms=10.0, outcome="ok", record_seq=1),
        record(measured_ms=20.0, outcome="ok", record_seq=2),
        record(measured_ms=30.0, outcome="ok", record_seq=3),
        record(measured_ms=40.0, outcome="ok", record_seq=4),
        record(measured_ms=50.0, outcome="ok", record_seq=5),
        record(measured_ms=100.0, outcome="timeout", record_seq=6),
        record(measured_ms=200.0, outcome="timeout", record_seq=7),
        record(measured_ms=300.0, outcome="invalid", record_seq=8),
    ])

    stratum = build_report([path], outcome_latency=True)["strata"][0]
    latency = stratum["outcome_measured_ms"]

    assert latency["ok"] == {"count": 5, "p50": 30.0, "p95": 50.0}
    assert latency["timeout"] == {"count": 2, "p50": 100.0, "p95": 200.0}
    assert latency["invalid"] == {"count": 1, "p50": 300.0, "p95": 300.0}
    assert latency["error"] == {"count": 0, "p50": None, "p95": None}
    assert set(latency) == {"ok", "timeout", "invalid", "error"}
    assert {name: item["count"] for name, item in latency.items()} == stratum["outcomes"]
    assert sum(item["count"] for item in latency.values()) == stratum["records"]
    assert stratum["measured_ms"]["count"] == stratum["records"]


def test_default_output_is_unchanged(tmp_path):
    path = raw(tmp_path / "legacy.jsonl", [record()])

    default = build_report([path])

    assert default == build_report([path], outcome_latency=False)
    assert "outcome_measured_ms" not in default["strata"][0]


def test_outcome_latency_is_separated_per_stratum(tmp_path):
    path = raw(tmp_path / "strata.jsonl", [
        record(measured_ms=10.0, outcome="ok"),
        record(run_id="run-b", scene_id="scene-2", measured_ms=500.0, outcome="ok"),
        record(run_id="run-b", scene_id="scene-2", measured_ms=900.0, outcome="error"),
    ])

    by_run = {s["run_id"]: s["outcome_measured_ms"]
              for s in build_report([path], outcome_latency=True)["strata"]}

    assert by_run["run-a"]["ok"] == {"count": 1, "p50": 10.0, "p95": 10.0}
    assert by_run["run-b"]["ok"] == {"count": 1, "p50": 500.0, "p95": 500.0}
    assert by_run["run-b"]["error"] == {"count": 1, "p50": 900.0, "p95": 900.0}
    assert by_run["run-a"]["error"]["count"] == 0


def test_empty_input_reports_no_strata(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_bytes(b"")

    report = build_report([path], outcome_latency=True)

    assert report["strata"] == []
    assert report["no_records"] is True


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0])
def test_malformed_samples_are_still_refused(tmp_path, value):
    path = raw(tmp_path / "bad.jsonl", [record(measured_ms=value)])

    with pytest.raises(ReportError, match="field measured_ms"):
        build_report([path], outcome_latency=True)


def test_record_cap_is_unchanged_with_the_flag(tmp_path):
    path = raw(tmp_path / "cap.jsonl",
               [record(record_seq=i) for i in range(3)])

    with pytest.raises(ReportError, match="record limit"):
        build_report([path], max_records=2, outcome_latency=True)


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_non_bool_flag_is_refused_before_opening_input(value):
    with pytest.raises(ReportError, match="outcome_latency"):
        build_report(PoisonPaths(["/poison.jsonl"]), outcome_latency=value)


def test_main_cli_switch_is_opt_in(tmp_path):
    path = raw(tmp_path / "cli.jsonl", [record(measured_ms=42.0, outcome="ok")])

    default = run_cli(path)
    assert default.returncode == 0
    assert "outcome_measured_ms" not in json.loads(default.stdout)["strata"][0]

    enabled = run_cli("--outcome-latency", path)
    assert enabled.returncode == 0
    latency = json.loads(enabled.stdout)["strata"][0]["outcome_measured_ms"]
    assert latency["ok"] == {"count": 1, "p50": 42.0, "p95": 42.0}
