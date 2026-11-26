"""RED-first local campaign manifest report (#138/R09): caller-named files only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pokegent.evaluation_campaign import CampaignAttemptRejected, summarize_attempts
from tools import campaign_report
from tools.campaign_report import ReportError, build_report, main

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = json.loads(
    (ROOT / "docs/benchmarks/scenario-manifest.example.json").read_text(encoding="utf-8")
)
SENTINEL = "SENTINEL_SECRET_VALUE"


def document(**overrides):
    return dict(EXAMPLE, **overrides)


def written(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_disabled_is_inert_and_never_consumes_paths():
    def poison():
        raise AssertionError("disabled report consumed paths")
        yield

    assert build_report(poison(), cohort_id=None) == summarize_attempts((), enabled=False)


def test_example_files_follow_the_started_denominator(tmp_path):
    completed = written(tmp_path / "completed.json", document(
        attempt_id="done", status="completed"))
    invalid = written(tmp_path / "invalid.json", document(
        attempt_id="infra", status="infrastructure_invalid"))
    planned = written(tmp_path / "planned.json", document(
        attempt_id="planned", status="planned"))
    report = build_report([completed, invalid, planned], cohort_id="cohort-1", enabled=True)
    assert report["counts"] == {
        "supplied": 3, "planned": 1, "started": 2, "completed": 1, "failed": 0,
        "timed_out": 0, "censored": 0, "infrastructure_invalid": 1, "assisted": 0,
        "reported_unassisted_completed": 1,
    }
    assert report["reported_unassisted_completion_rate"] == 0.5
    assert report["cohorts"][0]["infrastructure_invalid"] == 1
    assert report["cohorts"][0]["reported_unassisted_completion_rate"] == 0.5


def test_repeated_segment_of_one_attempt_is_refused(tmp_path):
    first = written(tmp_path / "first.json", document(segment_id="segment-1"))
    second = written(tmp_path / "second.json", document(segment_id="segment-2"))
    with pytest.raises(CampaignAttemptRejected, match="duplicate"):
        build_report([first, second], cohort_id="cohort-1", enabled=True)


@pytest.mark.parametrize("content,needle", [
    (b"{not json " + SENTINEL.encode(), "JSON"),
    (b"\xff\xfe" + SENTINEL.encode(), "UTF-8"),
    (json.dumps([SENTINEL]).encode(), "manifest"),
    (json.dumps(document(schema_version="pokegent-evaluation/2")).encode(), "schema_version"),
    (json.dumps({key: value for key, value in document().items()
                 if key != "campaign_id"}).encode(), "campaign_id"),
])
def test_rejected_inputs_never_leak_values_or_paths(tmp_path, content, needle):
    path = tmp_path / "input.json"
    path.write_bytes(content)
    with pytest.raises((ReportError, CampaignAttemptRejected)) as caught:
        build_report([str(path)], cohort_id="cohort-1", enabled=True)
    message = str(caught.value)
    assert needle in message
    assert SENTINEL not in message
    assert str(path) not in message


def test_manifest_and_aggregate_byte_bounds_reject_oversize(tmp_path, monkeypatch):
    path = tmp_path / "big.json"
    path.write_bytes(b"{" + b"x" * 64 + b"}")
    monkeypatch.setattr(campaign_report, "MAX_MANIFEST_BYTES", 32)
    with pytest.raises(ReportError, match="byte"):
        build_report([str(path)], cohort_id="cohort-1", enabled=True)
    monkeypatch.setattr(campaign_report, "MAX_MANIFEST_BYTES", 4096)
    one = written(tmp_path / "one.json", document(attempt_id="one"))
    two = written(tmp_path / "two.json", document(attempt_id="two"))
    monkeypatch.setattr(campaign_report, "MAX_AGGREGATE_BYTES", len(json.dumps(document())))
    with pytest.raises(ReportError, match="byte"):
        build_report([one, two], cohort_id="cohort-1", enabled=True)


def test_path_cap_stops_after_one_extra_and_rejects_non_strings(tmp_path, monkeypatch):
    consumed = 0

    def paths():
        nonlocal consumed
        for index in range(10):
            consumed += 1
            yield str(tmp_path / f"{index}.json")

    monkeypatch.setattr(campaign_report, "MAX_PATHS", 2)
    with pytest.raises(ReportError, match="paths"):
        build_report(paths(), cohort_id="cohort-1", enabled=True)
    assert consumed == 3
    with pytest.raises(ReportError, match="path"):
        build_report([object()], cohort_id="cohort-1", enabled=True)
    with pytest.raises(ReportError, match="read"):
        build_report(["\x00"], cohort_id="cohort-1", enabled=True)


def test_main_enabled_prints_one_sorted_json_report(tmp_path, capsys):
    path = written(tmp_path / "done.json", document(status="completed"))
    assert main([path, "--cohort", "cohort-1", "--enabled"]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert captured.err == ""
    assert captured.out.strip() == json.dumps(report, sort_keys=True)
    assert report["enabled"] is True and report["counts"]["started"] == 1


def test_main_default_off_ignores_missing_paths(capsys):
    assert main(["/definitely/missing.json", "--cohort", "cohort-1"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == summarize_attempts((), enabled=False)
    assert captured.err == ""


def test_main_invalid_input_is_nonzero_with_no_stdout(tmp_path, capsys):
    missing = str(tmp_path / "missing.json")
    assert main([missing, "--cohort", "cohort-1", "--enabled"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("campaign_report: ")
    broken = tmp_path / "broken.json"
    broken.write_bytes(b"{" + SENTINEL.encode())
    assert main([str(broken), "--cohort", "cohort-1", "--enabled"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert SENTINEL not in captured.err
    assert str(broken) not in captured.err
