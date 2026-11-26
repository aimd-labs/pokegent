"""RED-first campaign attempt denominators (#138/R09): synthetic caller labels only."""

from __future__ import annotations

import dataclasses

import pytest

from pokegent.evaluation_campaign import (
    CampaignAttempt,
    CampaignAttemptRejected,
    summarize_attempts,
)


def attempt(campaign="campaign-a", attempt_id="attempt-1", cohort="cohort-1",
            status="completed", interventions=0):
    return CampaignAttempt(
        campaign_id=campaign, attempt_id=attempt_id, cohort_id=cohort,
        status=status, operator_interventions=interventions,
    )


def test_disabled_is_inert_and_does_not_consume_input():
    def poison():
        raise AssertionError("disabled summarizer consumed its input")
        yield

    report = summarize_attempts(poison())
    assert report == {
        "kind": "campaign_attempt_summary/1", "enabled": False,
        "evaluated": False, "eligibility": "not_evaluated",
    }


def test_mixed_statuses_prove_the_started_denominator():
    report = summarize_attempts([
        attempt(attempt_id="planned", status="planned"),
        attempt(attempt_id="done", status="completed"),
        attempt(attempt_id="assisted", status="completed", interventions=2),
        attempt(attempt_id="failed", status="failed"),
        attempt(attempt_id="timeout", status="timed_out"),
        attempt(attempt_id="censor", status="censored"),
        attempt(attempt_id="infra", status="infrastructure_invalid"),
    ], enabled=True)
    assert report["kind"] == "campaign_attempt_summary/1"
    assert report["enabled"] is True and report["evaluated"] is True
    assert report["eligibility"] == "not_evaluated"
    assert report["native_verification"] == "not_evaluated"
    assert report["counts"] == {
        "supplied": 7, "planned": 1, "started": 6, "completed": 2,
        "failed": 1, "timed_out": 1, "censored": 1,
        "infrastructure_invalid": 1, "assisted": 1,
        "reported_unassisted_completed": 1,
    }
    assert report["reported_unassisted_completion_rate"] == pytest.approx(1 / 6)
    assert report["cohorts"][0]["reported_unassisted_completion_rate"] == pytest.approx(1 / 6)
    assert report["limits"]["labels"] == "caller_supplied_not_certification"


def test_zero_starts_returns_null_rates():
    report = summarize_attempts([
        attempt(attempt_id="planned-1", status="planned", interventions=1),
        attempt(attempt_id="planned-2", status="planned"),
    ], enabled=True)
    assert report["counts"]["started"] == 0
    assert report["counts"]["assisted"] == 0
    assert report["reported_unassisted_completion_rate"] is None
    assert report["cohorts"][0]["reported_unassisted_completion_rate"] is None


def test_infrastructure_invalid_stays_in_denominator_and_visible():
    report = summarize_attempts([
        attempt(attempt_id="infra", status="infrastructure_invalid"),
    ], enabled=True)
    assert report["counts"]["started"] == 1
    assert report["counts"]["infrastructure_invalid"] == 1
    assert report["reported_unassisted_completion_rate"] == 0.0
    assert report["cohorts"][0]["infrastructure_invalid"] == 1


def test_duplicate_attempt_across_cohort_is_refused():
    with pytest.raises(CampaignAttemptRejected, match="duplicate"):
        summarize_attempts([
            attempt(attempt_id="same", cohort="cohort-x"),
            attempt(attempt_id="same", cohort="cohort-y"),
        ], enabled=True)


def test_cohorts_and_campaigns_stay_separate_and_ordered():
    report = summarize_attempts([
        attempt(campaign="campaign-b", cohort="cohort-z", attempt_id="b-1"),
        attempt(campaign="campaign-a", cohort="cohort-y", attempt_id="a-2"),
        attempt(campaign="campaign-a", cohort="cohort-x", attempt_id="a-1"),
        attempt(campaign="campaign-b", cohort="cohort-z", attempt_id="b-2",
                status="failed"),
    ], enabled=True)
    keys = [(row["campaign_id"], row["cohort_id"]) for row in report["cohorts"]]
    assert keys == [
        ("campaign-a", "cohort-x"), ("campaign-a", "cohort-y"),
        ("campaign-b", "cohort-z"),
    ]
    assert report["cohorts"][2]["supplied"] == 2
    assert report["cohorts"][2]["started"] == 2
    assert report["cohorts"][2]["reported_unassisted_completion_rate"] == 0.5


@pytest.mark.parametrize("enabled", [1, 0, "true", None])
def test_non_bool_enabled_is_refused(enabled):
    with pytest.raises(CampaignAttemptRejected, match="bool"):
        summarize_attempts([], enabled=enabled)


@pytest.mark.parametrize("row", [{"campaign_id": "c"}, None, object(), "attempt"])
def test_non_campaign_attempt_rows_are_rejected_not_omitted(row):
    with pytest.raises(CampaignAttemptRejected):
        summarize_attempts([attempt(attempt_id="ok"), row], enabled=True)


@pytest.mark.parametrize("field,value", [
    ("campaign_id", ""), ("campaign_id", "   "), ("campaign_id", 7),
    ("attempt_id", "x" * 129), ("attempt_id", None),
    ("cohort_id", ""), ("cohort_id", b"cohort"),
    ("status", "unknown"), ("status", "Completed"), ("status", None),
    ("operator_interventions", True), ("operator_interventions", -1),
    ("operator_interventions", "0"), ("operator_interventions", 1.0),
])
def test_invalid_field_types_are_rejected(field, value):
    kwargs = {"campaign_id": "campaign-a", "attempt_id": "attempt-1",
              "cohort_id": "cohort-1", "status": "completed",
              "operator_interventions": 0, field: value}
    with pytest.raises(CampaignAttemptRejected):
        CampaignAttempt(**kwargs)


def test_limit_bound_generator_stops_after_1001_rows():
    consumed = 0

    def rows():
        nonlocal consumed
        for index in range(5000):
            consumed += 1
            yield attempt(attempt_id=f"attempt-{index}")

    with pytest.raises(CampaignAttemptRejected, match="1000"):
        summarize_attempts(rows(), enabled=True)
    assert consumed == 1001


def test_records_are_frozen():
    record = attempt()
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.status = "failed"


def test_returned_limits_are_detached_from_other_reports():
    first = summarize_attempts([attempt()], enabled=True)
    first["limits"]["labels"] = "mutated"
    second = summarize_attempts([attempt()], enabled=True)
    assert second["limits"]["labels"] == "caller_supplied_not_certification"
