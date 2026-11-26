# Parsed manifest batch summary (R09/#138)

Status: documentation for the default-off `summarize_manifests` helper. It accepts caller-built
manifest dictionaries; it is not a loader, CLI, benchmark result or certification.

## Scope

`summarize_manifests(manifests, *, cohort_id, enabled=False)` projects already-parsed manifest
dictionaries into the frozen `CampaignAttempt` records used by
[campaign attempt summary](campaign-attempt-summary.md), then reuses `summarize_attempts`.
Cohort identity is passed once, separately; it is not read from a row. There is no file
reader, glob, download, JSON parsing or command-line entry point in either helper.

## Input contract

`enabled` must be exactly a `bool` and defaults to `False`. A disabled call returns the
existing inert summary and does not touch `manifests` at all.

When enabled, `manifests` must be a plain `list` or `tuple`; subclasses, iterators, strings,
bytes, dicts and sets are refused. At most `MAX_ATTEMPTS` = 1000 rows are allowed. The
container type and cap are checked before any row is projected.

Each row is a plain `dict` carrying `schema_version` fixed to `pokegent-evaluation/1` and
the four projected fields: `campaign_id`, `attempt_id`, `status` and
`operator_interventions`. `cohort_id` comes from the call, not the row.

## Batch rules

The existing row validator checks nonblank identifiers of at most 128 characters, a status
from the frozen list and an integer `operator_interventions >= 0`; booleans are refused.
Duplicate `(campaign_id, attempt_id)` pairs are refused. Any bad row rejects the whole
batch: there is no partial count, skipped row or best-effort report. Rejections never echo
row contents.

## Counts and rates

Every non-`planned` status counts as started, including `failed`, `timed_out`, `censored`
and `infrastructure_invalid`; `planned` appears only in `supplied`/`planned`. `assisted`
counts started rows with `operator_interventions > 0`. `reported_unassisted_completed`
counts only `completed` rows with zero interventions. The reported rate is
`reported_unassisted_completed / started`, globally and per `(campaign_id, cohort_id)`.
Zero started rows, including an empty batch, give `None`, never 0.

An empty batch has no cohort row, because cohorts are built from rows; no global cohort
validation runs. Do not read that as proof that an empty or any other batch is
cohort-valid or comparable.

## What the numbers do not mean

All counts are caller labels. The report records `caller_supplied_not_certification` and
leaves `native_verification` at `not_evaluated`. It does not verify receipts, native
completion or ROM behaviour, prove independence, select comparable cohorts, compute
confidence intervals, apply reliability thresholds, certify native outcomes, or grant
any authority.

## Synthetic example

Invented already-parsed dictionaries exercise the real API, explicit count and rate
assertions, default-off behaviour, an empty batch and several refusals.

```python
"""Synthetic, ROM-free batch example for manifest-batch-summary.md."""

from pokegent.evaluation_campaign import CampaignAttemptRejected
from pokegent.evaluation_manifest import summarize_manifests

BASE = {"schema_version": "pokegent-evaluation/1", "campaign_id": "synthetic-campaign",
        "attempt_id": "attempt-1", "status": "planned", "operator_interventions": 0}

def row(attempt_id, status, interventions=0, **extra):
    return dict(BASE, attempt_id=attempt_id, status=status,
                operator_interventions=interventions, **extra)

manifests = [
    row("done", "completed"), row("assisted", "completed", 1),
    row("failed", "failed"), row("infra", "infrastructure_invalid"),
    row("planned", "planned"),
]
report = summarize_manifests(manifests, cohort_id="synthetic-cohort", enabled=True)
counts = report["counts"]
print("counts:", counts)

assert counts == {"supplied": 5, "planned": 1, "started": 4, "completed": 2,
                  "failed": 1, "timed_out": 0, "censored": 0,
                  "infrastructure_invalid": 1, "assisted": 1,
                  "reported_unassisted_completed": 1}
assert report["reported_unassisted_completion_rate"] == 0.25
assert report["cohorts"][0]["reported_unassisted_completion_rate"] == 0.25
assert report["limits"]["labels"] == "caller_supplied_not_certification"
assert report["native_verification"] == "not_evaluated"
print("rate:", report["reported_unassisted_completion_rate"])

class Poison:
    def __getitem__(self, key):
        raise AssertionError("disabled call inspected input")
    def get(self, key, default=None):
        raise AssertionError("disabled call inspected input")

disabled = summarize_manifests(Poison(), cohort_id=None)
print("disabled:", disabled)
assert disabled == {"kind": "campaign_attempt_summary/1", "enabled": False,
                    "evaluated": False, "eligibility": "not_evaluated"}
empty = summarize_manifests([], cohort_id=None, enabled=True)
print("empty rate:", empty["reported_unassisted_completion_rate"])
assert empty["cohorts"] == [] and empty["reported_unassisted_completion_rate"] is None

def refused(label, call):
    try:
        call()
    except CampaignAttemptRejected as error:
        print(label, "->", error)
    else:
        raise AssertionError(f"{label} was not refused")

refused("enabled=1", lambda: summarize_manifests(manifests, cohort_id="c", enabled=1))
refused("iterator", lambda: summarize_manifests(iter(manifests), cohort_id="c", enabled=True))
refused("bad schema", lambda: summarize_manifests(
    [row("x", "completed", schema_version="pokegent-evaluation/2")], cohort_id="c", enabled=True))
refused("missing status", lambda: summarize_manifests(
    [{"schema_version": "pokegent-evaluation/1", "campaign_id": "c",
      "attempt_id": "x", "operator_interventions": 0}], cohort_id="c", enabled=True))
refused("duplicate id", lambda: summarize_manifests(
    [row("same", "completed"), row("same", "failed")], cohort_id="c", enabled=True))
refused("1001 rows", lambda: summarize_manifests(
    [row(str(i), "planned") for i in range(1001)], cohort_id="c", enabled=True))
print("synthetic labels only: no certification, independence or authority claim")
```
