# Campaign attempt summary (R09/#138)

Status: documentation for the default-off `pokegent.evaluation_campaign` helper. It consumes
caller-supplied labels only; this is not a benchmark result and grants nothing.

## Scope

`summarize_attempts(attempts, *, enabled=False)` turns `CampaignAttempt` records into counts and
rates. It is not a schema validator beyond its own frozen records, not a manifest loader, and not a
runtime or persistence store. It does not validate receipts, native outcomes, independence or
cohort comparability.

## Attempt fields

- `campaign_id`, `attempt_id`, `cohort_id`: nonblank strings of at most 128 characters.
- `status`: one of `planned`, `completed`, `failed`, `timed_out`, `censored`,
  `infrastructure_invalid`.
- `operator_interventions`: integer >= 0; booleans and other numeric types are rejected.

Records are frozen. Invalid rows raise `CampaignAttemptRejected`; row details are not echoed.

## Default-off and bounded input

`enabled` must be a real `bool` and defaults to `False`. A disabled call returns an inert report
(`enabled: false`, `evaluated: false`, `eligibility: not_evaluated`) without touching the input
iterable. Enabled calls materialize at most `MAX_ATTEMPTS + 1` rows (1000 + 1); a 1001st row is
refused instead of silently truncated. Duplicate `(campaign_id, attempt_id)` pairs are refused even
when the rows claim different cohorts. Missing or invalid rows are rejected, not omitted.

## Denominator rules

Every non-`planned` status counts as started, including `failed`, `timed_out`, `censored` and
`infrastructure_invalid`. `planned` rows stay visible in `supplied`/`planned` but not in the
denominator. `assisted` counts started attempts with `operator_interventions > 0` and is reported
separately. `reported_unassisted_completed` counts only `completed` rows with zero interventions.
The rate is `reported_unassisted_completed / started`, computed globally and per
`(campaign_id, cohort_id)`; cohorts are sorted and kept separate. A zero-start value, including an
enabled call with no rows, yields `None`, never 0 or an exception.

## What the numbers do not mean

The counts are caller labels: `limits` records `caller_supplied_not_certification`, and
`native_verification` stays `not_evaluated`. The helper does not verify receipts or native
completion, prove independence or pooling suitability, or decide success, release or eligibility.
It does not change the research proposal's acceptance gates and must not be cited as a benchmark
result.

## Synthetic example

This invented example uses one cohort with an unassisted completion, an `infrastructure_invalid`
start and a `planned` row. Enabled counts show `started=2`, `planned=1` and a reported unassisted
completion rate of `0.5`; disabled is inert, empty starts report `None`, cohort and global rates match.

```python
from pokegent.evaluation_campaign import CampaignAttempt, summarize_attempts

rows = [
    CampaignAttempt(campaign_id="c", attempt_id="done", cohort_id="cohort", status="completed", operator_interventions=0),
    CampaignAttempt(campaign_id="c", attempt_id="infra", cohort_id="cohort", status="infrastructure_invalid", operator_interventions=0),
    CampaignAttempt(campaign_id="c", attempt_id="planned", cohort_id="cohort", status="planned", operator_interventions=0),
]

enabled = summarize_attempts(rows, enabled=True)
disabled = summarize_attempts(rows)
empty = summarize_attempts([], enabled=True)

print("enabled counts:", enabled["counts"])
print("cohort rate:", enabled["cohorts"][0]["reported_unassisted_completion_rate"])
print("disabled evaluated:", disabled["evaluated"])
print("empty enabled rate:", empty["reported_unassisted_completion_rate"])

assert enabled["counts"]["started"] == 2
assert enabled["counts"]["planned"] == 1
assert enabled["reported_unassisted_completion_rate"] == 0.5
assert enabled["cohorts"][0]["reported_unassisted_completion_rate"] == 0.5
assert disabled == {"kind": "campaign_attempt_summary/1", "enabled": False,
                    "evaluated": False, "eligibility": "not_evaluated"}
assert empty["reported_unassisted_completion_rate"] is None
print("synthetic labels only: no native completion, independence or eligibility claim")
```
