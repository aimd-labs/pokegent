"""Profile digest binding at caller-registry admission (#139).

A caller-registry plugin claiming the same game/profile with a different ROM
SHA1 must be refused before compose; build_runner passes the profile digest.
"""

from __future__ import annotations

import hashlib

import pytest
from test_runner_plugin_registry import (
    InventedFireRedPlugin,
    NoReadActor,
    _build,
    _profile,
)

import pokegent.app as app_module
from pokegent.games.gen3.firered.plugin import FIRERED_CAPABILITIES
from pokegent.games.gen3.firered.reader.snapshot_sources import CompositeSnapshotSource
from pokegent.games.plugin import PluginRefusal, RefusalCode, ReviewedPluginRegistry

ROM_A = b"synthetic-digest-binding-a" * 8
ROM_B = b"synthetic-digest-binding-b" * 8


def _digest(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class _CoercedLookalike:
    """str() yields a valid digest; strict typing must still refuse it."""

    def __str__(self) -> str:
        return _digest(ROM_A)


def test_digest_mismatch_refuses_before_compose_or_actor():
    profile = _profile(ROM_A)
    plugin = InventedFireRedPlugin(label="mismatch", profile=profile, rom_bytes=ROM_B)
    registry = ReviewedPluginRegistry(enabled=True)
    registry.register(plugin)
    actor = NoReadActor()

    with pytest.raises(PluginRefusal) as caught:
        _build(
            profile,
            actor=actor,
            experimental_game_plugin=True,
            plugin_rom_bytes=ROM_B,
            plugin_registry=registry,
        )

    assert caught.value.code is RefusalCode.PROFILE_MISMATCH
    assert plugin.composed == 0
    assert actor.touched == []


def test_matching_digest_selects_the_caller_bundle(monkeypatch):
    profile = _profile(ROM_B)
    plugin = InventedFireRedPlugin(label="match", profile=profile, rom_bytes=ROM_B)
    registry = ReviewedPluginRegistry(enabled=True)
    registry.register(plugin)

    def fail(*args, **kwargs):
        raise AssertionError("an admitted bundle used an app-level default factory")

    monkeypatch.setattr(app_module, "register_families", fail)
    monkeypatch.setattr(app_module, "make_objective_observation_services", fail)
    monkeypatch.setattr(app_module, "CompositeSnapshotSource", fail)
    actor = NoReadActor()

    app = _build(
        profile,
        actor=actor,
        experimental_game_plugin=True,
        plugin_rom_bytes=ROM_B,
        plugin_registry=registry,
    )

    assert plugin.composed == 1
    assert type(app.source) is CompositeSnapshotSource
    assert actor.touched == []


def _select(registry, profile, expected):
    return registry.select(
        rom_bytes=ROM_B,
        game="firered",
        profile_id=profile.profile_id,
        required_capabilities=FIRERED_CAPABILITIES,
        **({"expected_rom_sha1": expected} if expected is not None else {}),
    )


def test_direct_select_rejects_expected_mismatch_before_compose():
    profile = _profile(ROM_B)
    plugin = InventedFireRedPlugin(label="direct", profile=profile, rom_bytes=ROM_B)
    registry = ReviewedPluginRegistry(enabled=True)
    registry.register(plugin)

    with pytest.raises(PluginRefusal) as caught:
        _select(registry, profile, _digest(ROM_A))

    assert caught.value.code is RefusalCode.PROFILE_MISMATCH
    assert plugin.composed == 0


@pytest.mark.parametrize(
    "expected",
    [
        "abc",
        _digest(ROM_A).upper(),
        "z" + _digest(ROM_A)[1:],
        _digest(ROM_A) + "00",
        _CoercedLookalike(),
    ],
    ids=["short", "uppercase", "nonhex", "overlong", "coercion-trap"],
)
def test_malformed_expected_refuses_without_coercion(expected):
    profile = _profile(ROM_B)
    plugin = InventedFireRedPlugin(label="malformed", profile=profile, rom_bytes=ROM_B)
    registry = ReviewedPluginRegistry(enabled=True)
    registry.register(plugin)

    with pytest.raises(ValueError):
        _select(registry, profile, expected)

    assert plugin.composed == 0


def test_omitted_expected_keeps_legacy_lookup():
    profile = _profile(ROM_B)
    plugin = InventedFireRedPlugin(label="legacy", profile=profile, rom_bytes=ROM_B)
    registry = ReviewedPluginRegistry(enabled=True)
    registry.register(plugin)

    selection = _select(registry, profile, None)

    assert plugin.composed == 1
    assert selection.descriptor.rom_sha1 == _digest(ROM_B)
