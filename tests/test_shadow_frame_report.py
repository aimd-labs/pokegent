"""Opt-in shadow frame-identity counts; synthetic events only, no provider or game input."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.shadow_report import ReportError, build_report

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "shadow_report.py"
LEGACY_KEYS = {
    "tool", "version", "event_type", "authority", "sources", "scanned", "records",
    "no_records", "strata", "agreement_semantics", "action_acceptance", "accuracy",
    "verified_progress", "eligibility", "note",
}
LEGACY_STRATUM_KEYS = {
    "run_id", "scene_id", "provider", "model_id", "records", "attempts_observed",
    "attempts_failed", "agreement", "measured_ms",
}


def shadow_event(**overrides):
    details = {
        "option_id": "sign",
        "model_id": "m1",
        "provider_id": "local",
        "confidence": 0.5,
        "measured_ms": 10.0,
        "accepted": False,
        "authority": "shadow",
        "agreement": False,
        "accuracy": "unknown",
        "agent_option_id": "walk",
    }
    details.update(overrides.pop("details", {}))
    base = {
        "schema_rev": 1,
        "id": "run-a:1",
        "run_id": "run-a",
        "seq": 1,
        "observed_at": "2026-01-01T00:00:00.000+00:00",
        "type": "decision.systemone.completed",
        "scene_id": "scene-1",
        "request_id": "dec-1",
        "provider": "local",
        "outcome": "observed",
        "source": "runner",
        "public_visibility": False,
    }
    base.update(overrides)
    base["details"] = details
    return base


def raw(path, records):
    path.write_text(
        "".join(json.dumps(record, allow_nan=True) + "\n" for record in records),
        encoding="utf-8",
    )


def run_cli(*args):
    return subprocess.run(
        [sys.executable, str(TOOL), *map(str, args)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )


def test_opt_in_counts_explicit_field_without_agreement_inference(tmp_path):
    path = tmp_path / "frames.jsonl"
    raw(path, [
        shadow_event(details={"agreement": True, "frame_matches_current": True}),
        shadow_event(details={"agreement": True, "frame_matches_current": False}),
        shadow_event(details={"agreement": True}),
        shadow_event(details={"agreement": False, "frame_matches_current": None}),
    ])

    row = build_report([path], frame_identity=True)["strata"][0]

    assert row["frame_identity"] == {"true": 1, "false": 1, "unknown": 2}
    assert row["agreement"] == {"true": 3, "false": 1, "unknown": 0}


def test_opt_out_keeps_legacy_shape_and_ignores_frame_field(tmp_path):
    path = tmp_path / "legacy.jsonl"
    raw(path, [
        shadow_event(details={"frame_matches_current": "invalid-but-ignored"}),
        shadow_event(details={"frame_matches_current": True}),
    ])

    default = build_report([path])
    explicit = build_report([path], frame_identity=False)

    assert default == explicit
    assert set(default) == LEGACY_KEYS
    assert set(default["strata"][0]) == LEGACY_STRATUM_KEYS
    assert default["strata"][0]["agreement"] == {"true": 0, "false": 2, "unknown": 0}


@pytest.mark.parametrize("value", ["true", 1, 0, [], {}])
def test_invalid_frame_field_fails_only_under_opt_in(tmp_path, value):
    event = shadow_event(details={"frame_matches_current": value, "error": "FRAME_SECRET"})
    path = tmp_path / "invalid.jsonl"
    raw(path, [event])

    assert build_report([path])["records"] == 1
    with pytest.raises(ReportError, match="field frame_matches_current") as excinfo:
        build_report([path], frame_identity=True)
    assert "FRAME_SECRET" not in str(excinfo.value)


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_frame_identity_keyword_requires_exact_bool(tmp_path, value):
    path = tmp_path / "one.jsonl"
    raw(path, [shadow_event(details={"frame_matches_current": True})])

    with pytest.raises(ReportError, match="frame_identity"):
        build_report([path], frame_identity=value)


def test_non_shadow_records_are_ignored(tmp_path):
    path = tmp_path / "mixed.jsonl"
    raw(path, [
        shadow_event(details={"frame_matches_current": True}),
        {"type": "decision.agent.completed", "run_id": "run-a", "seq": 2,
         "details": {"authority": "shadow", "accepted": False,
                     "frame_matches_current": "ignored"}},
        {"type": "scene.entered", "run_id": "run-a", "seq": 3, "details": {}},
        shadow_event(seq=4, details={"frame_matches_current": False}),
    ])

    report = build_report([path], frame_identity=True)

    assert report["records"] == 2
    assert report["scanned"] == 4
    assert report["strata"][0]["frame_identity"] == {"true": 1, "false": 1, "unknown": 0}


def test_multi_strata_order_and_bounds_are_retained(tmp_path):
    path = tmp_path / "strata.jsonl"
    raw(path, [
        shadow_event(run_id="run-b", scene_id="scene-2",
                     details={"frame_matches_current": None}),
        shadow_event(run_id="run-a", scene_id="scene-1",
                     details={"frame_matches_current": True}),
        shadow_event(run_id="run-a", scene_id="scene-1",
                     details={"frame_matches_current": False}),
    ])

    report = build_report([path], frame_identity=True)

    assert [row["run_id"] for row in report["strata"]] == ["run-a", "run-b"]
    assert report["strata"][0]["frame_identity"] == {"true": 1, "false": 1, "unknown": 0}
    assert report["strata"][1]["frame_identity"] == {"true": 0, "false": 0, "unknown": 1}
    with pytest.raises(ReportError, match="record limit"):
        build_report([path], frame_identity=True, max_records=2)
    with pytest.raises(ReportError, match="strata limit"):
        build_report([path], frame_identity=True, max_strata=1)


def test_cli_flag_opts_in_and_default_output_is_unchanged(tmp_path):
    path = tmp_path / "cli.jsonl"
    raw(path, [shadow_event(details={"frame_matches_current": True})])

    default = run_cli(path)
    opted = run_cli("--frame-identity", path)

    assert default.returncode == 0 and default.stderr == ""
    assert opted.returncode == 0 and opted.stderr == ""
    legacy = json.loads(default.stdout)
    report = json.loads(opted.stdout)
    assert set(legacy) == LEGACY_KEYS
    assert set(legacy["strata"][0]) == LEGACY_STRATUM_KEYS
    assert set(report) == LEGACY_KEYS
    assert set(report["strata"][0]) == LEGACY_STRATUM_KEYS | {"frame_identity"}
    assert report["strata"][0]["frame_identity"] == {"true": 1, "false": 0, "unknown": 0}
    assert report["strata"][0]["agreement"] == legacy["strata"][0]["agreement"]


def test_cli_invalid_frame_field_is_typed_and_redacted(tmp_path):
    path = tmp_path / "bad.jsonl"
    raw(path, [shadow_event(details={"frame_matches_current": "FRAME_SECRET"})])

    result = run_cli("--frame-identity", path)

    assert result.returncode != 0 and result.stdout == ""
    assert "field frame_matches_current" in result.stderr
    assert "FRAME_SECRET" not in result.stderr
    assert "Traceback" not in result.stderr
