"""Snapshot source of the field family: core snapshot + UI evidence + map block, ONE atomic read.

The field detector needs ``snapshot.ui`` and the builder needs ``snapshot.map``. A plain
``LiveSnapshotSource`` has neither. This source takes one atomic snapshot and feeds the core reader,
the UI evidence reader and the map reader, so position, scene and map are one coherent moment. The
integrator's composite source (dialog text, battle block, map) can do the same read once and attach
all blocks; this class is the standalone version.

Read-only; nothing is pressed.
"""

from __future__ import annotations

from pokegent.games.gen3.firered.maps.map_data import MapReader, RomMapCache
from pokegent.games.gen3.firered.maps.map_schema import DiscoveryPolicy
from pokegent.games.gen3.firered.reader.live import (
    AtomicSnapshotPort,
    AtomicSnapshotSource,
    LiveReader,
)
from pokegent.games.gen3.firered.reader.reader import PartySource
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.games.gen3.firered.rom.rom_data import RomProfile
from pokegent.state.evidence import UiEvidenceReader


class FieldSnapshotSource:
    """``SnapshotSource`` (``read_snapshot()``) with the ``ui`` and ``map`` blocks filled."""

    def __init__(
        self,
        source: AtomicSnapshotSource,
        profile: RomProfile,
        *,
        run_id: str = "",
        party_source: PartySource = "live",
        cache: RomMapCache | None = None,
        policy: DiscoveryPolicy | None = None,
    ) -> None:
        self._source = source
        self._profile = profile
        self._live = LiveReader(source, profile, run_id=run_id, party_source=party_source)
        self._cache = cache if cache is not None else RomMapCache()
        self._policy = policy

    def read_snapshot(self) -> StateSnapshot:
        atomic = self._source.read_atomic_snapshot()
        live = self._live.read(atomic)
        port = AtomicSnapshotPort(atomic)
        ui = UiEvidenceReader(port, self._profile).read()
        snapshot = live.snapshot.with_ui(ui)
        if snapshot.health.valid:
            result = MapReader(port, self._profile, cache=self._cache).read(policy=self._policy)
            if result.usable:
                snapshot = snapshot.with_map(result.state)
        return snapshot
