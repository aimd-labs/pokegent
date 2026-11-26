"""Synthetic tests for the opt-in FireRed observation projection (#139/R10).

The projection is internal SDK data: not a provider payload and not a redaction claim.
No ROM, actor, provider, app or native backend is involved.
"""

from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

import pokegent.games.gen3.firered.observation as observation_module
from pokegent.games.gen3.firered.observation import project_observation
from pokegent.games.gen3.firered.plugin import (
    FireRedPlugin,
    FireRedServices,
)
from pokegent.games.gen3.firered.reader.schemas import (
    FlagSet,
    Health,
    Player,
    ReadError,
    StateSnapshot,
    VarTable,
)
from pokegent.games.gen3.firered.rom.rom_data import load_profiles
from pokegent.games.observation import GameObservation, ObservationRejected

KNOWN_ROM = b"synthetic-firered-observation-projection" * 8


def _profile():
    base = load_profiles().get("firered-us-rev0")
    digest = hashlib.sha1(KNOWN_ROM).hexdigest()
    return base.model_copy(update={"rom": base.rom.model_copy(update={"sha1": digest})})


def _snapshot(*, usable=True, seq=5):
    error = ReadError(subsystem="synthetic", code="invalid", message="synthetic")
    return StateSnapshot.create(
        rom_profile="firered-us-rev0",
        snapshot_seq=seq,
        emulator_frame=1,
        health=Health(valid=usable, read_errors=() if usable else (error,)),
        player=Player(
            name="Test",
            gender="male",
            tid=0,
            sid=0,
            map_group=3,
            map_num=7,
            map_layout_id=0,
            x=10,
            y=11,
            money=0,
            coins=0,
        )
        if usable
        else None,
        flags=FlagSet() if usable else None,
        vars=VarTable() if usable else None,
    )


class PoisonSnapshot:
    def __getattr__(self, name):
        raise AssertionError(f"snapshot.{name} touched before the flag/type check")


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"enabled": None}, {"enabled": 0}, {"enabled": 1}, {"enabled": "true"}, {"enabled": []}],
)
def test_flag_must_be_an_exact_bool_before_any_snapshot_access(kwargs):
    with pytest.raises(ObservationRejected):
        project_observation(PoisonSnapshot(), restore_epoch=0, **kwargs)


def test_actual_state_snapshot_is_required_even_when_enabled():
    for candidate in (PoisonSnapshot(), SimpleNamespace(rom_profile="x"), object()):
        with pytest.raises(ObservationRejected):
            project_observation(candidate, restore_epoch=0, enabled=True)


def test_usable_snapshot_projects_exactly_four_frozen_fields():
    snapshot = _snapshot()
    observation = project_observation(snapshot, restore_epoch=2, enabled=True)

    assert isinstance(observation, GameObservation)
    assert (observation.game_id, observation.profile_id) == ("firered", snapshot.rom_profile)
    assert (observation.snapshot_seq, observation.restore_epoch) == (5, 2)
    assert observation.usable is True
    assert dict(observation.payload) == {"map_group": 3, "map_num": 7, "x": 10, "y": 11}
    assert observation.require_usable() is observation
    with pytest.raises(TypeError):
        observation.payload["x"] = 99
    with pytest.raises(FrozenInstanceError):
        observation.usable = False


def test_unusable_snapshot_projects_an_empty_unknown_payload():
    snapshot = _snapshot(usable=False)
    assert snapshot.player is None

    observation = project_observation(snapshot, restore_epoch=0, enabled=True)

    assert observation.usable is False
    assert dict(observation.payload) == {}
    assert observation.snapshot_seq == snapshot.snapshot_seq
    with pytest.raises(ObservationRejected):
        observation.require_usable()


@pytest.mark.parametrize("epoch", [-1, True, False, 1.0, "1", None])
def test_restore_epoch_must_be_a_caller_supplied_plain_counter(epoch):
    with pytest.raises(ObservationRejected):
        project_observation(_snapshot(), restore_epoch=epoch, enabled=True)


def test_compose_names_the_projection_without_invoking_it(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("compose must only name the projection callable")

    monkeypatch.setattr(observation_module, "project_observation", fail)
    services = FireRedPlugin(_profile()).compose()

    assert services.observation_factory is fail
    assert "observation_projection" in FireRedPlugin(_profile()).descriptor.capabilities


def test_previous_manual_services_construction_stays_compatible():
    fillers = "adapter snapshot_source_factory scene_catalog_factory menu_services_factory".split()
    fillers += "family_registration_factory objective_observation_factory".split()
    fillers += "dex_fallback_factory story_fallback_factory story_identity_validator".split()
    fillers += "local_knowledge_factory".split()
    services = FireRedServices(
        profile=_profile(),
        pre_game_callbacks=frozenset(),
        **{name: object() for name in fillers},
    )

    assert services.observation_factory is None
    assert services.battle_facts_factory is None
