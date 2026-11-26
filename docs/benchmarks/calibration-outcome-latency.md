# Calibration outcome latency (R09/#138)

Status: documentation for the reviewed, default-off `--outcome-latency` option in
`tools/calibration_report.py`. Synthetic evidence only; not a benchmark result and grants nothing.

## Behavior

The report still reads caller-named schema-1 JSONL files and emits its usual response-validity,
attempt, truncation, identity and aggregate latency summaries. The flag adds a per-outcome
breakdown of the logged `measured_ms` field inside each existing stratum. With the flag off, the
files are read exactly as before and the output is unchanged; only the per-outcome extension
defaults off. This does not disable file I/O.

- CLI: `python -m tools.calibration_report --outcome-latency FILE [FILE ...]`.
- API: `build_report(paths, outcome_latency=True)`; the keyword defaults to `False` and requires
  a real `bool` (`type(value) is bool`). `1`, `0`, `"true"` and `None` raise `ReportError`
  before any input path is opened.

## Strata and output

Strata are unchanged: rows are grouped by the existing `source`, `run_id`, `scene_id`,
`provider_id`, `provider_version`, `model_id`, `prompt_schema_version` and `option_order`
fields, and no run, scene, provider, model or option order is pooled with another.

When enabled, each stratum gains `outcome_measured_ms` with one entry for every existing
response outcome: `ok` (valid response logged), `timeout` (logged attempt timed out),
`invalid` (logged response failed validation) and `error` (logged response errored). Each entry
has `count`, `p50` and `p95`, using the report's existing nearest-rank convention: sort the
values, then take index `max(1, ceil(q * n)) - 1`. An outcome with no records stays present with
`count: 0` and null `p50`/`p95`.

Aggregates are unchanged: `records`, `outcomes`, `measured_ms`, `attempts_*`, `truncated`,
identity fields and `eligibility` keep their previous values, and the four per-outcome counts
sum to the stratum's logged record count.

## Meaning and limits

`count` counts logged calibration records in an outcome bucket. It is not an HTTP attempt,
campaign-start or retry count. `measured_ms` is a value already in each record; the report does
not measure, re-time or verify it, and a p50/p95 describes those logged values, not provider or
handoff latency.

The report does not prove end-to-end or handoff coverage, independence, action acceptance,
accuracy, progress, eligibility or native results. A timeout count is not censoring analysis.
Acceptance gates stay in the [evaluation protocol](evaluation-protocol.md). Refs
[#138](https://github.com/aimd-labs/pokegent/issues/138), R09.

## Synthetic example
An invented public schema-1 fixture compares one stratum and the outcomes `ok`/`timeout`, flag off and on, inside one temporary directory.
```python
import json
import tempfile
from pathlib import Path

from tools.calibration_report import build_report

record = {
    "schema": 1,
    "record_seq": 1,
    "measured_ms": 10.0,
    "truncated": False,
    "outcome": "ok",
    "stages": [],
    "run_id": "run-synthetic",
    "scene_id": "scene-synthetic",
    "provider_id": "local-synthetic",
    "provider_version": "pv-synthetic",
    "model_id": "model-synthetic",
    "prompt_schema_version": "ps-synthetic",
    "option_order": "frame",
    "attempts": 1,
}
rows = [
    dict(record, record_seq=1, measured_ms=10.0, outcome="ok"),
    dict(record, record_seq=2, measured_ms=20.0, outcome="ok"),
    dict(record, record_seq=3, measured_ms=30.0, outcome="ok"),
    dict(record, record_seq=4, measured_ms=40.0, outcome="ok"),
    dict(record, record_seq=5, measured_ms=50.0, outcome="ok"),
    dict(record, record_seq=6, measured_ms=100.0, outcome="timeout"),
]

with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "calibration.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    default = build_report([str(path)])
    enabled = build_report([str(path)], outcome_latency=True)

    assert "outcome_measured_ms" not in default["strata"][0]
    latency = enabled["strata"][0]["outcome_measured_ms"]
    assert latency["ok"] == {"count": 5, "p50": 30.0, "p95": 50.0}
    assert latency["timeout"] == {"count": 1, "p50": 100.0, "p95": 100.0}
    assert latency["invalid"] == {"count": 0, "p50": None, "p95": None}
    assert latency["error"] == {"count": 0, "p50": None, "p95": None}
    assert enabled["records"] == default["records"] == len(rows)
    assert enabled["eligibility"] == default["eligibility"] == "not_evaluated"

print("calibration outcome latency: synthetic ok/timeout buckets, nearest-rank percentiles, "
      "empty buckets and unchanged aggregate checks passed")
```
