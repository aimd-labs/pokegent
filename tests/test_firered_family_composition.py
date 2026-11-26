"""FireRed's game-owned family registration composition (#139), synthetic only.

``register_families`` is the single extracted seam that owns the exact family setup the
default runner built inline: optional-family filtering, the prompt policy, system-flow and
field options, and the unchanged :class:`FamilyBundle` returned by ``register_all``.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

from pokegent.actions import Registries
from pokegent.app import AppConfig, build_runner
from pokegent.games.gen3.firered.families import register_families
from pokegent.games.gen3.firered.reader.menu_rom import FireRedMenuResolver
from pokegent.games.gen3.firered.rom.rom_data import load_profiles


def _synthetic_profile():
    base = load_profiles().get("firered-us-rev0")
    digest = hashlib.sha1(b"synthetic-firered-family-composition").hexdigest()
    return base.model_copy(update={"rom": base.rom.model_copy(update={"sha1": digest})})


class NoReadActor:
    """Fails if composition or build wiring touches the actor's read/input surface."""

    realtime_idle_while_deciding = False

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str):
        self.touched.append(name)
        raise AssertionError(f"actor.{name} touched during composition")


def _config(**overrides):
    values = {
        "map_mode": "field",
        "unknown_prompts": "escalate",
        "new_game": SimpleNamespace(enabled=False, gender="male"),
        "external": {"walk_step_limit": 8},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _menu_services(*, menus=None, tables=None, menu_entries=None):
    return SimpleNamespace(
        menus=menus if menus is not None else object(),
        tables=tables,
        menu_entries=menu_entries if menu_entries is not None else (lambda *a, **k: None),
    )


def _register(config, services, **kwargs):
    return register_families(Registries.new(), config=config, menu_services=services, **kwargs)


def test_map_mode_off_removes_only_the_field_family():
    bundle = _register(_config(map_mode="off"), _menu_services())

    assert "field" not in bundle.plugins
    assert "field" not in bundle.skipped
    assert "system_flow" in bundle.plugins
    assert "dialog" in bundle.manifests
    assert "battle" in bundle.manifests


def test_map_mode_on_includes_the_field_family():
    bundle = _register(_config(map_mode="field"), _menu_services())

    assert "field" in bundle.plugins


def test_prompt_policy_keeps_the_unknown_mode_callback_and_tables():
    tables = object()
    menu_entries = lambda *a, **k: None  # noqa: E731 - identity matters, not style
    services = _menu_services(tables=tables, menu_entries=menu_entries)

    bundle = _register(_config(unknown_prompts="offer"), services)

    policy = bundle.config.prompt_policy
    assert policy is not None
    assert policy.unknown_prompts == "offer"
    assert policy.menu_entries is menu_entries
    assert policy.tables is tables


def test_system_flow_options_keep_new_game_menus_and_rom_path(tmp_path):
    services = _menu_services()
    config = _config()
    rom_path = tmp_path / "synthetic.gba"

    bundle = _register(config, services, rom_path=rom_path)

    system_flow = bundle.config.options["system_flow"]
    assert system_flow["new_game"] is config.new_game.__dict__
    assert system_flow["menus"] is services.menus
    assert system_flow["rom_path"] == str(rom_path)

    no_rom = _register(config, services)
    assert no_rom.config.options["system_flow"]["rom_path"] is None


def test_field_walk_step_limit_only_applies_under_external_control():
    services = _menu_services()

    normal = _register(_config(), services)
    assert normal.config.options["field"]["walk_step_limit"] is None

    external = _register(_config(), services, external_control=True)
    assert external.config.options["field"]["walk_step_limit"] == 8

    explicit = _register(
        _config(external={"walk_step_limit": 12}), services, external_control=True
    )
    assert explicit.config.options["field"]["walk_step_limit"] == 12


def test_callers_objects_are_passed_through_unchanged():
    facts = object()
    objectives = object()
    graph = object()
    navigation = object()

    bundle = _register(
        _config(),
        _menu_services(),
        facts=facts,
        objectives=objectives,
        graph=graph,
        navigation=navigation,
    )

    assert bundle.config.facts is facts
    assert bundle.config.objectives is objectives
    assert bundle.config.graph is graph
    assert bundle.config.navigation is navigation


def test_build_runner_uses_the_helper_once_for_default_registration(monkeypatch):
    import pokegent.app as app_module
    import pokegent.games.gen3.firered.families as families_module

    real = families_module.register_families
    calls = []

    def spy(*args, **kwargs):
        bundle = real(*args, **kwargs)
        calls.append((args, kwargs, bundle))
        return bundle

    monkeypatch.setattr(app_module, "register_families", spy)
    actor = NoReadActor()

    app = build_runner(
        actor=actor,
        profile=_synthetic_profile(),
        events=object(),
        config=AppConfig(run_id="family-composition", map_mode="always"),
    )

    assert len(calls) == 1
    _args, kwargs, bundle = calls[0]
    assert app.bundle is bundle
    assert kwargs["config"] is app.config
    assert kwargs["rom_path"] is None
    assert kwargs["external_control"] is False

    system_flow = app.bundle.config.options["system_flow"]
    assert isinstance(system_flow["menus"], FireRedMenuResolver)
    assert system_flow["rom_path"] is None
    assert "field" in app.bundle.plugins
    assert app.source._source is actor
    assert app.source.sidecar is app.bundle.sidecar
    assert actor.touched == []
