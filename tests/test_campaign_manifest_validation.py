"""RED-first optional structural manifest validation (#138/R09).

Validated manifests are checked against the bundled scenario-manifest.schema.json
before projection; invalid input rejects the whole report with generic, index-only
errors. Disabled/validate-off paths never import the validator or read the schema.
Non-finite numbers (raw NaN/Infinity tokens, 1e999 overflow) are refused only on
the validation path; legacy parsing is untouched.
"""

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
BAD_BUDGET = {"wall_seconds": "x", "action_decisions": 1, "provider_requests": 0}


def document(**overrides):
    return dict(EXAMPLE, **overrides)


def saved(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload),
                    encoding="utf-8")
    return str(path)


def refused(manifest):
    with pytest.raises(ReportError):
        build_report([manifest], cohort_id="c", enabled=True, validate_manifests=True)


def test_validate_off_keeps_legacy_partial_projection(tmp_path):
    path = saved(tmp_path, "legacy.json", document(status="completed"))
    off = build_report([path], cohort_id="c", enabled=True, validate_manifests=False)
    assert off == build_report([path], cohort_id="c", enabled=True)
    assert off["counts"]["completed"] == 1


def test_valid_manifest_matches_legacy_output(tmp_path):
    path = saved(tmp_path, "valid.json", document())
    flagged = build_report([path], cohort_id="c", enabled=True, validate_manifests=True)
    assert flagged == build_report([path], cohort_id="c", enabled=True)


@pytest.mark.parametrize(("override", "case"), [
    ({"layer": "L2b"}, "native-hash"),
    ({"preparation": "reviewed_checkpoint"}, "checkpoint-hash"),
    ({"status": "completed"}, "completion-receipt"),
])
def test_conditional_hashes_are_required(tmp_path, override, case):
    refused(saved(tmp_path, f"{case}.json", document(**override)))


def test_unknown_field_is_refused(tmp_path):
    refused(saved(tmp_path, "extra.json", document(extra=1)))


def test_missing_required_field_is_refused(tmp_path):
    bad = document()
    bad.pop("budgets")
    refused(saved(tmp_path, "budgetless.json", bad))


@pytest.mark.parametrize("probes", [[], ["dup", "dup"]], ids=["empty", "duplicate"])
def test_required_probes_unique_and_nonempty(tmp_path, probes):
    refused(saved(tmp_path, "probes.json", document(required_probes=probes)))


def test_budget_types_are_refused(tmp_path):
    refused(saved(tmp_path, "budget.json",
                    document(budgets={**BAD_BUDGET, "wall_seconds": "ten"})))


@pytest.mark.parametrize(("before", "after", "case"), [
    ('"wall_seconds": 10', '"wall_seconds": 1e999', "1e999-overflow"),
    ('"operator_interventions": 0', '"operator_interventions": NaN', "nan-token"),
    ('"operator_interventions": 0', '"operator_interventions": Infinity', "inf-token"),
])
def test_non_finite_numbers_rejected_on_validation(tmp_path, before, after, case):
    raw = json.dumps(document()).replace(before, after, 1)
    refused(saved(tmp_path, "finite.json", raw))


def test_invalid_input_rejects_the_whole_report(tmp_path):
    good = saved(tmp_path, "a.json", document())
    bad = saved(tmp_path, "b.json", document(budgets=BAD_BUDGET))
    with pytest.raises(ReportError):
        build_report([good, bad], cohort_id="c", enabled=True, validate_manifests=True)


def test_validate_manifests_must_be_exact_bool(tmp_path):
    path = saved(tmp_path, "b.json", document())
    with pytest.raises(CampaignAttemptRejected):
        build_report([path], cohort_id="c", enabled=True, validate_manifests="yes")


def test_disabled_never_builds_the_validator(tmp_path, monkeypatch):
    def bomb(*args, **kwargs):
        raise AssertionError("disabled report built a validator")

    monkeypatch.setattr(campaign_report, "_build_validator", bomb)
    assert build_report([], cohort_id="c", enabled=False, validate_manifests=True) == (
        summarize_attempts((), enabled=False)
    )


def test_inert_validate_only_never_consumes_paths():
    def poison():
        raise AssertionError("disabled report consumed paths")
        yield

    assert build_report(poison(), cohort_id="c", enabled=False,
                        validate_manifests=True) == summarize_attempts((), enabled=False)


def test_missing_validator_dependency_fails_generically(tmp_path, monkeypatch):
    real_import = __import__

    def no_jsonschema(name, *args, **kwargs):
        if name == "jsonschema":
            raise ImportError("no jsonschema")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", no_jsonschema)
    path = saved(tmp_path, "dep.json", document())
    with pytest.raises(ReportError) as caught:
        build_report([path], cohort_id="c", enabled=True, validate_manifests=True)
    assert path not in str(caught.value)


def test_missing_schema_file_fails_generically(tmp_path, monkeypatch):
    monkeypatch.setattr(campaign_report, "_SCHEMA_PATH", tmp_path / "missing.json")
    path = saved(tmp_path, "ok.json", document())
    with pytest.raises(ReportError) as caught:
        build_report([path], cohort_id="c", enabled=True, validate_manifests=True)
    assert path not in str(caught.value)


def test_cli_validate_requires_enabled_errors_before_io(tmp_path, capsys):
    code = main(["--validate-manifests", "--cohort", "c",
                 str(tmp_path / "missing.json")])
    assert code == 1
    captured = capsys.readouterr()
    assert "requires --enabled" in captured.err
    assert captured.out == ""


def test_cli_invalid_flagged_input_never_writes_output(tmp_path, capsys):
    source = saved(tmp_path, "bad.json", document(budgets=BAD_BUDGET))
    out = tmp_path / "report.json"
    code = main(["--enabled", "--validate-manifests", "--cohort", "c", "--output",
                 str(out), source])
    assert code == 1
    assert not out.exists()
    assert capsys.readouterr().out == ""


def test_cli_validate_error_is_generic_without_echo(tmp_path, capsys):
    source = saved(tmp_path, "bad.json", document(budgets=BAD_BUDGET))
    code = main(["--enabled", "--validate-manifests", "--cohort", "c", source])
    assert code == 1
    captured = capsys.readouterr()
    assert "campaign_report:" in captured.err
    assert source not in captured.err
    assert captured.out == ""


def test_cli_valid_flagged_output_matches_legacy(tmp_path, capsys):
    path = saved(tmp_path, "ok.json", document())
    assert main(["--enabled", "--validate-manifests", "--cohort", "c", path]) == 0
    flagged = capsys.readouterr().out
    assert main(["--enabled", "--cohort", "c", path]) == 0
    assert flagged == capsys.readouterr().out
    out = tmp_path / "report.json"
    assert main(["--enabled", "--validate-manifests", "--cohort", "c", "--output",
                 str(out), path]) == 0
    assert out.read_text(encoding="utf-8") == flagged


def test_validation_never_touches_the_network(tmp_path, monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("validation called the network")

    monkeypatch.setattr("urllib.request.urlopen", deny)
    path = saved(tmp_path, "ok.json", document())
    report = build_report([path], cohort_id="c", enabled=True, validate_manifests=True)
    assert report["enabled"] is True
    assert report == build_report([path], cohort_id="c", enabled=True)
