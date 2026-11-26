"""FireRed-owned battle facts factory and its app/plugin seams (#139).

ROM-free: the checked-in FireRed profile supplies the identity and every mechanics call is a
synthetic recording of the existing factory.
"""

from __future__ import annotations

import sys

import pytest

import pokegent.app as app_module
import pokegent.games.gen3.adapters.battle_facts as adapter_module
import pokegent.games.gen3.firered.battle as battle_module
from pokegent.games.gen3.firered.plugin import (
    FIRERED_CAPABILITIES,
    FireRedPlugin,
    FireRedServices,
)
from pokegent.games.gen3.firered.rom.rom_data import load_profiles

BATTLE_FACTS_MODULE = "pokegent.games.gen3.adapters.battle_facts"
ROM_PATH = "/synthetic/firered.gba"


def _profile():
    return load_profiles().get("firered-us-rev0")


class Exploding:
    """Fails on any attribute, path or module access."""

    def __getattr__(self, name):
        raise AssertionError(f"unexpected access: {name}")

    def __fspath__(self):
        raise AssertionError("unexpected ROM path access")


def _mechanics(monkeypatch, from_rom_file):
    monkeypatch.setattr(
        adapter_module,
        "MechanicsFacts",
        type("FakeMechanicsFacts", (), {"from_rom_file": staticmethod(from_rom_file)}),
    )


@pytest.mark.parametrize("kind, has_path", [("basic", True), ("mechanics", False)])
def test_basic_or_missing_path_skips_profile_path_and_import(monkeypatch, kind, has_path):
    rom_path = Exploding() if has_path else None
    monkeypatch.setitem(sys.modules, BATTLE_FACTS_MODULE, Exploding())

    assert battle_module.make_battle_facts(Exploding(), rom_path, kind) is None


def test_nonbasic_forwards_path_profile_id_and_knowledge_once(monkeypatch):
    calls = []
    sentinel = object()

    def from_rom_file(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel

    _mechanics(monkeypatch, from_rom_file)
    profile = _profile()

    assert battle_module.make_battle_facts(profile, ROM_PATH, "mechanics") is sentinel
    assert calls == [
        ((ROM_PATH,), {"profile_id": profile.profile_id, "opponent_knowledge": "guide"})
    ]

    battle_module.make_battle_facts(
        profile, ROM_PATH, "mechanics", opponent_knowledge="none"
    )
    assert calls[1] == (
        (ROM_PATH,),
        {"profile_id": profile.profile_id, "opponent_knowledge": "none"},
    )


def test_original_exception_propagates(monkeypatch):
    class OriginalError(RuntimeError):
        pass

    def fail(*args, **kwargs):
        raise OriginalError("mechanics refused")

    _mechanics(monkeypatch, fail)

    with pytest.raises(OriginalError, match="mechanics refused"):
        battle_module.make_battle_facts(_profile(), ROM_PATH, "mechanics")


def test_app_build_facts_delegates_once(monkeypatch):
    sentinel = object()
    calls = []

    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(battle_module, "make_battle_facts", fake)
    profile = _profile()

    result = app_module.build_facts(
        profile, ROM_PATH, "mechanics", opponent_knowledge="none"
    )

    assert result is sentinel
    assert calls == [((profile, ROM_PATH, "mechanics"), {"opponent_knowledge": "none"})]


def test_compose_supplies_the_same_factory_without_calling_it(monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("compose must not invoke the battle factory")

    monkeypatch.setattr(battle_module, "make_battle_facts", fail)
    services = FireRedPlugin(_profile()).compose()

    assert services.battle_facts_factory is fail
    assert "battle_facts" in FIRERED_CAPABILITIES
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

    assert services.battle_facts_factory is None
