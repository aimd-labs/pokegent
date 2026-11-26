"""RED-first batch manifest summary (#138/R09): composes projection and summary only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pokegent.evaluation_campaign import (
    MAX_ATTEMPTS,
    CampaignAttempt,
    CampaignAttemptRejected,
    summarize_attempts,
)
from pokegent.evaluation_manifest import summarize_manifests

EXAMPLE = json.loads(
    (Path(__file__).resolve().parents[1]
     / "docs/benchmarks/scenario-manifest.example.json").read_text(encoding="utf-8")
)


def manifest(campaign="campaign-a", attempt_id="attempt-1", status="completed",
             interventions=0, **extra):
    return dict(EXAMPLE, campaign_id=campaign, attempt_id=attempt_id, status=status,
                operator_interventions=interventions, **extra)


class Poison:
    def get(self, key, default=None):
        raise AssertionError("disabled batch summary inspected the input")

    def __getitem__(self, key):
        raise AssertionError("disabled batch summary inspected the input")


class ListSubclass(list):
    pass


class TupleSubclass(tuple):
    pass


class TrapIterable:
    def __iter__(self):
        raise AssertionError("non-list container was iterated")


def test_disabled_is_the_existing_disabled_summary_and_touches_nothing():
    assert summarize_manifests(Poison(), cohort_id=None) == summarize_attempts(
        (), enabled=False)


@pytest.mark.parametrize("enabled", [1, 0, "true", None])
def test_enabled_must_be_an_exact_bool(enabled):
    with pytest.raises(CampaignAttemptRejected, match="bool"):
        summarize_manifests(Poison(), cohort_id="cohort-1", enabled=enabled)


def test_batch_of_four_field_manifests_matches_the_existing_summary():
    rows = [
        manifest(campaign="campaign-b", attempt_id="done", status="completed"),
        manifest(campaign="campaign-a", attempt_id="assisted", status="completed",
                 interventions=2),
        manifest(campaign="campaign-a", attempt_id="failed", status="failed"),
        manifest(campaign="campaign-a", attempt_id="planned", status="planned"),
        manifest(campaign="campaign-b", attempt_id="infra",
                 status="infrastructure_invalid"),
    ]
    report = summarize_manifests(rows, cohort_id="cohort-1", enabled=True)
    expected = summarize_attempts([
        CampaignAttempt(campaign_id=row["campaign_id"], attempt_id=row["attempt_id"],
                        cohort_id="cohort-1", status=row["status"],
                        operator_interventions=row["operator_interventions"])
        for row in rows
    ], enabled=True)
    assert report == expected
    assert report["counts"] == {
        "supplied": 5, "planned": 1, "started": 4, "completed": 2,
        "failed": 1, "timed_out": 0, "censored": 0,
        "infrastructure_invalid": 1, "assisted": 1,
        "reported_unassisted_completed": 1,
    }
    assert report["reported_unassisted_completion_rate"] == 0.25
    assert [(row["campaign_id"], row["cohort_id"]) for row in report["cohorts"]] == [
        ("campaign-a", "cohort-1"), ("campaign-b", "cohort-1"),
    ]
    assert report["limits"]["labels"] == "caller_supplied_not_certification"


@pytest.mark.parametrize("empty", [[], ()])
def test_empty_batch_has_no_global_cohort_validator(empty):
    assert summarize_manifests(empty, cohort_id=None, enabled=True) == summarize_attempts(
        (), enabled=True)


@pytest.mark.parametrize("container", [
    None, {}, "rows", b"rows", {1}, iter([]), ListSubclass(), TupleSubclass(),
    TrapIterable(),
], ids=["none", "dict", "str", "bytes", "set", "iterator", "list-subclass",
        "tuple-subclass", "iterable"])
def test_only_plain_lists_and_tuples_are_accepted(container):
    with pytest.raises(CampaignAttemptRejected, match="plain list or tuple"):
        summarize_manifests(container, cohort_id="cohort-1", enabled=True)


def test_container_type_and_cap_are_checked_before_any_projection(monkeypatch):
    projected = []
    monkeypatch.setattr("pokegent.evaluation_manifest.project_attempt",
                        lambda *args, **kwargs: projected.append(args))
    with pytest.raises(CampaignAttemptRejected, match="plain list or tuple"):
        summarize_manifests("rows", cohort_id="cohort-1", enabled=True)
    with pytest.raises(CampaignAttemptRejected, match="1000"):
        summarize_manifests([None] * (MAX_ATTEMPTS + 1), cohort_id="cohort-1",
                            enabled=True)
    assert projected == []


@pytest.mark.parametrize("bad", [
    dict(EXAMPLE, schema_version="pokegent-evaluation/2"),
    {key: value for key, value in EXAMPLE.items() if key != "attempt_id"},
    dict(EXAMPLE, status="unknown"),
    dict(EXAMPLE, operator_interventions=True),
    dict(EXAMPLE, campaign_id=""),
])
def test_one_bad_row_rejects_the_whole_batch_instead_of_partial_counts(bad):
    with pytest.raises(CampaignAttemptRejected):
        summarize_manifests([manifest(attempt_id="ok"), bad], cohort_id="cohort-1",
                            enabled=True)


def test_invalid_cohort_is_rejected_by_the_projected_dataclass():
    with pytest.raises(CampaignAttemptRejected, match="cohort_id"):
        summarize_manifests([manifest()], cohort_id="", enabled=True)


def test_duplicate_campaign_and_attempt_is_rejected():
    with pytest.raises(CampaignAttemptRejected, match="duplicate"):
        summarize_manifests([manifest(attempt_id="same"), manifest(attempt_id="same")],
                            cohort_id="cohort-1", enabled=True)


PRIVATE = "private-host-7f3a"


def test_rejection_does_not_echo_private_row_fields():
    row = manifest(attempt_id="leaky", host=PRIVATE, status="private-status")
    with pytest.raises(CampaignAttemptRejected) as caught:
        summarize_manifests([row], cohort_id="cohort-1", enabled=True)
    assert PRIVATE not in str(caught.value)
    assert "private-status" not in str(caught.value)
