"""RED-first optional report artifact (#138/R09): explicit --output only."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from pokegent.evaluation_campaign import summarize_attempts
from tools import campaign_report
from tools.campaign_report import build_report, main

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = json.loads(
    (ROOT / "docs/benchmarks/scenario-manifest.example.json").read_text(encoding="utf-8")
)
MARKER = "PRIVATE_SENTINEL_PATH_4c1e"


def written(path, payload=None):
    path.write_text(json.dumps(payload or dict(EXAMPLE, status="completed")),
                    encoding="utf-8")
    return str(path)


def artifact(source):
    report = build_report([source], cohort_id="cohort-1", enabled=True)
    return json.dumps(report, sort_keys=True, allow_nan=False) + "\n"


def test_flag_absent_preserves_stdout_bytes_and_creates_no_file(tmp_path, capsys):
    source = written(tmp_path / "done.json")
    before = sorted(path.name for path in tmp_path.iterdir())
    assert main([source, "--cohort", "cohort-1", "--enabled"]) == 0
    captured = capsys.readouterr()
    assert captured.out == artifact(source)
    assert captured.out.endswith("\n") and not captured.out.endswith("\n\n")
    assert captured.err == ""
    assert sorted(path.name for path in tmp_path.iterdir()) == before


def test_explicit_artifact_matches_stdout_json_and_is_private(tmp_path, capsys):
    source = written(tmp_path / "done.json")
    out = tmp_path / "report.json"
    assert main([source, "--cohort", "cohort-1", "--enabled",
                 "--output", str(out)]) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert out.read_text(encoding="utf-8") == artifact(source)
    assert json.loads(out.read_text(encoding="utf-8"))["enabled"] is True
    if os.name == "posix":
        assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_output_without_enabled_fails_before_any_io(tmp_path, capsys, monkeypatch):
    def poison(*args, **kwargs):
        raise AssertionError("build ran before the --enabled check")

    monkeypatch.setattr(campaign_report, "build_report", poison)
    missing = tmp_path / "missing.json"
    out = tmp_path / "report.json"
    assert main([str(missing), "--cohort", "cohort-1", "--output", str(out)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("campaign_report: ")
    assert str(missing) not in captured.err and str(out) not in captured.err
    assert not out.exists()


def test_default_off_without_output_stays_inert(tmp_path, capsys):
    missing = tmp_path / "missing.json"
    assert main([str(missing), "--cohort", "cohort-1"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == summarize_attempts((), enabled=False)
    assert captured.err == ""
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("payload", [b"{not json ", None], ids=["malformed", "missing"])
def test_bad_input_leaves_output_absent(tmp_path, capsys, payload):
    source = tmp_path / "input.json"
    if payload is not None:
        source.write_bytes(payload + MARKER.encode())
    out = tmp_path / "report.json"
    assert main([str(source), "--cohort", "cohort-1", "--enabled",
                 "--output", str(out)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("campaign_report: ")
    assert MARKER not in captured.err and str(source) not in captured.err
    assert not out.exists()


def test_existing_targets_are_refused_without_change(tmp_path, capsys):
    source = written(tmp_path / "done.json")

    def attempt(out):
        return main([source, "--cohort", "cohort-1", "--enabled",
                     "--output", str(out)])

    existing = tmp_path / "existing.json"
    existing.write_text("ORIGINAL", encoding="utf-8")
    assert attempt(existing) == 1
    assert existing.read_text(encoding="utf-8") == "ORIGINAL"
    directory = tmp_path / "directory"
    directory.mkdir()
    assert attempt(directory) == 1
    assert directory.is_dir() and list(directory.iterdir()) == []
    target = tmp_path / "target.json"
    target.write_text("TARGET", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    assert attempt(link) == 1
    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "TARGET"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("campaign_report: ") == 3
    assert str(existing) not in captured.err


def test_missing_output_parent_is_generic_and_not_created(tmp_path, capsys):
    source = written(tmp_path / "done.json")
    parent = tmp_path / MARKER
    out = parent / "report.json"
    assert main([source, "--cohort", "cohort-1", "--enabled",
                 "--output", str(out)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("campaign_report: ")
    assert MARKER not in captured.err and "report.json" not in captured.err
    assert not parent.exists()
