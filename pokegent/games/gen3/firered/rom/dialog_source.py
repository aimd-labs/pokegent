"""Snapshot source of the dialog family: core snapshot + UI evidence + text, one atomic read.

``read_snapshot_with_ui`` (M2.1) builds the core snapshot and the UI evidence from ONE atomic
snapshot. The message text must come from the same moment, so this source does the same two reads
and additionally decodes the dialog text from that atomic snapshot and files it in the family's
:class:`~pokegent.games.gen3.firered.rom.dialog_text.TextSidecar` under the snapshot's identity.

Use this (or any source that fills the sidecar) instead of ``LiveSnapshotSource``: the latter has no
``ui`` block at all, so no dialog detector can claim anything. Read-only; nothing is pressed.
"""

from __future__ import annotations

from pokegent.games.gen3.firered.reader.live import (
    AtomicSnapshotPort,
    AtomicSnapshotSource,
    LiveReader,
)
from pokegent.games.gen3.firered.reader.reader import PartySource
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.games.gen3.firered.rom.dialog_text import TextSidecar, read_dialog_text
from pokegent.games.gen3.firered.rom.rom_data import RomProfile
from pokegent.state.evidence import UiEvidenceReader


class DialogSnapshotSource:
    """``SnapshotSource`` (``read_snapshot()``) with the UI block and the text sidecar filled."""

    def __init__(
        self,
        source: AtomicSnapshotSource,
        profile: RomProfile,
        sidecar: TextSidecar,
        *,
        run_id: str = "",
        party_source: PartySource = "live",
    ) -> None:
        self._source = source
        self._profile = profile
        self._sidecar = sidecar
        self._run_id = run_id
        self._party_source: PartySource = party_source

    @property
    def sidecar(self) -> TextSidecar:
        return self._sidecar

    def read_snapshot(self) -> StateSnapshot:
        atomic = self._source.read_atomic_snapshot()
        live = LiveReader(
            self._source, self._profile, run_id=self._run_id, party_source=self._party_source
        ).read(atomic)
        port = AtomicSnapshotPort(atomic)
        ui = UiEvidenceReader(port, self._profile).read()
        snapshot = live.snapshot.with_ui(ui)
        self._sidecar.put(snapshot, read_dialog_text(port, self._profile, ui))
        return snapshot
