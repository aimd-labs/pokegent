"""FireRed's game-owned menu composition (#139), synthetic only."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

from pokegent.app import AppConfig, build_runner
from pokegent.games.gen3.firered.composition import (
    FireRedMenuServices,
    make_menu_services,
)
from pokegent.games.gen3.firered.reader.menu_rom import FireRedMenuResolver
from pokegent.games.gen3.firered.rom.rom_data import load_profiles
from pokegent.games.gen3.firered.rom.rom_tables import GameTables


def _synthetic_profile():
    base = load_profiles().get("firered-us-rev0")
    digest = hashlib.sha1(b"synthetic-firered-menu-composition").hexdigest()
    return base.model_copy(update={"rom": base.rom.model_copy(update={"sha1": digest})})


class NoReadActor:
    """Fails if composition or build wiring touches the actor's read/input surface."""

    realtime_idle_while_deciding = False

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str):
        self.touched.append(name)
        raise AssertionError(f"actor.{name} touched during composition")


class RecordingNavigation:
    def __init__(self):
        self.calls = []
        self.result = None

    def ferry_menu_entries(self, snapshot, resolved, count, entries, *, menu_id=None):
        self.calls.append((snapshot, resolved, count, entries, menu_id))
        return self.result


class SessionStore:
    def __init__(self, resolved):
        self._resolved = resolved

    def resolved(self):
        return self._resolved


class MenuSession:
    def __init__(self, resolved=None):
        self.store = SessionStore(resolved)


class RecordingLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, *args):
        self.warnings.append(args)


def test_no_rom_path_never_loads_tables(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("GameTables loader called without a ROM path")

    monkeypatch.setattr(GameTables, "from_rom_file", fail)

    services = make_menu_services(object(), _synthetic_profile())

    assert isinstance(services, FireRedMenuServices)
    assert isinstance(services.menus, FireRedMenuResolver)
    assert services.tables is None
    assert callable(services.menu_entries)


def test_successful_load_uses_the_exact_rom_path(monkeypatch, tmp_path):
    tables = object()
    calls = []

    def load(rom_path):
        calls.append(rom_path)
        return tables

    monkeypatch.setattr(GameTables, "from_rom_file", load)
    rom_path = tmp_path / "synthetic.gba"

    services = make_menu_services(object(), _synthetic_profile(), rom_path)

    assert calls == [rom_path]
    assert services.tables is tables


def test_load_failure_warns_on_the_passed_logger_and_returns_no_tables(
    monkeypatch, tmp_path
):
    rom_path = tmp_path / "broken.gba"
    error = RuntimeError("synthetic tables failure")

    def fail(_rom_path):
        raise error

    monkeypatch.setattr(GameTables, "from_rom_file", fail)
    logger = RecordingLogger()

    services = make_menu_services(
        object(), _synthetic_profile(), rom_path, logger=logger
    )

    assert services.tables is None
    assert logger.warnings == [("ROM tables unavailable for %s: %s", rom_path, error)]


def test_multichoice_keys_labels_and_none_are_preserved(monkeypatch):
    entries = (
        SimpleNamespace(stable_key="choice:0", label="Bulbasaur"),
        SimpleNamespace(stable_key="choice:1", label="Charmander"),
    )
    calls = []

    def multichoice(self, snapshot, menu_id, count):
        calls.append((snapshot, menu_id, count))
        return entries if count else None

    monkeypatch.setattr(FireRedMenuResolver, "multichoice", multichoice)
    services = make_menu_services(object(), _synthetic_profile())
    snapshot = object()

    assert services.menu_entries(snapshot, 4, 2) == (
        ("choice:0", "Bulbasaur"),
        ("choice:1", "Charmander"),
    )
    assert calls == [(snapshot, 4, 2)]
    assert services.menu_entries(snapshot, 4, 0) is None
    assert calls[-1] == (snapshot, 4, 0)


def test_ferry_override_sees_the_current_goal_and_forwards_every_call(monkeypatch):
    entry = SimpleNamespace(stable_key="choice:0", label="Bulbasaur")
    monkeypatch.setattr(
        FireRedMenuResolver, "multichoice", lambda self, snapshot, menu_id, count: (entry,)
    )
    navigation = RecordingNavigation()
    session = MenuSession("goal:one")
    services = make_menu_services(
        object(), _synthetic_profile(), navigation=navigation, session=session
    )
    snapshot = object()

    navigation.result = (("ferry:one", "One Island"),)
    assert services.menu_entries(snapshot, 7, 1) == (("ferry:one", "One Island"),)
    assert navigation.calls == [
        (snapshot, "goal:one", 1, (("choice:0", "Bulbasaur"),), 7)
    ]

    navigation.result = None
    session.store._resolved = "goal:two"
    assert services.menu_entries(snapshot, 8, 1) is None
    assert navigation.calls[-1] == (
        snapshot,
        "goal:two",
        1,
        (("choice:0", "Bulbasaur"),),
        8,
    )


def test_build_runner_wires_composition_without_actor_reads(monkeypatch, tmp_path):
    tables = object()
    load_calls = []

    def load(rom_path):
        load_calls.append(rom_path)
        return tables

    monkeypatch.setattr(GameTables, "from_rom_file", load)
    multichoice_calls = []

    def multichoice(self, snapshot, menu_id, count):
        multichoice_calls.append((snapshot, menu_id, count))
        return (SimpleNamespace(stable_key="choice:0", label="Bulbasaur"),)

    monkeypatch.setattr(FireRedMenuResolver, "multichoice", multichoice)
    actor = NoReadActor()
    rom_path = tmp_path / "synthetic.gba"

    app = build_runner(
        actor=actor,
        profile=_synthetic_profile(),
        rom_path=rom_path,
        events=object(),
        config=AppConfig(run_id="menu-composition", map_mode="always"),
        objectives=tmp_path / "objective.json",
    )

    policy = app.bundle.config.prompt_policy
    assert policy is not None
    assert policy.tables is tables
    assert app.objectives.tracker.tables is tables
    snapshot = object()
    assert policy.menu_entries(snapshot, 2, 1) == (("choice:0", "Bulbasaur"),)
    assert multichoice_calls == [(snapshot, 2, 1)]
    assert load_calls == [rom_path]

    system_flow = app.bundle.config.options["system_flow"]
    assert isinstance(system_flow["menus"], FireRedMenuResolver)
    assert system_flow["rom_path"] == str(rom_path)
    assert app.source._source is actor
    assert app.source.sidecar is app.bundle.sidecar
    assert app.source.party_source == "live"
    assert actor.touched == []
