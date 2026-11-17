"""``ObjectiveStore``: the objective file, its append-only history, and the safe way to change it.

agent (an external process) hands the game an objective in exactly one of two ways: it calls
:meth:`ObjectiveStore.propose` (in process), or it writes the JSON file with the same discipline
(temp file in the same directory, ``fsync``, ``rename``; :func:`atomic_write_json`). The game reads
the file strictly: size-bounded, schema-validated (unknown keys are errors), semantically validated
(names resolve), revision-monotonic. **A bad file never crashes the loop and never changes what the
game is doing**: the last valid objective stays active and a typed :class:`StoreWarning` is
recorded.

History: ``<file>.history.jsonl``, append-only, one JSON object per line with a sequence number, the
event (``accepted`` / ``external`` / ``rejected`` / ``warning``) and the objective revision. It is
the replay material for "why did the game do this".

Concurrency: writers serialise on ``<file>.lock`` (``flock``); readers need no lock because the file
is replaced atomically. Two proposals for the same next revision: exactly one is accepted, the other
gets ``stale_revision``.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import threading
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pokegent.goals.models import Objective
from pokegent.goals.validate import (
    MapDirectory,
    Reason,
    ResolvedObjective,
    Validation,
    validate,
)

MAX_FILE_BYTES = 64 * 1024
logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


def atomic_write_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    """Write ``payload`` so a reader sees the old file or the new one, never a torn one.

    Temp file in the SAME directory (rename must not cross a filesystem), ``fsync`` of the file,
    ``os.replace``, ``fsync`` of the directory. This is what agent should call (or imitate).
    """
    target = Path(path)
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    dir_fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


@dataclass(frozen=True)
class StoreWarning:
    """Something wrong with the file that did NOT change the active objective."""

    code: str  # unreadable_file | file_too_large | schema_invalid | stale_revision | expired ...
    detail: str
    at: str
    revision: int | None = None
    reasons: tuple[Reason, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "detail": self.detail,
            "at": self.at,
            "revision": self.revision,
            "reasons": [r.as_dict() for r in self.reasons],
        }


@dataclass(frozen=True)
class Proposal:
    accepted: bool
    reasons: tuple[Reason, ...] = ()
    objective: Objective | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "revision": self.objective.revision if self.objective else None,
            "reasons": [r.as_dict() for r in self.reasons],
        }


@dataclass
class _Active:
    objective: Objective | None = None
    resolved: ResolvedObjective | None = None


@dataclass
class ObjectiveStore:
    path: Path
    history_path: Path | None = None
    clock: Callable[[], datetime] = utc_now
    directory: MapDirectory | None = None
    max_bytes: int = MAX_FILE_BYTES
    max_warnings: int = 64
    warnings: deque[StoreWarning] = field(default_factory=deque)
    _active: _Active = field(default_factory=_Active)
    _signature: tuple[int, int, int] | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _seq: int = 0
    _expired_noted: tuple[str, int] | None = None
    _missing_noted: bool = False
    _lock_depth: int = 0
    _warned: int = 0
    _revision_floor: int = 0
    _committed_completions: set[tuple[str, int]] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if self.history_path is None:
            self.history_path = self.path.with_name(self.path.name + ".history.jsonl")
        self.history_path = Path(self.history_path)
        self.warnings = deque(maxlen=self.max_warnings)
        self._seq = self._count_history()
        self._revision_floor = self._history_revision()
        self._committed_completions = {
            (str(row["objective_id"]), int(row["revision"]))
            for row in self.history()
            if row.get("event") == "completed"
            and isinstance(row.get("objective_id"), str)
            and isinstance(row.get("revision"), int)
        }

    def _history_revision(self) -> int:
        return max(
            (
                int(row["revision"])
                for row in self.history()
                if row.get("event") in ("accepted", "external", "completed", "completion_cleared")
                and isinstance(row.get("revision"), int)
            ),
            default=0,
        )

    # -- reading --------------------------------------------------------------------------------

    def current(self) -> Objective | None:
        """The active objective, or ``None`` (goal-less default: the game goes on without one)."""
        with self._lock:
            self.poll()
            obj = self._active.objective
            if obj is not None and obj.expires_at is not None and obj.expires_at <= self.clock():
                note = (obj.objective_id, obj.revision)
                if self._expired_noted != note:
                    self._expired_noted = note
                    self._warn(
                        "expired", f"objective {obj.objective_id} r{obj.revision} expired",
                        obj.revision,
                    )  # fmt: skip
                return None
            return obj

    def resolved(self) -> ResolvedObjective | None:
        """The active objective with names resolved to ids (None when none/expired)."""
        with self._lock:
            return self._active.resolved if self.current() is not None else None

    def revision(self) -> int:
        with self._lock:
            self.poll()
            return max(
                self._active.objective.revision if self._active.objective else 0,
                self._revision_floor,
            )

    def get(self, objective_id: str | None) -> Objective | None:
        """The active objective if its id matches, else the newest history entry with that id."""
        if objective_id is None:
            return None
        active = self.current()
        if active is not None and active.objective_id == objective_id:
            return active
        for entry in reversed(self.history()):
            obj = entry.get("objective")
            if (
                entry.get("event") in ("accepted", "external")
                and obj
                and (obj.get("objective_id") == objective_id)
            ):
                parsed, _ = _safe_parse(obj)
                if parsed is not None:
                    return parsed
        return None

    def poll(self) -> None:
        """Re-read the file if it changed. Never raises; a bad file becomes a warning."""
        with self._lock:
            try:
                st = os.stat(self.path)
            except FileNotFoundError:
                if self._active.objective is not None and not self._missing_noted:
                    self._missing_noted = True
                    self._warn("file_missing", "objective file disappeared; keeping the last one")
                return
            except OSError as exc:
                self._warn("unreadable_file", f"{type(exc).__name__}: {exc}")
                return
            self._missing_noted = False
            signature = (st.st_mtime_ns, st.st_size, st.st_ino)
            if signature == self._signature:
                return
            self._signature = signature
            self._ingest(st.st_size)

    def _ingest(self, size: int) -> None:
        if size > self.max_bytes:
            self._warn("file_too_large", f"{size} bytes > {self.max_bytes}")
            return
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            self._warn("unreadable_file", f"{type(exc).__name__}: {exc}")
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            self._warn("unreadable_file", f"not valid JSON: {exc}"[:300])
            return
        if not isinstance(data, dict):
            self._warn("schema_invalid", "the file must hold one JSON object")
            return
        verdict = validate(data, now=self.clock(), directory=self.directory)
        if not verdict.ok or verdict.objective is None or verdict.resolved is None:
            rev = data.get("revision") if isinstance(data.get("revision"), int) else None
            self._warn(
                verdict.reasons[0].code if verdict.reasons else "schema_invalid",
                "; ".join(f"{r.field}: {r.detail}" for r in verdict.reasons)[:300],
                rev,
                verdict.reasons,
            )
            self._log("warning", rev, data.get("objective_id"), verdict.reasons, data)
            return
        new = verdict.objective
        if any(
            row.get("event") == "completion_cleared"
            and row.get("objective_id") == new.objective_id
            and row.get("revision") == new.revision
            for row in self.history()
        ):
            self._warn("stale_revision", "completed objective was already cleared", new.revision)
            return
        active = self._active.objective
        current_rev = max(active.revision if active is not None else 0, self._revision_floor)
        self._warn_forbid_drop(active, new)
        if (
            new.set_by == "script"
            and active is not None
            and active.set_by != "script"
            and not (active.set_by == "operator" and self._completion_committed(active))
        ):
            self._warn(
                "source_priority",
                f"scripted objective {new.objective_id!r} cannot supersede "
                f"{active.set_by} objective {active.objective_id!r}",
                new.revision,
            )
            self._restore_active_file(active)
            return
        if (
            active is not None
            and active.set_by == "operator"
            and new.set_by != "operator"
            and not self._completion_committed(active)
        ):
            self._warn(
                "source_priority",
                f"{new.set_by} objective {new.objective_id!r} cannot supersede operator "
                f"objective {active.objective_id!r}",
                new.revision,
            )
            self._restore_active_file(active)
            return
        if new.revision <= current_rev:
            if active is not None and new == active:
                return  # the file we already hold
            known = any(
                row.get("event") in ("accepted", "external")
                and row.get("revision") == new.revision
                and row.get("objective") == new.model_dump(mode="json")
                for row in self.history()
            )
            if active is None and known:
                self._activate(verdict)
                return
            self._warn(
                "stale_revision",
                f"file revision {new.revision} <= active {current_rev}; ignored",
                new.revision,
            )
            return
        self._activate(verdict)
        with self._lock, self._file_lock():
            seen = any(
                h.get("event") in ("accepted", "external") and h.get("revision") == new.revision
                for h in self.history()
            )
            if not seen:  # another process (or store) may have logged this very revision already
                self._log(
                    "external", new.revision, new.objective_id, (), new.model_dump(mode="json")
                )

    def _activate(self, verdict: Validation) -> None:
        assert verdict.objective is not None and verdict.resolved is not None
        self._active = _Active(verdict.objective, verdict.resolved)
        self._revision_floor = max(self._revision_floor, verdict.objective.revision)
        self._expired_noted = None

    def _restore_active_file(self, active: Objective) -> None:
        """Undo a direct external overwrite that violated source ownership."""
        try:
            atomic_write_json(self.path, active.model_dump(mode="json"))
            st = os.stat(self.path)
            self._signature = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError as exc:
            self._warn(
                "source_priority_restore_failed",
                f"could not restore active objective: {type(exc).__name__}: {exc}",
                active.revision,
            )

    # -- validation / proposals -----------------------------------------------------------------

    def validate(self, data: Mapping[str, Any] | Objective) -> Validation:
        return validate(data, now=self.clock(), directory=self.directory)

    def _warn_forbid_drop(self, active: Objective | None, new: Objective) -> None:
        """Warn when a replacement omits hard policy before ownership rules reject or accept it."""
        if active is None:
            return
        dropped = tuple(item for item in active.forbid if item not in new.forbid)
        if not dropped:
            return
        detail = (
            f"objective {new.objective_id!r} revision {new.revision} omitted forbid {dropped}; "
            f"supersedes {active.objective_id!r} revision {active.revision}"
        )
        logger.warning("objective forbid dropped by replacement: %s", detail)
        self._warn("forbid_dropped", detail, new.revision)

    def propose(
        self, data: Mapping[str, Any] | Objective, *, expected_revision: int | None = None
    ) -> Proposal:
        """Validate and, if acceptable, make ``data`` the active objective (atomic file write +
        history). ``expected_revision`` is a compare-and-set on the revision agent last saw."""
        with self._lock, self._file_lock():
            self._signature = None  # force a re-read inside the lock: writers serialise here
            self.poll()
            self._revision_floor = max(self._revision_floor, self._history_revision())
            verdict = self.validate(data)
            raw = data.model_dump(mode="json") if isinstance(data, Objective) else dict(data)
            if not verdict.ok or verdict.objective is None:
                rev = raw.get("revision") if isinstance(raw.get("revision"), int) else None
                self._log("rejected", rev, raw.get("objective_id"), verdict.reasons, raw)
                return Proposal(False, verdict.reasons, verdict.objective)
            new = verdict.objective
            active = self._active.objective
            current_rev = max(active.revision if active else 0, self._revision_floor)
            self._warn_forbid_drop(active, new)
            problems: list[Reason] = []
            if (
                new.set_by == "script"
                and active is not None
                and active.set_by != "script"
                and not (active.set_by == "operator" and self._completion_committed(active))
            ):
                problems.append(
                    Reason(
                        "source_priority",
                        "set_by",
                        f"a scripted objective cannot supersede {active.set_by} objective "
                        f"{active.objective_id!r} (revision {active.revision})",
                    )
                )
            if expected_revision is not None and expected_revision != current_rev:
                problems.append(
                    Reason(
                        "stale_revision", "revision",
                        f"expected revision {expected_revision}, the game holds {current_rev}",
                    )
                )  # fmt: skip
            if (
                active is not None
                and active.set_by == "operator"
                and new.set_by != "operator"
                and not self._completion_committed(active)
            ):
                problems.append(
                    Reason(
                        "source_priority",
                        "set_by",
                        f"a {new.set_by} objective cannot supersede operator objective "
                        f"{active.objective_id!r} (revision {active.revision})",
                    )
                )
            if new.revision <= current_rev:
                problems.append(
                    Reason(
                        "stale_revision", "revision",
                        f"revision {new.revision} must be greater than {current_rev}",
                    )
                )  # fmt: skip
            if problems:
                self._log("rejected", new.revision, new.objective_id, problems, raw)
                return Proposal(False, tuple(problems), new)
            atomic_write_json(self.path, new.model_dump(mode="json"))
            st = os.stat(self.path)
            self._signature = (st.st_mtime_ns, st.st_size, st.st_ino)
            self._activate(verdict)
            self._log("accepted", new.revision, new.objective_id, (), new.model_dump(mode="json"))
            return Proposal(True, (), new)

    def _completion_committed(self, objective: Objective) -> bool:
        return (objective.objective_id, objective.revision) in self._committed_completions

    def commit_completion(
        self, *, objective_id: str, expected_revision: int, clear_completed: bool = False
    ) -> bool:
        """Commit fresh-RAM completion; optionally clear that exact revision in the same lock.

        Completion authorizes retiring even an operator objective. Manual ``clear`` retains
        its separate authority guard. A queued or concurrently proposed successor wins.
        """
        with self._lock, self._file_lock():
            self._signature = None
            self.poll()
            active = self._active.objective
            if (
                active is None
                or active.objective_id != objective_id
                or active.revision != expected_revision
            ):
                return False
            key = (active.objective_id, active.revision)
            self._committed_completions.update(
                (row["objective_id"], row["revision"])
                for row in self.history()
                if row.get("event") == "completed"
                and isinstance(row.get("objective_id"), str)
                and isinstance(row.get("revision"), int)
            )
            if key not in self._committed_completions:
                if not self._log(
                    "completed",
                    active.revision,
                    active.objective_id,
                    (),
                    active.model_dump(mode="json"),
                ):
                    return False
                self._committed_completions.add(key)
            if clear_completed:
                # Persist the tombstone first: a crash before unlink must not revive this file.
                if not self._log(
                    "completion_cleared", active.revision, active.objective_id, (), None
                ):
                    return False
                self._remove_active()
            return True

    def _remove_active(self) -> None:
        if self.path.exists():
            self.path.unlink()
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        self._active = _Active()
        self._signature = None
        self._missing_noted = True

    def clear(
        self, *, expected_revision: int | None = None, set_by: str | None = None
    ) -> dict[str, Any]:
        """Remove the active file without losing its revision history.

        This is for a supervisor handing an ended objective back to a location-selected script.
        The compare-and-set prevents a concurrent agent write from being deleted.
        """
        with self._lock, self._file_lock():
            self._signature = None
            self.poll()
            current = self.revision()
            if expected_revision is None:
                return {
                    "cleared": False,
                    "revision": current,
                    "reason": "expected_revision_required",
                }
            if expected_revision != current:
                return {
                    "cleared": False,
                    "revision": current,
                    "reason": "stale_revision",
                }
            active = self._active.objective
            if active is not None and active.set_by == "operator" and set_by != "operator":
                detail = (
                    f"clear request would drop operator forbid from {active.objective_id!r} "
                    f"revision {active.revision}; request set_by={set_by!r}"
                )
                logger.warning("objective forbid dropped by clear request: %s", detail)
                self._warn("forbid_dropped", detail, active.revision)
                return {
                    "cleared": False,
                    "revision": current,
                    "reason": "forbid_dropped",
                }
            self._remove_active()
            self._log("cleared", current, None, (), None)
            return {"cleared": True, "revision": current}

    # -- history ----------------------------------------------------------------------------------

    def history(self) -> list[dict[str, Any]]:
        assert self.history_path is not None
        if not self.history_path.exists():
            return []
        out = []
        with open(self.history_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue  # a torn last line (crash mid-append) is skipped, never fatal
        return out

    def _count_history(self) -> int:
        return len(self.history())

    def _log(
        self,
        event: str,
        revision: int | None,
        objective_id: Any,
        reasons: Any,
        objective: Any,
    ) -> bool:
        assert self.history_path is not None
        with self._lock, self._file_lock():
            self._seq = self._count_history() + 1  # the file is the truth (other processes append)
            entry = {
                "seq": self._seq,
                "at": self.clock().isoformat(),
                "event": event,
                "revision": revision,
                "objective_id": objective_id if isinstance(objective_id, str) else None,
                "reasons": [r.as_dict() for r in reasons],
                "objective": objective if isinstance(objective, dict) else None,
            }
            line = json.dumps(entry, sort_keys=True, ensure_ascii=True, default=str) + "\n"
            try:
                fd = os.open(self.history_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
                try:
                    os.write(fd, line.encode("ascii"))
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as exc:  # replay material, not a reason to stop the game
                self._warn("history_unwritable", f"{type(exc).__name__}: {exc}")
                return False
            return True

    # -- plumbing ---------------------------------------------------------------------------------

    def _warn(
        self,
        code: str,
        detail: str,
        revision: int | None = None,
        reasons: tuple[Reason, ...] = (),
    ) -> None:
        self._warned += 1
        self.warnings.append(
            StoreWarning(code, detail, self.clock().isoformat(), revision, tuple(reasons))
        )

    @property
    def warning_count(self) -> int:
        """Warnings recorded since the store was created (``warnings`` keeps only the newest)."""
        return self._warned

    def warnings_since(self, seen: int) -> tuple[StoreWarning, ...]:
        """The warnings recorded after the first ``seen`` ones (bounded by ``max_warnings``)."""
        with self._lock:
            new = self._warned - seen
            if new <= 0:
                return ()
            kept = list(self.warnings)
            return tuple(kept[-new:] if new < len(kept) else kept)

    @contextlib.contextmanager
    def _file_lock(self) -> Iterator[None]:
        """Cross-process writer lock; re-entrant inside one store (callers hold ``self._lock``)."""
        if self._lock_depth > 0:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        lock_path = self.path.with_name(self.path.name + ".lock")
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._lock_depth = 1
            yield
        finally:
            self._lock_depth = 0
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def last_warning(self) -> StoreWarning | None:
        return self.warnings[-1] if self.warnings else None


def _safe_parse(data: Mapping[str, Any]) -> tuple[Objective | None, tuple[Reason, ...]]:
    from pokegent.goals.validate import parse

    return parse(data)
