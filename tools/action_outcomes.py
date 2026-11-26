"""Offline, read-only measured action outcomes from ONE runner event JSONL file.

An attributed outcome joins one accepted decision claim, one real ``action.started`` and one
terminal event by ``(run_id, request_id)`` with matching option ids. ``verified`` counts only
``details.verified is True`` and ``progressed`` only ``details.progressed is True``; no event
name, shadow agreement or provider result is proof. No provider, network, scan, write or authority.
"""
import argparse
import json
import sys
from collections.abc import Iterator, Mapping
from typing import Any

MAX_LINE_BYTES, MAX_RECORDS, MAX_PENDING, MAX_STRATA, MAX_TEXT = (
    1024 * 1024, 100_000, 10_000, 10_000, 256)
STATUSES = ("applied", "rejected", "interrupted", "timed_out", "uncertain")
DECISIONS = {"decision.agent.completed", "decision.systemone.completed", "decision.route"}
TERMINALS = {"action.verified", "action.failed", "action.interrupted", "option.rejected"}
NOTE = ("Denominator is attributed terminal actions (one accepted claim, one real action.started, "
        "matching run/request/option ids). verified/progressed count only explicit details true; "
        "an action.verified event is not proof. Accuracy, story completion, durable outcomes and "
        "eligibility are not evaluated.")


class ReportError(Exception):
    """Rejected input or exceeded bound; never echoes the raw record."""


def _text(value: Any, path: str, line: int, field: str, nullable: bool = False) -> Any:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT:
        raise ReportError(f"{path}:{line}: field {field}")
    return value


def _bool(value: Any, path: str, line: int, field: str) -> Any:
    if value is not None and type(value) is not bool:
        raise ReportError(f"{path}:{line}: field {field}")
    return value


def _optional(value: Any, path: str, line: int, field: str) -> str | None:
    """A nullable identity; an empty string from a defaulted ChoiceResult is unknown."""
    if value is None or value == "":
        return None
    return _text(value, path, line, field)


def _fields(event_type: Any, record: dict[str, Any], path: str, line: int):
    """Return ``(slot, run_id, request_id, values)`` or ``None`` for irrelevant records."""
    if not isinstance(event_type, str):
        raise ReportError(f"{path}:{line}: field type")
    if event_type not in DECISIONS | TERMINALS | {"action.started"}:
        return None
    details = record.get("details")
    if not isinstance(details, Mapping):
        raise ReportError(f"{path}:{line}: field details")
    if event_type in DECISIONS:
        if details.get("authority") == "shadow" or details.get("accepted") is False:
            return None
        if record.get("outcome") != "accepted":
            return None
        if "accepted" in details:
            _bool(details["accepted"], path, line, "accepted")
    run_id = _text(record.get("run_id"), path, line, "run_id")
    request_id = _text(record.get("request_id"), path, line, "request_id")
    if event_type in DECISIONS:
        return 0, run_id, request_id, (
            _text(details.get("option_id"), path, line, "option_id"),
            _optional(record.get("provider"), path, line, "provider"),
            _optional(details.get("model_id"), path, line, "model_id"))
    if event_type == "action.started":
        return 1, run_id, request_id, (
            _text(details.get("option_id"), path, line, "option_id"),
            _text(details.get("source"), path, line, "source"),
            _text(record.get("scene_id"), path, line, "scene_id", True))
    if event_type == "option.rejected" and "status" not in details:
        return None
    status = details.get("status")
    if status not in STATUSES:
        raise ReportError(f"{path}:{line}: field status")
    return 2, run_id, request_id, (
        _text(details.get("option_id"), path, line, "option_id"), status,
        _bool(details.get("verified"), path, line, "verified"),
        _bool(details.get("progressed"), path, line, "progressed"))


def _lines(path: str, limit: int) -> Iterator[tuple[int, bytes]]:
    with open(path, "rb") as handle:
        for number, raw in enumerate(iter(lambda: handle.readline(limit + 1), b""), 1):
            body = raw[:-1] if raw.endswith(b"\n") else raw
            if len(body) > limit:
                raise ReportError(f"{path}:{number}: line exceeds {limit} bytes")
            yield number, body


def build_report(paths: list[str], *, max_line_bytes: int = MAX_LINE_BYTES,
                 max_records: int = MAX_RECORDS, max_pending: int = MAX_PENDING,
                 max_strata: int = MAX_STRATA) -> dict[str, Any]:
    pending: dict[tuple[str, str], list[Any]] = {}
    finished: set[tuple[str, str]] = set()
    strata: dict[tuple[Any, ...], list[Any]] = {}
    scanned = records = uncorrelated = 0
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
            item = _fields(record.get("type"), record, path, line)
            if item is None:
                continue
            slot, run_id, request_id, values = item
            key = (run_id, request_id)
            if slot == 2:
                if key in finished:
                    raise ReportError(f"{path}:{line}: field request_id")
                finished.add(key)
                entry = pending.pop(key, None)
                if entry is None or None in entry:
                    uncorrelated += 1
                    continue
                claim, action = entry
                if not (claim[0] == action[0] == values[0]):
                    raise ReportError(f"{path}:{line}: field option_id")
                row_key = (key[0], action[2], action[1], claim[1], claim[2])
                row = strata.get(row_key)
                if row is None:
                    if len(strata) >= max_strata:
                        raise ReportError(f"{path}:{line}: strata limit {max_strata} exceeded")
                    row = strata[row_key] = [0, dict.fromkeys(STATUSES, 0),
                                             {"true": 0, "false": 0, "unknown": 0},
                                             {"true": 0, "false": 0, "unknown": 0}, 0]
                row[0] += 1
                row[1][values[1]] += 1
                row[2]["unknown" if values[2] is None else str(values[2]).lower()] += 1
                row[3]["unknown" if values[3] is None else str(values[3]).lower()] += 1
                row[4] += values[2] is True and values[3] is True
                records += 1
                continue
            if key in finished:
                continue
            entry = pending.get(key)
            if entry is None:
                if len(pending) >= max_pending:
                    raise ReportError(f"{path}:{line}: pending limit {max_pending} exceeded")
                entry = pending[key] = [None, None]
            if entry[slot] is not None:
                raise ReportError(f"{path}:{line}: field request_id")
            entry[slot] = values

    rows = []
    names = ("run_id", "scene_id", "source", "provider", "model_id")
    for key in sorted(strata, key=lambda k: tuple((v is not None, v or "") for v in k)):
        row = dict(zip(names, key))
        row.update(zip(("terminal_actions", "status", "verified", "progressed",
                        "verified_progressed"), strata[key]))
        rows.append(row)
    return {"tool": "action_outcomes", "version": 1, "sources": len(paths),
            "scanned": scanned, "records": records, "no_records": records == 0,
            "uncorrelated_terminals": uncorrelated, "strata": rows,
            "denominator": "attributed_terminal_actions",
            "verified_semantics": "details_verified_true_only", "accuracy": None,
            "story_completion": None, "durable_outcome": None,
            "eligibility": "not_evaluated", "note": NOTE}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("events", metavar="EVENTS_JSONL")
    args = parser.parse_args(argv)
    try:
        report = build_report([args.events])
    except (ReportError, OSError) as exc:
        print(f"action_outcomes: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
