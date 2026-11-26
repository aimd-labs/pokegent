"""FireRed-owned fresh objective observation behind explicit plugin admission (#139).

Synthetic actor/profile and snapshots only: the checked-in profile is copied for an invented
test digest, no ROM, actor read, game input or filesystem game data is involved.
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
import pokegent.games.gen3.firered.plugin as plugin_module
import pokegent.games.gen3.firered.reader.live as live_module
import pokegent.games.gen3.firered.reader.snapshot_sources as snapshot_sources_module
from pokegent.app import AppConfig, build_runner
from pokegent.games.gen3.firered.admission import (
    ObjectiveObservationServices,
    make_objective_observation_services,
)
from pokegent.games.gen3.firered.plugin import (
    FIRERED_CAPABILITIES,
    FireRedPlugin,
    FireRedServices,
)
from pokegent.games.gen3.firered.reader.schemas import (
    BagItem,
    BagPocket,
    FlagSet,
    Health,
    Player,
    ReadError,
    StateSnapshot,
    VarTable,
)
from pokegent.games.gen3.firered.rom.rom_data import load_profiles
from pokegent.games.plugin import PluginRefusal, RefusalCode
from pokegent.goals.shop_admission import ShopObservation

KNOWN_ROM = b"synthetic-firered-objective-observation-known" * 8


def _digest(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _profile(data: bytes = KNOWN_ROM):
    """The real FireRed profile with only the invented test digest changed."""
    base = load_profiles().get("firered-us-rev0")
    return base.model_copy(update={"rom": base.rom.model_copy(update={"sha1": _digest(data)})})


def _snapshot(profile, *, usable=True, rom_profile=None):
    return SimpleNamespace(
        usable=usable,
        rom_profile=profile.profile_id if rom_profile is None else rom_profile,
        bag=(SimpleNamespace(items=(SimpleNamespace(item_id=1, quantity=2),)),),
    )


def _state_snapshot(profile, *, usable=True, rom_profile=None):
    """A real validated StateSnapshot, matching the objective fixture pattern."""
    return StateSnapshot.create(
        rom_profile=profile.profile_id if rom_profile is None else rom_profile,
        snapshot_seq=1,
        emulator_frame=1,
        health=Health(
            valid=usable,
            read_errors=()
            if usable
            else (ReadError(subsystem="test", code="invalid", message="synthetic"),),
        ),
        player=Player(
            name="Test",
            gender="male",
            tid=0,
            sid=0,
            map_group=0,
            map_num=0,
            map_layout_id=0,
            x=0,
            y=0,
            money=0,
            coins=0,
        )
        if usable
        else None,
        bag=(
            (
                BagPocket(
                    pocket="items",
                    capacity=20,
                    items=(BagItem(item_id=7, quantity=4),),
                ),
            )
            if usable
            else ()
        ),
        flags=FlagSet() if usable else None,
        vars=VarTable() if usable else None,
    )


class FakeLiveReader:
    instances: list["FakeLiveReader"] = []
    snapshot = None
    error: Exception | None = None

    def __init__(self, actor, profile, *, run_id="") -> None:
        self.actor = actor
        self.profile = profile
        self.run_id = run_id
        self.reads = 0
        type(self).instances.append(self)

    def read(self):
        self.reads += 1
        if type(self).error is not None:
            raise type(self).error
        return SimpleNamespace(snapshot=type(self).snapshot)


@pytest.fixture
def fake_reader(monkeypatch):
    FakeLiveReader.instances = []
    FakeLiveReader.error = None
    monkeypatch.setattr(live_module, "LiveReader", FakeLiveReader)
    return FakeLiveReader


def test_absent_actor_or_profile_returns_no_providers_and_no_reader(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("an absent input constructed a LiveReader")

    monkeypatch.setattr(live_module, "LiveReader", fail)
    profile = _profile()

    for actor, candidate in ((None, profile), (object(), None)):
        services = make_objective_observation_services(actor, candidate, "run-id")
        assert services == ObjectiveObservationServices(None, None)


def test_provider_validation_and_shop_sha_use_one_shared_fresh_reader(fake_reader):
    profile = _profile()
    snapshot = _snapshot(profile)
    fake_reader.snapshot = snapshot
    actor = object()

    services = make_objective_observation_services(actor, profile, "run-id")

    assert len(fake_reader.instances) == 1
    reader = fake_reader.instances[0]
    assert (reader.actor, reader.profile, reader.run_id) == (actor, profile, "run-id")
    assert services.fresh_snapshot() is snapshot
    assert services.fresh_snapshot() is snapshot
    observation = services.shop_observation()
    assert isinstance(observation, ShopObservation)
    assert observation.profile_id == profile.profile_id
    assert observation.rom_sha1 == profile.rom.sha1
    assert observation.quantity(1) == 2
    assert reader.reads == 3


def test_real_state_snapshot_roundtrips_through_shared_providers(fake_reader):
    profile = _profile()
    snapshot = _state_snapshot(profile)
    fake_reader.snapshot = snapshot
    actor = object()

    services = make_objective_observation_services(actor, profile, "run-id")

    assert len(fake_reader.instances) == 1
    reader = fake_reader.instances[0]
    assert reader.reads == 0
    assert services.fresh_snapshot() is snapshot
    observation = services.shop_observation()
    assert observation.profile_id == profile.profile_id
    assert observation.rom_sha1 == profile.rom.sha1
    assert observation.usable
    assert observation.quantity(7) == 4
    assert reader.reads == 2


@pytest.mark.parametrize(
    "usable, rom_profile",
    [(False, None), (True, "other-profile")],
    ids=["unusable", "profile-mismatch"],
)
def test_real_state_snapshot_rejects_unusable_or_mismatch(fake_reader, usable, rom_profile):
    profile = _profile()
    fake_reader.snapshot = _state_snapshot(
        profile, usable=usable, rom_profile=rom_profile
    )

    services = make_objective_observation_services(object(), profile, "run-id")

    assert services.fresh_snapshot() is None
    assert services.shop_observation() is None


@pytest.mark.parametrize("rom_profile", [None, "other-profile"])
def test_unusable_or_mismatched_snapshot_returns_none(fake_reader, rom_profile):
    profile = _profile()
    snapshot = _snapshot(profile, rom_profile=rom_profile)
    if rom_profile == "other-profile":
        snapshot.usable = True
    else:
        snapshot.usable = False
    fake_reader.snapshot = snapshot

    services = make_objective_observation_services(object(), profile, "run-id")

    assert services.fresh_snapshot() is None
    assert services.shop_observation() is None


def test_read_failure_returns_none_and_stays_optional(fake_reader):
    fake_reader.error = RuntimeError("synthetic read failure")

    services = make_objective_observation_services(object(), _profile(), "run-id")

    assert services.fresh_snapshot() is None
    assert services.shop_observation() is None


def test_plugin_compose_names_the_factory_without_invoking_it(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("compose must only name the supplied factory")

    monkeypatch.setattr(admission_module, "make_objective_observation_services", fail)
    services = FireRedPlugin(_profile()).compose()

    assert isinstance(services, FireRedServices)
    assert services.objective_observation_factory is fail


def test_capability_is_exactly_the_supplied_objective_observation_factory():
    descriptor = FireRedPlugin(_profile()).descriptor

    assert descriptor.capabilities == FIRERED_CAPABILITIES
    assert "objective_observation" in FIRERED_CAPABILITIES
    assert not FIRERED_CAPABILITIES & {"verification", "supported", "input"}


class FakeRunner:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self._last_snapshot = SimpleNamespace(usable=False)


def _build(monkeypatch, tmp_path, *, profile, actor, experimental=False, rom_bytes=None):
    """Build the real runner around inert collaborators; no actor read occurs."""
    fake_registries = SimpleNamespace(
        detectors=object(), builders=object(), executors=object(), verifiers=object()
    )
    fake_bundle = SimpleNamespace(plugins={}, sidecar=None, confirm_policy=lambda catalog: None)
    menu_services = SimpleNamespace(tables=None)

    def fake_menu(*args, **kwargs):
        return menu_services

    def fake_family(*args, **kwargs):
        return fake_bundle

    class FakeSource:
        def __init__(self, *args, **kwargs) -> None:
            pass

    monkeypatch.setattr(app_module, "load_scene_catalog", lambda: object())
    monkeypatch.setattr(app_module.Registries, "new", lambda catalog: fake_registries)
    monkeypatch.setattr(app_module, "register_families", fake_family)
    monkeypatch.setattr(app_module, "CompositeSnapshotSource", FakeSource)
    monkeypatch.setattr(app_module, "Perceiver", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(
        app_module, "Executor", lambda *args, **kwargs: SimpleNamespace(perceiver=None)
    )
    monkeypatch.setattr(app_module, "Runner", FakeRunner)
    monkeypatch.setattr(composition_module, "make_menu_services", fake_menu)
    monkeypatch.setattr(families_module, "register_families", fake_family)
    monkeypatch.setattr(snapshot_sources_module, "CompositeSnapshotSource", FakeSource)
    monkeypatch.setattr(
        "pokegent.games.gen3.firered.reader.menu_rom.FireRedMenuResolver",
        lambda actor, profile: None,
    )
    return build_runner(
        actor=actor,
        profile=profile,
        events=object(),
        decider=SimpleNamespace(),
        config=AppConfig(run_id="objective-observation", map_mode="always"),
        objectives=tmp_path / "objective.json",
        experimental_game_plugin=experimental,
        plugin_rom_bytes=rom_bytes,
    )


def test_default_build_selects_the_app_helper_once(monkeypatch, tmp_path):
    profile = _profile()
    actor = object()
    fresh = lambda: "fresh"  # noqa: E731
    shop = lambda: "shop"  # noqa: E731
    calls = []

    def spy(candidate_actor, candidate_profile, run_id):
        calls.append((candidate_actor, candidate_profile, run_id))
        return ObjectiveObservationServices(fresh, shop)

    monkeypatch.setattr(app_module, "make_objective_observation_services", spy)
    app = _build(monkeypatch, tmp_path, profile=profile, actor=actor)

    assert calls == [(actor, profile, "objective-observation")]
    assert app.objectives.store._snapshot is fresh
    assert app.objectives.store._observation is shop


def test_admitted_build_selects_the_plugin_supplied_factory_once(monkeypatch, tmp_path):
    fresh = lambda: "fresh"  # noqa: E731
    shop = lambda: "shop"  # noqa: E731
    calls = []

    def default_fail(*args, **kwargs):
        raise AssertionError("an admitted build used the app-level default helper")

    def spy(candidate_actor, candidate_profile, run_id):
        calls.append((candidate_actor, candidate_profile, run_id))
        return ObjectiveObservationServices(fresh, shop)

    monkeypatch.setattr(app_module, "make_objective_observation_services", default_fail)
    monkeypatch.setattr(admission_module, "make_objective_observation_services", spy)
    profile = _profile()
    actor = object()
    app = _build(
        monkeypatch,
        tmp_path,
        profile=profile,
        actor=actor,
        experimental=True,
        rom_bytes=KNOWN_ROM,
    )

    assert calls == [(actor, profile, "objective-observation")]
    assert app.objectives.store._snapshot is fresh
    assert app.objectives.store._observation is shop


class MissingObservationCapabilityPlugin(FireRedPlugin):
    @property
    def descriptor(self):
        return replace(
            super().descriptor,
            capabilities=FIRERED_CAPABILITIES - {"objective_observation"},
        )

    def compose(self):
        raise AssertionError("a plugin missing objective_observation reached compose")


def test_missing_capability_refuses_before_compose_and_observation(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin_module, "FireRedPlugin", MissingObservationCapabilityPlugin)

    def fail(*args, **kwargs):
        raise AssertionError("refused admission reached objective observation")

    monkeypatch.setattr(admission_module, "make_objective_observation_services", fail)
    monkeypatch.setattr(live_module, "LiveReader", fail)

    with pytest.raises(PluginRefusal) as caught:
        _build(
            monkeypatch,
            tmp_path,
            profile=_profile(),
            actor=object(),
            experimental=True,
            rom_bytes=KNOWN_ROM,
        )

    assert caught.value.code is RefusalCode.MISSING_CAPABILITY
