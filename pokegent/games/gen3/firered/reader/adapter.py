"""FireRed game adapter and its lazily loaded game-owned readers."""

from __future__ import annotations

from typing import Any, Callable

from pokegent.games.gen3.firered.reader.reader import FireRedReader


class FireRedAdapter:
    """Generation-3/FireRed adapter that delegates snapshot reads to ``FireRedReader``."""

    def __init__(
        self,
        *,
        profile: Any = None,
        snapshot_source: Any = None,
        reader_factory: Callable[..., FireRedReader] = FireRedReader,
    ) -> None:
        self.profile = profile
        self.snapshot_source = snapshot_source
        self._reader_factory = reader_factory
        self.identity = "firered"
        # Keep these imports local: legacy compatibility modules deliberately resolve lazily, and
        # importing the adapter must not re-enter a UI/family reader during package initialisation.
        from pokegent.games.gen3.firered.maps.map_provider import FireRedMapProvider
        from pokegent.games.gen3.firered.maps.world import WorldAdapter
        from pokegent.games.gen3.firered.reader import menu_mappings
        from pokegent.games.gen3.firered.reader.battle_reader import battle_of
        from pokegent.games.gen3.firered.reader.capabilities import FireRedCapabilities
        from pokegent.games.gen3.firered.reader.ui import UiEvidenceReader
        from pokegent.games.gen3.firered.rom import item_catalog
        from pokegent.games.gen3.firered.rom import rom_tables as tables
        from pokegent.games.gen3.firered.rom.dialog_source import DialogSnapshotSource
        from pokegent.games.gen3.firered.rom.field_source import FieldSnapshotSource
        from pokegent.games.gen3.ruleset import FireRedRuleset

        self.readers = {
            "snapshot": reader_factory,
            "ui": UiEvidenceReader,
            "battle": battle_of,
            "dialog": DialogSnapshotSource,
            "field": FieldSnapshotSource,
        }
        self.codecs = {}
        self.input_mapping = {}
        self.tables = tables
        self.rules = {"items": item_catalog, "menu": menu_mappings, "battle": FireRedRuleset()}
        self.map_provider = FireRedMapProvider()
        self.world_provider = WorldAdapter
        self.ui_decoder = UiEvidenceReader
        self.battle_rules = FireRedRuleset()
        self.catch_rules = self.battle_rules.catch
        self.capabilities = FireRedCapabilities()
        self.story_facts = None

    def reader(self, port: Any) -> FireRedReader:
        """Construct the existing reader with the configured FireRed profile."""
        return self._reader_factory(port, self.profile)

    def register_options(self, factory: Any) -> None:
        """Reserve the add-on registration seam; existing registrations are unchanged."""
        del factory
