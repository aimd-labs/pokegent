"""Synthetic tests for the plugin-owned local knowledge factory (#139)."""

from __future__ import annotations

import hashlib

import pytest

import pokegent.games.gen3.firered.knowledge as knowledge_module
from pokegent.games.gen3 import mechanics_constants as K
from pokegent.games.gen3.firered.knowledge import FireRedKnowledge
from pokegent.games.gen3.firered.plugin import FIRERED_CAPABILITIES, FireRedPlugin
from pokegent.games.gen3.firered.rom.rom_data import load_profiles
from pokegent.games.gen3.firered.rom.rom_tables import GameTables, MoveData, TypeChart
from pokegent.games.plugin import PluginRefusal, RefusalCode, ReviewedPluginRegistry

KNOWN_ROM = b"synthetic-firered-plugin-local-knowledge" * 8


def _test_profile():
    base = load_profiles().get("firered-us-rev0")
    sha1 = hashlib.sha1(KNOWN_ROM).hexdigest()
    return base.model_copy(update={"rom": base.rom.model_copy(update={"sha1": sha1})})


def _move(move_id, *, name="Synthetic Move", power=40):
    return MoveData(
        move_id=move_id,
        name=name,
        effect=0,
        power=power,
        type=K.TYPE_NORMAL,
        accuracy=95,
        pp=20,
        secondary_chance=0,
        target=0,
        priority=0,
        flags=0,
    )


def _tables(profile_id, *, moves=()):
    return GameTables(
        moves=tuple(moves),
        species=(),
        items=(),
        type_chart=TypeChart(bytes([0xFE, 0, 0, 0xFF, 0, 0])),
        ability_names=(),
        type_names=(),
        profile_id=profile_id,
    )


def _composed():
    profile = _test_profile()
    plugin = FireRedPlugin(profile)
    registry = ReviewedPluginRegistry(enabled=True)
    registry.register(plugin)
    selection = registry.select(
        rom_bytes=KNOWN_ROM,
        game="firered",
        profile_id=profile.profile_id,
        required_capabilities=FIRERED_CAPABILITIES,
    )
    return profile, plugin, selection


def test_capability_and_factory_are_advertised_without_instantiating(monkeypatch):
    profile, plugin, selection = _composed()

    assert "local_knowledge" in plugin.descriptor.capabilities
    assert selection.services.profile is profile
    assert selection.services.local_knowledge_factory is FireRedKnowledge
    assert not FIRERED_CAPABILITIES & {"verification", "supported", "input"}

    def fail(*args, **kwargs):
        raise AssertionError("compose must not instantiate local knowledge")

    monkeypatch.setattr(knowledge_module, "FireRedKnowledge", fail)
    services = plugin.compose()
    assert services.local_knowledge_factory is fail


def test_caller_opt_in_reads_matching_profile_tables_through_the_factory():
    profile, _, selection = _composed()
    factory = selection.services.local_knowledge_factory
    tables = _tables(profile.profile_id, moves=(_move(0), _move(1, name="Alpha", power=15)))

    with pytest.raises(PluginRefusal) as refused:
        factory(selection, tables)
    assert refused.value.code is RefusalCode.DISABLED

    reader = factory(selection, tables, enabled=True)
    known = reader.move(1)
    assert known.known is True
    assert known.profile_id == profile.profile_id
    assert dict(known.values)["name"] == "Alpha"
    assert dict(known.values)["power"] == 15

    unknown = reader.move(2)
    assert unknown.known is False
    assert unknown.values == ()
    assert unknown.record_id == 2


def test_mismatched_tables_are_refused_by_the_composed_factory():
    _, _, selection = _composed()
    tables = _tables("another-profile", moves=(_move(0),))

    with pytest.raises(PluginRefusal) as refused:
        selection.services.local_knowledge_factory(selection, tables, enabled=True)
    assert refused.value.code is RefusalCode.PROFILE_MISMATCH
