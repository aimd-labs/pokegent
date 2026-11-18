"""Existing memory-port contract using tiny invented byte regions only."""

import pytest

from pokegent.games.gen3.firered.reader.memory_port import (
    MemoryPort,
    MemoryReadError,
    SnapshotMemoryReader,
)


def test_little_endian_reads_and_log():
    port = SnapshotMemoryReader({0x100: b"\x12\x34\x56\x78\x9a"})
    assert isinstance(port, MemoryPort)
    assert port.read_u8(0x100) == 0x12
    assert port.read_u16(0x101) == 0x5634
    assert port.read_u32(0x101) == 0x9A785634
    assert port.read_range(0x100, 5) == b"\x12\x34\x56\x78\x9a"
    assert port.read_log == [
        ("u8", 0x100, 1), ("u16", 0x101, 2), ("u32", 0x101, 4), ("range", 0x100, 5),
    ]
    port.clear_log()
    assert port.read_log == []


def test_constructor_and_returned_bytes_are_copies():
    source = bytearray(b"ABCD")
    regions = {0x100: source, 0x200: b"EFGH"}
    port = SnapshotMemoryReader(regions)
    source[:] = b"XXXX"
    regions.clear()
    captured = port.read_range(0x100, 4)
    assert captured == b"ABCD" and type(captured) is bytes
    port.poke(0x101, b"xy")
    assert captured == b"ABCD"
    assert port.read_range(0x100, 4) == b"AxyD"
    assert port.read_range(0x200, 4) == b"EFGH"
    port.poke_u32(0x100, 0x112345678)
    assert port.read_range(0x100, 4) == b"\x78\x56\x34\x12"


@pytest.mark.parametrize("addr,size", [(0xFF, 1), (0x103, 2), (0x108, 1), (-1, 1)])
def test_unmapped_partial_and_adjacent_reads_are_not_stitched(addr, size):
    port = SnapshotMemoryReader({0x104: b"EFGH", 0x100: b"ABCD"})
    with pytest.raises(MemoryReadError) as caught:
        port.read_range(addr, size)
    assert type(caught.value) is MemoryReadError
    assert (caught.value.addr, caught.value.size, caught.value.reason) == (addr, size, "unmapped")
    assert port.read_log == [("range", addr, size)]


def test_negative_size_rejected_before_log_or_hook():
    calls = []
    port = SnapshotMemoryReader({0x100: b"ABCD"}, on_read=calls.append)
    with pytest.raises(ValueError, match="size must be >= 0"):
        port.read_range(0x100, -1)
    assert port.read_log == calls == []
    assert port.read_range(0x102, 0) == b""


@pytest.mark.parametrize("regions", [
    {0x102: b"ABCD", 0x100: b"ABCD"}, {-1: b"A"}, {0xFFFFFFFF: b"AB"},
])
def test_overlapping_and_out_of_address_space_regions_rejected(regions):
    with pytest.raises(ValueError, match="overlaps|outside the 32-bit space"):
        SnapshotMemoryReader(regions)


def test_poke_failure_is_atomic_and_does_not_log_reads():
    port = SnapshotMemoryReader({0x100: b"ABCD"})
    with pytest.raises(MemoryReadError) as caught:
        port.poke(0x103, b"XY")
    assert (caught.value.addr, caught.value.size) == (0x103, 2)
    assert port.read_log == []
    assert port.read_range(0x100, 4) == b"ABCD"


def test_hook_runs_after_logging_before_read_and_includes_failed_calls():
    observed = []

    def before_read(index):
        observed.append((index, port.read_log[index]))
        if index == 0:
            port.poke(0x100, b"Z")

    port = SnapshotMemoryReader({0x100: b"ABCD"}, on_read=before_read)
    assert port.read_u8(0x100) == ord("Z")
    with pytest.raises(MemoryReadError):
        port.read_u16(0x103)
    assert port.read_range(0x101, 2) == b"BC"
    assert observed == [(0, ("u8", 0x100, 1)), (1, ("u16", 0x103, 2)), (2, ("range", 0x101, 2))]
    port.clear_log()
    assert port.read_u8(0x100) == ord("Z")
    assert observed[-1] == (0, ("u8", 0x100, 1))
