"""Offline, read-only report over existing schema-1 calibration JSONL files.

This is evidence tooling: it reads records already written by ``CalibrationLog`` and prints
bounded response-validity, attempt, truncation, identity and latency aggregates. With the explicit
``--choice-validity`` switch it also counts the optional ``framed_choice_valid`` field per stratum;
old records without it are ``unknown``. With ``--outcome-latency`` it adds count/p50/p95 of
``measured_ms`` per existing outcome in each stratum. It never calls a provider, reads game state,
writes files or grants eligibility. ``outcome: ok`` means only
that a model response was valid; it is not action acceptance, accuracy or verified progress.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Iterator
from typing import Any

MAX_LINE_BYTES = 1024 * 1024
MAX_RECORDS = 100_000
MAX_STRATA = 10_000
MAX_TEXT = 256
OUTCOMES = ("ok", "timeout", "invalid", "error")
KEY_FIELDS = ("source", "run_id", "scene_id", "provider_id", "provider_version",
              "model_id", "prompt_schema_version", "option_order")
NOTE = ("Response outcome ok means a valid model response only; it is not action acceptance, "
        "accuracy, verified progress or scene eligibility.")


class ReportError(Exception):
    """A rejected input or exceeded bound; the message never contains the raw record."""


def _fail(path: str, line: int, field: str, detail: str | None = None) -> None:
    suffix = f"field {field}" if detail is None else f"field {field} ({detail})"
    raise ReportError(f"{path}:{line}: {suffix}")


def _present(record: dict[str, Any], name: str, path: str, line: int) -> Any:
    if name not in record:
        _fail(path, line, name)
    return record[name]


def _field(record: dict[str, Any], name: str, path: str, line: int, *, text: bool = False,
           nullable: bool = False, minimum: int | None = None) -> Any:
    value = _present(record, name, path, line)
    if value is None and nullable:
        return None
    if text:
        if not isinstance(value, str) or len(value) > MAX_TEXT:
            _fail(path, line, name)
    elif (not isinstance(value, int) or isinstance(value, bool)
          or (minimum is not None and value < minimum)):
        _fail(path, line, name)
    return value


def _validate(record: dict[str, Any], path: str, line: int) -> dict[str, Any]:
    if _field(record, "schema", path, line) != 1:
        _fail(path, line, "schema", "unsupported schema")
    sequence = _field(record, "record_seq", path, line)
    measured = _present(record, "measured_ms", path, line)
    truncated = _present(record, "truncated", path, line)
    try:
        finite = (isinstance(measured, (int, float)) and not isinstance(measured, bool)
                  and math.isfinite(measured))
    except OverflowError:
        finite = False
    if not finite or measured < 0:
        _fail(path, line, "measured_ms")
    if not isinstance(truncated, bool):
        _fail(path, line, "truncated")
    outcome = _field(record, "outcome", path, line, text=True)
    if outcome not in OUTCOMES:
        _fail(path, line, "outcome")
    stages = _present(record, "stages", path, line)
    if not isinstance(stages, list):
        _fail(path, line, "stages")
    stage_models: list[str | None] = []
    for stage in stages:
        if not isinstance(stage, dict):
            _fail(path, line, "stages")
        model = stage.get("model_id")
        if model is not None and (not isinstance(model, str) or len(model) > MAX_TEXT):
            _fail(path, line, "stages")
        stage_models.append(model or None)
    fields = {name: _field(record, name, path, line, text=True) for name in (
        "run_id", "scene_id", "provider_id", "provider_version", "prompt_schema_version",
        "option_order")}
    fields.update(
        record_seq=sequence,
        model_id=_field(record, "model_id", path, line, text=True, nullable=True) or None,
        outcome=outcome,
        attempts=_field(record, "attempts", path, line, nullable=True, minimum=0),
        truncated=truncated,
        measured_ms=float(measured),
        stage_models=stage_models,
    )
    return fields


def _lines(path: str, max_line_bytes: int) -> Iterator[tuple[int, bytes]]:
    with open(path, "rb") as handle:
        number = 0
        while raw := handle.readline(max_line_bytes + 1):
            number += 1
            body = raw[:-1] if raw.endswith(b"\n") else raw
            if len(body) > max_line_bytes:
                raise ReportError(f"{path}:{number}: line exceeds {max_line_bytes} bytes")
            yield number, body


def _new_stratum() -> dict[str, Any]:
    return {"records": 0, "outcomes": {name: 0 for name in OUTCOMES},
            "attempts_total": 0, "attempts_missing": 0, "truncated": 0, "samples": [],
            "identity_missing": 0, "identity_mixed": 0, "stage_models": set()}


def _percentile(values: list[float], q: float) -> float:
    return float(values[max(1, math.ceil(q * len(values))) - 1])


def _key_sort(key: tuple[Any, ...]) -> tuple[Any, ...]:
    return (key[0], *key[1:5], (key[5] is not None, key[5] or ""), key[6], key[7])


def _eligibility_policy(ms: Any, rate: Any, p95: Any) -> tuple[int, float, float]:
    """Mirror S1 EligibilityPolicy rules, stdlib-only (no pydantic import)."""
    if not isinstance(ms, int) or isinstance(ms, bool) or ms < 1:
        raise ValueError("min_samples must be an int >= 1")
    if not isinstance(rate, (int, float)) or isinstance(rate, bool) or not 0 < rate <= 1:
        raise ValueError("min_valid_rate must be in (0, 1]")
    if (not isinstance(p95, (int, float)) or isinstance(p95, bool)
            or not math.isfinite(p95) or p95 <= 0):
        raise ValueError("max_p95_ms must be finite and > 0")
    return ms, float(rate), float(p95)


def scene_eligibility_report(scenes: dict[str, dict[str, Any]],
                             policy: tuple[Any, Any, Any]) -> dict[str, Any]:
    ms, rate, cap = _eligibility_policy(*policy)
    rows = []
    for name, scene in sorted(scenes.items()):
        samples = sorted(scene["samples"])
        p95 = _percentile(samples, 0.95) if samples else None
        if scene["records"] < ms:
            eligible, reason = "unknown", "below_min_samples"
        elif scene["unknown"] > 0:
            eligible, reason = "unknown", "unknown_choices"
        elif scene["valid"] / (scene["valid"] + scene["invalid"]) < rate:
            eligible, reason = False, "below_valid_rate"
        elif p95 is not None and p95 > cap:
            eligible, reason = False, "over_latency_cap"
        else:
            eligible, reason = True, "eligible"
        rows.append({"scene_id": scene["scene_id"], "records": scene["records"],
                     "valid": scene["valid"], "invalid": scene["invalid"],
                     "unknown": scene["unknown"],
                     "p50_ms": _percentile(samples, 0.50) if samples else None,
                     "p95_ms": p95, "eligible": eligible, "reason": reason})
    return {"policy": {"min_samples": ms, "min_valid_rate": rate, "max_p95_ms": cap},
            "scenes": rows, "accuracy": None, "no_progress": None, "authority_granted": False}


def build_report(paths: list[str], *, max_line_bytes: int = MAX_LINE_BYTES,
                 max_records: int = MAX_RECORDS, max_strata: int = MAX_STRATA,
                 choice_validity: bool = False,
                 outcome_latency: bool = False) -> dict[str, Any]:
    if type(outcome_latency) is not bool:
        raise ReportError("outcome_latency must be an exact bool")
    strata: dict[tuple[Any, ...], dict[str, Any]] = {}
    sequence_state: dict[tuple[int, str], dict[str, Any]] = {}
    records = 0
    for source, path in enumerate(paths):
        for line, body in _lines(path, max_line_bytes):
            if not body.strip():
                continue
            if records >= max_records:
                raise ReportError(f"{path}:{line}: record limit {max_records} exceeded")
            try:
                record = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                raise ReportError(f"{path}:{line}: malformed JSON") from None
            if not isinstance(record, dict):
                _fail(path, line, "record")
            fields = _validate(record, path, line)
            key = (source, fields["run_id"], fields["scene_id"], fields["provider_id"],
                   fields["provider_version"], fields["model_id"],
                   fields["prompt_schema_version"], fields["option_order"])
            if key not in strata:
                if len(strata) >= max_strata:
                    raise ReportError(f"{path}:{line}: strata limit {max_strata} exceeded")
                strata[key] = _new_stratum()
                if outcome_latency:
                    strata[key]["outcome_samples"] = {name: [] for name in OUTCOMES}
            stratum = strata[key]
            stratum["records"] += 1
            if choice_validity:
                if "framed_choice_valid" not in record:
                    bucket = "unknown"
                else:
                    framed = record["framed_choice_valid"]
                    if framed is None:
                        bucket = "unknown"
                    elif isinstance(framed, bool):
                        bucket = "valid" if framed else "invalid"
                    else:
                        _fail(path, line, "framed_choice_valid")
                counts = stratum.setdefault(
                    "choice_validity", {"valid": 0, "invalid": 0, "unknown": 0})
                counts[bucket] += 1
            stratum["outcomes"][fields["outcome"]] += 1
            if fields["attempts"] is None:
                stratum["attempts_missing"] += 1
            else:
                stratum["attempts_total"] += fields["attempts"]
            stratum["truncated"] += int(fields["truncated"])
            stratum["samples"].append(fields["measured_ms"])
            if outcome_latency:
                stratum["outcome_samples"][fields["outcome"]].append(fields["measured_ms"])
            present = [model for model in fields["stage_models"] if model]
            distinct = set(present)
            if (not fields["stage_models"] or len(present) != len(fields["stage_models"])
                    or fields["model_id"] is None):
                stratum["identity_missing"] += 1
            if len(distinct) > 1 or (fields["model_id"] is not None and distinct
                                     and fields["model_id"] not in distinct):
                stratum["identity_mixed"] += 1
            stratum["stage_models"].update(distinct)
            seq_key = (source, fields["run_id"])
            state = sequence_state.setdefault(
                seq_key, {"duplicates": 0, "non_monotonic": 0, "seen": set(), "high": None})
            sequence = fields["record_seq"]
            if sequence in state["seen"]:
                state["duplicates"] += 1
            elif state["high"] is not None and sequence < state["high"]:
                state["non_monotonic"] += 1
            state["seen"].add(sequence)
            state["high"] = sequence if state["high"] is None else max(state["high"], sequence)
            records += 1

    output_strata = []
    for key in sorted(strata, key=_key_sort):
        stratum = strata[key]
        samples = sorted(stratum.pop("samples"))
        row = dict(zip(KEY_FIELDS, key))
        row.update(
            records=stratum["records"], outcomes=stratum["outcomes"],
            attempts_total=stratum["attempts_total"],
            attempts_missing=stratum["attempts_missing"],
            truncated=stratum["truncated"],
            measured_ms={"count": len(samples),
                         "p50": _percentile(samples, 0.50) if samples else None,
                         "p95": _percentile(samples, 0.95) if samples else None},
            identity_missing=stratum["identity_missing"],
            identity_mixed=stratum["identity_mixed"],
            distinct_stage_models=len(stratum["stage_models"]),
        )
        if choice_validity:
            row["choice_validity"] = stratum.get(
                "choice_validity", {"valid": 0, "invalid": 0, "unknown": 0})
        if outcome_latency:
            row["outcome_measured_ms"] = {}
            for name in OUTCOMES:
                values = sorted(stratum["outcome_samples"][name])
                row["outcome_measured_ms"][name] = {
                    "count": len(values),
                    "p50": _percentile(values, 0.50) if values else None,
                    "p95": _percentile(values, 0.95) if values else None,
                }
        output_strata.append(row)
    diagnostics = [
        {"source": source, "run_id": run_id, "duplicate_count": state["duplicates"],
         "non_monotonic_count": state["non_monotonic"]}
        for (source, run_id), state in sorted(sequence_state.items())
        if state["duplicates"] or state["non_monotonic"]]
    return {
        "tool": "calibration_report", "version": 1,
        "calibration_schema": 1, "outcome_semantics": "response_validity",
        "sources": len(paths), "records": records, "no_records": records == 0,
        "strata": output_strata, "sequence_diagnostics": diagnostics,
        "labels": "none", "accuracy": None, "disagreement": None,
        "verified_progress": None, "eligibility": "not_evaluated", "note": NOTE,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", metavar="CALIBRATION_JSONL")
    parser.add_argument("--choice-validity", action="store_true",
                        help="count logged framed-choice validity per stratum (default off)")
    parser.add_argument("--outcome-latency", action="store_true",
                        help="report per-outcome measured latency per stratum (default off)")
    args = parser.parse_args(argv)
    try:
        report = build_report(args.paths, choice_validity=args.choice_validity,
                              outcome_latency=args.outcome_latency)
    except ReportError as exc:
        print(f"calibration_report: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"calibration_report: {exc.filename or 'input'}: cannot read", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
