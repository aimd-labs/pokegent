"""Plugin-owned menu/family factory integration behind explicit admission (#139).

Synthetic exact-ROM bytes only. Every experimental test wraps the real game-owned helpers so the
assertions can show which composition seam actually ran; no ROM file, actor read or profile edit
is involved.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

import pokegent.app as app_module
import pokegent.games.gen3.firered.composition as composition_module
import pokegent.games.gen3.firered.families as families_module
import pokegent.games.gen3.firered.plugin as plugin_module
import pokegent.games.gen3.firered.reader.snapshot_sources as snapshot_sources_module
from pokegent.app import AppConfig, build_runner
from pokegent.games.gen3.firered.plugin import (
    FIRERED_CAPABILITIES,
    FireRedPlugin,
    FireRedServices,
)
from pokegent.games.gen3.firered.reader.snapshot_sources import CompositeSnapshotSource
from pokegent.games.gen3.firered.rom.rom_data import load_profiles
from pokegent.games.plugin import PluginRefusal, RefusalCode

KNOWN_ROM = b"synthetic-firered-plugin-composition-known" * 8
UNKNOWN_ROM = b"synthetic-firered-plugin-composition-unknown" * 8


def _digest(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _test_profile(data: bytes = KNOWN_ROM):
    """The real FireRed profile with only the invented test digest changed."""
    base = load_profiles().get("firered-us-rev0")
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


class Recording:
    """Calls and records the real helper; it never replaces its behavior."""

    def __init__(self, real) -> None:
        self.real = real
        self.calls: list[tuple[tuple, dict, object]] = []

    def __call__(self, *args, **kwargs):
        result = self.real(*args, **kwargs)
        self.calls.append((args, kwargs, result))
        return result


def _build(profile, *, actor=None, **kwargs):
    actor = actor if actor is not None else NoReadActor()
    return build_runner(
        actor=actor,
        profile=profile,
        events=object(),
        config=AppConfig(run_id="plugin-composition", map_mode="always"),
        **kwargs,
    )


class RecordingSource(CompositeSnapshotSource):
    """Counts constructions without changing the source's behavior."""

    calls = 0

    def __init__(self, *args, **kwargs):
        type(self).calls += 1
        super().__init__(*args, **kwargs)


def test_compose_stores_the_game_owned_factories_without_calling_them(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("compose must only reference the supplied factories")

    monkeypatch.setattr(composition_module, "make_menu_services", fail)
    monkeypatch.setattr(families_module, "register_families", fail)

    services = FireRedPlugin(_test_profile()).compose()

    assert isinstance(services, FireRedServices)
    assert services.menu_services_factory is fail
    assert services.family_registration_factory is fail


def test_capabilities_cover_exactly_the_supplied_source_factories():
    descriptor = FireRedPlugin(_test_profile()).descriptor

    assert descriptor.capabilities == FIRERED_CAPABILITIES
    assert FIRERED_CAPABILITIES == {
        "snapshot_source",
        "reader",
        "scene_catalog",
        "menu_services",
        "family_registration",
        "objective_observation",
        "dex_fallback",
        "story_fallback",
        "pre_game_callbacks",
        "local_knowledge",
        "battle_facts",
        "observation_projection",
    }
    assert not FIRERED_CAPABILITIES & {"verification", "supported", "input"}


def test_admitted_build_uses_the_composed_factories_once(monkeypatch):
    menu_spy = Recording(composition_module.make_menu_services)
    family_spy = Recording(families_module.register_families)
    monkeypatch.setattr(composition_module, "make_menu_services", menu_spy)
    monkeypatch.setattr(families_module, "register_families", family_spy)
    monkeypatch.setattr(snapshot_sources_module, "CompositeSnapshotSource", RecordingSource)
    monkeypatch.setattr(RecordingSource, "calls", 0)

    def fail_default_registrar(*args, **kwargs):
        raise AssertionError("admitted build used the app-level default registrar")

    monkeypatch.setattr(app_module, "register_families", fail_default_registrar)

    profile = _test_profile()
    actor = NoReadActor()
    app = _build(
        profile,
        actor=actor,
        experimental_game_plugin=True,
        plugin_rom_bytes=KNOWN_ROM,
    )

    assert len(menu_spy.calls) == 1
    assert len(family_spy.calls) == 1
    assert RecordingSource.calls == 1
    assert type(app.source) is RecordingSource
    assert app.source.profile is profile
    assert app.source.sidecar is app.bundle.sidecar
    assert app.source._source is actor
    assert family_spy.calls[0][2] is app.bundle
    assert actor.touched == []


def test_default_flag_keeps_the_app_helpers_and_never_composes(monkeypatch):
    menu_spy = Recording(composition_module.make_menu_services)
    family_spy = Recording(app_module.register_families)
    monkeypatch.setattr(composition_module, "make_menu_services", menu_spy)
    monkeypatch.setattr(app_module, "register_families", family_spy)

    def fail(*args, **kwargs):
        raise AssertionError("the disabled opt-in composed the plugin")

    monkeypatch.setattr(plugin_module.FireRedPlugin, "compose", fail)

    actor = NoReadActor()
    app = _build(_test_profile(), actor=actor)

    assert len(menu_spy.calls) == 1
    assert len(family_spy.calls) == 1
    assert type(app.source) is CompositeSnapshotSource
    assert app.source._source is actor
    assert actor.touched == []


class MissingFactoryCapabilityPlugin(FireRedPlugin):
    """A reviewed plugin that supplies neither new source factory."""

    @property
    def descriptor(self):
        return replace(
            super().descriptor,
            capabilities=frozenset({"snapshot_source", "reader"}),
        )

    def compose(self):
        raise AssertionError("a plugin missing factory capabilities reached compose")


def test_missing_factory_capability_refuses_before_compose(monkeypatch):
    monkeypatch.setattr(plugin_module, "FireRedPlugin", MissingFactoryCapabilityPlugin)
    menu_spy = Recording(composition_module.make_menu_services)
    family_spy = Recording(families_module.register_families)
    monkeypatch.setattr(composition_module, "make_menu_services", menu_spy)
    monkeypatch.setattr(families_module, "register_families", family_spy)
    actor = NoReadActor()

    with pytest.raises(PluginRefusal) as caught:
        _build(
            _test_profile(),
            actor=actor,
            experimental_game_plugin=True,
            plugin_rom_bytes=KNOWN_ROM,
        )

    assert caught.value.code is RefusalCode.MISSING_CAPABILITY
    assert menu_spy.calls == []
    assert family_spy.calls == []
    assert actor.touched == []


@pytest.mark.parametrize(
    "rom_bytes, mutator, code",
    [
        (
            UNKNOWN_ROM,
            lambda profile: profile,
            RefusalCode.UNKNOWN_IDENTITY,
        ),
        (
            KNOWN_ROM,
            lambda profile: profile.model_copy(update={"supported": False}),
            RefusalCode.PROFILE_MISMATCH,
        ),
        (
            KNOWN_ROM,
            lambda profile: profile.model_copy(
                update={"rom": profile.rom.model_copy(update={"game_code": "AXVE"})}
            ),
            RefusalCode.PROFILE_MISMATCH,
        ),
    ],
    ids=["unknown-digest", "unsupported", "non-bpre"],
)
def test_refused_admission_never_calls_a_factory(rom_bytes, mutator, code, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("refused admission reached compose")

    monkeypatch.setattr(plugin_module.FireRedPlugin, "compose", fail)
    menu_spy = Recording(composition_module.make_menu_services)
    family_spy = Recording(families_module.register_families)
    monkeypatch.setattr(composition_module, "make_menu_services", menu_spy)
    monkeypatch.setattr(families_module, "register_families", family_spy)
    actor = NoReadActor()

    with pytest.raises(PluginRefusal) as caught:
        _build(
            mutator(_test_profile()),
            actor=actor,
            experimental_game_plugin=True,
            plugin_rom_bytes=rom_bytes,
        )

    assert caught.value.code is code
    assert menu_spy.calls == []
    assert family_spy.calls == []
    assert actor.touched == []
