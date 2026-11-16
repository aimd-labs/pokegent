"""Decision telemetry: ``decision.systemone.*`` events and the append-only calibration JSONL.

Events go through ``pokegent.events`` (redacted, sequenced). The runner already emits
``requested/completed/timeout/invalid`` around every ``Decider.decide`` call; use the
telemetry's ``emit_events`` only when the decider runs *outside* the runner (for example, a CLI).

The calibration file (spec 8.4, 16.3) holds one JSON object per decision attempt: scene, goal,
the reduced state sent, ALL criteria, the distribution, the choice, outcome, truncation, hashes
and sequence numbers. It never holds secrets, executor references or argument values, and has no
wall-clock timestamp: ``measured_ms`` is the only non-replayable field. Observed outcomes are
joined later by ``decision_id``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pokegent.events import EventSink, EventType
from pokegent.options.models import DecisionFrame

from .redact import Scrubber

CALIBRATION_SCHEMA = 1
log = logging.getLogger("pokegent.system_one")


class CalibrationLog:
    """Append-only JSONL. One flushed line per record; opened in append mode, never truncated.
    ``record_seq`` continues across restarts so the order is explicit without a clock."""

    def __init__(self, path: str | os.PathLike[str], *, scrubber: Scrubber | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._scrubber = scrubber or Scrubber()
        self._lock = threading.Lock()
        self._seq = self._last_seq() + 1
        self._fh = open(self.path, "ab")
        if self._fh.tell() > 0 and not self._ends_with_newline():  # torn last line: terminate it
            self._fh.write(b"\n")
            self._fh.flush()

    def _ends_with_newline(self) -> bool:
        with open(self.path, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) == b"\n"

    def _last_seq(self) -> int:
        if not self.path.exists():
            return 0
        last = 0
        with open(self.path, "rb") as fh:
            for line in fh:
                try:
                    last = int(json.loads(line)["record_seq"])
                except (ValueError, KeyError, TypeError):
                    continue
        return last

    def append(self, record: Mapping[str, Any]) -> dict[str, Any]:
        clean = self._scrubber.value(dict(record))
        with self._lock:
            clean = {"schema": CALIBRATION_SCHEMA, "record_seq": self._seq, **clean}
            line = json.dumps(clean, sort_keys=True, separators=(",", ":"), allow_nan=False)
            self._fh.write(line.encode("utf-8") + b"\n")
            self._fh.flush()
            self._seq += 1
        return clean

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()


def read_calibration(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    out = []
    with open(path, "rb") as fh:
        for line in fh:
            if line.strip():
                out.append(json.loads(line))
    return out


class DecisionTelemetry:
    """Fan-out of one decision's facts to events and the calibration file."""

    def __init__(
        self,
        *,
        events: EventSink | None = None,
        calibration: CalibrationLog | None = None,
        emit_events: bool = False,
        scrubber: Scrubber | None = None,
        provider_id: str = "http",
    ) -> None:
        self.events = events
        self.calibration = calibration
        self.emit_events = emit_events and events is not None
        self.scrubber = scrubber or Scrubber()
        self.provider_id = provider_id
        self.degraded = False  # True once a telemetry write failed

    # -- events -------------------------------------------------------------------------------

    def _emit(self, type_: EventType, frame: DecisionFrame, outcome: str | None, details: Any):
        if not self.emit_events or self.events is None:
            return
        try:
            self.events.emit(
                type_,
                scene_id=frame.scene_id,
                request_id=frame.decision_id,
                provider=self.provider_id,
                outcome=outcome,
                source="system_one",
                details=self.scrubber.event_details(details),
            )
        except Exception as exc:  # telemetry is fail-open: it never breaks a decision
            self._degrade("event", exc)

    def requested(self, frame: DecisionFrame, audit: Mapping[str, Any]) -> None:
        self._emit(
            EventType.DECISION_SYSTEMONE_REQUESTED,
            frame,
            None,
            {
                "criteria": list(frame.chooseable_ids()),
                "deadline_ms": frame.deadline_ms,
                "stages_hierarchical": audit.get("hierarchical"),
                "option_set_hash": audit.get("option_set_hash"),
                "snapshot_seq": frame.snapshot_seq,
                "state_est": audit.get("state_est_after"),
                "state_reduced": bool(audit.get("reduction")),
            },
        )

    def completed(self, frame: DecisionFrame, record: Mapping[str, Any]) -> None:
        self._emit(
            EventType.DECISION_SYSTEMONE_COMPLETED,
            frame,
            "ok",
            {
                "option_id": record.get("choice"),
                "model_id": record.get("model_id"),
                "confidence": record.get("confidence"),
                "truncated": record.get("truncated"),
                "stages": len(record.get("stages", ())),
                "attempts": record.get("attempts"),
                "usage_in": record.get("usage_in"),
                "usage_out": record.get("usage_out"),
                "measured_ms": record.get("measured_ms"),
            },
        )

    def timeout(self, frame: DecisionFrame, record: Mapping[str, Any]) -> None:
        self._emit(
            EventType.DECISION_SYSTEMONE_TIMEOUT,
            frame,
            "E_DECIDER_TIMEOUT",
            {"reason": record.get("error")},
        )

    def invalid(self, frame: DecisionFrame, record: Mapping[str, Any]) -> None:
        self._emit(
            EventType.DECISION_SYSTEMONE_INVALID,
            frame,
            record.get("error_code"),
            {"reason": record.get("error")},
        )

    # -- calibration --------------------------------------------------------------------------

    def record(self, record: Mapping[str, Any]) -> None:
        if self.calibration is not None:
            try:
                self.calibration.append(record)
            except Exception as exc:  # fail-open (spec 0.2 rule 5): the decision still stands
                self._degrade("calibration", exc)

    def _degrade(self, what: str, exc: Exception) -> None:
        self.degraded = True
        log.warning("System One %s telemetry failed: %s", what, self.scrubber.text(repr(exc)))

    def close(self) -> None:
        if self.calibration is not None:
            self.calibration.close()
