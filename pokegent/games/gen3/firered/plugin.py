"""Built-in, exact-ROM FireRed plugin for opt-in source composition (#139).

The plugin is inert unless a caller explicitly enables :class:`ReviewedPluginRegistry` and the
built-in FireRed plugin is admitted for exact caller-supplied ROM bytes. ``compose`` only builds
the existing adapter and names the existing composite source class; it does not read an actor,
touch a ROM file, register options or claim verification/support beyond source + reader
composition.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pokegent.games.gen3.firered.rom.rom_data import RomProfile
from pokegent.games.plugin import GamePluginDescriptor, PluginRefusal, RefusalCode

FIRERED_GAME = "firered"
FIRERED_CONTRACT_VERSION = 1
FIRERED_GAME_CODE = "BPRE"
FIRERED_ROM_VERSION = 0
#: Only what this composition actually supplies: the existing adapter's readers/rules and the
#: existing composite snapshot source. No verification, input, support or title promotion.
FIRERED_CAPABILITIES = frozenset({"snapshot_source", "reader"})


@dataclass(frozen=True, slots=True)
class FireRedServices:
    """Composed FireRed services; the adapter is the existing one, not a parallel reader."""

    profile: RomProfile
    adapter: Any
    snapshot_source_factory: Callable[..., Any]


def _require_supported_firered_profile(profile: Any) -> RomProfile:
    if not isinstance(profile, RomProfile):
        raise PluginRefusal(RefusalCode.PROFILE_MISMATCH, "a FireRed RomProfile is required")
    if not profile.supported:
        raise PluginRefusal(
            RefusalCode.PROFILE_MISMATCH, f"profile {profile.profile_id} is not supported"
        )
    if (
        profile.rom.game_code != FIRERED_GAME_CODE
        or profile.rom.version != FIRERED_ROM_VERSION
    ):
        detail = (
            f"profile {profile.profile_id} is {profile.rom.game_code}/"
            f"version {profile.rom.version}, not {FIRERED_GAME_CODE}/version "
            f"{FIRERED_ROM_VERSION}"
        )
        raise PluginRefusal(RefusalCode.PROFILE_MISMATCH, detail)
    return profile


class FireRedPlugin:
    """Reviewed FireRed plugin over one supplied supported exact-ROM profile."""

    def __init__(self, profile: Any) -> None:
        self._profile = _require_supported_firered_profile(profile)
        self._descriptor = GamePluginDescriptor(
            plugin_id=f"{FIRERED_GAME}:{self._profile.profile_id}",
            game=FIRERED_GAME,
            profile_id=self._profile.profile_id,
            contract_version=FIRERED_CONTRACT_VERSION,
            rom_sha1=self._profile.rom.sha1,
            capabilities=FIRERED_CAPABILITIES,
        )

    @property
    def descriptor(self) -> GamePluginDescriptor:
        return self._descriptor

    def compose(self) -> FireRedServices:
        """Lazily build the existing adapter and name the existing composite source class."""
        from pokegent.games.gen3.firered.reader.adapter import FireRedAdapter
        from pokegent.games.gen3.firered.reader.snapshot_sources import CompositeSnapshotSource

        adapter = FireRedAdapter(profile=self._profile)
        return FireRedServices(
            profile=self._profile,
            adapter=adapter,
            snapshot_source_factory=CompositeSnapshotSource,
        )
