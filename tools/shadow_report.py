"""Offline, read-only report over existing runner shadow-observation event JSONL.

Only unaccepted (accepted false) authority=shadow decision.systemone.completed records
are consumed; other well-formed events are ignored. No provider call, directory
discovery, file write or eligibility grant. Agreement is not accuracy or accepted action.
"""
import argparse
import json
import math
import sys
from collections.abc import Iterator
from typing import Any

MAX_LINE_BYTES, MAX_RECORDS, MAX_STRATA, MAX_TEXT = 1024 * 1024, 100_000, 10_000, 256
NOTE = ("Shadow observations are unaccepted, zero-authority claims. Agreement is not accuracy; "
        "accuracy and verified progress are unknown and eligibility is not evaluated.")


class ReportError(Exception):
    """A rejected input or exceeded bound; the message never contains the raw record."""


def _text(value: Any, path: str, line: int, field: str, nullable: bool = False) -> Any:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT:
        raise ReportError(f"{path}:{line}: field {field}")
    return value


def _latency(value: Any, path: str, line: int) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        number = -1.0
    if (isinstance(value, bool) or type(value) not in (int, float)
            or not math.isfinite(number) or number < 0):
        raise ReportError(f"{path}:{line}: field measured_ms")
    return number


def _fields(record: dict[str, Any], path: str, line: int) -> dict[str, Any]:
    details = record["details"]
    agreement = details.get("agreement")
    for valid, field in (
        (details.get("accepted") is False, "accepted"),
        (record.get("outcome") in ("observed", "failed"), "outcome"),
        (agreement is None or isinstance(agreement, bool), "agreement"),
    ):
        if not valid:
            raise ReportError(f"{path}:{line}: field {field}")
    provider = details["provider_id"] if "provider_id" in details else record.get("provider")
    return {"run_id": _text(record.get("run_id"), path, line, "run_id"),
            "scene_id": _text(record.get("scene_id"), path, line, "scene_id", True),
            "provider": _text(provider, path, line, "provider", True),
            "model_id": _text(details.get("model_id"), path, line, "model_id", True),
            "outcome": record.get("outcome"), "agreement": agreement,
            "measured_ms": _latency(details.get("measured_ms"), path, line)}


def _lines(path: str, limit: int) -> Iterator[tuple[int, bytes]]:
    with open(path, "rb") as handle:
        for number, raw in enumerate(iter(lambda: handle.readline(limit + 1), b""), 1):
            body = raw[:-1] if raw.endswith(b"\n") else raw
            if len(body) > limit:
                raise ReportError(f"{path}:{number}: line exceeds {limit} bytes")
            yield number, body


def build_report(paths: list[str], *, max_line_bytes: int = MAX_LINE_BYTES,
                 max_records: int = MAX_RECORDS, max_strata: int = MAX_STRATA) -> dict[str, Any]:
    strata: dict[tuple[Any, ...], dict[str, Any]] = {}
    scanned = records = 0
    for path in paths:
        for line, body in _lines(path, max_line_bytes):
            if not body.strip():
                continue
            if scanned >= max_records:
                raise ReportError(f"{path}:{line}: record limit {max_records} exceeded")
            try:
                record = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                raise ReportError(f"{path}:{line}: malformed JSON") from None
            if not isinstance(record, dict):
                raise ReportError(f"{path}:{line}: field record")
            scanned += 1
            details = record.get("details")
            if (record.get("type") != "decision.systemone.completed"
                    or not isinstance(details, dict)
                    or details.get("authority") != "shadow"):
                continue
            fields = _fields(record, path, line)
            key = tuple(fields[name] for name in ("run_id", "scene_id", "provider", "model_id"))
            if key not in strata:
                if len(strata) >= max_strata:
                    raise ReportError(f"{path}:{line}: strata limit {max_strata} exceeded")
                strata[key] = {"records": 0, "attempts_observed": 0, "attempts_failed": 0,
                               "agreement": {"true": 0, "false": 0, "unknown": 0},
                               "samples": [], "latency_missing": 0}
            stratum = strata[key]
            stratum["records"] += 1
            stratum[f"attempts_{fields['outcome']}"] += 1
            label = "unknown" if fields["agreement"] is None else str(fields["agreement"]).lower()
            stratum["agreement"][label] += 1
            if fields["measured_ms"] is None:
                stratum["latency_missing"] += 1
            else:
                stratum["samples"].append(fields["measured_ms"])
            records += 1
    rows = []
    for key in sorted(strata, key=lambda k: tuple((v is not None, v or "") for v in k)):
        stratum = strata[key]
        samples = sorted(stratum["samples"])
        p50 = float(samples[max(1, math.ceil(.5 * len(samples))) - 1]) if samples else None
        p95 = float(samples[max(1, math.ceil(.95 * len(samples))) - 1]) if samples else None
        row = dict(zip(("run_id", "scene_id", "provider", "model_id"), key))
        row.update(records=stratum["records"], attempts_observed=stratum["attempts_observed"],
                   attempts_failed=stratum["attempts_failed"], agreement=stratum["agreement"],
                   measured_ms={"count": len(samples), "missing": stratum["latency_missing"],
                                "p50": p50, "p95": p95})
        rows.append(row)
    return {"tool": "shadow_report", "version": 1, "event_type": "decision.systemone.completed",
            "authority": "shadow", "sources": len(paths), "scanned": scanned,
            "records": records, "no_records": records == 0, "strata": rows,
            "agreement_semantics": "observed_agreement_not_accuracy",
            "action_acceptance": "not_inferred", "accuracy": None, "verified_progress": None,
            "eligibility": "not_evaluated", "note": NOTE}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", metavar="EVENTS_JSONL")
    args = parser.parse_args(argv)
    try:
        report = build_report(args.paths)
    except (ReportError, OSError) as exc:
        detail = (str(exc) if isinstance(exc, ReportError)
                  else f"{exc.filename or 'input'}: cannot read")
        print(f"shadow_report: {detail}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
