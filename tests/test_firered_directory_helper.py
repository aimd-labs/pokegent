"""FireRed objective-directory helper construction and its plugin seam (#139).

ROM-free: recording doubles replace the existing constructors, so synthetic bytes never reach
geometry and no path, actor or digest is read.
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

ROM = b"synthetic-firered-objective-directory" * 8


def _profile():
    return load_profiles().get("firered-us-rev0")


class Recorder:
    def __init__(self, label, calls, result):
        self._label = label
        self._calls = calls
        self._result = result

    def __call__(self, *args, **kwargs):
        self._calls.append((self._label, args, kwargs))
        return self._result


def _wire_navigation(monkeypatch, calls):
    graph, adapter = object(), object()
    monkeypatch.setattr(navigation_module.MapGraph, "from_rom",
                        Recorder("graph", calls, graph))
    monkeypatch.setattr(navigation_module.NavigationGraph, "from_rom",
                        Recorder("nav_graph", calls, object()))
    monkeypatch.setattr(navigation_module, "NavigationAdapter",
                        Recorder("adapter", calls, adapter))
    return graph, adapter


def test_helper_forwards_the_same_bytes_and_profile_directly(monkeypatch):
    calls = []
    directory = object()
    profile = _profile()
    monkeypatch.setattr(
        "pokegent.goals.directory.RomMapDirectory",
        Recorder("directory", calls, directory),
    )

    result = navigation_module.make_objective_directory(ROM, profile)
    assert result is directory
    assert [label for label, _, _ in calls] == ["directory"]
    assert calls[0][1:] == ((ROM, profile), {})


def test_helper_does_not_catch_or_wrap_the_original_error(monkeypatch):
    class OriginalError(RuntimeError):
        pass

    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise OriginalError("directory refused")

    monkeypatch.setattr("pokegent.goals.directory.RomMapDirectory", fail)

    with pytest.raises(OriginalError, match="directory refused"):
        navigation_module.make_objective_directory(ROM, _profile())

    assert len(calls) == 1


def test_navigation_factory_uses_the_helper_only_when_directory_is_needed(monkeypatch):
    calls = []
    directory = object()
    profile = _profile()
    graph, adapter = _wire_navigation(monkeypatch, calls)
    monkeypatch.setattr(
        navigation_module, "make_objective_directory",
        Recorder("directory", calls, directory),
    )

    services = navigation_module.make_navigation_services(
        ROM, profile, events=object(), needs_directory=True
    )

    assert [label for label, _, _ in calls] == [
        "graph", "nav_graph", "adapter", "directory"
    ]
    assert calls[3][1:] == ((ROM, profile), {})
    assert services.graph is graph
    assert services.navigation is adapter
    assert services.directory is directory

    calls.clear()
    inert = navigation_module.make_navigation_services(ROM, profile, events=object())
    assert [label for label, _, _ in calls] == ["graph", "nav_graph", "adapter"]
    assert inert.directory is None


def test_compose_names_the_directory_helper_without_invoking_it(monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("compose must not invoke the objective directory helper")

    monkeypatch.setattr(navigation_module, "make_objective_directory", fail)

    services = FireRedPlugin(_profile()).compose()
    assert services.objective_directory_factory is fail
    assert "objective_directory" in FIRERED_CAPABILITIES
    assert calls == []


def test_manual_services_construction_keeps_the_optional_factory():
    services = FireRedServices(
        _profile(), object(), object(), object(), object(), object(),
        object(), object(), object(), object(), object(), frozenset(),
    )
    assert services.objective_directory_factory is None
