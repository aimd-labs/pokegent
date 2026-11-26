"""Python-API opt-in into an admitted plugin's navigation factory (#139/R10)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_runner_plugin_registry import (
    KNOWN_ROM,
    UNKNOWN_ROM,
    InventedFireRedPlugin,
    NoReadActor,
    _build,
    _profile,
)

import pokegent.games.plugin as plugin_module
from pokegent.games.plugin import PluginRefusal, RefusalCode, ReviewedPluginRegistry

GRAPH, NAVIGATION, DIRECTORY = object(), object(), object()


class PoisonBytes(bytes):
    """A bytes subclass whose conversion callback must not run on the opt-in path."""

    def __bytes__(self):
        raise AssertionError("bytes conversion ran before bounded validation")


class NoNavigationAccess:
    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        assert name != "navigation_factory", "navigation_factory accessed without the opt-in"
        return getattr(self._inner, name)


class NavPlugin(InventedFireRedPlugin):
    """Adds a navigation factory plus menu/family mocks to an existing invented plugin."""

    def __init__(self, *, navigation_factory, guard=False, **kwargs):
        super().__init__(**kwargs)
        self.navigation_factory, self.guard = navigation_factory, guard

    def compose(self):
        services = super().compose()
        services.navigation_factory = self.navigation_factory
        services.menu_services_factory = self.menu = Mock(
            wraps=services.menu_services_factory)
        services.family_registration_factory = self.family = Mock(
            wraps=services.family_registration_factory)
        return NoNavigationAccess(services) if self.guard else services


def _registry(plugin):
    registry = ReviewedPluginRegistry(enabled=True)
    registry.register(plugin)
    return registry


@pytest.fixture
def live():
    profile, nav = _profile(), Mock(return_value=SimpleNamespace(
        graph=GRAPH, navigation=NAVIGATION, directory=DIRECTORY))
    plugin = NavPlugin(label="navigation", profile=profile, rom_bytes=KNOWN_ROM,
                       navigation_factory=nav)
    registry, actor = _registry(plugin), NoReadActor()
    registry.select = select = Mock(wraps=registry.select)

    def build(**kwargs):
        options = {"experimental_game_plugin": True,
                   "experimental_plugin_navigation": True,
                   "plugin_rom_bytes": KNOWN_ROM, "plugin_registry": registry}
        options.update(kwargs)
        return _build(profile, actor=actor, **options)

    yield SimpleNamespace(profile=profile, nav=nav, plugin=plugin, select=select, build=build)
    assert actor.touched == []


def test_opt_in_freezes_once_and_forwards_the_exact_bundle(tmp_path, live):
    supplied = PoisonBytes(KNOWN_ROM)
    app = live.build(objectives=tmp_path / "objective.json", plugin_rom_bytes=supplied)
    assert live.nav.call_count == 1
    rom, profile = live.nav.call_args.args
    assert type(rom) is bytes and rom == KNOWN_ROM and rom is not supplied
    assert rom is live.select.call_args.kwargs["rom_bytes"]
    assert profile is live.profile
    assert live.nav.call_args.kwargs["events"] is app.events
    assert live.nav.call_args.kwargs["needs_directory"] is True
    menu, family = live.plugin.menu.call_args, live.plugin.family.call_args
    assert live.plugin.menu.call_count == live.plugin.family.call_count == 1
    assert menu.kwargs["navigation"] is NAVIGATION
    assert family.kwargs["graph"] is GRAPH
    assert family.kwargs["navigation"] is NAVIGATION
    assert app.objectives.store.directory is DIRECTORY


@pytest.mark.parametrize(("rom", "error", "cap"), [(1, TypeError, None),
    (PoisonBytes(KNOWN_ROM), ValueError, len(KNOWN_ROM) - 1),
    (memoryview(KNOWN_ROM).cast("I"), ValueError, len(memoryview(KNOWN_ROM).cast("I")))],
    ids=["int", "over-cap", "view-nbytes"])
def test_bounded_rom_is_checked_before_freezing(monkeypatch, live, rom, error, cap):
    if cap is not None:
        monkeypatch.setattr(plugin_module, "MAX_ROM_BYTES", cap)
    with pytest.raises(error):
        live.build(plugin_rom_bytes=rom)
    assert live.plugin.composed == 0 and live.nav.call_count == 0


def test_default_off_forwards_original_buffer_without_inspecting_navigation(live):
    live.plugin.guard = True
    supplied = bytearray(KNOWN_ROM)
    app = live.build(experimental_plugin_navigation=False, plugin_rom_bytes=supplied)
    assert live.select.call_args.kwargs["rom_bytes"] is supplied
    assert live.nav.call_count == 0 and app.objectives is None


@pytest.mark.parametrize("value", [1, "true", None], ids=["int", "str", "none"])
def test_navigation_flag_must_be_exactly_bool(live, value):
    with pytest.raises(TypeError):
        live.build(experimental_plugin_navigation=value)
    assert live.nav.call_count == 0


def test_navigation_opt_in_without_game_plugin_is_refused(live):
    with pytest.raises(PluginRefusal) as caught:
        live.build(experimental_game_plugin=False)
    assert caught.value.code is RefusalCode.DISABLED
    assert live.plugin.composed == 0 and live.nav.call_count == 0


@pytest.mark.parametrize("name", ["objective_graph", "objective_navigation",
                                  "objective_directory"])
def test_supplied_navigation_object_is_rejected_not_overridden(live, name):
    with pytest.raises(ValueError):
        live.build(**{name: object()})
    assert live.plugin.composed == 0 and live.nav.call_count == 0


def test_missing_navigation_capability_refuses_before_compose(live):
    plugin = NavPlugin(label="no-nav", profile=live.profile, rom_bytes=KNOWN_ROM,
                       navigation_factory=Mock(),
                       capabilities=live.plugin.descriptor.capabilities - {"navigation"})
    with pytest.raises(PluginRefusal) as caught:
        live.build(plugin_registry=_registry(plugin))
    assert caught.value.code is RefusalCode.MISSING_CAPABILITY
    assert plugin.composed == 0 and live.nav.call_count == 0


@pytest.mark.parametrize("factory", [None, object()], ids=["none", "noncallable"])
def test_claimed_navigation_without_a_callable_factory_is_refused(live, factory):
    plugin = NavPlugin(label="bad-factory", profile=live.profile, rom_bytes=KNOWN_ROM,
                       navigation_factory=factory)
    with pytest.raises(PluginRefusal) as caught:
        live.build(plugin_registry=_registry(plugin))
    assert caught.value.code is RefusalCode.MISSING_CAPABILITY and plugin.composed == 1


@pytest.mark.parametrize("bundle", [
    None,
    SimpleNamespace(graph=None, navigation=NAVIGATION, directory=DIRECTORY),
    SimpleNamespace(graph=GRAPH, navigation=None, directory=DIRECTORY),
], ids=["none", "no-graph", "no-navigation"])
def test_invalid_bundle_is_refused_without_fallback(live, bundle):
    live.nav.return_value = bundle
    with pytest.raises(PluginRefusal) as caught:
        live.build()
    assert caught.value.code is RefusalCode.MISSING_CAPABILITY and live.nav.call_count == 1
    assert live.nav.call_args.kwargs["needs_directory"] is False


def test_directory_is_required_only_when_objectives_need_it(tmp_path, live):
    live.nav.return_value = SimpleNamespace(
        graph=GRAPH, navigation=NAVIGATION, directory=None)
    app = live.build()
    with pytest.raises(PluginRefusal) as caught:
        live.build(objectives=tmp_path / "objective.json")
    assert app.objectives is None and caught.value.code is RefusalCode.MISSING_CAPABILITY
    assert [call.kwargs["needs_directory"] for call in live.nav.call_args_list] == [False, True]


@pytest.mark.parametrize("rom", [None, UNKNOWN_ROM], ids=["none", "unknown"])
def test_unknown_rom_refuses_before_navigation(live, rom):
    with pytest.raises(PluginRefusal) as caught:
        live.build(plugin_rom_bytes=rom)
    assert caught.value.code is RefusalCode.UNKNOWN_IDENTITY
    assert live.plugin.composed == 0 and live.nav.call_count == 0


def test_navigation_constructor_error_propagates_unchanged(live):
    error = RuntimeError("sentinel navigation failed")
    live.nav.side_effect = error
    with pytest.raises(RuntimeError) as caught:
        live.build()
    assert caught.value is error and live.nav.call_count == 1
