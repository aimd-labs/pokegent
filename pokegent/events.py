"""Versioned event envelope and append-only JSONL log (spec 13.4).

* :class:`EventEnvelope` is the one record type: ``schema_rev``, ``id``, ``run_id``, ``seq``,
  ``observed_at``, ``emulator_frame``, ``type``, ``scene_id``, ``request_id``, ``provider``,
  ``outcome``, ``source``, ``public_visibility``, ``details``.
* :class:`EventType` lists the types of spec 13.4 (a minimum) plus the few the runner needs
  (``decision.forced``, ``escalation.requested``).
* :class:`EventLog` appends one JSON line per event, flushes after every line, optionally
  fsyncs, rotates by size into never-overwritten segments, and redacts secrets before anything
  is written. Ids are deterministic (``<run_id>:<seq>``); nothing here is random.
* :func:`read_events` reads segments plus the active file back in order.

The log is the replay record (spec 0.2 "Replay first"): it is append-only; rotation renames,
it never deletes unless ``keep_segments`` is set explicitly.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pokegent.frozen import FrozenMap, freeze

SCHEMA_REV = 1
TYPE_RE = re.compile(r"^[a-z][a-z_]*(?:\.[a-z][a-z_]*)+$")


class EventType(str, Enum):
    PLAN_BUDGET_EXCEEDED = "navigation.plan_budget_exceeded"
    RUNTIME_STARTED = "runtime.started"
    RUNTIME_PAUSED = "runtime.paused"
    RUNTIME_RESUMED = "runtime.resumed"
    RUNTIME_DEGRADED = "runtime.degraded"
    RUNTIME_RECOVERING = "runtime.recovering"
    RUNTIME_RECOVERED = "runtime.recovered"
    RUNTIME_BLOCKED = "runtime.blocked"
    RUNTIME_STOPPED = "runtime.stopped"
    ROM_PROFILE_VALIDATED = "rom.profile_validated"
    EMULATOR_STARTED = "emulator.started"
    EMULATOR_CRASHED = "emulator.crashed"
    EMULATOR_RELOADED = "emulator.reloaded"
    CHECKPOINT_SAVED = "checkpoint.saved"
    CHECKPOINT_RESTORED = "checkpoint.restored"
    CHECKPOINT_FAILED = "checkpoint.failed"
    SCENE_ENTERED = "scene.entered"
    SCENE_UNKNOWN = "scene.unknown"
    SCENE_UNSUPPORTED = "scene.unsupported"
    OPTIONS_BUILT = "options.built"
    OPTION_REJECTED = "option.rejected"
    DECISION_SYSTEMONE_REQUESTED = "decision.systemone.requested"
    DECISION_SYSTEMONE_COMPLETED = "decision.systemone.completed"
    DECISION_SYSTEMONE_TIMEOUT = "decision.systemone.timeout"
    DECISION_SYSTEMONE_INVALID = "decision.systemone.invalid"
    DECISION_SYSTEMONE_WAITING = "decision.systemone.waiting"
    DECISION_SYSTEMONE_RECOVERED = "decision.systemone.recovered"
    DECISION_AGENT_REQUESTED = "decision.agent.requested"
    DECISION_AGENT_COMPLETED = "decision.agent.completed"
    DECISION_AGENT_REJECTED = "decision.agent.rejected"
    LIVE_TURN = "pokegent.live_turn"
    LIVE_TURN_LEGACY = "pokejev.live_turn"
    # Deprecated wire aliases retained for one release. New events use ``agent``.
    ACTION_STARTED = "action.started"
    ACTION_INPUT = "action.input"
    ACTION_INTERRUPTED = "action.interrupted"
    ACTION_VERIFIED = "action.verified"
    ACTION_FAILED = "action.failed"
    PROGRESS_GOAL_CHANGED = "progress.goal_changed"
    PROGRESS_MILESTONE_COMPLETED = "progress.milestone_completed"
    PROGRESS_STUCK = "progress.stuck"
    PROGRESS_GAME_CLEARED = "progress.game_cleared"
    MEMORY_RECALLED = "memory.recalled"
    MEMORY_RETAINED = "memory.retained"
    MEMORY_DEGRADED = "memory.degraded"
    RUN_CREATED = "run.created"
    RUN_COMPLETED = "run.completed"
    RUN_ARCHIVED = "run.archived"
    RUN_RESTARTED = "run.restarted"
    # -- additions to the spec's minimum list -----------------------------------------------------
    DECISION_FORCED = "decision.forced"  # exactly one chooseable option: no decider was called
    ESCALATION_REQUESTED = "escalation.requested"  # the runner asked the escalation sink for help
    BATTLE_WON = "battle.won"
    BATTLE_LOST = "battle.lost"
    BATTLE_FLED = "battle.fled"
    BATTLE_CAUGHT = "battle.caught"


class EventEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_rev: int = SCHEMA_REV
    id: str
    run_id: str
    seq: int = Field(ge=1)
    observed_at: str
    emulator_frame: int | None = None
    type: str
    scene_id: str | None = None
    request_id: str | None = None
    provider: str | None = None
    outcome: str | None = None
    source: str = "runner"
    public_visibility: bool = False  # True: may appear on the read-only stream (spec 13.4)
    details: FrozenMap = Field(default_factory=dict)

    @model_validator(mode="after")
    def _well_formed(self) -> EventEnvelope:
        if not TYPE_RE.match(self.type):
            raise ValueError(f"malformed event type {self.type!r}")
        return self


# -- redaction ------------------------------------------------------------------------------------

_SECRET_KEY = re.compile(
    r"(api[_-]?key|secret|token|passw(or)?d|authorization|cookie|credential|bearer)", re.I
)
_SECRET_VALUE = re.compile(
    r"(\bBearer\s+[A-Za-z0-9._\-]{8,}|\b(sk|pk|ghp|gho|xox[bap])[-_][A-Za-z0-9_\-]{12,})"
)
REDACTED = "[REDACTED]"
_PAYLOAD_LEAK_KINDS = frozenset(
    {
        "fingerprint",
        "decision_id",
        "run_id",
        "executor_ref",
        "postcondition_ref",
        "source_task",
        "executor_arg",
    }
)


def default_redact(value: Any) -> Any:
    """Mask secret-looking keys and token-looking strings, recursively. Always applied."""
    if isinstance(value, Mapping):
        return {
            str(k): (REDACTED if _SECRET_KEY.search(str(k)) else default_redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [default_redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub(REDACTED, value)
    return value


Redactor = Callable[[dict[str, Any]], dict[str, Any]]
FsyncPolicy = Literal["never", "each", "interval"]


class EventSink(Protocol):
    def emit(
        self,
        type: EventType | str,
        *,
        scene_id: str | None = None,
        outcome: str | None = None,
        request_id: str | None = None,
        provider: str | None = None,
        emulator_frame: int | None = None,
        source: str | None = None,
        public_visibility: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> EventEnvelope: ...

    def close(self) -> None: ...


class _LogBase:
    """Builds envelopes: sequence, deterministic id, clock, redaction. Subclasses store them."""

    def __init__(
        self,
        run_id: str,
        *,
        source: str = "runner",
        redactor: Redactor | None = None,
        wall_clock: Callable[[], float] = time.time,
        start_seq: int = 1,
    ) -> None:
        self.run_id = run_id
        self.source = source
        self._redactor = redactor
        self._wall_clock = wall_clock
        self._seq = start_seq
        self._lock = threading.Lock()
        self._listeners: list[Callable[[EventEnvelope], None]] = []

    def add_listener(self, listener: Callable[[EventEnvelope], None]) -> None:
        """Call ``listener(envelope)`` after every stored event (the viewer's tap).

        Observation only and fail-open: it runs on the emitting thread after the event is stored
        and outside the log's lock, and an exception in it is swallowed (a broken viewer must never
        cost an event or stop the run).
        """
        self._listeners.append(listener)

    def emit(
        self,
        type: EventType | str,
        *,
        scene_id: str | None = None,
        outcome: str | None = None,
        request_id: str | None = None,
        provider: str | None = None,
        emulator_frame: int | None = None,
        source: str | None = None,
        public_visibility: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> EventEnvelope:
        type_value = EventType(type).value  # unknown types are a programming error
        raw_details = dict(details or {})
        clean = default_redact(raw_details)
        # Payload diagnostics intentionally expose the offending internal token to operators.
        # They are private events by default and this narrow exception does not weaken general
        # secret redaction for arbitrary event details.
        if (
            type_value
            in {
                EventType.DECISION_SYSTEMONE_INVALID.value,
                EventType.RUNTIME_DEGRADED.value,
            }
            and raw_details.get("kind") in _PAYLOAD_LEAK_KINDS
            and raw_details.get("token")
        ):
            clean["token"] = raw_details["token"]
        payload_leak = raw_details.get("payload_leak")
        if (
            type_value == EventType.ESCALATION_REQUESTED.value
            and isinstance(payload_leak, Mapping)
            and payload_leak.get("kind") in _PAYLOAD_LEAK_KINDS
            and payload_leak.get("token")
        ):
            clean["payload_leak"]["token"] = payload_leak["token"]
        if self._redactor is not None:
            clean = self._redactor(clean)
        with self._lock:
            seq = self._seq
            envelope = EventEnvelope(
                id=f"{self.run_id}:{seq}",
                run_id=self.run_id,
                seq=seq,
                observed_at=_iso(self._wall_clock()),
                emulator_frame=emulator_frame,
                type=type_value,
                scene_id=scene_id,
                request_id=request_id,
                provider=provider,
                outcome=outcome,
                source=source or self.source,
                public_visibility=public_visibility,
                details=freeze(clean),
            )
            self._store(envelope)
            self._seq += 1
        for listener in self._listeners:
            try:
                listener(envelope)
            except Exception:  # noqa: BLE001 - observers never influence the log or the run
                pass
        return envelope

    def _store(self, envelope: EventEnvelope) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def close(self) -> None:
        pass


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="milliseconds")


class MemoryEventLog(_LogBase):
    """In-memory sink with the same redaction and numbering (tests, dry runs)."""

    def __init__(self, run_id: str = "r-test", **kw: Any) -> None:
        super().__init__(run_id, **kw)
        self.events: list[EventEnvelope] = []

    def _store(self, envelope: EventEnvelope) -> None:
        self.events.append(envelope)

    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def of_type(self, *types: EventType | str) -> list[EventEnvelope]:
        wanted = {t.value if isinstance(t, EventType) else t for t in types}
        return [e for e in self.events if e.type in wanted]


def _dumps(envelope: EventEnvelope) -> str:
    return json.dumps(
        envelope.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


class EventLog(_LogBase):
    """Append-only JSONL file ``<directory>/<basename>.jsonl`` with size-bounded rotation.

    * one line per event, ``flush()`` after every line;
    * ``fsync="each"`` fsyncs every event, ``"interval"`` every ``fsync_every`` events and on
      rotation/close, ``"never"`` leaves it to the OS;
    * when the active file would exceed ``max_bytes`` it is renamed to
      ``<basename>-<NNNNNN>.jsonl`` (next free index, a target is never overwritten) and a new
      active file starts; ``seq`` continues across segments and across process restarts;
    * a torn last line (crash mid-write) is terminated with a newline on open, never rewritten;
    * ``keep_segments`` (default ``None`` = keep everything) bounds the number of rotated
      segments by deleting the oldest. Only set it deliberately: it deletes replay material.
    """

    def __init__(
        self,
        directory: str | os.PathLike[str],
        run_id: str,
        *,
        basename: str = "events",
        max_bytes: int = 8 * 1024 * 1024,
        fsync: FsyncPolicy = "never",
        fsync_every: int = 50,
        keep_segments: int | None = None,
        source: str = "runner",
        redactor: Redactor | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if max_bytes < 256:
            raise ValueError("max_bytes must be at least 256")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.basename = basename
        self.max_bytes = max_bytes
        self.fsync = fsync
        self.fsync_every = max(1, fsync_every)
        self.keep_segments = keep_segments
        self.path = self.directory / f"{basename}.jsonl"
        self.torn_on_open = False
        self.rotations = 0
        self._since_sync = 0
        super().__init__(
            run_id,
            source=source,
            redactor=redactor,
            wall_clock=wall_clock,
            start_seq=self._last_seq() + 1,
        )
        self._fh = open(self.path, "ab")
        if self.torn_on_open:
            self._fh.write(b"\n")
            self._fh.flush()
        self._size = self._fh.tell()

    # -- paths ----------------------------------------------------------------------------------

    def segments(self) -> list[Path]:
        return segment_paths(self.directory, self.basename)

    def _last_seq(self) -> int:
        """Highest seq already on disk (active file first, else the newest segment)."""
        for candidate in (self.path, *reversed(self.segments())):
            if not candidate.exists():
                continue
            data = candidate.read_bytes()
            if data and not data.endswith(b"\n") and candidate == self.path:
                self.torn_on_open = True
            for line in reversed(data.splitlines()):
                try:
                    return int(json.loads(line)["seq"])
                except (ValueError, KeyError, TypeError):
                    continue
        return 0

    # -- writing ----------------------------------------------------------------------------------

    def _store(self, envelope: EventEnvelope) -> None:
        line = (_dumps(envelope) + "\n").encode("ascii")
        if self._size > 0 and self._size + len(line) > self.max_bytes:
            self._rotate()
        self._fh.write(line)
        self._fh.flush()
        self._size += len(line)
        self._since_sync += 1
        if self.fsync == "each" or (
            self.fsync == "interval" and self._since_sync >= self.fsync_every
        ):
            self._sync()

    def _sync(self) -> None:
        os.fsync(self._fh.fileno())
        self._since_sync = 0

    def _rotate(self) -> None:
        self._fh.flush()
        if self.fsync != "never":
            self._sync()
        self._fh.close()
        existing = self.segments()
        index = (_segment_index(existing[-1], self.basename) + 1) if existing else 1
        target = self.directory / f"{self.basename}-{index:06d}.jsonl"
        if target.exists():  # cannot happen with the index above, but never overwrite
            raise FileExistsError(target)
        os.rename(self.path, target)
        self._fh = open(self.path, "ab")
        self._size = 0
        self.rotations += 1
        if self.keep_segments is not None:
            segs = self.segments()
            for old in segs[: max(0, len(segs) - self.keep_segments)]:
                old.unlink()

    def close(self) -> None:
        with self._lock:
            if self._fh.closed:
                return
            self._fh.flush()
            if self.fsync != "never":
                self._sync()
            self._fh.close()

    def __enter__(self) -> EventLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _segment_index(path: Path, basename: str) -> int:
    return int(path.name[len(basename) + 1 : -len(".jsonl")])


def segment_paths(directory: str | os.PathLike[str], basename: str = "events") -> list[Path]:
    """Rotated segments of a log in write order (oldest first), without the active file."""
    pattern = re.compile(rf"^{re.escape(basename)}-(\d{{6}})\.jsonl$")
    found = [p for p in Path(directory).iterdir() if pattern.match(p.name)]
    return sorted(found, key=lambda p: _segment_index(p, basename))


def read_events(
    directory: str | os.PathLike[str], basename: str = "events", *, skip_corrupt: bool = False
) -> Iterator[EventEnvelope]:
    """All events of a log in order: rotated segments, then the active file.

    A line that is not a valid envelope raises ``ValueError`` unless ``skip_corrupt`` is set
    (use it to read a log whose last line was torn by a crash).
    """
    directory = Path(directory)
    files = [*segment_paths(directory, basename)]
    active = directory / f"{basename}.jsonl"
    if active.exists():
        files.append(active)
    for file in files:
        with open(file, "rb") as fh:
            for lineno, raw in enumerate(fh, start=1):
                text = raw.strip()
                if not text:
                    continue
                try:
                    yield EventEnvelope.model_validate_json(text)
                except ValueError as exc:
                    if skip_corrupt:
                        continue
                    raise ValueError(f"{file.name}:{lineno}: {exc}") from exc
