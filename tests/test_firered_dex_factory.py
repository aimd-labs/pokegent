"""FireRed-owned Dex fallback factory behind the explicit plugin opt-in (#139).

Synthetic encounter rows only. No ROM file, actor read or private table is involved; the
injected factory and the real game helper are checked against the existing ``DexFallback``.
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
import pokegent.games.gen3.firered.objectives as objectives_module
import pokegent.games.gen3.firered.plugin as plugin_module
import pokegent.games.gen3.firered.reader.snapshot_sources as snapshot_sources_module
from pokegent.app import AppConfig, AppError, build_runner, make_objective_session
from pokegent.games.gen3.firered.objectives import make_dex_fallback
from pokegent.games.gen3.firered.plugin import (
    FIRERED_CAPABILITIES,
    FireRedPlugin,
    FireRedServices,
)
from pokegent.games.gen3.firered.rom.rom_data import load_profiles
from pokegent.games.gen3.firered.rom.wild import Encounter
from pokegent.games.plugin import PluginRefusal, RefusalCode
from pokegent.goals.dex_fallback import DexFallback

KNOWN_ROM = b"synthetic-firered-dex-factory-known" * 8
TABLE = (
    Encounter("RouteOne", "land", 19, 0.5, 2, 3),
    Encounter("RouteTwo", "land", 16, 0.25, 2, 3),
)


def _test_profile(data: bytes = KNOWN_ROM):
    base = load_profiles().get("firered-us-rev0")
    return base.model_copy(
        update={"rom": base.rom.model_copy(update={"sha1": hashlib.sha1(data).hexdigest()})}
    )


class NoReadActor:
    """Fails if admission or composition touches the actor's read/input surface."""

    realtime_idle_while_deciding = False

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str):
        self.touched.append(name)
        raise AssertionError(f"actor.{name} touched before admission/composition")


class FalseyFactory:
    """A callable whose truthiness is false but which must still be selected."""

    def __init__(self) -> None:
        self.calls = 0

    def __bool__(self) -> bool:
        return False

    def __call__(self) -> DexFallback:
        self.calls += 1
        return DexFallback(TABLE)


class FailingFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> DexFallback:
        self.calls += 1
        raise AssertionError("dex factory must not be called")


def test_game_helper_wraps_the_invented_encounter_table_unchanged(monkeypatch):
    from pokegent.games.gen3.firered.rom import wild

    monkeypatch.setattr(wild, "load_wild_table", lambda: TABLE)

    fallback = make_dex_fallback()

    assert type(fallback) is DexFallback
    assert fallback.table is TABLE
    assert fallback.table == TABLE


def test_plugin_names_the_game_factory_without_calling_it(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("compose must only name the Dex fallback factory")

    monkeypatch.setattr(objectives_module, "make_dex_fallback", fail)

    services = FireRedPlugin(_test_profile()).compose()

    assert isinstance(services, FireRedServices)
    assert services.dex_fallback_factory is fail
    assert "dex_fallback" in FIRERED_CAPABILITIES


def test_injected_factory_is_called_once_and_keeps_the_existing_type(tmp_path):
    calls = []

    def factory():
        calls.append(True)
        return DexFallback(TABLE)

    session = make_objective_session(
        tmp_path / "objective.json", dex_fallback=True, dex_fallback_factory=factory
    )

    assert calls == [True]
    assert type(session.fallback) is DexFallback
    assert session.fallback.table is TABLE


def test_falsey_callable_is_selected_by_identity_not_truthiness(tmp_path):
    factory = FalseyFactory()

    session = make_objective_session(
        tmp_path / "objective.json", dex_fallback=True, dex_fallback_factory=factory
    )

    assert factory.calls == 1
    assert type(session.fallback) is DexFallback


def test_disabled_flag_never_calls_an_injected_factory(tmp_path):
    factory = FailingFactory()

    session = make_objective_session(
        tmp_path / "objective.json", dex_fallback=False, dex_fallback_factory=factory
    )

    assert session.fallback is None
    assert factory.calls == 0


@pytest.mark.parametrize(
    "arguments, message",
    [
        ((None,), "fallback needs the objective file"),
        (("missing-parent/objective.json",), "directory does not exist"),
        (("both.json",), "cannot combine with dex"),
    ],
)
def test_invalid_flags_and_missing_directory_fail_before_the_factory(
    tmp_path, arguments, message
):
    factory = FailingFactory()
    kwargs = {"dex_fallback": True, "dex_fallback_factory": factory}
    targets = (tmp_path / arguments[0],) if arguments[0] is not None else (None,)
    if message == "cannot combine with dex":
        kwargs["story_fallback"] = True

    with pytest.raises(AppError, match=message):
        make_objective_session(targets[0], **kwargs)

    assert factory.calls == 0


def test_default_path_missing_private_data_is_redacted_with_exit_environment(tmp_path, monkeypatch):
    from pokegent.games.gen3.firered.rom import wild

    def missing():
        raise FileNotFoundError("synthetic private path must not be printed")

    monkeypatch.setattr(wild, "load_wild_table", missing)

    with pytest.raises(
        AppError, match="^dex fallback requires valid private ROM encounter data$"
    ) as error:
        make_objective_session(tmp_path / "objective.json", dex_fallback=True)

    assert error.value.exit_code == 4
    assert "private path" not in str(error.value)


class FakeRunner:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


def _build_with_inert_collaborators(
    monkeypatch, *, config, experimental=False, rom_bytes=None, actor=None
):
    captured: dict[str, object] = {}
    fake_registries = SimpleNamespace(
        detectors=object(), builders=object(), executors=object(), verifiers=object()
    )
    fake_bundle = SimpleNamespace(plugins={}, sidecar=None, confirm_policy=lambda catalog: None)

    def fake_menu(*args, **kwargs):
        return SimpleNamespace(tables=None)

    def fake_family(*args, **kwargs):
        return fake_bundle

    def fake_observation(*args, **kwargs):
        return SimpleNamespace(fresh_snapshot=None, shop_observation=None)

    def fake_session(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return None

    class FakeSource:
        def __init__(self, *args, **kwargs) -> None:
            pass

    monkeypatch.setattr(app_module, "load_scene_catalog", lambda: object())
    monkeypatch.setattr(app_module.Registries, "new", lambda catalog: fake_registries)
    monkeypatch.setattr(app_module, "register_families", fake_family)
    monkeypatch.setattr(app_module, "CompositeSnapshotSource", FakeSource)
    monkeypatch.setattr(app_module, "make_objective_session", fake_session)
    monkeypatch.setattr(
        app_module, "make_objective_observation_services", fake_observation
    )
    monkeypatch.setattr(app_module, "Perceiver", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(
        app_module, "Executor", lambda *args, **kwargs: SimpleNamespace(perceiver=None)
    )
    monkeypatch.setattr(app_module, "Runner", FakeRunner)
    monkeypatch.setattr(admission_module, "make_objective_observation_services", fake_observation)
    monkeypatch.setattr(composition_module, "make_menu_services", fake_menu)
    monkeypatch.setattr(families_module, "register_families", fake_family)
    monkeypatch.setattr(snapshot_sources_module, "CompositeSnapshotSource", FakeSource)

    actor = actor if actor is not None else NoReadActor()
    app = build_runner(
        actor=actor,
        profile=_test_profile(),
        events=object(),
        decider=SimpleNamespace(),
        config=config,
        objectives=None,
        experimental_game_plugin=experimental,
        plugin_rom_bytes=rom_bytes,
    )
    return app, captured, actor


def test_build_runner_passes_the_admitted_factory_to_the_session(monkeypatch):
    config = AppConfig(run_id="dex-factory", map_mode="always", dex_fallback=True)

    _, captured, actor = _build_with_inert_collaborators(
        monkeypatch,
        config=config,
        experimental=True,
        rom_bytes=KNOWN_ROM,
    )

    assert captured["kwargs"]["dex_fallback_factory"] is objectives_module.make_dex_fallback
    assert actor.touched == []


def test_default_build_runner_passes_no_game_factory(monkeypatch):
    _, captured, _ = _build_with_inert_collaborators(
        monkeypatch,
        config=AppConfig(run_id="dex-factory", map_mode="always", dex_fallback=True),
    )

    assert captured["kwargs"]["dex_fallback_factory"] is None


class MissingDexCapabilityPlugin(FireRedPlugin):
    @property
    def descriptor(self):
        return replace(
            super().descriptor,
            capabilities=FIRERED_CAPABILITIES - {"dex_fallback"},
        )


class NeverComposeMissingDexCapabilityPlugin(MissingDexCapabilityPlugin):
    def compose(self):
        raise AssertionError("a plugin missing dex_fallback reached compose")


def test_missing_dex_capability_refuses_before_compose_and_actor(monkeypatch):
    monkeypatch.setattr(
        plugin_module, "FireRedPlugin", NeverComposeMissingDexCapabilityPlugin
    )
    actor = NoReadActor()

    with pytest.raises(PluginRefusal) as caught:
        build_runner(
            actor=actor,
            profile=_test_profile(),
            events=object(),
            config=AppConfig(run_id="dex-factory", dex_fallback=True),
            experimental_game_plugin=True,
            plugin_rom_bytes=KNOWN_ROM,
        )

    assert caught.value.code is RefusalCode.MISSING_CAPABILITY
    assert actor.touched == []


def test_dex_capability_is_not_required_when_the_flag_is_off(monkeypatch):
    monkeypatch.setattr(plugin_module, "FireRedPlugin", MissingDexCapabilityPlugin)

    _, captured, _ = _build_with_inert_collaborators(
        monkeypatch,
        config=AppConfig(run_id="dex-factory", map_mode="always", dex_fallback=False),
        experimental=True,
        rom_bytes=KNOWN_ROM,
    )

    assert "dex_fallback_factory" in captured["kwargs"]
