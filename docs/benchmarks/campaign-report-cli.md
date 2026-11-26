# Campaign report CLI

`tools.campaign_report` is a default-off source-checkout command that reads caller-named attempt
manifests and prints one sorted JSON summary of caller-supplied labels. It runs no game or
provider and grants no acceptance; see [CONTRIBUTING.md](../../CONTRIBUTING.md) for setup.

## Run it

```sh
python -m tools.campaign_report MANIFEST_JSON [MANIFEST_JSON ...] --cohort LABEL [--enabled] [--output REPORT_JSON] [--validate-manifests]
```

`--cohort` is required: a nonblank string of at most 128 characters. `--enabled` is off by
default. Without it the command opens and consumes no paths and prints only the inert summary
(`enabled: false`, `evaluated: false`). `--output` without `--enabled` instead errors before any
I/O. With `--enabled`, each manifest is opened by the path you gave: no directory scan, glob,
fetch or runtime configuration. Each file must be one UTF-8 JSON object of at most 1 MiB,
with at most 1000 paths and 8 MiB of total manifest bytes. `schema_version` must be exactly
`pokegent-evaluation/1` for projection-version admission. The command projects `campaign_id`,
`attempt_id`, `status` and `operator_interventions`; `status` is one of `planned`, `completed`,
`failed`, `timed_out`, `censored`, `infrastructure_invalid`, and `operator_interventions` is an
integer >= 0. Other object fields are ignored. A bad file, a missing or invalid projected field,
or a duplicate `(campaign_id, attempt_id)` rejects the entire report instead of omitting a start.

Rejected input prints a constant, index or field message prefixed `campaign_report:` to stderr,
writes no stdout and exits 1. Messages do not echo raw values or caller paths. Argument-parsing
failures are standard `argparse` behavior (usage on stderr), so not every argument error should
be expected to exit 1.

The optional `--output REPORT_JSON` writes the same summary to a new file instead of stdout and
requires `--enabled` first: without it, the command prints `campaign_report: --output requires
--enabled` to stderr and exits 1 before any input or output I/O. The file is created exclusively
with POSIX mode 0600 and holds the sorted strict JSON plus one trailing newline; stdout stays
empty when an artifact is requested. An existing file, directory or final symlink at that path is
refused and left unchanged, and no missing parent directory is created. The write is not atomic:
a mid-write I/O error can leave a partially written new file. Without `--output`, stdout and the
existing `build_report` behavior are unchanged.

The optional `--validate-manifests` requires `--enabled` first and adds explicit structural
validation of each bounded parsed manifest against the bundled
`docs/benchmarks/scenario-manifest.schema.json` (Draft 2020-12) before projection. Without it,
validation is off and the legacy tolerant projection is unchanged. It refuses the entire report
with a generic `campaign_report:` stderr message (index-only, no value or path echo) when any
manifest fails the schema, contains a non-finite number (raw NaN/Infinity tokens or float
overflow such as `1e999`), or when the validation dependency or bundled schema is unavailable.
With `--validate-manifests` and no `--enabled`, the command prints
`campaign_report: --validate-manifests requires --enabled` to stderr and exits 1 before any
input or output I/O.

The `--validate-manifests` path needs the pinned validator; from an existing Python 3.11
source checkout, install the optional `evaluation` extra with
`uv pip install -e '.[evaluation]'`. The bundled `docs/benchmarks/scenario-manifest.schema.json`
must remain present. The default and validate-off paths never import the extra.

The output is one `json.dumps(..., sort_keys=True)` object of caller-label counts: `supplied`,
`planned`, `started`, `completed`, `failed`, `timed_out`, `censored`, `infrastructure_invalid`,
`assisted`, `reported_unassisted_completed`. `reported_unassisted_completion_rate` is
`reported_unassisted_completed / started`, or `null` when `started` is 0. The summary repeats the
caller's labels; it does not validate a full schema, redact values, trust the labels, verify
native outcomes or receipts, establish cohort independence or comparability, enforce wall-clock
or I/O deadlines, report benchmark results, or guarantee throughput.

## Synthetic example

Run this from the repository root. The fixture is committed public synthetic data, not a native
run. The first call uses a missing path to show that default-off consumes nothing; only the
second call reads the fixture.

```python
import contextlib
import io

from tools.campaign_report import main

COHORT = "example-cohort"
MISSING = "/definitely/not/a/real/manifest.json"
FIXTURE = "docs/benchmarks/scenario-manifest.example.json"


def run(path, enabled):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        status = main([path, "--cohort", COHORT] + (["--enabled"] if enabled else []))
    assert status == 0, (status, stderr.getvalue())
    assert stderr.getvalue() == ""
    print(f"{'enabled' if enabled else 'disabled'} exit {status}")
    print(stdout.getvalue(), end="")


run(MISSING, enabled=False)
run(FIXTURE, enabled=True)
```

Expected disabled output (exit 0):

```json
{"eligibility": "not_evaluated", "enabled": false, "evaluated": false, "kind": "campaign_attempt_summary/1"}
```

Expected output for the public fixture (exit 0), a planned synthetic attempt:

```json
{"attempt_cap": 1000, "cohorts": [{"assisted": 0, "campaign_id": "synthetic-example", "censored": 0, "cohort_id": "example-cohort", "completed": 0, "failed": 0, "infrastructure_invalid": 0, "planned": 1, "reported_unassisted_completed": 0, "reported_unassisted_completion_rate": null, "started": 0, "supplied": 1, "timed_out": 0}], "counts": {"assisted": 0, "censored": 0, "completed": 0, "failed": 0, "infrastructure_invalid": 0, "planned": 1, "reported_unassisted_completed": 0, "started": 0, "supplied": 1, "timed_out": 0}, "eligibility": "not_evaluated", "enabled": true, "evaluated": true, "kind": "campaign_attempt_summary/1", "limits": {"independence_and_comparable_cohorts": "not_verified", "labels": "caller_supplied_not_certification", "missing_or_invalid_rows": "rejected_not_omitted", "rate_denominator": "reported_unassisted_completed/started", "receipts_and_native_completion": "not_verified", "statistics_and_authority": "no confidence interval, reliability threshold or authority"}, "native_verification": "not_evaluated", "reported_unassisted_completion_rate": null}
```
