# Calibration scene eligibility (#134, #400)

Status: documentation for the reviewed, default-off `--scene-eligibility` option in
`tools/calibration_report.py`. Offline and synthetic evidence only. It grants no action
authority and does not complete #134.

## Behavior

The report reads the same schema-1 calibration JSONL files as before. With the switch off the
output is unchanged. With it on, each record is counted once into its `scene_id` (across runs,
providers and models) and the report gains a top-level `scene_eligibility` object.

- CLI: `python -m tools.calibration_report --scene-eligibility --min-samples N --min-valid-rate R --max-p95-ms MS FILE [FILE ...]`.
- All three thresholds are required with the switch. `N` is an integer of at least 1, `R` is in
  (0, 1] and `MS` is finite and above 0. Anything else exits 2.
- API: `build_report(paths, scene_eligibility=True, policy=(N, R, MS))`; a missing policy raises
  `ReportError`.

## Output

Per scene: `records`, `valid`, `invalid`, `unknown`, `p50_ms`, `p95_ms` (nearest rank over the
logged `measured_ms`), `eligible` and `reason`. `valid`, `invalid` and `unknown` come from the
optional `framed_choice_valid` field; a record without it is `unknown`. They always add up to
`records`. Top-level `accuracy` and `no_progress` are `null` because calibration records carry
neither, and `authority_granted` is `false`.

Labels are checked in this order and the first match wins:

1. `records` below `N`: `eligible: "unknown"`, reason `below_min_samples`.
2. Any unknown choice: `eligible: "unknown"`, reason `unknown_choices`.
3. `valid / (valid + invalid)` below `R`: `eligible: false`, reason `below_valid_rate`.
4. p95 above `MS`: `eligible: false`, reason `over_latency_cap`.
5. Otherwise `eligible: true`, reason `eligible`.

An unknown result is neither a pass nor a failure, and old logs without `framed_choice_valid`
can never come out eligible.

## Meaning and limits

The thresholds are chosen by the operator for each run. They are not calibrated, learned or
validated against live play. A label describes logged response validity and latency for that
scene. It does not show accuracy, verified progress, no-progress episodes, provider or model
quality, native results or readiness for System One authority. Acceptance stays with the
[evaluation protocol](evaluation-protocol.md). Refs [#134](https://github.com/aimd-labs/pokegent/issues/134).
