"""S1a: invented RAM facts through real probe owners; no game data."""

import json

import pytest
from test_objective_completion_continue import harness as harness
from test_objective_stuck_review import PRE_FIELD_CURRENT, PRE_FIELD_HISTORY

from pokegent.actions.field.builder import FieldBuilder
from pokegent.games.gen3.firered.reader.schemas import (
    FlagSet,
    Health,
    Player,
    ReadError,
    StateSnapshot,
    VarTable,
)
from pokegent.goals.models import Objective
from pokegent.goals.store import ObjectiveStore
from pokegent.goals.tracker import evaluate_probe
from pokegent.goals.validate import validate
from pokegent.navigation.serving import serves_goal


def goal(*probes, kind="hold", revision=1):
    return dict(objective_id="probe_goal", revision=revision, kind=kind,
                success_probes=list(probes), set_by="operator", forbid=["sell"])


def threshold(value=3):
    return dict(kind="var_at_least", var=0x4001, value=value)


def either(*children):
    return dict(kind="any_of", children=list(children))


def resolved(*probes):
    result = validate(goal(*probes))
    assert result.ok, result.reasons
    return result.resolved


def snapshot(value=0, *, flag=False, usable=True):
    return StateSnapshot.create(
        rom_profile="synthetic", snapshot_seq=1, emulator_frame=0,
        player=Player(name="Test", gender="male", tid=0, sid=0, map_group=0,
                      map_num=0, map_layout_id=0, x=0, y=0, money=0, coins=0) if usable else None,
        health=Health(valid=usable, read_errors=() if usable else (
            ReadError(subsystem="test", code="invalid", message="invented read failure"),
        )), flags=FlagSet(set_ids=(7,) if flag else ()) if usable else None,
        vars=VarTable(nonzero=((0x4001, value),) if value else ()) if usable else None,
    )


@pytest.mark.parametrize("value,expected", [(0, False), (2, False), (3, True), (4, True)])
def test_threshold_and_original_equality(value, expected):
    probe = resolved(threshold()).probes[0]
    assert evaluate_probe(probe, snapshot(value)) is expected
    old = resolved(dict(kind="var", var=0x4001, value=3)).probes[0]
    assert evaluate_probe(old, snapshot(value)) is (value == 3)
    assert ">= 3" in probe.label()
    assert FieldBuilder._probe_text(probe) == probe.label()


def test_missing_or_unusable_ram_never_proves_threshold_or_group():
    for probe in resolved(threshold(0), either(threshold(0))).probes:
        assert not evaluate_probe(probe, snapshot(4, usable=False))
        assert not evaluate_probe(probe, snapshot().model_copy(update={"vars": None}))
    # A present sparse table means zero, rather than unreadable RAM.
    assert evaluate_probe(resolved(threshold(0)).probes[0], snapshot())


def test_consumed_item_or_persistent_proof_and_conjunction():
    group = either(dict(kind="item", item=17), dict(kind="flag", flag=7))
    probes = resolved(group, threshold()).probes
    assert not evaluate_probe(probes[0], snapshot(4))
    assert evaluate_probe(probes[0], snapshot(0, flag=True))
    assert not all(evaluate_probe(p, snapshot(0, flag=True)) for p in probes)
    assert all(evaluate_probe(p, snapshot(4, flag=True)) for p in probes)
    assert "flag 0x7" in probes[0].label()
    assert FieldBuilder._probe_text(probes[0]) == probes[0].label()


@pytest.mark.parametrize("probe", [either(), either(*([threshold()] * 9)),
                                   either(either(threshold())), threshold(-1),
                                   dict(kind="var_at_least", var=0x3FFF, value=2),
                                   either(dict(kind="unknown")),
                                   either(dict(kind="flag", flag=0))])
def test_invalid_groups_and_leaves(probe):
    assert not validate(goal(probe)).ok


def test_eight_leaves_schema_and_vocabulary():
    from pokegent.goals.models import PROBE_KINDS
    obj = Objective.model_validate(goal(either(*([threshold()] * 8))))
    assert len(obj.success_probes[0].children) == 8
    assert {"any_of", "var_at_least"} <= set(PROBE_KINDS)
    assert "AnyOfProbe" in Objective.model_json_schema()["$defs"]


@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize("value,expected", [(2, False), (3, True), (4, True)])
def test_serving_effect_is_guidance_and_never_fresh_completion(grouped, value, expected):
    probe = either(threshold(), dict(kind="flag", flag=7)) if grouped else threshold()
    obj = Objective.model_validate(goal(probe, kind="reach"))
    facts = {"target": {"kind": "npc", "map": (0, 0)}, "set_vars": [(0x4001, value)]}
    # A targetless reach needs no route when there is no effect match.
    assert (serves_goal(facts, obj) is True) is expected
    assert not evaluate_probe(resolved(probe).probes[0], snapshot())
    if grouped:
        assert serves_goal({**facts, "set_flags": [7]}, obj) is True


@pytest.mark.parametrize("group,must_wait", [(False, True), (True, True), ("mixed", False)])
def test_runner_admission_and_hold_wait(harness, group, must_wait):
    h = harness(amount=99, policy="hold")
    probe = threshold()
    if group:
        other = dict(kind="item", item=17) if group == "mixed" else dict(kind="flag", flag=7)
        probe = either(probe, other)
    assert h.store.propose(goal(probe, revision=2), expected_revision=1).accepted
    assert h.runner._iteration() is None
    assert bool(h.session.hold_waiting_reason) is must_wait
    assert not h.session.done and h.store.current().forbid == ("sell",)
    assert h.state.actions == (0 if must_wait else 1)


def test_prefield_serialization_and_cold_history_are_identical(tmp_path):
    path = tmp_path / "objective.json"
    path.write_text(PRE_FIELD_CURRENT)
    path.with_name(path.name + ".history.jsonl").write_text(PRE_FIELD_HISTORY)
    store = ObjectiveStore(path)
    obj = store.current()
    assert obj.model_dump(mode="json") == json.loads(PRE_FIELD_CURRENT)
    assert store.history()[0]["objective"] == obj.model_dump(mode="json")
    assert not store.propose(obj.model_copy(update={"revision": 2}), expected_revision=0).accepted
    assert path.read_text() == PRE_FIELD_CURRENT
    cold = ObjectiveStore(path)
    assert cold.current() == obj and cold.revision() == 1


def test_actual_runner_group_completion_still_needs_current_ram(harness):
    h = harness(amount=99)
    assert h.store.propose(goal(either(threshold(), dict(kind="money_at_least", amount=1)),
                                revision=2), expected_revision=1).accepted
    assert h.runner._iteration() is None
    assert not h.session.completed and h.store.current().revision == 2
    assert h.state.actions == 1
    assert h.runner._iteration() is None
    assert h.store.current() is None
    assert [row["event"] for row in h.store.history()].count("completed") == 1
    assert h.store.revision() == 2


def test_consumable_item_present_before_persistent_proof():
    from pokegent.games.gen3.firered.reader.schemas import BagItem, BagPocket
    probe = resolved(either(dict(kind="item", item=17), dict(kind="flag", flag=7))).probes[0]
    current = snapshot().model_copy(update={"bag": (
        BagPocket(pocket="items", capacity=1, items=(BagItem(item_id=17, quantity=1),)),
    )})
    assert evaluate_probe(probe, current)
    assert not evaluate_probe(probe, snapshot())
    assert evaluate_probe(probe, snapshot(flag=True))


def test_real_tracker_conjunction_recomputes_after_unusable_frame(harness):
    h = harness(amount=99)
    assert h.store.propose(goal(either(dict(kind="item", item=17), dict(kind="flag", flag=7)),
                                threshold(), revision=2), expected_revision=1).accepted
    h.session.refresh()
    h.session.observe(snapshot(4))
    assert not h.session.completed
    h.session.observe(snapshot(2, flag=True))
    assert not h.session.completed
    h.session.observe(snapshot(4, flag=True))
    assert h.session.completed
    h.session.observe(snapshot(4, usable=False))
    assert not h.session.completed
    assert h.store.current().revision == 2
    assert not any(row["event"] == "completed" for row in h.store.history())


def test_unknown_map_in_group_is_rejected_at_child_location(tmp_path, monkeypatch):
    from pokegent.games.gen3.firered.maps import map_names
    from pokegent.goals.validate import _name_index
    path = tmp_path / "maps.json"
    path.write_text(json.dumps({"group_order": ["Demo"], "groups": {"Demo": ["DemoRoom"]}}))
    monkeypatch.setenv("POKEGENT_MAP_GROUPS", str(path))
    _name_index.cache_clear()
    map_names._load.cache_clear()
    result = validate(goal(either(dict(kind="map_arrival", map="InventedMissingRoom"))))
    assert not result.ok
    _name_index.cache_clear()
    map_names._load.cache_clear()
    assert any(reason.code == "unknown_map" and reason.field == "success_probes[0].children[0]"
               for reason in result.reasons)


@pytest.mark.parametrize("grouped", [False, True])
def test_adapter_scripted_step_classifies_threshold(grouped):
    from pokegent.goals.validate import ResolvedObjective, ResolvedTarget
    from pokegent.navigation.adapter import NavigationAdapter
    from pokegent.navigation.models import NavigationNode, NavigationPlan, NavigationTarget
    from pokegent.options.models import Legality, Option
    probe = either(threshold()) if grouped else threshold()
    obj = Objective.model_validate(goal(probe, kind="reach"))
    current = ResolvedObjective(obj, ResolvedTarget(group=0, number=0), resolved(probe).probes)
    option = Option(id="step", label="Invented step", legality=Legality.LEGAL,
                    executor_ref="field.walk_to", facts={"script_tiles": 1,
                    "target": {"kind": "tile", "map": (0, 0), "x": 1, "y": 1}})
    plan = NavigationPlan(start=NavigationNode(0, 0, 0, 0),
                          target=NavigationTarget(0, 0), cost=0)
    assert NavigationAdapter._current_map_step([option], plan, current) is option


def test_group_dex_leaf_reaches_navigation_and_battle_guidance():
    from types import SimpleNamespace

    from pokegent.actions.battle.catch_facts import wanted_species
    from pokegent.navigation.objectives import resolve_collect_navigation
    data = goal(either(dict(kind="dex_owned", species=7), threshold()), kind="dex")
    data["target"] = {"count": 2}
    current = validate(data).resolved
    assert current is not None
    world = SimpleNamespace(wild_encounter_sources=lambda species: (((0, 1), frozenset({"land"})),))
    routed, _ = resolve_collect_navigation(snapshot(), current, world)
    assert routed.navigation_map_keys == frozenset({(0, 1)})
    assert wanted_species(data) == 7
    assert wanted_species(goal(either(threshold()))) is None
