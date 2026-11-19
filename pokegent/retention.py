"""Conservative retention for autonomous run output (issue #59).

A 24/7 supervised unit creates one ``runs/<run-id>/`` directory per run with events,
calibration and a final state; calibration grows linearly with run length and nothing
bounds the on-disk footprint of *completed* runs.  This module prunes older completed
run directories under a parent while ALWAYS preserving:

* the **active / currently writing** run directory,
* the newest completed run(s),
* every sibling path still referenced by the recovery / current-state wiring (a
  ``current.state``-style pointer, a checkpoint, a save path the runtime may resume).

Deletion is opt-in (default keeps everything), never touches a directory without a
``summary.json`` completion marker, never deletes inside an active directory, and is pure
(no I/O beyond listing/paths) so it is trivially unit-testable.
"""

from __future__ import annotations

import re
from pathlib import Path

#: The completion marker that proves a run directory finished (and is safe to consider
#: for pruning).  ``app.run`` writes ``summary.json`` only at the very end.
COMPLETION_MARKER = "summary.json"

#: A conservative default: keep the N most recent completed runs plus the active one.
DEFAULT_KEEP = 5


class RunRetentionError(ValueError):
    """Raised for an invalid retention configuration."""


def completed_run_dirs(parent: Path) -> list[Path]:
    """Completed run directories (those carrying a ``summary.json`` completion marker)."""
    if not parent.is_dir():
        return []
    out: list[Path] = []
    for child in parent.iterdir():
        if not child.is_dir():
            continue
        if (child / COMPLETION_MARKER).is_file():
            out.append(child)
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)  # newest first
    return out


def _is_referenced(path: Path, references: str | Path | None) -> bool:
    if references is None:
        return False
    ref = Path(str(references))
    try:
        return path.resolve() in {ref.resolve(), *ref.resolve().parents}
    except OSError:
        return str(path) in str(ref)


def prune_completed_runs(
    parent: Path,
    *,
    keep: int = DEFAULT_KEEP,
    active: Path | None = None,
    references: str | Path | None = None,
) -> list[Path]:
    """Prune older COMPLETED run directories under ``parent``.

    ``keep`` (default 5) is how many most-recent completed run dirs to retain.  The
    ``active`` directory and anything referenced by ``references`` (a current/recovery
    state pointer) are always preserved, even without a completion marker.  Returns the
    deleted paths.  Raises :class:`RunRetentionError` when ``keep`` is invalid.
    """
    if keep < 0:
        raise RunRetentionError("keep must be >= 0")
    if parent is None:
        return []
    completed = completed_run_dirs(parent)
    doomed = completed[keep:]  # everything beyond the newest ``keep``
    deleted: list[Path] = []
    for path in doomed:
        if active is not None and path.resolve() == Path(active).resolve():
            continue
        if _is_referenced(path, references):
            continue
        try:
            _rmtree_shallow(path)
        except OSError:
            continue
        deleted.append(path)
    return deleted


def _rmtree_shallow(path: Path) -> None:
    """Remove a run directory's own files (never descend into nested dirs that may be live)."""
    for child in list(path.iterdir()):
        if child.is_file():
            child.unlink(missing_ok=True)
    path.rmdir()


_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def safe_run_id(name: str) -> str:
    """Sanitize a run-id name used as a directory component (defense in depth for pruning)."""
    if not _SAFE_RUN_ID.match(name):
        raise RunRetentionError(f"unsafe run-id component: {name!r}")
    return name
