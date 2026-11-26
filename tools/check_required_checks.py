#!/usr/bin/env python3
"""Evaluate a GitHub REST check-runs export for one exact head.

Local check evidence only; no review, mergeability or admin-policy claim."""

import argparse
import json
import re
import sys
from pathlib import Path

REPORT_KIND = "required-checks/1"
NOTE = "local check evidence only; not merge authorization"
HEAD_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
MAX_REQUIRED = 64
NAME_LIMIT = 256
MAX_CHECKS = 5000
MAX_FILE_BYTES = 1_000_000


class CheckInputError(ValueError):
    """Invalid or unreadable input; messages never echo record values."""


def _name(value):
    if not isinstance(value, str) or not 0 < len(value) <= NAME_LIMIT:
        raise CheckInputError("check names must be bounded non-empty strings")
    return value


def evaluate(document, head, required):
    if not isinstance(head, str) or not HEAD_PATTERN.fullmatch(head):
        raise CheckInputError("head must be 40 lowercase hex characters")
    if not isinstance(required, (list, tuple)) or not 0 < len(required) <= MAX_REQUIRED:
        raise CheckInputError("required names must be a bounded non-empty list")
    wanted = {_name(name) for name in required}
    if not isinstance(document, dict) or not isinstance(document.get("check_runs"), list):
        raise CheckInputError("input must be an object with a check_runs list")
    rows = document["check_runs"]
    if len(rows) > MAX_CHECKS:
        raise CheckInputError("check_runs exceeds the supported count")
    total_count = document.get("total_count")
    if type(total_count) is not int or not 0 <= total_count <= MAX_CHECKS:
        raise CheckInputError("total_count must be a bounded nonnegative integer")
    if total_count != len(rows):
        raise CheckInputError("check_runs count does not match total_count")
    exact = {}
    unknown = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise CheckInputError(f"check run {index} must be an object")
        try:
            name = _name(row.get("name"))
        except CheckInputError:
            raise CheckInputError(f"check run {index} has an invalid name") from None
        status, conclusion, sha = row.get("status"), row.get("conclusion"), row.get("head_sha")
        if not isinstance(status, str) or not (conclusion is None or isinstance(conclusion, str)):
            raise CheckInputError(f"check run {index} has an invalid status or conclusion")
        if sha is not None and not isinstance(sha, str):
            raise CheckInputError(f"check run {index} has an invalid head_sha")
        if name not in wanted:
            continue
        if sha is None:
            unknown.add(name)
        elif sha == head:
            exact.setdefault(name, []).append((status, conclusion))
    checks = []
    for name in sorted(wanted):
        found = exact.get(name, [])
        if name in unknown:
            state = "unknown"
        elif not found:
            state = "missing"
        elif len(found) > 1:
            state = "duplicate"
        elif found[0][0] != "completed":
            state = "pending"
        else:
            state = "success" if found[0][1] == "success" else "failed"
        checks.append({"name": name, "state": state})
    ready = all(entry["state"] == "success" for entry in checks)
    return {"kind": REPORT_KIND, "head": head, "ready": ready, "checks": checks, "note": NOTE}


def read_document(path):
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise CheckInputError("input file is unreadable") from None
    if len(data) > MAX_FILE_BYTES:
        raise CheckInputError("input file exceeds the supported size")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise CheckInputError("input is not valid UTF-8 JSON or is too deeply nested") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--head", required=True)
    parser.add_argument("--required", action="append", default=[], metavar="NAME")
    args = parser.parse_args(argv)
    try:
        report = evaluate(read_document(args.input), args.head, args.required)
    except CheckInputError as error:
        print(f"required-check evaluation failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
