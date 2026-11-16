"""Memory access port for the FireRed reader, plus an in-memory test double.

``MemoryPort`` is the only thing the reader needs from an emulator backend. It is a
``typing.Protocol`` (structural), so the existing ``Emulator`` ABC in
``pokegent.emulator`` already satisfies it: it has ``read_u8``, ``read_u16``,
``read_u32`` and ``read_range`` with the same signatures.

``SnapshotMemoryReader`` is a clean-room test double built from ``{base_address: bytes}``
region dictionaries. It never touches an emulator or a ROM, which keeps the reader testable
in CI without proprietary data (spec 16.1: synthetic fixtures are the CI default).

Conventions
-----------
* Addresses are GBA bus addresses (EWRAM ``0x02000000``, IWRAM ``0x03000000``, ROM
  ``0x08000000``). Multi-byte reads are little-endian.
* A read that is not fully backed by memory raises :class:`MemoryReadError`. A port never
  returns partial or zero-filled data for an unmapped range.
* A caller that wants a coherent view must not advance the emulator between reads. That is
  the executor/actor lock's job (spec 4.1); the reader additionally re-checks the save-block
  pointers at the end of a snapshot to catch a violation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol, runtime_checkable

# GBA memory regions the reader cares about.
EWRAM_START = 0x0200_0000
EWRAM_END = 0x0204_0000  # exclusive (256 KiB)
IWRAM_START = 0x0300_0000
IWRAM_END = 0x0300_8000  # exclusive (32 KiB)
ROM_START = 0x0800_0000


class MemoryReadError(Exception):
    """A read touched memory the port cannot provide."""

    def __init__(self, addr: int, size: int, reason: str = "unmapped") -> None:
        super().__init__(f"cannot read {size} byte(s) at 0x{addr:08X}: {reason}")
        self.addr = addr
        self.size = size
        self.reason = reason


@runtime_checkable
class MemoryPort(Protocol):
    """Minimal read-only view of GBA memory. Little-endian."""

    def read_u8(self, addr: int) -> int: ...

    def read_u16(self, addr: int) -> int: ...

    def read_u32(self, addr: int) -> int: ...

    def read_range(self, addr: int, size: int) -> bytes: ...


class SnapshotMemoryReader:
    """In-memory ``MemoryPort`` built from region dictionaries.

    ``regions`` maps a base address to the bytes mapped there. Regions must not overlap.
    Every read must lie completely inside one region.

    ``read_log`` records ``(kind, addr, size)`` for every call, so tests can assert that the
    reader uses bulk reads. ``on_read`` (optional) is called *before* each read with the
    0-based call index; a test uses it to mutate memory mid-snapshot and simulate an
    interrupt or a frame advance between reads.
    """

    def __init__(
        self,
        regions: Mapping[int, bytes | bytearray],
        *,
        on_read: Callable[[int], None] | None = None,
    ) -> None:
        self._regions: dict[int, bytearray] = {}
        for base, data in sorted(regions.items()):
            if base < 0 or base + len(data) > 0x1_0000_0000:
                raise ValueError(f"region at 0x{base:08X} is outside the 32-bit space")
            self._regions[base] = bytearray(data)
        previous_end = -1
        for base, data in self._regions.items():
            if base < previous_end:
                raise ValueError(f"region at 0x{base:08X} overlaps the previous region")
            previous_end = base + len(data)
        self.read_log: list[tuple[str, int, int]] = []
        self._on_read = on_read

    # -- test-double helpers (never part of the MemoryPort contract) ------------------

    def poke(self, addr: int, data: bytes) -> None:
        """Overwrite bytes inside an existing region (test setup only)."""
        base, region = self._locate(addr, len(data))
        offset = addr - base
        region[offset : offset + len(data)] = data

    def poke_u32(self, addr: int, value: int) -> None:
        self.poke(addr, (value & 0xFFFF_FFFF).to_bytes(4, "little"))

    def clear_log(self) -> None:
        self.read_log.clear()

    # -- MemoryPort ------------------------------------------------------------------

    def read_u8(self, addr: int) -> int:
        return self._read("u8", addr, 1)[0]

    def read_u16(self, addr: int) -> int:
        return int.from_bytes(self._read("u16", addr, 2), "little")

    def read_u32(self, addr: int) -> int:
        return int.from_bytes(self._read("u32", addr, 4), "little")

    def read_range(self, addr: int, size: int) -> bytes:
        if size < 0:
            raise ValueError("size must be >= 0")
        return self._read("range", addr, size)

    # -- internals -------------------------------------------------------------------

    def _read(self, kind: str, addr: int, size: int) -> bytes:
        index = len(self.read_log)
        self.read_log.append((kind, addr, size))
        if self._on_read is not None:
            self._on_read(index)
        base, region = self._locate(addr, size)
        offset = addr - base
        return bytes(region[offset : offset + size])

    def _locate(self, addr: int, size: int) -> tuple[int, bytearray]:
        for base, region in self._regions.items():
            if base <= addr and addr + size <= base + len(region):
                return base, region
        raise MemoryReadError(addr, size)
