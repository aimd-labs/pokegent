"""Synthetic contract tests for the opt-in local objective SDK facade.

Real temporary ``ObjectiveStore`` plus an invented admitted FireRed selection. No ROM, server,
runtime wiring, game input or network is involved; the store's already-bound admission snapshot is
used only where a test exercises already-satisfied admission.
"""

from __future__ import annotations

import hashlib
import importlib
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from pokegent.games.gen3.firered.reader.schemas import (
    FlagSet,
    Health,
    Player,
    StateSnapshot,
    VarTable,
)
from pokegent.games.plugin import GamePluginDescriptor, PluginSelection, ReviewedPluginRegistry
from pokegent.goals.store import ObjectiveStore, Proposal
from pokegent.goals.validate import MapFacts
from pokegent.sdk_objectives import ObjectiveSdk, ObjectiveSdkRejected

ROM = b"synthetic-objective-sdk-rom" * 8
MAP = "PewterCity"
MAP_KEY = (3, 7)


@dataclass
class _Plugin:
    descriptor: GamePluginDescriptor

    def compose(self) -> object:
        return object()


class _Directory:
    def describe(self, group, number):
        return MapFacts(10, 10, 1, frozenset({1}))

    def resolve_item(self, name):
        return None

    def leader_for_badge(self, badge):
        return None


def _selection(game="firered", profile="profile-synthetic", data=ROM):
    descriptor = GamePluginDescriptor(
        plugin_id=f"{game}:{profile}",
        game=game,
        profile_id=profile,
        contract_version=1,
        rom_sha1=hashlib.sha1(data).hexdigest(),
        capabilities=frozenset({"objective_observation"}),
    )
    registry = ReviewedPluginRegistry(enabled=True)
    registry.register(_Plugin(descriptor))
    return registry.select(rom_bytes=data, game=game, profile_id=profile)


def _store(tmp_path, *, clock=None, name="objective.json"):
    kwargs = {"directory": _Directory()}
    if clock is not None:
        kwargs["clock"] = clock
    return ObjectiveStore(tmp_path / name, **kwargs)


def _opened(tmp_path, *, clock=None):
    store = _store(tmp_path, clock=clock)
    return ObjectiveSdk(_selection(), store, enabled=True), store


def _hold(revision=1, **extra):
    return {"objective_id": f"hold{revision}", "revision": revision, "kind": "hold", **extra}


def _reach(revision, **extra):
    return {
        "objective_id": f"reach{revision}",
        "revision": revision,
        "kind": "reach",
        "target": {"map": MAP},
        **extra,
    }


def _snapshot():
    return StateSnapshot.create(
        rom_profile="synthetic",
        snapshot_seq=1,
        emulator_frame=0,
        health=Health(valid=True, read_errors=()),
        player=Player(
            name="Test",
            gender="male",
            tid=0,
            sid=0,
            map_group=MAP_KEY[0],
            map_num=MAP_KEY[1],
            map_layout_id=0,
            x=4,
            y=5,
            money=0,
            coins=0,
        ),
        flags=FlagSet(set_ids=()),
        vars=VarTable(),
    )


@pytest.fixture(autouse=True)
def map_resolution(monkeypatch):
    module = importlib.import_module("pokegent.goals.validate")
    monkeypatch.setattr(
        module, "resolve_map", lambda name: None if name == "Nowhere" else MAP_KEY
    )
    monkeypatch.setattr(module.map_names, "map_name", lambda group, number: "MAP_PEWTER_CITY")


def _explode(*args, **kwargs):
    raise AssertionError("the store must not be called")


def test_facade_is_disabled_by_default_and_touches_no_store(tmp_path):
    store = _store(tmp_path)
    disabled = ObjectiveSdk(_selection(), store)
    store.current = _explode
    store.propose = _explode

    with pytest.raises(ObjectiveSdkRejected):
        disabled.current()
    with pytest.raises(ObjectiveSdkRejected):
        disabled.propose(_hold(), expected_revision=0)
    assert list(tmp_path.iterdir()) == []


def test_constructor_requires_admitted_selection_store_and_exact_bool(tmp_path):
    store = _store(tmp_path)
    for not_a_selection in (object(), _selection().descriptor, None):
        with pytest.raises(TypeError):
            ObjectiveSdk(not_a_selection, store, enabled=True)
    for not_a_store in (object(), str(tmp_path), None):
        with pytest.raises(TypeError):
            ObjectiveSdk(_selection(), not_a_store, enabled=True)
    for not_a_bool in (1, 0, "true", None):
        with pytest.raises(TypeError):
            ObjectiveSdk(_selection(), store, enabled=not_a_bool)


def test_foreign_game_namespace_is_refused_before_any_store_call(tmp_path):
    store = _store(tmp_path)
    store.current = _explode
    store.propose = _explode

    for game in ("synthetic-alpha", "emerald"):
        with pytest.raises(ObjectiveSdkRejected):
            ObjectiveSdk(_selection(game), store, enabled=True)


def test_current_returns_the_typed_store_objective_and_expiry_is_observed(tmp_path):
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    sdk, store = _opened(tmp_path, clock=lambda: now[0])

    assert sdk.current() is None
    assert sdk.propose(_hold(expires_at="2026-01-02T00:00:00Z"), expected_revision=0).accepted
    current = sdk.current()
    assert current is not None
    assert isinstance(current, type(store.current()))
    assert (current.objective_id, current.revision) == ("hold1", 1)

    now[0] = datetime(2026, 1, 3, tzinfo=UTC)
    assert sdk.current() is None


def test_propose_accepts_one_agent_objective_and_returns_the_native_proposal(tmp_path):
    sdk, store = _opened(tmp_path)

    result = sdk.propose(_reach(1), expected_revision=0)

    assert isinstance(result, Proposal)
    assert result.accepted is True
    assert result.reasons == ()
    assert store.current().set_by == "agent"
    assert [row["event"] for row in store.history()] == ["accepted"]


@pytest.mark.parametrize(
    ("payload", "expected_revision", "code", "field"),
    [
        (_hold(2), 0, "stale_revision", "revision"),
        ({"objective_id": "Bad", "revision": 2, "kind": "hold"}, 1, "schema_invalid",
          "objective_id"),
        ({"objective_id": "reach2", "revision": 2, "kind": "reach",
          "target": {"map": "Nowhere"}}, 1, "unknown_map", "target.map"),
    ],
)
def test_native_reasons_are_returned_unchanged(tmp_path, payload, expected_revision, code, field):
    sdk, store = _opened(tmp_path)
    assert sdk.propose(_hold(1), expected_revision=0).accepted

    result = sdk.propose(payload, expected_revision=expected_revision)

    assert isinstance(result, Proposal)
    assert result.accepted is False
    assert result.reasons[0].code == code
    assert result.reasons[0].field == field
    assert store.current().objective_id == "hold1"


def test_already_satisfied_guard_still_rejects_reach(tmp_path):
    sdk, store = _opened(tmp_path)
    assert sdk.propose(_hold(1), expected_revision=0).accepted
    store.bind_snapshot(_snapshot)

    result = sdk.propose(_reach(2), expected_revision=1)

    assert result.accepted is False
    assert [reason.code for reason in result.reasons] == ["objective_already_satisfied"]


def test_operator_priority_is_preserved_for_agent_proposals(tmp_path):
    sdk, store = _opened(tmp_path)
    assert store.propose(_hold(1, set_by="operator")).accepted

    result = sdk.propose(_hold(2), expected_revision=1)

    assert result.accepted is False
    assert [reason.code for reason in result.reasons] == ["source_priority"]
    assert store.current().objective_id == "hold1"
    assert store.current().set_by == "operator"


@pytest.mark.parametrize("claimed", ["operator", "script"])
def test_payload_cannot_impersonate_another_writer(tmp_path, claimed):
    sdk, store = _opened(tmp_path)

    result = sdk.propose(_hold(1, set_by=claimed), expected_revision=0)

    assert result.accepted is True
    assert result.objective.set_by == "agent"
    assert store.current().set_by == "agent"


def test_caller_mapping_is_not_modified(tmp_path):
    sdk, _ = _opened(tmp_path)
    target = {"map": MAP}
    payload = {
        "objective_id": "reach1",
        "revision": 1,
        "kind": "reach",
        "target": target,
        "set_by": "operator",
    }
    before = {
        "objective_id": "reach1",
        "revision": 1,
        "kind": "reach",
        "target": {"map": MAP},
        "set_by": "operator",
    }

    assert sdk.propose(payload, expected_revision=0).accepted
    assert payload == before
    assert target == {"map": MAP}


def test_mandatory_plain_nonnegative_expected_revision_is_checked_before_the_store(tmp_path):
    sdk, store = _opened(tmp_path)
    store.propose = _explode

    with pytest.raises(TypeError):
        sdk.propose(_hold())
    for bad in (-1, True, 1.0, "0", None):
        with pytest.raises(ObjectiveSdkRejected):
            sdk.propose(_hold(), expected_revision=bad)
    for bad in (None, [], "hold1", 1):
        with pytest.raises(ObjectiveSdkRejected):
            sdk.propose(bad, expected_revision=0)


def test_construction_does_not_touch_the_store(tmp_path):
    store = _store(tmp_path)
    store.current = _explode
    store.propose = _explode

    sdk = ObjectiveSdk(_selection(), store, enabled=False)
    assert isinstance(sdk, ObjectiveSdk)
    plugin_selection = _selection()
    assert isinstance(plugin_selection, PluginSelection)
