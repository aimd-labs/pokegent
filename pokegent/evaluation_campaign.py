"""Default-off campaign attempt denominators (#138/R09): caller labels only, no certification."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import islice
from typing import Any

MAX_ATTEMPTS, MAX_IDENTITY = 1000, 128
STATUSES = ("planned", "completed", "failed", "timed_out", "censored", "infrastructure_invalid")
FIELDS = ("supplied", "planned", "started", "completed", "failed", "timed_out", "censored",
          "infrastructure_invalid", "assisted", "reported_unassisted_completed")
LIMITS = {
    "labels": "caller_supplied_not_certification",
    "receipts_and_native_completion": "not_verified",
    "independence_and_comparable_cohorts": "not_verified",
    "missing_or_invalid_rows": "rejected_not_omitted",
    "rate_denominator": "reported_unassisted_completed/started",
    "statistics_and_authority": "no confidence interval, reliability threshold or authority",
}


class CampaignAttemptRejected(ValueError):
    """Malformed campaign-attempt request; row details are never echoed."""


def _identity(value: Any, name: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > MAX_IDENTITY:
        raise CampaignAttemptRejected(f"{name} must be nonblank and at most {MAX_IDENTITY} chars")
    return value


def _validate(attempt: CampaignAttempt) -> None:
    _identity(attempt.campaign_id, "campaign_id")
    _identity(attempt.attempt_id, "attempt_id")
    _identity(attempt.cohort_id, "cohort_id")
    if type(attempt.status) is not str or attempt.status not in STATUSES:
        raise CampaignAttemptRejected(f"status must be one of {STATUSES}")
    interventions = attempt.operator_interventions
    if type(interventions) is not int or interventions < 0:
        raise CampaignAttemptRejected("operator_interventions must be an int >= 0")


@dataclass(frozen=True)
class CampaignAttempt:
    """One caller-labeled campaign attempt; not verified by this module."""

    campaign_id: str
    attempt_id: str
    cohort_id: str
    status: str
    operator_interventions: int

    def __post_init__(self) -> None:
        _validate(self)


def _collect(attempts: Iterable[CampaignAttempt]) -> list[CampaignAttempt]:
    if isinstance(attempts, (str, bytes, bytearray)):
        raise CampaignAttemptRejected("attempts must be an iterable of CampaignAttempt")
    try:
        materialized = list(islice(iter(attempts), MAX_ATTEMPTS + 1))
    except TypeError:
        raise CampaignAttemptRejected("attempts must be an iterable of attempts") from None
    if len(materialized) > MAX_ATTEMPTS:
        raise CampaignAttemptRejected(f"attempts exceeds {MAX_ATTEMPTS}")
    return materialized


def _tally(rows: Iterable[CampaignAttempt]) -> dict[str, int]:
    counts = dict.fromkeys(FIELDS, 0)
    for attempt in rows:
        counts["supplied"] += 1
        counts[attempt.status] += 1
        counts["started"] += int(attempt.status != "planned")
        assisted = attempt.status != "planned" and attempt.operator_interventions > 0
        counts["assisted"] += int(assisted)
        counts["reported_unassisted_completed"] += int(
            attempt.status == "completed" and attempt.operator_interventions == 0)
    return counts


def _rate(counts: dict[str, int]) -> float | None:
    if not counts["started"]:
        return None
    return counts["reported_unassisted_completed"] / counts["started"]


def summarize_attempts(
    attempts: Iterable[CampaignAttempt], *, enabled: bool = False
) -> dict[str, Any]:
    """Count caller-labeled attempts only when ``enabled is True``; otherwise inert."""
    if type(enabled) is not bool:
        raise CampaignAttemptRejected("enabled must be a bool")
    if not enabled:
        return {"kind": "campaign_attempt_summary/1", "enabled": False, "evaluated": False,
                "eligibility": "not_evaluated"}
    materialized = _collect(attempts)
    seen: set[tuple[str, str]] = set()
    grouped: dict[tuple[str, str], list[CampaignAttempt]] = {}
    for attempt in materialized:
        if not isinstance(attempt, CampaignAttempt):
            raise CampaignAttemptRejected("attempts must contain CampaignAttempt values")
        _validate(attempt)
        key = (attempt.campaign_id, attempt.attempt_id)
        if key in seen:
            raise CampaignAttemptRejected("duplicate (campaign_id, attempt_id) attempt")
        seen.add(key)
        grouped.setdefault((attempt.campaign_id, attempt.cohort_id), []).append(attempt)
    counts = _tally(materialized)
    cohorts = []
    for key in sorted(grouped):
        row = _tally(grouped[key])
        cohorts.append({"campaign_id": key[0], "cohort_id": key[1], **row,
                        "reported_unassisted_completion_rate": _rate(row)})
    return {
        "kind": "campaign_attempt_summary/1", "enabled": True, "evaluated": True,
        "eligibility": "not_evaluated", "native_verification": "not_evaluated",
        "counts": counts, "reported_unassisted_completion_rate": _rate(counts),
        "cohorts": cohorts, "limits": dict(LIMITS), "attempt_cap": MAX_ATTEMPTS,
    }
