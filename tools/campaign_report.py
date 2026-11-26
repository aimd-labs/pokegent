"""Default-off local campaign manifest report (#138/R09): reads caller-named files only."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from itertools import islice
from pathlib import Path
from typing import Any

from pokegent.evaluation_campaign import (
    MAX_IDENTITY,
    CampaignAttempt,
    CampaignAttemptRejected,
    summarize_attempts,
)
from pokegent.evaluation_manifest import project_attempt

MAX_PATHS = 1000
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_AGGREGATE_BYTES = 8 * 1024 * 1024
_SCHEMA_PATH = Path(__file__).resolve().parents[1] / (
    "docs/benchmarks/scenario-manifest.schema.json"
)


class ReportError(Exception):
    """Rejected report input; the message never contains a path or record value."""


def _cohort(cohort_id: Any) -> None:
    if (type(cohort_id) is not str or not cohort_id.strip()
            or len(cohort_id) > MAX_IDENTITY):
        raise CampaignAttemptRejected(
            f"cohort_id must be nonblank and at most {MAX_IDENTITY} chars")


def _paths(paths: Any) -> list[str]:
    if isinstance(paths, (str, bytes, bytearray)):
        raise ReportError("paths must be an iterable of strings")
    try:
        materialized = list(islice(iter(paths), MAX_PATHS + 1))
    except TypeError:
        raise ReportError("paths must be an iterable of strings") from None
    if len(materialized) > MAX_PATHS:
        raise ReportError(f"paths exceeds {MAX_PATHS}")
    for path in materialized:
        if type(path) is not str or not path:
            raise ReportError("path must be a nonblank string")
    return materialized


def _read(index: int, path: str, remaining: int) -> tuple[Any, int]:
    limit = min(MAX_MANIFEST_BYTES, remaining)
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except (OSError, ValueError):
        raise ReportError(f"input {index} cannot be read") from None
    if len(data) > limit:
        raise ReportError(f"input {index} exceeds the byte bound")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ReportError(f"input {index} is not UTF-8") from None
    try:
        return json.loads(text), len(data)
    except (ValueError, RecursionError):
        raise ReportError(f"input {index} is not one JSON object") from None


def _build_validator() -> Any:
    """Resolve and check the bundled schema once per opted-in report."""
    try:
        from jsonschema import Draft202012Validator, SchemaError, ValidationError
    except ImportError:
        raise ReportError("manifest validation dependency is unavailable") from None
    try:
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, ValueError, SchemaError):
        raise ReportError("manifest validation schema is unavailable") from None
    return Draft202012Validator(schema), ValidationError


def _reject_non_finite(index: int, manifest: Any) -> None:
    """Refuse NaN/Infinity and float overflow conservatively before validation."""
    stack = [manifest]
    while stack:
        node = stack.pop()
        if isinstance(node, float) and not math.isfinite(node):
            raise ReportError(f"input {index} contains a non-finite number")
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def build_report(
    paths: Any,
    *,
    cohort_id: Any,
    enabled: bool = False,
    validate_manifests: bool = False,
) -> dict[str, Any]:
    """Project caller-named manifests only when ``enabled is True``; else remain inert."""
    if type(enabled) is not bool:
        raise CampaignAttemptRejected("enabled must be a bool")
    if type(validate_manifests) is not bool:
        raise CampaignAttemptRejected("validate_manifests must be a bool")
    if not enabled:
        return summarize_attempts((), enabled=False)
    _cohort(cohort_id)
    validator, validation_error = (
        _build_validator() if validate_manifests else (None, None)
    )
    attempts: list[CampaignAttempt] = []
    remaining = MAX_AGGREGATE_BYTES
    for index, path in enumerate(_paths(paths)):
        manifest, size = _read(index, path, remaining)
        if validator is not None:
            _reject_non_finite(index, manifest)
            try:
                validator.validate(manifest)
            except validation_error:
                raise ReportError(
                    f"input {index} is not a valid campaign manifest"
                ) from None
        remaining -= size
        attempts.append(project_attempt(manifest, cohort_id=cohort_id, enabled=True))
    return summarize_attempts(attempts, enabled=True)


def _write_report(destination: str, report: dict[str, Any]) -> None:
    """Create a new artifact from fully serialized text; never replace a target."""
    payload = json.dumps(report, sort_keys=True, allow_nan=False) + "\n"
    try:
        descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
    except OSError:
        raise ReportError("report artifact cannot be written") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", metavar="MANIFEST_JSON")
    parser.add_argument("--cohort", required=True)
    parser.add_argument("--enabled", action="store_true",
                        help="read the caller-named manifests (default off)")
    parser.add_argument("--output", metavar="REPORT_JSON",
                        help="write the report to a new exclusive file (default off)")
    parser.add_argument("--validate-manifests", action="store_true",
                        help="require bundled schema conformance (default off)")
    args = parser.parse_args(argv)
    if args.output is not None and not args.enabled:
        print("campaign_report: --output requires --enabled", file=sys.stderr)
        return 1
    if args.validate_manifests and not args.enabled:
        print("campaign_report: --validate-manifests requires --enabled", file=sys.stderr)
        return 1
    try:
        report = build_report(args.paths, cohort_id=args.cohort, enabled=args.enabled,
                              validate_manifests=args.validate_manifests)
    except (CampaignAttemptRejected, ReportError) as exc:
        print(f"campaign_report: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("campaign_report: input cannot be read", file=sys.stderr)
        return 1
    except RecursionError:
        print("campaign_report: input is not one JSON object", file=sys.stderr)
        return 1
    if args.output is not None:
        try:
            _write_report(args.output, report)
        except ReportError as exc:
            print(f"campaign_report: {exc}", file=sys.stderr)
            return 1
        return 0
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
