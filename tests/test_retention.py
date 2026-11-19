"""Retention for autonomous run output (issue #59)."""
from pathlib import Path

import pytest

from pokegent.retention import (
    RunRetentionError,
    completed_run_dirs,
    prune_completed_runs,
    safe_run_id,
)


@pytest.fixture
def parent(tmp_path):
    p = tmp_path / "runs"
    p.mkdir()
    return p


def _mk(parent: Path, name: str, *, completed: bool = True, extra: str = "") -> Path:
    d = parent / name
    d.mkdir()
    if completed:
        (d / "summary.json").write_text("{}")
    (d / "events.jsonl").write_text("e")
    if extra:
        (d / extra).write_text("x")
    return d


def test_completed_run_dirs_only_lists_markered(tmp_path):
    parent = tmp_path / "runs"
    parent.mkdir()
    _mk(parent, "live-0001", completed=True)
    _mk(parent, "live-0002", completed=False)
    # a non-run file/dir
    (parent / "0000-not-a-run").mkdir()
    got = {p.name for p in completed_run_dirs(parent)}
    assert got == {"live-0001"}


def test_prune_keeps_newest_completed_and_active(tmp_path):
    parent = tmp_path / "runs"
    parent.mkdir()
    for i in range(1, 9):
        p = _mk(parent, f"live-{i:04d}", completed=True)
        # ensure ascending mtime ordering is reproducible
        import os

        os.utime(p, (p.stat().st_atime, 1_000_000 + i))
    # keep = 3 newest completed (live-0008..0006); active live-0099 untouched
    active = _mk(parent, "live-0099", completed=False, extra="final.state")
    deleted = prune_completed_runs(parent, keep=3, active=active)
    remaining = {p.name for p in completed_run_dirs(parent)}
    assert len(deleted) == 5
    assert {"live-0006", "live-0007", "live-0008"} <= remaining
    assert active.exists()  # active dir with no summary marker is preserved
    assert (active / "final.state").exists()
    # the oldest completed runs were removed
    assert "live-0001" not in remaining


def test_prune_preserves_referenced_recovery_state(tmp_path):
    parent = tmp_path / "runs"
    parent.mkdir()
    import os

    ref_dir = _mk(parent, "live-0003", completed=True, extra="recover.state")
    os.utime(ref_dir, (ref_dir.stat().st_atime, 1_000))
    for i in range(1, 6):
        p = _mk(parent, f"live-00{10 + i}", completed=True)
        os.utime(p, (p.stat().st_atime, 2_000 + i))
    # keep=2 -> live-0003 (oldest, but the recovery ref) must still be preserved.
    deleted = prune_completed_runs(parent, keep=2, references=ref_dir / "recover.state")
    assert all("live-0003" not in str(d) for d in deleted)  # the referenced run is never pruned
    assert ref_dir.exists()
    assert (ref_dir / "recover.state").exists()
    # The non-referenced oldest completed runs were pruned (keep=2), newest kept.
    remaining = {p.name for p in completed_run_dirs(parent)}
    assert "live-0003" in remaining
    assert {"live-0014", "live-0015"} <= remaining


def test_prune_invalid_keep_raises(tmp_path):
    with pytest.raises(RunRetentionError):
        prune_completed_runs(tmp_path, keep=-1)


@pytest.mark.parametrize("bad", ["../escape", "a/b", "x y", ""])
def test_safe_run_id_rejects(bad):
    with pytest.raises(RunRetentionError):
        safe_run_id(bad)


def test_safe_run_id_accepts_valid():
    assert safe_run_id("live-0042") == "live-0042"
