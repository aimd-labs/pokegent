"""Offline, read-only explicit counts of emitted objective progress events.

Only ``progress.goal_changed``, ``progress.stuck`` and ``progress.milestone_completed``
records are consumed. A repeated stuck event is another emitted event, not a deduplicated
episode and never a denominator. ``completed`` comes only from an exact boolean or null
``details.completed``, never from the event name. No action join, run/campaign completion,
elapsed duration, success rate, durable progress, accuracy or eligibility is inferred.
"""
import argparse
import json
import sys
from collections.abc import Iterator, Mapping
from typing import Any

MAX_LINE_BYTES, MAX_RECORDS, MAX_STRATA, MAX_TEXT = 1024 * 1024, 100_000, 10_000, 256
GOAL_CHANGED, MILESTONE, STUCK = (
    "progress.goal_changed", "progress.milestone_completed", "progress.stuck")
CONSUMED = {GOAL_CHANGED, MILESTONE, STUCK}
NOTE = ("Only explicitly emitted tracked progress records are counted. Each repeated stuck "
        "event is a separate event, not a deduplicated episode or a denominator. completed "
        "true/false/unknown comes only from details.completed as an exact bool or None, never "
        "the event name. Stuck reason labels are fixed known/unknown buckets; raw payloads are "
        "not echoed. No action joins, run/campaign completion, elapsed duration, success rate, "
        "durable progress, accuracy or eligibility is inferred.")


class ReportError(Exception):
    """A rejected input or exceeded bound; the message never contains the raw record."""


def _text(value: Any, path: str, line: int, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT:
        raise ReportError(f"{path}:{line}: field {field}")
    return value


def _revision(value: Any, path: str, line: int) -> int:
    if type(value) is not int or value < 0:
        raise ReportError(f"{path}:{line}: field revision")
    return value


def _lines(path: str, limit: int) -> Iterator[tuple[int, bytes]]:
    with open(path, "rb") as handle:
        for number, raw in enumerate(iter(lambda: handle.readline(limit + 1), b""), 1):
            body = raw[:-1] if raw.endswith(b"\n") else raw
            if len(body) > limit:
                raise ReportError(f"{path}:{number}: line exceeds {limit} bytes")
            yield number, body


def _stratum(rows, key, path, line, limit):
    row = rows.get(key)
    if row is None:
        if len(rows) >= limit:
            raise ReportError(f"{path}:{line}: strata limit {limit} exceeded")
        row = rows[key] = {
            "goal_changed_events": 0, "stuck_events": 0, "milestone_completed_events": 0,
            "completed": {"true": 0, "false": 0, "unknown": 0},
            "stuck_reason": {"known": 0, "unknown": 0},
        }
    return row


def build_report(paths: list[str], *, max_line_bytes: int = MAX_LINE_BYTES,
                 max_records: int = MAX_RECORDS, max_strata: int = MAX_STRATA) -> dict[str, Any]:
    strata: dict[tuple[str, str | None, int], dict[str, Any]] = {}
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
            event_type = record.get("type")
            if not isinstance(event_type, str):
                raise ReportError(f"{path}:{line}: field type")
            if event_type not in CONSUMED:
                continue
            details = record.get("details")
            if not isinstance(details, Mapping):
                raise ReportError(f"{path}:{line}: field details")
            run_id = _text(record.get("run_id"), path, line, "run_id")
            revision = _revision(details.get("revision"), path, line)
            raw_objective = details.get("objective_id")
            objective_id = None
            if raw_objective is not None:
                objective_id = _text(raw_objective, path, line, "objective_id")
            elif event_type != GOAL_CHANGED or revision != 0:
                raise ReportError(f"{path}:{line}: field objective_id")
            row = _stratum(strata, (run_id, objective_id, revision), path, line, max_strata)
            if event_type == GOAL_CHANGED:
                row["goal_changed_events"] += 1
            elif event_type == STUCK:
                row["stuck_events"] += 1
                reason = details.get("reason")
                label = "unknown"
                if reason is not None:
                    _text(reason, path, line, "reason")
                    label = "known"
                row["stuck_reason"][label] += 1
            else:
                completed = details.get("completed")
                if completed is not None and type(completed) is not bool:
                    raise ReportError(f"{path}:{line}: field completed")
                row["milestone_completed_events"] += 1
                row["completed"]["unknown" if completed is None else str(completed).lower()] += 1
            records += 1
    rows = []
    for key in sorted(strata, key=lambda k: (k[0], k[1] is not None, k[1] or "", k[2])):
        row = {"run_id": key[0], "objective_id": key[1], "revision": key[2]}
        row.update(strata[key])
        rows.append(row)
    return {"tool": "objective_events", "version": 1, "sources": len(paths),
            "scanned": scanned, "records": records, "no_records": records == 0,
            "strata": rows, "source_event_counts_only": True,
            "count_semantics": "emitted_events_not_episodes_or_denominator",
            "completed_semantics": "details_completed_bool_or_none_only",
            "stuck_reason_semantics": "fixed_known_unknown_buckets",
            "run_completion": "not_inferred", "campaign_completion": "not_inferred",
            "elapsed_duration": None, "success_rate": None, "durable_progress": None,
            "accuracy": None, "eligibility": "not_evaluated",
            "action_join": "not_performed", "reset_loop_causality": "not_claimed",
            "note": NOTE}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("events", metavar="EVENTS_JSONL")
    args = parser.parse_args(argv)
    try:
        report = build_report([args.events])
    except (ReportError, OSError) as exc:
        detail = (str(exc) if isinstance(exc, ReportError)
                  else f"{exc.filename or 'input'}: cannot read")
        print(f"objective_events: {detail}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
