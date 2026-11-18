"""Synthetic actor isolation regressions.

Adapted from AIMD Labs pokegent-archive (Nous Research MIT lineage),
492b71b1019bc92db299a20eab6ca7b22724c5c6:tests/emulator/test_emulator_actor.py.
Copyright/permission notices are retained in LICENSE. No historical game fixtures.
"""

import hashlib
import threading
from dataclasses import FrozenInstanceError

import pytest

from pokegent.emulator import (
    GBA_EWRAM,
    GBA_IWRAM,
    AtomicSnapshot,
    MemoryAccessError,
    ReadOnlyEmulatorView,
)


def test_snapshot_counters_and_old_bytes_survive_advance(actor, fake):
    before = actor.read_atomic_snapshot()
    saved = dict(before.regions)
    digest = before.ram_digest
    assert actor.advance_frames(3) == 3
    after = actor.read_atomic_snapshot()
    assert after.seq > before.seq
    assert (before.frame, before.core_frame, before.load_epoch) == (0, 1000, 0)
    assert (after.frame, after.core_frame, after.load_epoch) == (3, 1003, 0)
    for region in (GBA_EWRAM, GBA_IWRAM):
        assert len(after.region(region.name)) == region.size
        assert after.read_u32(region.base) == fake.frame_count == fake.frames_run == 3
        assert before.read_u32(region.base) == 0
    fake.poke(GBA_EWRAM.base, b"\xff" * 4)
    assert dict(before.regions) == saved
    assert before.ram_digest == digest != after.ram_digest
    assert after.read_u32(GBA_EWRAM.base) == 3
    assert after.keys_held == before.keys_held == frozenset()


@pytest.mark.parametrize("kind", [bytes, bytearray, memoryview])
def test_snapshot_copies_external_mapping_and_mutable_buffers(kind):
    buffer = bytearray(b"\x01\x02\x03\x04")
    regions = {"ewram": kind(buffer)}
    snap = AtomicSnapshot(1, 0, 1000, 0, frozenset(), regions, (GBA_EWRAM,), 0)
    expected = b"\x01\x02\x03\x04"
    digest = hashlib.sha256(b"ewram" + expected).hexdigest()
    assert snap.ram_digest == digest
    buffer[0] = 99
    assert snap.region("ewram") == expected
    regions["ewram"] = b"other"
    assert snap.read_range(GBA_EWRAM.base, 4) == expected
    assert isinstance(snap.region("ewram"), bytes)
    assert snap.ram_digest == digest
    assert snap.ram_digest == hashlib.sha256(b"ewram" + snap.region("ewram")).hexdigest()


def test_actor_snapshot_copies_aliased_backend_buffers(actor, fake, monkeypatch):
    buffers = {r.name: bytearray(fake.read_range(r.base, r.size))
               for r in (GBA_EWRAM, GBA_IWRAM)}
    monkeypatch.setattr(fake, "read_regions", lambda regions: buffers)
    snap = actor.read_atomic_snapshot()
    digest = snap.ram_digest
    buffers["ewram"][0] = 99
    assert snap.read_u32(GBA_EWRAM.base) == 0
    buffers["iwram"] = b"changed"
    assert snap.read_u32(GBA_IWRAM.base) == 0
    assert snap.ram_digest == digest


def test_snapshot_region_replacement_and_byte_mutation_are_refused(actor):
    snap = actor.read_atomic_snapshot()
    digest = snap.ram_digest
    with pytest.raises(TypeError):
        snap.regions["ewram"] = b"changed"
    with pytest.raises(TypeError):
        del snap.regions["iwram"]
    with pytest.raises(TypeError):
        snap.region("ewram")[0] = 99
    with pytest.raises(FrozenInstanceError):
        snap.frame = 99
    assert snap.read_u32(GBA_EWRAM.base) == 0
    assert snap.ram_digest == digest


def test_overlapping_reader_waits_for_complete_frame(actor, fake, monkeypatch):
    entered, release, queued = (threading.Event() for _ in range(3))
    results, errors = [], []
    submit_read = actor._clock.submit_read

    def enqueue(fn):
        future = submit_read(fn)
        queued.set()
        return future

    def mid_frame():
        entered.set()
        assert release.wait(3), "test did not release backend"

    def run(fn):
        try:
            results.append(fn())
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(actor._clock, "submit_read", enqueue)
    fake.mid_frame_hook = mid_frame
    writer = threading.Thread(target=run, args=(lambda: actor.advance_frames(1),))
    reader = threading.Thread(target=run, args=(actor.read_atomic_snapshot,))
    writer.start()
    try:
        assert entered.wait(3), "writer never entered mid-frame hook"
        # Negative control: bypassing the actor exposes the deliberately torn pair.
        assert fake.read_u32(GBA_EWRAM.base) == 1
        assert fake.read_u32(GBA_IWRAM.base) == 0
        reader.start()
        assert queued.wait(3), "reader never overlapped the paused writer"
        assert results == []
    finally:
        release.set()
        for thread in (writer, reader):
            if thread.ident is not None:
                thread.join(timeout=3)
                assert not thread.is_alive(), "test caller did not exit"
        fake.mid_frame_hook = None
    assert errors == []
    snap = next(result for result in results if isinstance(result, AtomicSnapshot))
    assert snap.read_u32(GBA_EWRAM.base) == snap.read_u32(GBA_IWRAM.base) == snap.frame == 1
    assert (snap.core_frame, snap.load_epoch, snap.keys_held) == (1001, 0, frozenset())


def test_readonly_view_has_no_writers_and_reads_one_coherent_snapshot(actor):
    actor.advance_frames(2)
    view = actor.reader()
    assert isinstance(view, ReadOnlyEmulatorView)
    for name in ("tap", "hold", "release_all", "advance_frames", "wait_for", "save_state",
                 "load_state", "close", "_clock", "_backend", "button_down", "poke"):
        assert not hasattr(view, name), name
    snap = view.read_atomic_snapshot()
    assert snap.read_u32(GBA_EWRAM.base) == snap.read_u32(GBA_IWRAM.base) == snap.frame == 2
    with pytest.raises(MemoryAccessError):
        snap.read_range(GBA_EWRAM.end - 1, 2)
