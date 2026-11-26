"""Caller-supplied reviewed registry injection into ``build_runner`` (#139).

Narrow, FireRed-only composition: synthetic exact-ROM bytes, no ROM file, no actor read, no
second-title or native claim. Two invented FireRed plugin implementations prove that a caller
registry's own factory bundle is selected and wired; a foreign game namespace is refused.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

import pokegent.app as app_module
from pokegent.app import AppConfig, build_runner
from pokegent.games.gen3.firered.plugin import FIRERED_CAPABILITIES, FireRedPlugin
from pokegent.games.gen3.firered.reader.snapshot_sources import CompositeSnapshotSource
from pokegent.games.gen3.firered.rom.rom_data import load_profiles
from pokegent.games.plugin import (
    GamePluginDescriptor,
    PluginRefusal,
    RefusalCode,
    ReviewedPluginRegistry,
)

KNOWN_ROM = b"synthetic-runner-registry-known" * 8
SECOND_ROM = b"synthetic-runner-registry-second" * 8
UNKNOWN_ROM = b"synthetic-runner-registry-unknown" * 8


def _digest(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _profile(data: bytes = KNOWN_ROM):
    """The real FireRed profile with only the invented test digest changed."""
    base = load_profiles().get("firered-us-rev0")
    return base.model_copy(update={"rom": base.rom.model_copy(update={"sha1": _digest(data)})})


class NoReadActor:
    """Fails if admission or composition touches the actor's read/input surface."""

    realtime_idle_while_deciding = False

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str):
        self.touched.append(name)
        raise AssertionError(f"actor.{name} touched before admission/composition")


def _invented_services(profile, label: str, recorder: list[tuple[str, str]]):
    real = FireRedPlugin(profile).compose()

    def wrap(name, factory):
        def call(*args, **kwargs):
            recorder.append((label, name))
            return factory(*args, **kwargs)

        return call

    return SimpleNamespace(
        snapshot_source_factory=wrap("snapshot_source", real.snapshot_source_factory),
        menu_services_factory=wrap("menu_services", real.menu_services_factory),
        family_registration_factory=wrap("family_registration", real.family_registration_factory),
        objective_observation_factory=wrap(
            "objective_observation", real.objective_observation_factory
        ),
        dex_fallback_factory=real.dex_fallback_factory,
        story_fallback_factory=real.story_fallback_factory,
        story_identity_validator=real.story_identity_validator,
    )


class InventedFireRedPlugin:
    """A reviewed non-built-in implementation claiming the admitted FireRed identity."""

    def __init__(
        self,
        *,
        label: str,
        profile,
        rom_bytes: bytes,
        recorder: list[tuple[str, str]] | None = None,
        game: str = "firered",
        capabilities: frozenset[str] = FIRERED_CAPABILITIES,
    ) -> None:
        self.profile = profile
        self.composed = 0
        self._label = label
        self._recorder = recorder if recorder is not None else []
        self._descriptor = GamePluginDescriptor(
            plugin_id=f"invented-firered-{label}",
            game=game,
            profile_id=profile.profile_id,
            contract_version=1,
            rom_sha1=_digest(rom_bytes),
            capabilities=capabilities,
        )

    @property
    def descriptor(self) -> GamePluginDescriptor:
        return self._descriptor

    def compose(self):
        self.composed += 1
        return _invented_services(self.profile, self._label, self._recorder)


def _build(profile, *, actor=None, **kwargs):
    actor = actor if actor is not None else NoReadActor()
    return build_runner(
        actor=actor,
        profile=profile,
        events=object(),
        config=AppConfig(run_id="runner-registry", map_mode="always"),
        **kwargs,
    )


def test_none_registry_keeps_the_builtin_firered_path():
    profile = _profile()
    actor = NoReadActor()

    app = _build(
        profile,
        actor=actor,
        experimental_game_plugin=True,
        plugin_rom_bytes=KNOWN_ROM,
    )

    assert type(app.source) is CompositeSnapshotSource
    assert app.source.profile is profile
    assert app.source._source is actor
    assert app.source.sidecar is app.bundle.sidecar
    assert app.source._live._run_id == "runner-registry"
    assert actor.touched == []


@pytest.mark.parametrize(("label", "rom_bytes"), [("first", KNOWN_ROM), ("second", SECOND_ROM)])
def test_caller_registry_admits_its_own_firered_bundle(label, rom_bytes, monkeypatch):
    profile = _profile(rom_bytes)
    recorder: list[tuple[str, str]] = []
    plugin = InventedFireRedPlugin(
        label=label, profile=profile, rom_bytes=rom_bytes, recorder=recorder
    )
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
        plugin_rom_bytes=rom_bytes,
        plugin_registry=registry,
    )

    assert plugin.composed == 1
    assert recorder == [
        (label, "objective_observation"),
        (label, "menu_services"),
        (label, "family_registration"),
        (label, "snapshot_source"),
    ]
    assert type(app.source) is CompositeSnapshotSource
    assert app.source.profile is profile
    assert app.source._source is actor
    assert app.source.sidecar is app.bundle.sidecar
    assert app.source._live._run_id == "runner-registry"
    assert actor.touched == []


def test_foreign_game_namespace_is_refused_not_composed():
    profile = _profile()
    plugin = InventedFireRedPlugin(
        label="foreign", profile=profile, rom_bytes=KNOWN_ROM, game="synthetic-other"
    )
    registry = ReviewedPluginRegistry(enabled=True)
    registry.register(plugin)
    actor = NoReadActor()

    with pytest.raises(PluginRefusal) as caught:
        _build(
            profile,
            actor=actor,
            experimental_game_plugin=True,
            plugin_rom_bytes=KNOWN_ROM,
            plugin_registry=registry,
        )

    assert caught.value.code is RefusalCode.PROFILE_MISMATCH
    assert plugin.composed == 0
    assert actor.touched == []


def test_empty_caller_registry_refuses_without_mutation_or_builtin_fallback():
    profile = _profile()
    registry = ReviewedPluginRegistry(enabled=True)
    actor = NoReadActor()

    with pytest.raises(PluginRefusal) as caught:
        _build(
            profile,
            actor=actor,
            experimental_game_plugin=True,
            plugin_rom_bytes=KNOWN_ROM,
            plugin_registry=registry,
        )

    assert caught.value.code is RefusalCode.UNKNOWN_IDENTITY
    assert actor.touched == []
    with pytest.raises(PluginRefusal) as again:
        registry.select(
            rom_bytes=KNOWN_ROM,
            game="firered",
            profile_id=profile.profile_id,
            required_capabilities=FIRERED_CAPABILITIES,
        )
    assert again.value.code is RefusalCode.UNKNOWN_IDENTITY


def test_disabled_caller_registry_refuses_before_compose_or_actor():
    profile = _profile()
    plugin = InventedFireRedPlugin(label="disabled", profile=profile, rom_bytes=KNOWN_ROM)
    registry = ReviewedPluginRegistry(enabled=False)
    registry.register(plugin)
    actor = NoReadActor()

    with pytest.raises(PluginRefusal) as caught:
        _build(
            profile,
            actor=actor,
            experimental_game_plugin=True,
            plugin_rom_bytes=KNOWN_ROM,
            plugin_registry=registry,
        )

    assert caught.value.code is RefusalCode.DISABLED
    assert plugin.composed == 0
    assert actor.touched == []


def test_unknown_rom_bytes_refuse_before_compose_or_actor():
    profile = _profile()
    plugin = InventedFireRedPlugin(label="unknown", profile=profile, rom_bytes=KNOWN_ROM)
    registry = ReviewedPluginRegistry(enabled=True)
    registry.register(plugin)
    actor = NoReadActor()

    with pytest.raises(PluginRefusal) as caught:
        _build(
            profile,
            actor=actor,
            experimental_game_plugin=True,
            plugin_rom_bytes=UNKNOWN_ROM,
            plugin_registry=registry,
        )

    assert caught.value.code is RefusalCode.UNKNOWN_IDENTITY
    assert plugin.composed == 0
    assert actor.touched == []


def test_profile_mismatch_refuses_before_compose_or_actor():
    profile = _profile()
    plugin = InventedFireRedPlugin(label="known", profile=profile, rom_bytes=KNOWN_ROM)
    registry = ReviewedPluginRegistry(enabled=True)
    registry.register(plugin)
    other = profile.model_copy(update={"profile_id": "synthetic-other-profile"})
    actor = NoReadActor()

    with pytest.raises(PluginRefusal) as caught:
        _build(
            other,
            actor=actor,
            experimental_game_plugin=True,
            plugin_rom_bytes=KNOWN_ROM,
            plugin_registry=registry,
        )

    assert caught.value.code is RefusalCode.PROFILE_MISMATCH
    assert plugin.composed == 0
    assert actor.touched == []


def test_missing_capability_refuses_before_compose_or_actor():
    profile = _profile()
    plugin = InventedFireRedPlugin(
        label="missing-capability",
        profile=profile,
        rom_bytes=KNOWN_ROM,
        capabilities=FIRERED_CAPABILITIES - {"objective_observation"},
    )
    registry = ReviewedPluginRegistry(enabled=True)
    registry.register(plugin)
    actor = NoReadActor()

    with pytest.raises(PluginRefusal) as caught:
        _build(
            profile,
            actor=actor,
            experimental_game_plugin=True,
            plugin_rom_bytes=KNOWN_ROM,
            plugin_registry=registry,
        )

    assert caught.value.code is RefusalCode.MISSING_CAPABILITY
    assert plugin.composed == 0
    assert actor.touched == []


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
def test_supplied_registry_still_runs_the_firered_profile_gate(mutate):
    profile = mutate(_profile())
    registry = ReviewedPluginRegistry(enabled=True)
    actor = NoReadActor()

    with pytest.raises(PluginRefusal) as caught:
        _build(
            profile,
            actor=actor,
            experimental_game_plugin=True,
            plugin_rom_bytes=KNOWN_ROM,
            plugin_registry=registry,
        )

    assert caught.value.code is RefusalCode.PROFILE_MISMATCH
    assert actor.touched == []


@pytest.mark.parametrize(
    "registry",
    [object(), SimpleNamespace(select=lambda **kwargs: None)],
    ids=["object", "duck-typed"],
)
def test_registry_must_be_a_reviewed_registry(registry):
    actor = NoReadActor()

    with pytest.raises(TypeError):
        _build(
            _profile(),
            actor=actor,
            experimental_game_plugin=True,
            plugin_rom_bytes=KNOWN_ROM,
            plugin_registry=registry,
        )

    assert actor.touched == []


def test_orphan_registry_without_the_opt_in_is_refused_before_bytes():
    profile = _profile()
    plugin = InventedFireRedPlugin(label="orphan", profile=profile, rom_bytes=KNOWN_ROM)
    registry = ReviewedPluginRegistry(enabled=True)
    registry.register(plugin)
    actor = NoReadActor()

    with pytest.raises(PluginRefusal) as caught:
        _build(profile, actor=actor, plugin_registry=registry)

    assert caught.value.code is RefusalCode.DISABLED
    assert plugin.composed == 0
    assert actor.touched == []
