"""Glue: one atomic snapshot -> reader snapshot plus UI evidence (M2.1).

``LiveReader.read()`` builds the core :class:`StateSnapshot` from an
:class:`~pokegent.emulator.AtomicSnapshot`. The UI evidence must come from the SAME
atomic snapshot (spec 4.1: one coherent moment per decision), so this helper takes the atomic
snapshot once and feeds both readers. The UI block is attached even when the core snapshot is
invalid: the party screen makes the party reader fail for 1-2 frames while its UI evidence is fine.
"""

from __future__ import annotations

from pokegent.games.gen3.firered.reader.live import (
    AtomicSnapshotPort,
    AtomicSnapshotSource,
    LiveReader,
)
from pokegent.games.gen3.firered.reader.reader import PartySource
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.games.gen3.firered.reader.ui_schemas import UiEvidence
from pokegent.games.gen3.firered.rom.rom_data import RomProfile
from pokegent.state.evidence import UiEvidenceReader


def read_snapshot_with_ui(
    source: AtomicSnapshotSource,
    profile: RomProfile,
    *,
    run_id: str = "",
    party_source: PartySource = "live",
) -> StateSnapshot:
    """Take one atomic snapshot and return the core snapshot with its ``ui`` block attached."""
    atomic = source.read_atomic_snapshot()
    live = LiveReader(source, profile, run_id=run_id, party_source=party_source).read(atomic)
    ui = UiEvidenceReader(AtomicSnapshotPort(atomic), profile).read()
    return live.snapshot.with_ui(ui)


def ui_evidence_of(source: AtomicSnapshotSource, profile: RomProfile) -> UiEvidence:
    """UI evidence alone from one fresh atomic snapshot."""
    return UiEvidenceReader(AtomicSnapshotPort(source.read_atomic_snapshot()), profile).read()
