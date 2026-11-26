"""RED-first manifest projection (#138/R09): caller data only, not a loader or validator."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from pokegent.evaluation_campaign import CampaignAttemptRejected, summarize_attempts
from pokegent.evaluation_manifest import project_attempt

EXAMPLE = json.loads(
    (Path(__file__).resolve().parents[1]
     / "docs/benchmarks/scenario-manifest.example.json").read_text(encoding="utf-8")
)


class Poison:
    def get(self, key, default=None):
        raise AssertionError("disabled projection inspected the manifest")

    def __getitem__(self, key):
        raise AssertionError("disabled projection inspected the manifest")


def test_disabled_is_inert_and_returns_none_without_inspecting():
    assert project_attempt(Poison(), cohort_id=None) is None


@pytest.mark.parametrize("enabled", [1, 0, "true", None])
def test_enabled_must_be_an_exact_bool(enabled):
    with pytest.raises(CampaignAttemptRejected, match="bool"):
        project_attempt(Poison(), cohort_id="cohort-1", enabled=enabled)


def test_example_projects_four_fields_and_hides_extras():
    attempt = project_attempt(EXAMPLE, cohort_id="cohort-1", enabled=True)
    assert attempt.campaign_id == EXAMPLE["campaign_id"]
    assert attempt.attempt_id == EXAMPLE["attempt_id"]
    assert attempt.status == EXAMPLE["status"]
    assert attempt.operator_interventions == EXAMPLE["operator_interventions"]
    assert attempt.cohort_id == "cohort-1"
    assert [field.name for field in dataclasses.fields(attempt)] == [
        "campaign_id", "attempt_id", "cohort_id", "status", "operator_interventions",
    ]


def test_segment_id_cannot_inflate_the_denominator():
    other = dict(EXAMPLE, segment_id="different-segment")
    first = project_attempt(EXAMPLE, cohort_id="cohort-1", enabled=True)
    second = project_attempt(other, cohort_id="cohort-1", enabled=True)
    assert first == second
    with pytest.raises(CampaignAttemptRejected, match="duplicate"):
        summarize_attempts([first, second], enabled=True)


@pytest.mark.parametrize("status", [
    "planned", "completed", "failed", "timed_out", "censored", "infrastructure_invalid",
])
def test_status_mutations_obey_the_existing_dataclass(status):
    attempt = project_attempt(dict(EXAMPLE, status=status), cohort_id="c-1", enabled=True)
    assert attempt.status == status


class DictSubclass(dict):
    pass


@pytest.mark.parametrize("root", [None, [], "manifest", object(), DictSubclass(EXAMPLE)])
def test_root_must_be_a_plain_dict(root):
    with pytest.raises(CampaignAttemptRejected, match="manifest"):
        project_attempt(root, cohort_id="cohort-1", enabled=True)


@pytest.mark.parametrize("value", [None, 1, "pokegent-evaluation/2", ["pokegent-evaluation/1"]])
def test_schema_version_must_be_the_exact_plain_string(value):
    with pytest.raises(CampaignAttemptRejected, match="schema_version"):
        project_attempt(dict(EXAMPLE, schema_version=value), cohort_id="c-1", enabled=True)


def test_missing_schema_version_is_rejected():
    manifest = dict(EXAMPLE)
    manifest.pop("schema_version")
    with pytest.raises(CampaignAttemptRejected, match="schema_version"):
        project_attempt(manifest, cohort_id="cohort-1", enabled=True)


@pytest.mark.parametrize("name", [
    "campaign_id", "attempt_id", "status", "operator_interventions",
])
def test_missing_projected_field_is_rejected_not_defaulted(name):
    manifest = dict(EXAMPLE)
    manifest.pop(name)
    with pytest.raises(CampaignAttemptRejected, match=name):
        project_attempt(manifest, cohort_id="cohort-1", enabled=True)


@pytest.mark.parametrize("cohort", ["", "   ", 7, b"cohort", None])
def test_invalid_cohort_is_rejected_by_the_dataclass(cohort):
    with pytest.raises(CampaignAttemptRejected, match="cohort_id"):
        project_attempt(EXAMPLE, cohort_id=cohort, enabled=True)


@pytest.mark.parametrize("field,value", [
    ("campaign_id", ""), ("campaign_id", 7), ("attempt_id", "x" * 129),
    ("attempt_id", None), ("status", "unknown"), ("status", "Completed"),
    ("status", None), ("operator_interventions", True),
    ("operator_interventions", -1), ("operator_interventions", "0"),
    ("operator_interventions", 1.0),
])
def test_field_validation_is_reused_from_the_dataclass(field, value):
    manifest = dict(EXAMPLE, **{field: value})
    with pytest.raises(CampaignAttemptRejected):
        project_attempt(manifest, cohort_id="cohort-1", enabled=True)
