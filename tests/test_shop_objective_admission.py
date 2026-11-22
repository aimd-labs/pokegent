"""Real objective admission for SHOP: store, server method, external file and factory."""

import importlib
import json
from types import SimpleNamespace

import pytest

from pokegent.app import make_objective_session
from pokegent.goals.shop_admission import ShopObservation
from pokegent.goals.shop_stock import ShopStockFacts, StockCoverage
from pokegent.goals.source import ObjectiveSession
from pokegent.goals.store import ObjectiveStore, atomic_write_json
from pokegent.goals.validate import MapFacts
from pokegent.server import ExternalDecider

PROFILE = "firered-us-rev0"
SHA = "a" * 40
MAP = "PewterMart"
MAP_KEY = (3, 7)


def stock(*items, key=None, coverage=StockCoverage.POSSIBLE_CLOSED, profile=PROFILE, sha=SHA):
    return ShopStockFacts(
        key or MAP_KEY,
        profile,
        sha,
        coverage,
        (),
        frozenset(items),
    )


def observed(*items, profile=PROFILE, sha=SHA, usable=True):
    counts: dict[int, int] = {}
    for entry in items:
        item_id, quantity = entry
        counts[item_id] = counts.get(item_id, 0) + quantity
    return ShopObservation(profile, sha, tuple(sorted(counts.items())), usable)


class Directory:
    def __init__(self, facts=None, *, error=None):
        self.facts = facts
        self.error = error
        self.calls: list[tuple[int, int]] = []

    def describe(self, group, number):
        return MapFacts(10, 10, 1, frozenset())

    def resolve_item(self, name):
        return None

    def leader_for_badge(self, badge):
        return None

    def shop_stock(self, group, number):
        self.calls.append((group, number))
        if self.error is not None:
            raise self.error
        return self.facts


def shop(revision=1, *, quantity=2, item=1, map_name=MAP):
    return {
        "objective_id": f"shop{revision}",
        "revision": revision,
        "kind": "shop",
        "target": {"map": map_name, "item": item, "quantity": quantity},
    }


def hold(revision=1):
    return {
        "objective_id": f"hold{revision}",
        "revision": revision,
        "kind": "hold",
        "success_probes": [{"kind": "money_at_least", "amount": 1}],
    }


@pytest.fixture(autouse=True)
def map_resolution(monkeypatch):
    module = importlib.import_module("pokegent.goals.validate")
    monkeypatch.setattr(module, "resolve_map", lambda name: (3, 7))
    monkeypatch.setattr(module.map_names, "map_name", lambda group, number: "MAP_PEWTER_MART")


def test_absent_unmet_fresh_observation_rejects_and_preserves_active(tmp_path):
    directory = Directory(stock())
    store = ObjectiveStore(tmp_path / "objective.json", directory=directory)
    assert store.propose(hold(1)).accepted
    store.bind_observation(lambda: observed((1, 0)))

    result = store.propose(shop(2))

    assert not result.accepted
    assert result.reasons[0].code == "shop_method_unavailable"
    assert result.reasons[0].field == "target.item"
    assert "not a claim of global unavailability" in result.reasons[0].detail
    assert store.current().revision == 1
    assert store.current().kind == "hold"
    assert [row["event"] for row in store.history()][-1] == "rejected"
    assert store.revision() == 1
    assert json.loads(store.path.read_text())["revision"] == 1
    assert directory.calls == [(3, 7)]


def test_fresh_owned_accepts_even_when_cached_observation_says_unmet(tmp_path):
    cached = observed((1, 0))
    assert cached.quantity(1) == 0
    store = ObjectiveStore(tmp_path / "objective.json", directory=Directory(stock()))
    store.bind_observation(lambda: observed((1, 2)))

    assert store.propose(shop(1, quantity=2)).accepted


def test_fresh_unmet_rejects_even_when_cached_observation_says_owned(tmp_path):
    cached = observed((1, 2))
    assert cached.quantity(1) == 2
    store = ObjectiveStore(tmp_path / "objective.json", directory=Directory(stock()))
    store.bind_observation(lambda: observed((1, 1)))

    result = store.propose(shop(1, quantity=2))

    assert not result.accepted
    assert result.reasons[0].code == "shop_method_unavailable"


@pytest.mark.parametrize(
    "facts,observation",
    [
        (stock(1), observed((1, 0))),  # possible-present is not proven absence
        (stock(), observed((1, 0), usable=False)),
        (stock(), observed((1, 0), profile="other")),
        (stock(), observed((1, 0), sha="b" * 40)),
        (stock(coverage=StockCoverage.UNKNOWN), observed((1, 0))),
        (stock(key=(9, 9)), observed((1, 0))),
        (None, observed((1, 0))),
        (stock(1, 2), observed((1, 0))),
    ],
)
def test_unknown_partial_mismatch_or_present_never_rejects(tmp_path, facts, observation):
    store = ObjectiveStore(tmp_path / "objective.json", directory=Directory(facts))
    store.bind_observation(lambda: observation)
    assert store.propose(shop(1)).accepted


def test_unusable_or_failed_observation_and_unbound_shop_stay_unknown(tmp_path):
    store = ObjectiveStore(tmp_path / "objective.json", directory=Directory(stock()))
    store.bind_observation(lambda: observed((1, 0), usable=False))
    assert store.propose(shop(1)).accepted

    other = ObjectiveStore(tmp_path / "other.json", directory=Directory(stock()))
    other.bind_observation(lambda: (_ for _ in ()).throw(RuntimeError("native refused")))
    assert other.propose(shop(1)).accepted

    unbound = ObjectiveStore(tmp_path / "unbound.json", directory=Directory(stock()))
    unbound.bind_observation(lambda: observed((1, 0)))
    assert unbound.propose({**shop(1), "target": {"item": 1}}).accepted


def test_already_owned_satisfies_without_purchase_event(tmp_path):
    directory = Directory(stock())
    store = ObjectiveStore(tmp_path / "objective.json", directory=directory)
    store.bind_observation(lambda: observed((1, 5)))
    result = store.propose(shop(1, quantity=2))
    assert result.accepted
    assert directory.calls == []  # possession is decided from the fresh bag alone


def test_server_objective_propose_uses_the_same_store_gate(tmp_path):
    store = ObjectiveStore(tmp_path / "objective.json", directory=Directory(stock()))
    assert store.propose(hold(1)).accepted
    store.bind_observation(lambda: observed((1, 0)))
    owner = object.__new__(ExternalDecider)
    owner._objective_store = store

    response = owner.objective_propose(shop(2))

    assert response["accepted"] is False
    assert response["proposal"]["reasons"][0]["code"] == "shop_method_unavailable"
    assert store.current().revision == 1


def test_external_file_ingest_uses_the_common_gate_and_keeps_active(tmp_path):
    store = ObjectiveStore(tmp_path / "objective.json", directory=Directory(stock()))
    assert store.propose(hold(1)).accepted
    store.bind_observation(lambda: observed((1, 0)))
    atomic_write_json(store.path, shop(2))

    store.poll()

    assert store.current().revision == 1
    warning = store.warnings[-1]
    assert warning.code == "shop_method_unavailable"
    assert warning.reasons[0].field == "target.item"
    assert [row["event"] for row in store.history()][-1] == "warning"


@pytest.mark.parametrize("form", ["path", "store", "session"])
def test_factory_binds_provider_before_use_for_every_supported_form(tmp_path, form):
    path = tmp_path / "objective.json"
    store = ObjectiveStore(path)
    arg = {"path": path, "store": store, "session": ObjectiveSession(store)}[form]
    calls: list[int] = []

    def observation():
        calls.append(1)
        return observed((1, 2))

    session = make_objective_session(
        arg, directory=Directory(stock()), shop_observation=observation
    )
    assert session is not None
    assert session.store._observation is observation
    assert session.store.directory is not None
    assert session.store.propose(hold(1)).accepted
    assert calls == []
    assert session.store.propose(shop(2)).accepted
    assert calls == [1]


def test_observation_from_snapshot_requires_usable_bag_and_sums_pockets():
    snapshot = SimpleNamespace(
        usable=True,
        rom_profile=PROFILE,
        bag=(
            SimpleNamespace(items=(SimpleNamespace(item_id=1, quantity=2),)),
            SimpleNamespace(items=(SimpleNamespace(item_id=1, quantity=3),)),
        ),
    )
    observation = ShopObservation.from_snapshot(snapshot, rom_sha1=SHA)
    assert observation.usable and observation.quantity(1) == 5

    snapshot.usable = False
    assert not ShopObservation.from_snapshot(snapshot, rom_sha1=SHA).usable


def test_build_runner_constructs_a_fresh_core_provider_not_the_decision_source(
    tmp_path, monkeypatch
):
    app_module = importlib.import_module("pokegent.app")
    reads: list[int] = []
    decision_reads: list[int] = []
    snapshot = SimpleNamespace(
        usable=True,
        rom_profile=PROFILE,
        bag=(SimpleNamespace(items=(SimpleNamespace(item_id=1, quantity=2),)),),
    )

    class FreshReader:
        def __init__(self, source, profile, **kwargs):
            self.source = source

        def read(self):
            reads.append(1)
            return SimpleNamespace(snapshot=snapshot)

    class DecisionSource:
        def __init__(self, *args, **kwargs):
            pass

        def read_snapshot(self):
            decision_reads.append(1)
            raise AssertionError("decision snapshot must not feed admission")

    class FakeRunner:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self._last_snapshot = SimpleNamespace(usable=False)

    fake_registries = SimpleNamespace(
        detectors=object(), builders=object(), executors=object(), verifiers=object()
    )
    fake_bundle = SimpleNamespace(plugins={}, sidecar=None, confirm_policy=lambda catalog: None)
    monkeypatch.setattr(app_module, "load_scene_catalog", lambda: object())
    monkeypatch.setattr(app_module.Registries, "new", lambda catalog: fake_registries)
    monkeypatch.setattr(app_module, "register_all", lambda *args, **kwargs: fake_bundle)
    monkeypatch.setattr(app_module, "CompositeSnapshotSource", DecisionSource)
    monkeypatch.setattr(app_module, "Perceiver", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(
        app_module, "Executor", lambda *args, **kwargs: SimpleNamespace(perceiver=None)
    )
    monkeypatch.setattr(app_module, "Runner", FakeRunner)
    monkeypatch.setattr(
        "pokegent.games.gen3.firered.reader.menu_rom.FireRedMenuResolver",
        lambda actor, profile: None,
    )
    monkeypatch.setattr("pokegent.games.gen3.firered.reader.live.LiveReader", FreshReader)
    profile = SimpleNamespace(profile_id=PROFILE, rom=SimpleNamespace(sha1=SHA))
    app = app_module.build_runner(
        actor=object(),
        profile=profile,
        events=object(),
        decider=SimpleNamespace(),
        objectives=tmp_path / "objective.json",
        objective_directory=Directory(stock()),
        sink=SimpleNamespace(),
    )
    store = app.objectives.store
    assert store.propose(hold(1)).accepted
    assert reads == []
    assert store.propose(shop(2)).accepted
    assert reads == [1]
    assert decision_reads == []


def test_collect_with_map_and_item_is_not_rejected_by_shop_admission(tmp_path):
    directory = Directory(stock())
    store = ObjectiveStore(tmp_path / "objective.json", directory=directory)
    calls: list[int] = []

    def observation():
        calls.append(1)
        return observed((1, 0))

    store.bind_observation(observation)

    result = store.propose(
        {
            "objective_id": "collect1",
            "revision": 1,
            "kind": "collect",
            "target": {"map": MAP, "item": 1, "quantity": 2},
        }
    )

    assert result.accepted
    assert calls == []
    assert directory.calls == []
