"""Conservative retention for autonomous run output (issue #59).

A 24/7 supervised unit creates one ``runs/<run-id>/`` directory per run with events,
calibration and a final state; calibration grows linearly with run length and nothing
bounds the on-disk footprint of *completed* runs.  This module prunes older completed
run directories under a parent while ALWAYS preserving:

* the **active / currently writing** run directory,
* the newest completed run(s),
* every sibling path still referenced by the recovery / current-state wiring (a
  ``current.state``-style pointer, a checkpoint, a save path the runtime may resume).

The module is **fail-closed by design** (hardened from independent safety review):

* it never follows a symlink — a symlinked run directory is never listed as a candidate
  (it might point outside the ``runs`` tree, and deleting "through" it could remove
  external ``final.state``/``summary``/``events`` files);
* ``_rmtree_shallow`` refuses to delete a directory that contains any subdirectory or
  symlink (e.g. a nested checkpoint block); it raises *before* unlinking anything, so a
  partially-pruned run can never happen;
* the CLI always feeds the live ``--state`` save and every ``current.state`` pointer
  target as ``references``, so ``--retain-runs N`` can never delete a state the runtime
  might resume from.

Deletion is opt-in (default keeps everything), never touches a directory without a
``summary.json`` completion marker, never deletes inside an active directory, and is pure
(no I/O beyond listing/paths) so it is trivially unit-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: The completion marker that proves a run directory finished (and is safe to consider
#: for pruning).  ``app.run`` writes ``summary.json`` only at the very end.
COMPLETION_MARKER = "summary.json"

#: A conservative default: keep the N most recent completed runs plus the active one.
DEFAULT_KEEP = 5


class RunRetentionError(ValueError):
    """Raised for an invalid retention configuration or an unsafe prune refused fail-closed."""


@dataclass
class PruneResult:
    """Outcome of a prune pass, with explicit diagnostics instead of a silent empty list.

    ``deleted`` are the run dirs that were actually removed.  ``failures`` pairs each
    run dir that was a prune candidate but could not be removed *safely* with a reason
    (nested content, symlink, unexpected I/O) — the caller can surface these rather than
    mistaking a non-delete for a success.
    """

    deleted: list[Path] = field(default_factory=list)
    failures: list[tuple[Path, str]] = field(default_factory=list)


def completed_run_dirs(parent: Path) -> list[Path]:
    """Completed run directories (with a ``summary.json`` marker), **excluding symlinks**.

    A child that is a symlink (even one resolving to a directory carrying a marker) is
    deliberately skipped: a symlinked run dir may point outside the ``runs`` tree and
    pruning it could delete external ``final.state``/``summary``/``events`` files.
    """
    if not parent.is_dir():
        return []
    out: list[Path] = []
    for child in parent.iterdir():
        if child.is_symlink():
            continue  # never prune through a symlink (fail-closed)
        if not child.is_dir():
            continue
        if (child / COMPLETION_MARKER).is_file():
            out.append(child)
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)  # newest first
    return out


def _is_referenced(path: Path, references: object) -> bool:
    """True if ``path`` is, or is a parent of, any reference (live save / current pointer)."""
    for ref in _iter_references(references):
        try:
            return path.resolve() in ref.resolve().parents or path.resolve() == ref.resolve()
        except OSError:
            if str(path) in str(ref):
                return True
    return False


def _iter_references(references: object):
    """Normalize a single path, a str, or a sequence of them into resolved Path refs."""
    if references is None:
        return
    items = references if isinstance(references, (list, tuple, set)) else [references]
    for item in items:
        if item is None:
            continue
        yield Path(str(item))


def prune_completed_runs(
    parent: Path,
    *,
    keep: int = DEFAULT_KEEP,
    active: Path | None = None,
    references: object = None,
) -> PruneResult:
    """Prune older COMPLETED run directories under ``parent`` (fail-closed).

    ``keep`` (default 5) is how many most-recent completed run dirs to retain.  The
    ``active`` directory and anything referenced by ``references`` (a live ``--state``
    save and/or a ``current.state``-style pointer) are always preserved, with or without
    a completion marker.

    Returns a :class:`PruneResult` listing what was deleted and (importantly) what could
    **not** be deleted — e.g. a run dir containing a nested checkpoint block or a
    symlink — so the caller gets diagnostics instead of a silent partial prune.  Raises
    :class:`RunRetentionError` when ``keep`` is invalid.
    """
    if keep < 0:
        raise RunRetentionError("keep must be >= 0")
    result = PruneResult()
    if parent is None:
        return result
    completed = completed_run_dirs(parent)
    doomed = completed[keep:]  # everything beyond the newest ``keep``
    for path in doomed:
        if active is not None and path.resolve() == Path(active).resolve():
            continue
        if _is_referenced(path, references):
            continue
        try:
            _rmtree_shallow(path)
        except (RunRetentionError, OSError) as exc:
            result.failures.append((path, str(exc)))
            continue
        result.deleted.append(path)
    return result


def _rmtree_shallow(path: Path) -> None:
    """Remove a run dir's own files, **fail-closed on untrusted content**.

    Never descends into (or unlinks through) nested directories, which may hold live
    checkpoint blocks.  If any child is a directory or symlink the whole deletion is
    refused BEFORE anything is unlinked, so a partially-pruned run can never occur.
    """
    children = list(path.iterdir())
    for child in children:
        if child.is_dir() or child.is_symlink():
            raise RunRetentionError(
                f"{path.name}: refuses delete (nested dir or symlink present: {child.name})"
            )
    for child in children:
        child.unlink(missing_ok=True)  # plain files only, guaranteed above
    path.rmdir()


_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def safe_run_id(name: str) -> str:
    """Sanitize a run-id name used as a directory component (defense in depth for pruning)."""
    if not _SAFE_RUN_ID.match(name):
        raise RunRetentionError(f"unsafe run-id component: {name!r}")
    return name
