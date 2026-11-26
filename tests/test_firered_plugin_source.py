"""Built-in FireRed source composition behind explicit, default-off admission (#139).

Synthetic bytes only: the checked-in profile is copied for the test digest, so no ROM,
no production profile/hash edit and no actor read is needed for composition.
"""

from __future__ import annotations

import hashlib

import pytest

import pokegent.games.gen3.firered.plugin as plugin_module
from pokegent.app import AppConfig, build_runner
from pokegent.games.gen3.firered.plugin import (
    FIRERED_CAPABILITIES,
    FireRedPlugin,
    FireRedServices,
)
from pokegent.games.gen3.firered.reader.adapter import FireRedAdapter
from pokegent.games.gen3.firered.reader.battle_reader import battle_of
from pokegent.games.gen3.firered.reader.reader import FireRedReader
from pokegent.games.gen3.firered.reader.snapshot_sources import CompositeSnapshotSource
from pokegent.games.gen3.firered.reader.ui import UiEvidenceReader
from pokegent.games.gen3.firered.rom.rom_data import load_profiles
from pokegent.games.plugin import (
    GamePluginDescriptor,
    PluginRefusal,
    RefusalCode,
    ReviewedPluginRegistry,
)

KNOWN_ROM = b"synthetic-firered-plugin-source-known" * 8
UNKNOWN_ROM = b"synthetic-firered-plugin-source-unknown" * 8


def _digest(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _base_profile():
    return load_profiles().get("firered-us-rev0")


def _test_profile(data: bytes = KNOWN_ROM):
    """The real FireRed profile with only the invented test digest changed."""
    base = _base_profile()
    return base.model_copy(
        update={"rom": base.rom.model_copy(update={"sha1": _digest(data)})}
    )


class NoReadActor:
    """Fails if composition or admission touches the actor's read/input surface."""

    realtime_idle_while_deciding = False

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str):
        self.touched.append(name)
        raise AssertionError(f"actor.{name} touched before admission/composition")


def _build(profile, *, actor=None, **kwargs):
    actor = actor if actor is not None else NoReadActor()
    return build_runner(
        actor=actor,
        profile=profile,
        events=object(),
        config=AppConfig(run_id="plugin-source", map_mode="always"),
        **kwargs,
    )


def test_descriptor_and_compose_use_the_real_firered_composition():
    profile = _test_profile()
    plugin = FireRedPlugin(profile)

    assert isinstance(plugin.descriptor, GamePluginDescriptor)
    assert plugin.descriptor.game == "firered"
    assert plugin.descriptor.profile_id == profile.profile_id
    assert plugin.descriptor.contract_version == 1
    assert plugin.descriptor.rom_sha1 == profile.rom.sha1 == _digest(KNOWN_ROM)
    assert plugin.descriptor.capabilities == FIRERED_CAPABILITIES
    assert "snapshot_source" in FIRERED_CAPABILITIES
    assert "reader" in FIRERED_CAPABILITIES
    assert not FIRERED_CAPABILITIES & {"verification", "supported", "input"}

    services = plugin.compose()
    assert isinstance(services, FireRedServices)
    assert services.profile is profile
    assert isinstance(services.adapter, FireRedAdapter)
    assert services.adapter.profile is profile
    assert services.adapter.readers["snapshot"] is FireRedReader
    assert services.adapter.readers["ui"] is UiEvidenceReader
    assert services.adapter.readers["battle"] is battle_of
    assert {"items", "menu", "battle"} <= set(services.adapter.rules)
    assert services.snapshot_source_factory is CompositeSnapshotSource


def test_registry_admits_the_builtin_plugin_only_for_the_exact_test_digest():
    profile = _test_profile()
    plugin = FireRedPlugin(profile)
    registry = ReviewedPluginRegistry(enabled=True)
    registry.register(plugin)

    selection = registry.select(
        rom_bytes=KNOWN_ROM,
        game="firered",
        profile_id=profile.profile_id,
        required_capabilities=FIRERED_CAPABILITIES,
    )

    assert selection.descriptor is plugin.descriptor
    assert selection.services.profile is profile
    assert selection.services.snapshot_source_factory is CompositeSnapshotSource


@pytest.mark.parametrize(
    "mutate",
    [
        lambda profile: profile.model_copy(update={"supported": False}),
        lambda profile: profile.model_copy(
            update={"rom": profile.rom.model_copy(update={"game_code": "AXVE"})}
        ),
        lambda profile: profile.model_copy(
            update={"rom": profile.rom.model_copy(update={"version": 1})}
        ),
    ],
    ids=["unsupported", "non-bpre", "version-one"],
)
def test_plugin_refuses_unsupported_or_non_firered_identity(mutate):
    with pytest.raises(PluginRefusal) as caught:
        FireRedPlugin(mutate(_test_profile()))

    assert caught.value.code is RefusalCode.PROFILE_MISMATCH


def test_compose_never_claims_option_registration(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("plugin composition must not own option registration")

    monkeypatch.setattr(FireRedAdapter, "register_options", fail)
    services = FireRedPlugin(_test_profile()).compose()
    assert services.adapter.readers["snapshot"] is FireRedReader


def test_default_build_runner_path_is_unchanged():
    profile = _test_profile()
    actor = NoReadActor()

    app = _build(profile, actor=actor)

    assert type(app.source) is CompositeSnapshotSource
    assert app.source.profile is profile
    assert app.source.sidecar is app.bundle.sidecar
    assert app.source.party_source == "live"
    assert app.source._map_mode == "always"
    assert app.source._source is actor
    assert actor.touched == []


def test_enabled_build_runner_uses_the_admitted_source_factory_unchanged():
    profile = _test_profile()
    actor = NoReadActor()

    app = _build(
        profile,
        actor=actor,
        experimental_game_plugin=True,
        plugin_rom_bytes=KNOWN_ROM,
    )

    assert type(app.source) is CompositeSnapshotSource
    assert app.source.profile is profile
    assert app.source.sidecar is app.bundle.sidecar
    assert app.source.party_source == "live"
    assert app.source._map_mode == "always"
    assert app.source._source is actor
    assert app.source._live._run_id == "plugin-source"
    assert actor.touched == []


def test_disabled_flag_with_bytes_is_refused_before_composition(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("disabled opt-in reached compose")

    monkeypatch.setattr(plugin_module.FireRedPlugin, "compose", fail)
    actor = NoReadActor()

    with pytest.raises(PluginRefusal) as caught:
        _build(_test_profile(), actor=actor, plugin_rom_bytes=KNOWN_ROM)

    assert caught.value.code is RefusalCode.DISABLED
    assert actor.touched == []


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_opt_in_flag_must_be_exactly_bool(value, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("invalid opt-in reached compose")

    monkeypatch.setattr(plugin_module.FireRedPlugin, "compose", fail)
    actor = NoReadActor()

    with pytest.raises(TypeError):
        _build(_test_profile(), actor=actor, experimental_game_plugin=value)

    assert actor.touched == []


@pytest.mark.parametrize(
    "rom_bytes, mutator",
    [
        (None, lambda profile: profile),
        (UNKNOWN_ROM, lambda profile: profile),
        (KNOWN_ROM, lambda profile: profile.model_copy(update={"supported": False})),
        (
            KNOWN_ROM,
            lambda profile: profile.model_copy(
                update={"rom": profile.rom.model_copy(update={"game_code": "AXVE"})}
            ),
        ),
        (
            KNOWN_ROM,
            lambda profile: profile.model_copy(
                update={"rom": profile.rom.model_copy(update={"version": 1})}
            ),
        ),
    ],
    ids=["missing-bytes", "unknown-digest", "unsupported", "non-bpre", "version-one"],
)
def test_enabled_admission_refuses_before_composition(rom_bytes, mutator, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("refused admission reached compose")

    monkeypatch.setattr(plugin_module.FireRedPlugin, "compose", fail)
    actor = NoReadActor()

    with pytest.raises(PluginRefusal):
        _build(
            mutator(_test_profile()),
            actor=actor,
            experimental_game_plugin=True,
            plugin_rom_bytes=rom_bytes,
        )

    assert actor.touched == []
