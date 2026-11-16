"""Glue between the single-writer emulator actor and the FireRed reader (WP1c, milestone M1).

The reader needs a :class:`~pokegent.games.gen3.firered.reader.memory_port.MemoryPort`. The live
source is
:meth:`pokegent.emulator.EmulatorActor.read_atomic_snapshot`, which returns an
:class:`~pokegent.emulator.AtomicSnapshot`: EWRAM, IWRAM and the ROM image captured
in one step between two frames. The reader therefore runs on an immutable copy, never on the
moving core: no torn reads, and the "pointers changed during the snapshot" guard of the reader
can only fire on a genuine game-side inconsistency, not on a frame advancing under it.

Two things are not already right on an ``AtomicSnapshot`` and are fixed here:

* **Error type.** ``AtomicSnapshot`` signals an unreadable range with ``MemoryAccessError``,
  the reader only converts ``MemoryReadError`` into ``health.valid == False``. Without the
  adapter an unmapped read would escape as an exception instead of an invalid snapshot.
  :class:`AtomicSnapshotPort` translates it.
* **Sequence number.** The reader's own counter says "how many snapshots this reader made".
  For live data the number must say *when* in emulator time the RAM was taken:
  ``snapshot_seq = frame + load_epoch`` (``frame`` is the backend's monotonic frame count,
  which survives savestate loads; ``load_epoch`` counts restores). Never the core's own frame
  counter, which jumps on a load. The value is non-decreasing, grows with every frame or load,
  and is equal only for two reads of an unchanged moment (then the RAM is identical too).

After each state load call :meth:`LiveReader.after_state_load`: it runs
``FireRedReader.check_rom_header()`` (profile belongs to the running game) and
``conformance.check_port`` (all reader invariants on that exact RAM) on one atomic snapshot.

Read-only: nothing here writes RAM, the ROM or any file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pokegent.emulator import AtomicSnapshot, MemoryAccessError
from pokegent.games.gen3.firered.reader.memory_port import MemoryReadError
from pokegent.games.gen3.firered.reader.reader import FireRedReader, PartySource, StorageDecodeCache
from pokegent.games.gen3.firered.reader.schemas import ReadError, StateSnapshot
from pokegent.games.gen3.firered.rom.conformance import check_port
from pokegent.games.gen3.firered.rom.rom_data import RomProfile


class AtomicSnapshotSource(Protocol):
    """Anything that can hand out an atomic snapshot (``EmulatorActor``, read-only view)."""

    def read_atomic_snapshot(self) -> AtomicSnapshot: ...


def live_snapshot_seq(atomic: AtomicSnapshot) -> int:
    """``snapshot_seq`` for a live read: backend frame count plus the load epoch."""
    return atomic.frame + atomic.load_epoch


class AtomicSnapshotPort:
    """``MemoryPort`` view of one :class:`AtomicSnapshot` (immutable, read-only).

    Behaves like the snapshot but raises :class:`MemoryReadError` for anything the snapshot
    cannot back, which is what :class:`FireRedReader` turns into an invalid snapshot.
    """

    __slots__ = ("_atomic",)

    def __init__(self, atomic: AtomicSnapshot) -> None:
        self._atomic = atomic

    @property
    def atomic(self) -> AtomicSnapshot:
        return self._atomic

    def read_range(self, addr: int, size: int) -> bytes:
        try:
            return self._atomic.read_range(addr, size)
        except MemoryAccessError as exc:
            raise MemoryReadError(addr, size, str(exc)) from exc

    def read_u8(self, addr: int) -> int:
        return self.read_range(addr, 1)[0]

    def read_u16(self, addr: int) -> int:
        return int.from_bytes(self.read_range(addr, 2), "little")

    def read_u32(self, addr: int) -> int:
        return int.from_bytes(self.read_range(addr, 4), "little")


@dataclass(frozen=True)
class LiveRead:
    """One reader result plus where in emulator time it was taken."""

    snapshot: StateSnapshot
    frame: int
    load_epoch: int
    ram_digest: str

    @property
    def valid(self) -> bool:
        return self.snapshot.health.valid

    @property
    def reasons(self) -> tuple[str, ...]:
        """``subsystem/code`` of every read error (empty for a valid snapshot)."""
        return tuple(f"{e.subsystem}/{e.code}" for e in self.snapshot.health.read_errors)


@dataclass(frozen=True)
class LoadCheck:
    """Result of the post-load checks. ``ok`` only if header and all invariants hold."""

    name: str
    ok: bool
    rom_header_errors: tuple[ReadError, ...]
    conformance: dict
    frame: int
    load_epoch: int
    ram_digest: str


class LiveReader:
    """Run :class:`FireRedReader` on atomic snapshots from an emulator actor."""

    def __init__(
        self,
        source: AtomicSnapshotSource,
        profile: RomProfile,
        *,
        run_id: str = "",
        party_source: PartySource = "live",
    ) -> None:
        self._source = source
        self._profile = profile
        self._run_id = run_id
        self._party_source: PartySource = party_source
        self._storage_cache = StorageDecodeCache()

    @property
    def profile(self) -> RomProfile:
        return self._profile

    @property
    def party_source(self) -> PartySource:
        """The RAM party representation used for every snapshot."""
        return self._party_source

    def read(self, atomic: AtomicSnapshot | None = None, *, defer_hash: bool = False) -> LiveRead:
        """Take a fresh atomic snapshot (or use ``atomic``) and read it."""
        if atomic is None:
            atomic = self._source.read_atomic_snapshot()
        reader = FireRedReader(
            AtomicSnapshotPort(atomic),
            self._profile,
            run_id=self._run_id,
            party_source=self._party_source,
            storage_cache=self._storage_cache,
        )
        snapshot = reader.read_snapshot(
            emulator_frame=atomic.frame,
            seq=live_snapshot_seq(atomic),
            defer_hash=defer_hash,
        )
        return LiveRead(snapshot, atomic.frame, atomic.load_epoch, atomic.ram_digest)

    def after_state_load(self, name: str = "state") -> LoadCheck:
        """ROM header check plus the full conformance invariants on one atomic snapshot.

        Call right after every savestate load. A failure means: do not trust the reader (and do
        not play) until the cause is understood.
        """
        atomic = self._source.read_atomic_snapshot()
        port = AtomicSnapshotPort(atomic)
        header_errors = tuple(FireRedReader(port, self._profile).check_rom_header())
        conformance = check_port(name, port, self._profile)
        return LoadCheck(
            name=name,
            ok=not header_errors and bool(conformance["ok"]),
            rom_header_errors=header_errors,
            conformance=conformance,
            frame=atomic.frame,
            load_epoch=atomic.load_epoch,
            ram_digest=atomic.ram_digest,
        )
