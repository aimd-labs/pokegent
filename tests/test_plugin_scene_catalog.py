"""Plugin-owned scene catalog selection behind explicit admission (#139).

Synthetic exact-ROM bytes only, matching the existing plugin composition fixtures. The alternative
catalog is a real ``SceneCatalog`` over the checked-in specs, wrapped in a test subclass so identity
shows that the admitted factory supplied it; this is not a second-game or native claim.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

import pokegent.app as app_module
import pokegent.games.gen3.firered.admission as admission_module
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
from pokegent.games.gen3.firered.rom.rom_data import load_profiles
from pokegent.games.plugin import PluginRefusal, RefusalCode
from pokegent.state.catalog import SceneCatalog, load_scene_catalog

KNOWN_ROM = b"synthetic-firered-plugin-scene-catalog-known" * 8
UNKNOWN_ROM = b"synthetic-firered-plugin-scene-catalog-unknown" * 8


def _digest(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _test_profile(data: bytes = KNOWN_ROM):
    base = load_profiles().get("firered-us-rev0")
    return base.model_copy(
        update={"rom": base.rom.model_copy(update={"sha1": _digest(data)})}
    )


class NoReadActor:
    """Fails if any actor read/input surface is touched during composition."""

    realtime_idle_while_deciding = False

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str):
        self.touched.append(name)
        raise AssertionError(f"actor.{name} touched during composition")


def _build(profile, *, actor=None, **kwargs):
    actor = actor if actor is not None else NoReadActor()
    return build_runner(
        actor=actor,
        profile=profile,
        events=object(),
        config=AppConfig(run_id="plugin-scene-catalog", map_mode="always"),
        **kwargs,
    )


class SyntheticCatalog(SceneCatalog):
    """A distinct real catalog instance supplied by the admitted plugin fixture."""


class FalseyCatalog(SyntheticCatalog):
    """A real catalog whose caller-supplied value is falsey; precedence must ignore truthiness."""

    def __bool__(self) -> bool:
        return False


def _synthetic_catalog() -> SyntheticCatalog:
    real = load_scene_catalog()
    return SyntheticCatalog(real.scenes, version=real.version, spec_sha256=real.spec_sha256)


def _falsey_catalog() -> FalseyCatalog:
    real = load_scene_catalog()
    return FalseyCatalog(real.scenes, version=real.version, spec_sha256=real.spec_sha256)


def _with_factory(services, factory):
    values = {name: getattr(services, name) for name in FireRedServices.__dataclass_fields__}
    values["scene_catalog_factory"] = factory
    return SimpleNamespace(**values)


class SceneCatalogPlugin(FireRedPlugin):
    """Built-in plugin with a recorded catalog factory and an optional capability omission."""

    def __init__(self, profile, factory, calls, *, strip_capability=False):
        super().__init__(profile)
        self._factory = factory
        self._calls = calls
        self._strip_capability = strip_capability

    @property
    def descriptor(self):
        descriptor = super().descriptor
        if self._strip_capability:
            descriptor = replace(
                descriptor,
                capabilities=descriptor.capabilities - {"scene_catalog"},
            )
        return descriptor

    def compose(self):
        self._calls.append("compose")
        return _with_factory(super().compose(), self._factory)


def _install_plugin(monkeypatch, make_plugin):
    monkeypatch.setattr(plugin_module, "FireRedPlugin", make_plugin)


class NeverComposeMissingCatalogPlugin(SceneCatalogPlugin):
    def compose(self):
        raise AssertionError("a plugin missing scene_catalog reached compose")


def test_admitted_build_calls_the_plugin_catalog_factory_once_after_compose(monkeypatch):
    calls: list[str] = []
    catalog = _synthetic_catalog()

    def factory():
        calls.append("catalog")
        return catalog

    _install_plugin(
        monkeypatch,
        lambda profile: SceneCatalogPlugin(profile, factory, calls),
    )
    actor = NoReadActor()

    app = _build(
        _test_profile(),
        actor=actor,
        experimental_game_plugin=True,
        plugin_rom_bytes=KNOWN_ROM,
    )

    assert calls == ["compose", "catalog"]
    assert type(app.runner.perceiver.catalog) is SyntheticCatalog
    assert app.runner.perceiver.catalog is catalog
    assert app.perceiver.catalog is catalog
    assert app.bundle.registries.detectors.catalog is catalog
    assert actor.touched == []


def test_explicit_catalog_bypasses_the_plugin_factory(monkeypatch):
    calls: list[str] = []
    explicit = _synthetic_catalog()

    def factory():
        calls.append("catalog")
        raise AssertionError("an explicit caller catalog must bypass the plugin factory")

    _install_plugin(
        monkeypatch,
        lambda profile: SceneCatalogPlugin(profile, factory, calls),
    )

    app = _build(
        _test_profile(),
        experimental_game_plugin=True,
        plugin_rom_bytes=KNOWN_ROM,
        catalog=explicit,
    )

    assert calls == ["compose"]
    assert app.runner.perceiver.catalog is explicit


def test_falsey_explicit_catalog_keeps_caller_precedence(monkeypatch):
    calls: list[str] = []
    explicit = _falsey_catalog()
    assert not explicit

    def factory():
        calls.append("catalog")
        raise AssertionError("a falsey explicit catalog must still bypass the plugin factory")

    _install_plugin(
        monkeypatch,
        lambda profile: SceneCatalogPlugin(profile, factory, calls),
    )

    app = _build(
        _test_profile(),
        experimental_game_plugin=True,
        plugin_rom_bytes=KNOWN_ROM,
        catalog=explicit,
    )

    assert calls == ["compose"]
    assert app.runner.perceiver.catalog is explicit


def test_default_build_still_uses_the_app_catalog_loader(monkeypatch):
    sentinel = _synthetic_catalog()
    monkeypatch.setattr(app_module, "load_scene_catalog", lambda: sentinel)

    def fail(*args, **kwargs):
        raise AssertionError("the disabled opt-in composed the plugin")

    monkeypatch.setattr(plugin_module.FireRedPlugin, "compose", fail)

    app = _build(_test_profile())

    assert app.runner.perceiver.catalog is sentinel


@pytest.mark.parametrize(
    "flag, rom_bytes, code",
    [
        (True, UNKNOWN_ROM, RefusalCode.UNKNOWN_IDENTITY),
        (False, KNOWN_ROM, RefusalCode.DISABLED),
    ],
    ids=["unknown-digest", "disabled-flag"],
)
def test_refused_admission_never_calls_the_catalog_factory(
    monkeypatch, flag, rom_bytes, code
):
    calls: list[str] = []

    def factory():
        calls.append("catalog")
        return _synthetic_catalog()

    _install_plugin(
        monkeypatch,
        lambda profile: SceneCatalogPlugin(profile, factory, calls),
    )
    actor = NoReadActor()

    with pytest.raises(PluginRefusal) as caught:
        _build(
            _test_profile(),
            actor=actor,
            experimental_game_plugin=flag,
            plugin_rom_bytes=rom_bytes,
        )

    assert caught.value.code is code
    assert calls == []
    assert actor.touched == []


def test_missing_declared_capability_refuses_before_compose(monkeypatch):
    calls: list[str] = []
    factory_calls: list[str] = []

    def factory():
        factory_calls.append("catalog")
        return _synthetic_catalog()

    _install_plugin(
        monkeypatch,
        lambda profile: NeverComposeMissingCatalogPlugin(
            profile, factory, calls, strip_capability=True
        ),
    )
    actor = NoReadActor()

    with pytest.raises(PluginRefusal) as caught:
        _build(
            _test_profile(),
            actor=actor,
            experimental_game_plugin=True,
            plugin_rom_bytes=KNOWN_ROM,
        )

    assert caught.value.code is RefusalCode.MISSING_CAPABILITY
    assert calls == []
    assert factory_calls == []
    assert actor.touched == []


def test_missing_catalog_service_refuses_before_actor_backed_construction(monkeypatch):
    calls: list[str] = []

    def fail(*args, **kwargs):
        raise AssertionError("actor-backed construction ran without a catalog factory")

    monkeypatch.setattr(admission_module, "make_objective_observation_services", fail)
    monkeypatch.setattr(composition_module, "make_menu_services", fail)
    monkeypatch.setattr(families_module, "register_families", fail)
    monkeypatch.setattr(snapshot_sources_module, "CompositeSnapshotSource", fail)
    _install_plugin(
        monkeypatch,
        lambda profile: SceneCatalogPlugin(profile, None, calls),
    )
    actor = NoReadActor()

    with pytest.raises(PluginRefusal) as caught:
        _build(
            _test_profile(),
            actor=actor,
            experimental_game_plugin=True,
            plugin_rom_bytes=KNOWN_ROM,
        )

    assert caught.value.code is RefusalCode.MISSING_CAPABILITY
    assert calls == ["compose"]
    assert actor.touched == []


@pytest.mark.parametrize(
    "value",
    [None, object()],
    ids=["none", "noncatalog"],
)
def test_plugin_factory_must_return_a_scene_catalog_before_registries(
    monkeypatch, value
):
    calls: list[str] = []

    def fail(*args, **kwargs):
        raise AssertionError("registries were built from an invalid plugin catalog")

    monkeypatch.setattr(app_module.Registries, "new", fail)
    _install_plugin(
        monkeypatch,
        lambda profile: SceneCatalogPlugin(profile, lambda: value, calls),
    )
    actor = NoReadActor()

    with pytest.raises(PluginRefusal) as caught:
        _build(
            _test_profile(),
            actor=actor,
            experimental_game_plugin=True,
            plugin_rom_bytes=KNOWN_ROM,
        )

    assert caught.value.code is RefusalCode.MISSING_CAPABILITY
    assert calls == ["compose"]
    assert actor.touched == []


def test_explicit_catalog_does_not_require_the_plugin_catalog_capability(monkeypatch):
    calls: list[str] = []
    explicit = _synthetic_catalog()

    def factory():
        raise AssertionError("an explicit caller catalog must bypass the plugin factory")

    _install_plugin(
        monkeypatch,
        lambda profile: SceneCatalogPlugin(
            profile, factory, calls, strip_capability=True
        ),
    )

    app = _build(
        _test_profile(),
        experimental_game_plugin=True,
        plugin_rom_bytes=KNOWN_ROM,
        catalog=explicit,
    )

    assert calls == ["compose"]
    assert app.runner.perceiver.catalog is explicit
    assert "scene_catalog" in FIRERED_CAPABILITIES
