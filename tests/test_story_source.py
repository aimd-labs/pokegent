"""Invented progression only: exercise the real loader, store and config seams."""
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pokegent.app import AppConfig, AppError, config_from_mapping, make_objective_session
from pokegent.games.gen3.firered.reader.schemas import FlagSet, VarTable
from pokegent.goals.dex_fallback import FallbackUnavailable
from pokegent.goals.source import ObjectiveSession
from pokegent.goals.store import ObjectiveStore
from pokegent.goals.story import StoryComplete, load_progression
from pokegent.user_config import load_user_config


def artifact():
    policy = {"order": ["p", "q"], "required": ["q"]}
    return dict(schema_version=1, policy=policy,
                policy_sha256=policy_hash(policy),
                provenance=dict(rom_sha256="a" * 64, source_sha256="b" * 64,
                                map_index_sha256="c" * 64, source_pin="invented",
                                generator="synthetic"),
                nodes=[dict(id=name, requires=[] if name == "p" else ["p"],
                            objective=dict(objective_id=f"story_{name}", revision=1,
                                           kind="hold", rationale=f"Resolve {name}",
                                           success_probes=[dict(kind="flag", flag=flag)]))
                       for name, flag in (("p", 1), ("q", 2))])


def policy_hash(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":"))
                          .encode()).hexdigest()


def load(tmp_path, data=None):
    (tmp_path / "progression.json").write_text(json.dumps(data or artifact()))
    return load_progression(path=tmp_path / "progression.json")


def snapshot(*flags, usable=True, vars=VarTable()):
    return SimpleNamespace(usable=usable, flags=FlagSet(set_ids=tuple(flags)), vars=vars,
                           bag=(), player=None)


def test_prerequisite_successor_terminal_use_only_current_snapshot(tmp_path):
    source = load(tmp_path)
    assert source.from_snapshot(snapshot())["objective_id"] == "story_p"
    assert source.from_snapshot(snapshot(1))["objective_id"] == "story_q"
    with pytest.raises(StoryComplete):
        source.from_snapshot(snapshot(1, 2))
    # A fresh proof of the downstream node discharges historical prerequisites.
    with pytest.raises(StoryComplete):
        source.from_snapshot(snapshot(2))
    assert source.from_snapshot(snapshot())["objective_id"] == "story_p"


@pytest.mark.parametrize("missing", ["usable", "flags", "vars"])
def test_unknown_never_supplies_completion_or_goal(tmp_path, missing):
    data = artifact()
    if missing == "vars":
        data["nodes"][0]["objective"]["success_probes"] = [
            dict(kind="var_at_least", var=0x4000, value=1)]
    source = load(tmp_path, data)
    current = snapshot()
    setattr(current, missing, False if missing == "usable" else None)
    with pytest.raises(FallbackUnavailable):
        source.from_snapshot(current)


@pytest.mark.parametrize("value,expected", [(0, "story_p"), (1, "story_q"), (2, "story_q")])
def test_threshold_and_sparse_zero(tmp_path, value, expected):
    data = artifact()
    data["nodes"][0]["objective"]["success_probes"] = [
        dict(kind="var_at_least", var=0x4000, value=1)]
    source = load(tmp_path, data)
    values = VarTable(nonzero=((0x4000, value),)) if value else VarTable()
    assert source.from_snapshot(snapshot(vars=values))["objective_id"] == expected


def test_consumed_item_or_persistent_proof_and_unknown_alternative(tmp_path):
    data = artifact()
    data["nodes"][0]["objective"]["success_probes"] = [dict(
        kind="any_of", children=[dict(kind="item", item=1, min_quantity=1),
                                  dict(kind="flag", flag=1)])]
    data["nodes"][1]["objective"]["success_probes"] = [
        dict(kind="var", var=0x4000, value=1)]
    source = load(tmp_path, data)
    assert source.from_snapshot(snapshot())["objective_id"] == "story_p"  # empty bag known
    assert source.from_snapshot(snapshot(1))["objective_id"] == "story_q"
    current = snapshot()
    current.flags = None
    with pytest.raises(FallbackUnavailable):
        source.from_snapshot(current)
    current.bag = (SimpleNamespace(items=(SimpleNamespace(item_id=1, quantity=1),)),)
    # One true alternative wins even with unknown flags on the same node.
    assert source.from_snapshot(current)["objective_id"] == "story_q"


@pytest.mark.parametrize("bad", ["cycle", "reference", "duplicate", "order", "hash", "extra",
                                  "probe", "empty", "expiry", "review", "target", "policy"])
def test_artifact_rejected_whole_and_redacted(tmp_path, bad):
    data = artifact()
    if bad == "cycle":
        data["nodes"][0]["requires"] = ["q"]
    elif bad == "reference":
        data["nodes"][0]["requires"] = ["absent"]
    elif bad == "duplicate":
        data["nodes"][1]["id"] = "p"
    elif bad == "order":
        data["policy"]["order"] = ["p", "p"]
        data["policy_sha256"] = policy_hash(data["policy"])
    elif bad == "hash":
        data["policy_sha256"] = "d" * 64
    elif bad == "extra":
        data["private_marker"] = "do not disclose"
    elif bad == "probe":
        data["nodes"][0]["objective"]["success_probes"] = [dict(kind="game_clear")]
    elif bad == "empty":
        data["nodes"][0]["objective"]["success_probes"] = []
    elif bad == "expiry":
        data["nodes"][0]["objective"]["expires_at"] = "2099-01-01T00:00:00Z"
    elif bad == "review":
        data["nodes"][0]["objective"]["next_review_on"] = ["stuck"]
    elif bad == "target":
        data["nodes"][0]["objective"]["target"] = {"npc": -1}
    else:
        data["policy"]["readiness"] = "unsupported"
    with pytest.raises(FallbackUnavailable) as error:
        load(tmp_path, data)
    assert str(error.value) == "invalid progression artifact"
    assert str(tmp_path) not in str(error.value)


def test_path_size_missing_and_exact_seven(tmp_path, monkeypatch):
    from pokegent import local_data
    with pytest.raises(FallbackUnavailable):
        load_progression(path=tmp_path / "absent")
    path = tmp_path / "progression.json"
    path.write_bytes(b" " * 65537)
    with pytest.raises(FallbackUnavailable):
        load_progression(path=path)
    path.write_text(json.dumps(artifact()))
    monkeypatch.setattr(local_data, "_CHECKOUT", tmp_path)
    alias = tmp_path.parent / f"{tmp_path.name}-alias.json"
    alias.symlink_to(path)
    try:
        with pytest.raises(FallbackUnavailable):
            load_progression(path=alias)
    finally:
        alias.unlink()
    assert len(local_data.SCHEMAS) == 7 and "progression.json" not in local_data.SCHEMAS


@pytest.mark.parametrize("policy", [None, "hold", "pause"])
def test_story_config_requires_explicit_continue(policy):
    raw = {"objectives": {"fallback": "story"}}
    if policy:
        raw["run"] = {"on_objective_done": policy}
    with pytest.raises(AppError, match="continue"):
        config_from_mapping(raw)


def test_config_file_and_final_override_phases(tmp_path):
    raw = dict(objectives={"fallback": "story"}, run={"on_objective_done": "continue"})
    cfg = config_from_mapping(raw)
    assert cfg.story_fallback and not cfg.dex_fallback
    for policy in ("hold", "pause"):
        with pytest.raises(AppError, match="continue"):
            replace(cfg, on_objective_done=policy)
    with pytest.raises(AppError):
        AppConfig(story_fallback=True, dex_fallback=True, on_objective_done="continue")
    path = tmp_path / "config.yaml"
    path.write_text("decider: first\nobjectives:\n  fallback: story\n")
    # User config validates enums; AppConfig owns the cross-field error.
    load_user_config(path, environ={})
    assert config_from_mapping({}).on_objective_done == "hold"
    assert config_from_mapping({"objectives": {"fallback": "dex"}}).dex_fallback


def test_factory_existing_session_and_direct_guards(tmp_path, monkeypatch):
    load(tmp_path)
    monkeypatch.setenv("POKEGENT_LOCAL_DATA_DIR", str(tmp_path))
    with pytest.raises(AppError):
        make_objective_session(None, story_fallback=True)
    with pytest.raises(AppError):
        make_objective_session(tmp_path / "goal.json", story_fallback=True, dex_fallback=True)
    store = ObjectiveStore(tmp_path / "goal.json")
    manual = dict(objective_id="manual", revision=1, kind="hold", set_by="operator")
    assert store.propose(manual).accepted
    session = ObjectiveSession(store)
    assert make_objective_session(session, story_fallback=True) is session
    session.refresh()
    assert session.objective.objective_id == "manual"
    assert session.fallback.from_snapshot(snapshot())["set_by"] == "fallback:story"


def test_actual_run_config_rejects_cli_rescue_and_late_override(tmp_path, monkeypatch):
    from pokegent import cli
    monkeypatch.setenv("POKEGENT_FIELD_MENU", "1")
    path = tmp_path / "config.yaml"
    for file_policy, override in ((None, "continue"), ("continue", "hold"),
                                   ("continue", "pause")):
        path.write_text('objectives:\n  fallback: story\n' + (
            f'run:\n  on_objective_done: {file_policy}\n' if file_policy else ''))
        with pytest.raises(SystemExit) as result:
            cli.main(["run", "--new-game", "--config", str(path), "--out", str(tmp_path / "out"),
                      "--on-objective-done", override])
        assert result.value.code == 2


@pytest.mark.parametrize("version", [True, 1.0, "1", 2])
def test_strict_artifact_version(tmp_path, version):
    data = artifact()
    data["schema_version"] = version
    with pytest.raises(FallbackUnavailable):
        load(tmp_path, data)


def test_factory_missing_artifact_error_is_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("POKEGENT_LOCAL_DATA_DIR", str(tmp_path / "private-marker"))
    with pytest.raises(AppError) as error:
        make_objective_session(tmp_path / "goal.json", story_fallback=True)
    assert error.value.exit_code == 4
    assert "private-marker" not in str(error.value)


def test_authored_policy_order_and_required_subset_are_explicit(tmp_path):
    data = artifact()
    data["nodes"][1]["requires"] = []
    data["policy"] = dict(order=["q", "p"], required=["q"])
    data["policy_sha256"] = policy_hash(data["policy"])
    source = load(tmp_path, data)
    assert source.from_snapshot(snapshot())["objective_id"] == "story_q"
    with pytest.raises(StoryComplete):
        source.from_snapshot(snapshot(2))  # unrelated optional P is not forced


def test_valid_json_probe_arrays_roundtrip_existing_objective_contract(tmp_path):
    from pokegent.goals.models import Objective
    data = artifact()
    data["nodes"][0]["objective"]["success_probes"] = [dict(
        kind="any_of", children=[dict(kind="flag", flag=1),
                                  dict(kind="var_at_least", var=0x4000, value=2)])]
    expected = Objective.model_validate_json(json.dumps(data["nodes"][0]["objective"]))
    source = load(tmp_path, data)
    assert source.nodes["p"].objective.model_dump(mode="json") == expected.model_dump(mode="json")
    proposal = source.from_snapshot(snapshot())
    proposal["set_by"] = expected.set_by
    assert Objective.model_validate_json(json.dumps(proposal)) == expected


@pytest.mark.parametrize("bad", ["scalar", "bool", "range", "extra", "nested_extra",
                                  "nested_bool", "nested_scalar"])
def test_invalid_nested_objective_preserves_existing_contract(tmp_path, bad):
    from pydantic import ValidationError

    from pokegent.goals.models import Objective
    data = artifact()
    obj = data["nodes"][0]["objective"]
    if bad in ("scalar", "bool"):
        obj = 7 if bad == "scalar" else True
    elif bad == "range":
        obj["success_probes"][0]["flag"] = 0
    elif bad == "extra":
        obj["private-marker"] = "hidden"
    elif bad == "nested_extra":
        obj["success_probes"][0]["private-marker"] = "hidden"
    elif bad == "nested_bool":
        obj["success_probes"][0]["flag"] = False
    else:
        obj["success_probes"] = [7]
    with pytest.raises(ValidationError):
        Objective.model_validate_json(json.dumps(obj))
    data["nodes"][0]["objective"] = obj
    with pytest.raises(FallbackUnavailable, match="^invalid progression artifact$"):
        load(tmp_path, data)
