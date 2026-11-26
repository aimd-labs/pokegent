"""Python-API opt-in into an admitted plugin's navigation factory (#139/R10).

Synthetic exact-ROM digests and a sentinel navigation bundle replace the ROM parser; no ROM file,
actor read, second title or native composition is claimed.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from pokegent.app import AppConfig, build_runner
from pokegent.games.gen3.firered.plugin import FIRERED_CAPABILITIES, FireRedPlugin
from pokegent.games.gen3.firered.rom.rom_data import load_profiles
from pokegent.games.plugin import (
    GamePluginDescriptor,
    PluginRefusal,
    RefusalCode,
    ReviewedPluginRegistry,
)

KNOWN_ROM = b"synthetic-plugin-navigation-known" * 8
UNKNOWN_ROM = b"synthetic-plugin-navigation-unknown" * 8
GRAPH, NAVIGATION, DIRECTORY, UNSET = object(), object(), object(), object()


def _profile(data: bytes = KNOWN_ROM):
    """The real FireRed profile with only the invented test digest changed."""
    base = load_profiles().get("firered-us-rev0")
    sha1 = hashlib.sha1(data).hexdigest()
    return base.model_copy(update={"rom": base.rom.model_copy(update={"sha1": sha1})})


class NoReadActor:
    """Fails if admission, navigation or composition touches the actor's read/input surface."""

    realtime_idle_while_deciding = False

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str):
        self.touched.append(name)
        raise AssertionError(f"actor.{name} touched before admission/composition")


class Nav:
    """Sentinel navigation factory: records its one call and returns a chosen bundle."""

    def __init__(self, bundle=UNSET, error: BaseException | None = None) -> None:
        self.calls: list[tuple] = []
        self.bundle, self.error = bundle, error

    def __call__(self, rom_bytes, profile, *, events, needs_directory=False):
        self.calls.append((rom_bytes, profile, events, needs_directory))
        if self.error is not None:
            raise self.error
        if self.bundle is not UNSET:
            return self.bundle
        return SimpleNamespace(graph=GRAPH, navigation=NAVIGATION,
                               directory=DIRECTORY if needs_directory else None)


def _services(real, recorder: list[tuple], navigation_factory):
    def wrap(name, factory):
        def call(*args, **kwargs):
            recorder.append((name, args, kwargs))
            return factory(*args, **kwargs)
        return call

    return SimpleNamespace(
        scene_catalog_factory=real.scene_catalog_factory,
        snapshot_source_factory=real.snapshot_source_factory,
        menu_services_factory=wrap("menu_services", real.menu_services_factory),
        family_registration_factory=wrap("family_registration", real.family_registration_factory),
        objective_observation_factory=wrap(
            "objective_observation", real.objective_observation_factory),
        dex_fallback_factory=real.dex_fallback_factory,
        story_fallback_factory=real.story_fallback_factory,
        story_identity_validator=real.story_identity_validator,
        navigation_factory=navigation_factory,
    )


class NoNavigationAccess:
    """Fails if the default-off path inspects a navigation factory at all."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        if name == "navigation_factory":
            raise AssertionError("navigation_factory accessed without the opt-in")
        return getattr(self._inner, name)


class Plugin:
    """A reviewed non-built-in implementation claiming the admitted FireRed identity."""

    def __init__(self, *, profile, rom_bytes, navigation_factory=None, guard=False,
                 recorder=None, capabilities=FIRERED_CAPABILITIES) -> None:
        self.profile, self.composed = profile, 0
        self._recorder = recorder if recorder is not None else []
        self._navigation_factory, self._guard = navigation_factory, guard
        self._descriptor = GamePluginDescriptor(
            plugin_id="invented-plugin-navigation", game="firered",
            profile_id=profile.profile_id, contract_version=1,
            rom_sha1=hashlib.sha1(rom_bytes).hexdigest(), capabilities=capabilities,
        )

    @property
    def descriptor(self) -> GamePluginDescriptor:
        return self._descriptor

    def compose(self):
        self.composed += 1
        bundle = _services(FireRedPlugin(self.profile).compose(), self._recorder,
                           self._navigation_factory)
        return NoNavigationAccess(bundle) if self._guard else bundle


class RecordingRegistry(ReviewedPluginRegistry):
    """Records the exact buffer handed to selection, to prove no copy on the off path."""

    def __init__(self) -> None:
        super().__init__(enabled=True)
        self.seen: list[object] = []

    def select(self, *, rom_bytes, game, profile_id, required_capabilities=()):
        self.seen.append(rom_bytes)
        return super().select(rom_bytes=rom_bytes, game=game, profile_id=profile_id,
                              required_capabilities=required_capabilities)


def _registry(plugin) -> ReviewedPluginRegistry:
    registry = ReviewedPluginRegistry(enabled=True)
    registry.register(plugin)
    return registry


def _build(profile, *, actor=None, events=None, **kwargs):
    actor = actor if actor is not None else NoReadActor()
    events = object() if events is None else events
    return build_runner(actor=actor, profile=profile, events=events,
                        config=AppConfig(run_id="plugin-navigation", map_mode="always"), **kwargs)


def _opt_in(**kwargs):
    return {"experimental_game_plugin": True, "experimental_plugin_navigation": True,
            "plugin_rom_bytes": KNOWN_ROM, **kwargs}


def test_opt_in_calls_once_and_forwards_the_exact_bundle(tmp_path, monkeypatch):
    profile, events, recorder, nav = _profile(), object(), [], Nav()
    plugin = Plugin(profile=profile, rom_bytes=KNOWN_ROM, navigation_factory=nav,
                    recorder=recorder)
    registry = RecordingRegistry()
    registry.register(plugin)
    actor, supplied = NoReadActor(), bytearray(KNOWN_ROM)

    def fail(*args, **kwargs):
        raise AssertionError("the default app factory replaced the admitted plugin factory")

    monkeypatch.setattr("pokegent.app.make_objective_observation_services", fail)
    monkeypatch.setattr("pokegent.app.register_families", fail)
    app = _build(profile, actor=actor, events=events, objectives=tmp_path / "objective.json",
                 plugin_registry=registry, **_opt_in(plugin_rom_bytes=supplied))
    assert len(nav.calls) == 1
    rom_bytes, seen_profile, seen_events, needs_directory = nav.calls[0]
    assert type(rom_bytes) is bytes and rom_bytes == KNOWN_ROM
    assert rom_bytes is registry.seen[0] and rom_bytes is not supplied
    assert seen_profile is profile and seen_events is events and needs_directory is True
    assert [row[0] for row in recorder] == [
        "objective_observation", "menu_services", "family_registration"]
    menu = next(row for row in recorder if row[0] == "menu_services")
    family = next(row for row in recorder if row[0] == "family_registration")
    assert menu[2]["navigation"] is NAVIGATION
    assert family[2]["graph"] is GRAPH and family[2]["navigation"] is NAVIGATION
    assert app.objectives.store.directory is DIRECTORY
    assert actor.touched == []


def test_default_off_path_neither_inspects_nor_freezes_navigation():
    profile, recorder, supplied = _profile(), [], bytearray(KNOWN_ROM)
    plugin = Plugin(profile=profile, rom_bytes=KNOWN_ROM, recorder=recorder, guard=True)
    registry = RecordingRegistry()
    registry.register(plugin)
    actor = NoReadActor()
    app = _build(profile, actor=actor, experimental_game_plugin=True,
                 plugin_rom_bytes=supplied, plugin_registry=registry)
    assert registry.seen == [supplied] and registry.seen[0] is supplied
    assert [row[0] for row in recorder] == [
        "objective_observation", "menu_services", "family_registration"]
    assert app.objectives is None and actor.touched == []


@pytest.mark.parametrize("value", [1, "true", None], ids=["int", "str", "none"])
def test_navigation_flag_must_be_exactly_bool(value):
    actor = NoReadActor()
    with pytest.raises(TypeError):
        _build(_profile(), actor=actor, experimental_game_plugin=True,
               experimental_plugin_navigation=value, plugin_rom_bytes=KNOWN_ROM)
    assert actor.touched == []


def test_navigation_opt_in_without_the_game_plugin_opt_in_is_refused():
    actor = NoReadActor()
    with pytest.raises(PluginRefusal) as caught:
        _build(_profile(), actor=actor, experimental_plugin_navigation=True)
    assert caught.value.code is RefusalCode.DISABLED and actor.touched == []


@pytest.mark.parametrize("injection", [{"objective_graph": object()},
                                       {"objective_navigation": object()},
                                       {"objective_directory": object()}],
                         ids=["graph", "navigation", "directory"])
def test_supplied_navigation_objects_are_rejected_not_overridden(injection):
    profile, recorder, nav = _profile(), [], Nav()
    plugin = Plugin(profile=profile, rom_bytes=KNOWN_ROM, navigation_factory=nav,
                    recorder=recorder)
    actor = NoReadActor()
    with pytest.raises(ValueError):
        _build(profile, actor=actor, plugin_registry=_registry(plugin), **_opt_in(**injection))
    assert plugin.composed == 0 and nav.calls == [] and actor.touched == []


def test_missing_navigation_capability_refuses_before_compose_or_factory():
    profile, nav = _profile(), Nav()
    plugin = Plugin(profile=profile, rom_bytes=KNOWN_ROM, navigation_factory=nav,
                    capabilities=FIRERED_CAPABILITIES - {"navigation"})
    actor = NoReadActor()
    with pytest.raises(PluginRefusal) as caught:
        _build(profile, actor=actor, plugin_registry=_registry(plugin), **_opt_in())
    assert caught.value.code is RefusalCode.MISSING_CAPABILITY
    assert plugin.composed == 0 and nav.calls == [] and actor.touched == []


@pytest.mark.parametrize("factory", [None, object()], ids=["none", "noncallable"])
def test_claimed_navigation_capability_without_a_callable_factory_is_refused(factory):
    profile = _profile()
    plugin = Plugin(profile=profile, rom_bytes=KNOWN_ROM, navigation_factory=factory)
    actor = NoReadActor()
    with pytest.raises(PluginRefusal) as caught:
        _build(profile, actor=actor, plugin_registry=_registry(plugin), **_opt_in())
    assert caught.value.code is RefusalCode.MISSING_CAPABILITY
    assert plugin.composed == 1 and actor.touched == []


@pytest.mark.parametrize("bundle", [
    None,
    SimpleNamespace(graph=None, navigation=NAVIGATION, directory=DIRECTORY),
    SimpleNamespace(graph=GRAPH, navigation=None, directory=DIRECTORY),
], ids=["none-bundle", "no-graph", "no-navigation"])
def test_invalid_navigation_bundle_is_refused_without_fallback(bundle):
    profile, nav = _profile(), Nav(bundle=bundle)
    plugin = Plugin(profile=profile, rom_bytes=KNOWN_ROM, navigation_factory=nav)
    actor = NoReadActor()
    with pytest.raises(PluginRefusal) as caught:
        _build(profile, actor=actor, plugin_registry=_registry(plugin), **_opt_in())
    assert caught.value.code is RefusalCode.MISSING_CAPABILITY
    assert len(nav.calls) == 1 and nav.calls[0][3] is False and actor.touched == []


def test_directory_is_required_only_when_objectives_or_a_plan_need_it(tmp_path):
    profile = _profile()
    nav = Nav(bundle=SimpleNamespace(graph=GRAPH, navigation=NAVIGATION, directory=None))
    plugin = Plugin(profile=profile, rom_bytes=KNOWN_ROM, navigation_factory=nav)
    registry, actor = _registry(plugin), NoReadActor()
    app = _build(profile, actor=actor, plugin_registry=registry, **_opt_in())
    assert nav.calls[0][3] is False and app.objectives is None
    with pytest.raises(PluginRefusal) as caught:
        _build(profile, actor=actor, objectives=tmp_path / "objective.json",
               plugin_registry=registry, **_opt_in())
    assert caught.value.code is RefusalCode.MISSING_CAPABILITY
    assert nav.calls[1][3] is True and actor.touched == []


def test_unknown_rom_refuses_before_the_navigation_factory():
    profile, nav = _profile(), Nav()
    plugin = Plugin(profile=profile, rom_bytes=KNOWN_ROM, navigation_factory=nav)
    actor = NoReadActor()
    with pytest.raises(PluginRefusal) as caught:
        _build(profile, actor=actor, plugin_registry=_registry(plugin),
               **_opt_in(plugin_rom_bytes=UNKNOWN_ROM))
    assert caught.value.code is RefusalCode.UNKNOWN_IDENTITY
    assert plugin.composed == 0 and nav.calls == [] and actor.touched == []


def test_navigation_constructor_error_propagates_unchanged():
    profile, error, nav = _profile(), RuntimeError("sentinel navigation failed"), None
    nav = Nav(error=error)
    plugin = Plugin(profile=profile, rom_bytes=KNOWN_ROM, navigation_factory=nav)
    actor = NoReadActor()
    with pytest.raises(RuntimeError) as caught:
        _build(profile, actor=actor, plugin_registry=_registry(plugin), **_opt_in())
    assert caught.value is error and len(nav.calls) == 1 and actor.touched == []
