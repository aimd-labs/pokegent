"""Opt-in read-only event page facade over caller-supplied, already-typed envelopes.

This module performs local validation and projection only. It owns no log, file, socket or stream,
and calls no runtime, server, MCP, provider, executor, callback or game input. The supplied
selection and events are trusted caller data; they are not cryptographically bound to a plugin ROM,
a running process or a transport, and this facade proves no such binding. Event names and outcomes
are data, never completion, verification or accuracy claims. Disabled unless the caller opts in.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import islice

from pokegent.events import EventEnvelope
from pokegent.games.plugin import PluginSelection

MAX_INPUT_ROWS = 10_000
MAX_RUN_ID_CHARS = 200
MAX_PAGE_ROWS = 100


class EventSdkRejected(ValueError):
    """The SDK refused construction or a read-page request."""


@dataclass(frozen=True, slots=True)
class EventRow:
    """One public event's metadata, detached from the caller's envelope and details."""

    id: str
    seq: int
    observed_at: str
    type: str
    scene_id: str | None
    outcome: str | None
    emulator_frame: int | None


@dataclass(frozen=True, slots=True)
class EventPage:
    """One immutable page plus the cursor for the next call."""

    rows: tuple[EventRow, ...]
    next_seq: int


class EventSdk:
    """Read bounded pages from one complete caller-owned iterable; no authority or I/O."""

    def __init__(self, selection: PluginSelection, *, enabled: bool = False) -> None:
        if not isinstance(selection, PluginSelection):
            raise TypeError("selection must be an admitted PluginSelection")
        if type(enabled) is not bool:
            raise TypeError("enabled must be exactly bool")
        self._selection = selection
        self._enabled = enabled

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise EventSdkRejected("event SDK is opt-in; construct it with enabled=True")

    @staticmethod
    def _validate_arguments(run_id: object, after_seq: object, limit: object) -> None:
        if (
            type(run_id) is not str
            or not run_id.strip()
            or run_id != run_id.strip()
            or len(run_id) > MAX_RUN_ID_CHARS
        ):
            raise EventSdkRejected(
                f"run_id must be a nonblank bounded string of at most {MAX_RUN_ID_CHARS} characters"
            )
        if type(after_seq) is not int or after_seq < 0:
            raise EventSdkRejected("after_seq must be a plain nonnegative int")
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_ROWS:
            raise EventSdkRejected(f"limit must be a plain int from 1 to {MAX_PAGE_ROWS}")

    @staticmethod
    def _scan(events: Iterable[EventEnvelope], run_id: str) -> list[EventEnvelope]:
        if isinstance(events, (str, bytes, bytearray, memoryview)):
            raise EventSdkRejected("events must be an iterable of EventEnvelope values")
        try:
            batch = tuple(islice(iter(events), MAX_INPUT_ROWS + 1))
        except TypeError as exc:
            raise EventSdkRejected("events must be a finite iterable") from exc
        if len(batch) > MAX_INPUT_ROWS:
            raise EventSdkRejected(f"events must contain at most {MAX_INPUT_ROWS} rows")
        requested: list[EventEnvelope] = []
        previous: int | None = None
        for row in batch:
            if not isinstance(row, EventEnvelope):
                raise EventSdkRejected("events must contain EventEnvelope values only")
            if row.run_id != run_id:
                continue
            if previous is not None and row.seq <= previous:
                raise EventSdkRejected(
                    "requested-run event sequences must strictly increase"
                )
            previous = row.seq
            requested.append(row)
        return requested

    def read_page(
        self,
        events: Iterable[EventEnvelope],
        *,
        run_id: str,
        after_seq: int = 0,
        limit: int = 100,
    ) -> EventPage:
        """Return up to ``limit`` public rows of ``run_id`` after ``after_seq`` frozen."""
        self._require_enabled()  # before touching a caller generator
        self._validate_arguments(run_id, after_seq, limit)
        requested = self._scan(events, run_id)
        eligible = [
            row
            for row in requested
            if row.seq > after_seq and row.public_visibility is True
        ]
        truncated = len(eligible) > limit  # another eligible public row actually exists
        selected = eligible[:limit]
        if truncated:
            next_seq = selected[-1].seq
        else:
            next_seq = after_seq
            for row in requested:
                if row.seq > after_seq:
                    next_seq = row.seq
        return EventPage(
            rows=tuple(
                EventRow(
                    id=row.id,
                    seq=row.seq,
                    observed_at=row.observed_at,
                    type=row.type,
                    scene_id=row.scene_id,
                    outcome=row.outcome,
                    emulator_frame=row.emulator_frame,
                )
                for row in selected
            ),
            next_seq=next_seq,
        )
