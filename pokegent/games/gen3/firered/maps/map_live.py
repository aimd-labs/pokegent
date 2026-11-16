"""Glue: one atomic emulator snapshot -> reader snapshot + map state (M2.2).

``LiveMapReader.read()`` takes ONE :class:`~pokegent.emulator.AtomicSnapshot`, runs the
core reader and the map reader on it, and returns both plus the snapshot with the ``map`` block
attached. Both see the same frozen RAM, so the player position in ``snapshot.player`` and the
placement in ``map.player`` are from the same instant (still possibly inconsistent with each other
during a warp: ``map.player.position_consistent`` says so).

The map reader runs even when the core snapshot is invalid (a party-screen checksum blip must not
blind navigation); the returned ``snapshot`` carries the map block only when it is valid.
"""

from __future__ import annotations

from dataclasses import dataclass

from pokegent.emulator import AtomicSnapshot
from pokegent.games.gen3.firered.maps.map_data import MapReader, MapReadResult, RomMapCache
from pokegent.games.gen3.firered.maps.map_schema import DiscoveryPolicy
from pokegent.games.gen3.firered.reader.live import (
    AtomicSnapshotPort,
    AtomicSnapshotSource,
    LiveReader,
)
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.games.gen3.firered.rom.rom_data import RomProfile


@dataclass(frozen=True)
class LiveMapRead:
    snapshot: StateSnapshot  # with the map block when both reads succeeded
    map_result: MapReadResult
    frame: int
    load_epoch: int

    @property
    def map(self):
        return self.map_result.state


class LiveMapReader:
    def __init__(
        self,
        source: AtomicSnapshotSource,
        profile: RomProfile,
        *,
        run_id: str = "",
        cache: RomMapCache | None = None,
    ) -> None:
        self._source = source
        self._profile = profile
        self._live = LiveReader(source, profile, run_id=run_id)
        self._cache = cache if cache is not None else RomMapCache()

    @property
    def cache(self) -> RomMapCache:
        return self._cache

    def read(
        self, atomic: AtomicSnapshot | None = None, *, policy: DiscoveryPolicy | None = None
    ) -> LiveMapRead:
        if atomic is None:
            atomic = self._source.read_atomic_snapshot()
        read = self._live.read(atomic)
        result = MapReader(AtomicSnapshotPort(atomic), self._profile, cache=self._cache).read(
            policy=policy
        )
        snapshot = read.snapshot
        if snapshot.health.valid:
            snapshot = snapshot.with_map(result.state)
        return LiveMapRead(snapshot, result, atomic.frame, atomic.load_epoch)
