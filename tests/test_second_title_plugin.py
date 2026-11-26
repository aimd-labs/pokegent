"""Synthetic second-title plugin through admission and observation (#392)."""

from __future__ import annotations

import hashlib

import pytest

import pokegent.games.observation as observation
import pokegent.games.plugin as plugin

TITLE = "crystal-next"
CAPS = frozenset({"snapshot_source", "tournament_tree"})
ROM = b"synthetic-second-title-rom" * 8
OTHER = b"synthetic-unrelated-rom" * 8


class SecondTitlePlugin:
    """Reviewed fake second game; one exact synthetic ROM digest gates admission."""

    def __init__(self) -> None:
        self.compose_calls = 0
        self.descriptor = plugin.GamePluginDescriptor(
            plugin_id="crystal-next:us-rev0", game=TITLE, profile_id="us-rev0",
            contract_version=1, rom_sha1=hashlib.sha1(ROM).hexdigest(),
            capabilities=CAPS,
        )

    def compose(self):
        self.compose_calls += 1
        return {"scene": "overworld"}


def _obs(seq, scene):
    return observation.create_observation(
        enabled=True, game_id=TITLE, profile_id="us-rev0", snapshot_seq=seq,
        restore_epoch=0, usable=True, payload={"scene": scene},
    )


def _admit(plugin_obj, rom=ROM):
    registry = plugin.ReviewedPluginRegistry(enabled=True)
    registry.register(plugin_obj)
    return registry.select(rom_bytes=rom, game=TITLE, profile_id="us-rev0")


def test_second_title_admission_composes_services():
    plugin_obj = SecondTitlePlugin()
    selection = _admit(plugin_obj)

    assert selection.descriptor.capabilities == CAPS
    assert selection.services["scene"] == "overworld"
    assert plugin_obj.compose_calls == 1

    scene = selection.services["scene"]
    assert observation.validate_transition(_obs(1, scene), _obs(2, scene)) == "sequence_advanced"


def test_unknown_digest_is_refused_and_never_composes():
    plugin_obj = SecondTitlePlugin()
    with pytest.raises(plugin.PluginRefusal) as caught:
        _admit(plugin_obj, OTHER)
    assert caught.value.code is plugin.RefusalCode.UNKNOWN_IDENTITY
    assert plugin_obj.compose_calls == 0
