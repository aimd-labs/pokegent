"""FireRed-owned navigation construction and its plugin seam (#139).

ROM-free: recording doubles replace the existing constructors, so the synthetic bytes never
reach geometry and no path, actor or digest is read.
"""

from __future__ import annotations

import pytest

import pokegent.games.gen3.firered.navigation as navigation_module
from pokegent.games.gen3.firered.plugin import (
    FIRERED_CAPABILITIES,
    FireRedPlugin,
    FireRedServices,
)
from pokegent.games.gen3.firered.rom.rom_data import load_profiles

ROM = b"synthetic-firered-navigation" * 8
EVENTS = object()


def _profile():
    return load_profiles().get("firered-us-rev0")


class Exploding:
    """Fails on any attribute, file or path access."""

    def __getattr__(self, name):
        raise AssertionError(f"unexpected access: {name}")

    def __fspath__(self):
        raise AssertionError("unexpected path access")


class Recorder:
    def __init__(self, label, calls, result):
        self._label = label
        self._calls = calls
        self._result = result

    def __call__(self, *args, **kwargs):
        self._calls.append((self._label, args, kwargs))
        return self._result


def _wire(monkeypatch, calls):
    graph = object()
    nav_graph = object()
    adapter = object()
    directory = object()
    monkeypatch.setattr(
        navigation_module.MapGraph, "from_rom", Recorder("graph", calls, graph)
    )
    monkeypatch.setattr(
        navigation_module.NavigationGraph, "from_rom",
        Recorder("nav_graph", calls, nav_graph),
    )
    monkeypatch.setattr(
        navigation_module, "NavigationAdapter", Recorder("adapter", calls, adapter)
    )
    monkeypatch.setattr(
        "pokegent.goals.directory.RomMapDirectory",
        Recorder("directory", calls, directory),
    )
    return graph, nav_graph, adapter, directory


def test_factory_constructs_in_order_and_forwards_the_same_arguments(monkeypatch):
    calls = []
    profile = _profile()
    graph, nav_graph, adapter, directory = _wire(monkeypatch, calls)

    services = navigation_module.make_navigation_services(
        ROM, profile, events=EVENTS, needs_directory=True
    )

    assert [label for label, _, _ in calls] == [
        "graph", "nav_graph", "adapter", "directory"
    ]
    assert calls[0][1:] == ((ROM, profile), {})
    assert calls[1][1:] == ((ROM, profile), {})
    assert calls[2][1:] == ((nav_graph,), {"events": EVENTS})
    assert calls[3][1:] == ((ROM, profile), {})
    assert services.graph is graph
    assert services.navigation is adapter
    assert services.directory is directory


def test_directory_is_never_constructed_or_read_when_not_needed(monkeypatch):
    calls = []
    _wire(monkeypatch, calls)
    monkeypatch.setattr("pokegent.goals.directory.RomMapDirectory", Exploding())

    services = navigation_module.make_navigation_services(
        Exploding(), Exploding(), events=EVENTS
    )

    assert services.directory is None
    assert [label for label, _, _ in calls] == ["graph", "nav_graph", "adapter"]


def test_original_exception_propagates_and_later_steps_do_not_run(monkeypatch):
    class OriginalError(RuntimeError):
        pass

    calls = []
    _wire(monkeypatch, calls)

    def fail(*args, **kwargs):
        calls.append(("graph", args, kwargs))
        raise OriginalError("map graph refused")

    monkeypatch.setattr(navigation_module.MapGraph, "from_rom", fail)

    with pytest.raises(OriginalError, match="map graph refused"):
        navigation_module.make_navigation_services(ROM, _profile(), events=EVENTS)

    assert [label for label, _, _ in calls] == ["graph"]


def test_compose_names_the_factory_without_invoking_it(monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("compose must not invoke the navigation factory")

    monkeypatch.setattr(navigation_module, "make_navigation_services", fail)
    services = FireRedPlugin(_profile()).compose()

    assert services.navigation_factory is fail
    assert "navigation" in FIRERED_CAPABILITIES
    assert calls == []


def test_manual_services_construction_keeps_the_optional_factory():
    services = FireRedServices(
        profile=_profile(),
        adapter=object(),
        snapshot_source_factory=object(),
        scene_catalog_factory=object(),
        menu_services_factory=object(),
        family_registration_factory=object(),
        objective_observation_factory=object(),
        dex_fallback_factory=object(),
        story_fallback_factory=object(),
        story_identity_validator=object(),
        local_knowledge_factory=object(),
        pre_game_callbacks=frozenset(),
    )

    assert services.navigation_factory is None
