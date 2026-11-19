"""Retention for autonomous run output (issue #59) — fail-closed safety contract."""
import os
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


def _age(p: Path, n: int):
    """Force a deterministic, monotonic mtime so ordering is reproducible across FS."""
    os.utime(p, (p.stat().st_atime, 1_000_000 + n))


def test_completed_run_dirs_only_lists_markered(tmp_path):
    parent = tmp_path / "runs"
    parent.mkdir()
    _mk(parent, "live-0001", completed=True)
    _mk(parent, "live-0002", completed=False)
    (parent / "0000-not-a-run").mkdir()
    got = {p.name for p in completed_run_dirs(parent)}
    assert got == {"live-0001"}


def test_prune_keeps_newest_completed_and_active(tmp_path):
    parent = tmp_path / "runs"
    parent.mkdir()
    for i in range(1, 9):
        _age(_mk(parent, f"live-{i:04d}", completed=True), i)
    active = _mk(parent, "live-0099", completed=False, extra="final.state")
    result = prune_completed_runs(parent, keep=3, active=active)
    remaining = {p.name for p in completed_run_dirs(parent)}
    assert {p.name for p in result.deleted} == {f"live-{i:04d}" for i in range(1, 6)}
    assert {"live-0006", "live-0007", "live-0008"} <= remaining
    assert active.exists()
    assert (active / "final.state").exists()
    assert "live-0001" not in remaining


def test_prune_preserves_referenced_recovery_state(tmp_path):
    parent = tmp_path / "runs"
    parent.mkdir()
    ref_dir = _mk(parent, "live-0003", completed=True, extra="recover.state")
    _age(ref_dir, 1)
    for i in range(1, 6):
        _age(_mk(parent, f"live-00{10 + i}", completed=True), 10 + i)
    deleted = prune_completed_runs(
        parent, keep=2, references=[ref_dir / "recover.state", ref_dir / "summary.json"]
    )
    # The referenced run directory (a parent of the state file) is never pruned.
    assert all("live-0003" not in str(d) for d in deleted.deleted)
    assert not deleted.failures
    assert ref_dir.exists()
    assert (ref_dir / "recover.state").exists()
    remaining = {p.name for p in completed_run_dirs(parent)}
    assert "live-0003" in remaining
    assert {"live-0014", "live-0015"} <= remaining


def test_prune_invalid_keep_raises(tmp_path):
    with pytest.raises(RunRetentionError):
        prune_completed_runs(tmp_path, keep=-1)


def test_completed_run_dirs_skips_symlink(tmp_path):
    """A symlinked run dir must never be a prune candidate (it could point outside runs/)."""
    parent = tmp_path / "runs"
    parent.mkdir()
    outside = tmp_path / "external"
    outside.mkdir()
    (outside / "summary.json").write_text("{}")
    (outside / "final.state").write_text("s")
    link = parent / "live-EXTERNAL"
    link.symlink_to(outside, target_is_directory=True)
    assert completed_run_dirs(parent) == []
    # A prune pass must not touch the external files even with keep=0.
    result = prune_completed_runs(parent, keep=0)
    assert result.deleted == []
    assert not result.failures  # not even listed -> nothing refused, nothing deleted
    assert (outside / "final.state").exists()
    assert (outside / "summary.json").exists()
    assert link.is_symlink()


def test_rmtree_shallow_refuses_nested_dir_fail_closed(tmp_path):
    """A run dir with a nested checkpoint block is refused BEFORE any file is unlinked."""
    parent = tmp_path / "runs"
    parent.mkdir()
    d = _mk(parent, "live-0001", completed=True, extra="final.state")
    (d / "checkpoints").mkdir()
    (d / "checkpoints" / "step-0012.state").write_text("c")
    result = prune_completed_runs(parent, keep=0)
    assert result.deleted == []
    assert any("nested dir" in why for _, why in result.failures)
    # NOTHING was partially deleted — the run's own files are still intact.
    assert (d / "final.state").exists()
    assert (d / "events.jsonl").exists()
    assert (d / "summary.json").exists()
    assert (d / "checkpoints" / "step-0012.state").exists()


def test_rmtree_shallow_refuses_symlink_child(tmp_path):
    parent = tmp_path / "runs"
    parent.mkdir()
    outside_file = tmp_path / "outside.state"
    outside_file.write_text("o")
    d = _mk(parent, "live-0001", completed=True)
    (d / "final.state").symlink_to(outside_file)
    result = prune_completed_runs(parent, keep=0)
    assert result.deleted == []
    assert result.failures
    assert outside_file.exists()  # the symlink target is untouched


def test_rmtree_shallow_refuses_symlink_to_external_dir_is_not_pruned(tmp_path):
    """A run dir that itself points at an external directory must be skipped entirely."""
    parent = tmp_path / "runs"
    parent.mkdir()
    outside = tmp_path / "ext"
    outside.mkdir()
    (outside / "summary.json").write_text("{}")
    (parent / "live-EXT").symlink_to(outside, target_is_directory=True)
    result = prune_completed_runs(parent, keep=0)
    assert result.deleted == []
    assert (outside / "summary.json").exists()


def test_prune_zero_keeps_active_and_reference(tmp_path):
    """--retain-runs 0 must delete all completed sibling runs but never active/live refs."""
    parent = tmp_path / "runs"
    parent.mkdir()
    for i in range(1, 5):
        _age(_mk(parent, f"live-{i:04d}", completed=True), i)
    active = _mk(parent, "live-ACTIVE", completed=False, extra="final.state")
    recovered = _mk(parent, "live-RECOVERED", completed=True, extra="resume.state")
    _age(recovered, 50)
    result = prune_completed_runs(
        parent, keep=0, active=active, references=[recovered / "resume.state"]
    )
    assert {p.name for p in result.deleted} == {f"live-{i:04d}" for i in range(1, 5)}
    assert active.exists()
    assert recovered.exists()  # live/resume reference preserved even with keep=0
    assert (recovered / "resume.state").exists()


@pytest.mark.parametrize("bad", ["../escape", "a/b", "x y", ""])
def test_safe_run_id_rejects(bad):
    with pytest.raises(RunRetentionError):
        safe_run_id(bad)


def test_safe_run_id_accepts_valid():
    assert safe_run_id("live-0042") == "live-0042"
