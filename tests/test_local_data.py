"""User-supplied file integration using invented rooms, flags and prompts only."""
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from pokegent.local_data import LocalDataError, local_path, read_data


@pytest.fixture
def local_files(tmp_path, monkeypatch):
    files = {
        "pret_map_groups.json": {
            "group_order": ["Demo"], "groups": {"Demo": ["DemoArena", "DemoExit"]},
        },
        "cinnabar_quiz.yaml": {"questions": [{
            "number": 1, "prompt": "Does the synthetic lamp glow?", "answer_yes": True,
            "door_flag": "0x11",
        }]},
        "elevators.yaml": {"elevators": {"demo": {
            "scene": "dialog.elevator", "map": [0, 0], "menu": "multichoice",
            "panels": [[1, 1]], "exit_mat": [2, 1], "exit_push": "down", "cancel": 127,
            "floors": [{"code": "exit", "label": "Demo", "map": [0, 1],
                        "place": "Synthetic exit", "arrival": [1, 1]}],
        }}},
        "puzzles.yaml": {"schema": "puzzles/1", "mansion": {
            "switch_flag": 18, "maps": {"Demo": [0, 0]},
            "secret_key": {"map": [0, 0], "tile": [2, 3], "hidden_item_flag": 20},
            "statues": [{"id": "demo", "floor": "Demo", "map": [0, 0], "statue": [2, 2],
                         "stand": [2, 1], "facing": "N", "opens": [[[3, 3]]],
                         "closes": [[[4, 4]]]}],
        }},
        "field_surf.yaml": {"capability": {
            "move": {"name": "Surf", "move_id": 57},
            "badge": {"number": 5, "name": "Soul", "flag": 2084},
            "prompt": {"text": "Synthetic water prompt"},
        }, "start_rule": {"player_elevation": 3, "front_tile": "Synthetic shore",
                          "result": "Synthetic water", "water_behaviors": {}},
            "dismount_rule": {"land_elevation": 3, "water_elevation": 1, "rule": "Synthetic"}},
        "prompt_rules.yaml": {"rules": [{
            "kind": "quiz", "patterns": ["Does the synthetic lamp glow?"], "default_yes": True,
            "wrong_answer_cost": "synthetic consequence", "metadata": {"door": 1, "door_flag": 17},
        }]},
        "fly_destinations.json": {"destinations": [{
            "mapsec": 0, "name": "DemoExit", "map_key": [0, 1], "landing": [1, 1],
            "visited_flag": 19, "cell": [0, 0],
        }]},
    }
    for name, data in files.items():
        encoded = json.dumps(data) if name.endswith("json") else yaml.safe_dump(data)
        (tmp_path / name).write_text(encoded)
    monkeypatch.delenv("POKEGENT_MAP_GROUPS", raising=False)
    monkeypatch.setenv("POKEGENT_LOCAL_DATA_DIR", str(tmp_path))
    return tmp_path, files


@pytest.mark.parametrize("filename", [
    "pret_map_groups.json", "cinnabar_quiz.yaml", "elevators.yaml", "puzzles.yaml",
    "field_surf.yaml", "prompt_rules.yaml", "fly_destinations.json",
])
def test_valid_local_file_uses_original_schema(local_files, filename):
    directory, expected = local_files
    assert local_path(filename) == directory / filename
    assert read_data(filename) == expected[filename]


@pytest.mark.parametrize('water', ['omitted', None, 0, 7])
def test_surf_unknown_and_legacy_water_elevation(local_files, water):
    directory, documents = local_files
    document = copy.deepcopy(documents['field_surf.yaml'])
    dismount = document['dismount_rule']
    if water == 'omitted':
        del dismount['water_elevation']
    else:
        dismount['water_elevation'] = water
    (directory / 'field_surf.yaml').write_text(yaml.safe_dump(document))
    assert read_data('field_surf.yaml') == document
    from pokegent.local_data import Surf
    assert Surf.model_validate(document).dismount_rule.water_elevation == (
        None if water == 'omitted' else water)


@pytest.mark.parametrize('water', [-1, '1', True, 1.5, {}, []])
def test_surf_invalid_water_elevation_still_refused(local_files, water):
    directory, documents = local_files
    document = copy.deepcopy(documents['field_surf.yaml'])
    document['dismount_rule']['water_elevation'] = water
    (directory / 'field_surf.yaml').write_text(yaml.safe_dump(document))
    with pytest.raises(LocalDataError, match='schema validation failed'):
        read_data('field_surf.yaml')


def test_documented_surf_schema_matches_model():
    from pokegent.local_data import Surf
    documented = json.loads((Path(__file__).resolve().parents[1]
                             / 'docs/local-data-schemas.json').read_text())
    assert documented['field_surf.yaml'] == Surf.model_json_schema()


@pytest.mark.parametrize("filename", [
    "pret_map_groups.json", "cinnabar_quiz.yaml", "elevators.yaml", "puzzles.yaml",
    "field_surf.yaml", "prompt_rules.yaml", "fly_destinations.json",
])
def test_invalid_supplied_files_never_silently_disable(local_files, filename):
    directory, _ = local_files
    (directory / filename).write_text('{"invalid": "synthetic secret not to log"}')
    with pytest.raises(LocalDataError, match="schema validation failed") as error:
        read_data(filename, optional=True)
    assert "synthetic secret" not in str(error.value)


def test_map_order_must_match_group_keys(local_files):
    directory, documents = local_files
    doc = copy.deepcopy(documents["pret_map_groups.json"])
    doc["group_order"] = ["Missing"]
    (directory / "pret_map_groups.json").write_text(json.dumps(doc))
    with pytest.raises(LocalDataError):
        read_data("pret_map_groups.json")


def test_local_data_directory_cannot_point_into_checkout(monkeypatch):
    monkeypatch.setenv("POKEGENT_LOCAL_DATA_DIR", str(Path(__file__).resolve().parents[1]))
    with pytest.raises(LocalDataError, match="outside the checkout"):
        read_data("puzzles.yaml", optional=True)


def test_enabled_features_register_and_consume_synthetic_files(local_files):
    # A fresh process proves import-time table loading and registration in the actual runtime.
    code = '''
from types import SimpleNamespace as NS
from pokegent.games.gen3.firered.maps import map_names
from pokegent.actions.field import fly
from pokegent.actions.field.surf.rules import reference, surf_capability
from pokegent.actions.special import elevators, mansion, quiz, switch_puzzle, register
from pokegent.actions.menu.dialog.prompts import classify_prompt, PromptKind
from pokegent.actions._exec.registry import ExecutorRegistry, VerifierRegistry
from pokegent.options.registry import BuilderRegistry
from pokegent.state.scenes import DetectorRegistry
from pokegent.state.catalog import load_scene_catalog
assert map_names.find_map("DemoExit") == (0, 1)
assert fly.DESTINATIONS_BY_KEY[(0, 1)].landing == (1, 1)
assert fly.visited(NS(flags=NS(is_set=lambda bit: bit == 19))) == fly.DESTINATIONS
assert elevators.BY_MAP[(0, 0)][0] == "demo"
assert quiz.QUESTIONS[0].answer_yes and quiz.DOORS[1] == 17
assert mansion.SWITCH_FLAG == 18 and mansion.SECRET_KEY_TILE == (2, 3)
assert mansion.statues()[0].opens == (((3, 3),),)
assert (0, 0) in switch_puzzle._MANSION_MAPS
assert reference()["capability"]["move"]["move_id"] == 57
snapshot = NS(usable=True, player=NS(badges=(5,)), party=(NS(species=1, moves=(NS(move_id=57),)),))
assert surf_capability(snapshot).available
text = NS(pages=("Does the synthetic lamp glow?",), last_page="Does the synthetic lamp glow?",
          text="Does the synthetic lamp glow?", digest="synthetic")
info = classify_prompt(text)
assert info.kind == PromptKind.QUIZ and info.default_yes is True
assert info.metadata["door_flag"] == 17
executors, verifiers = ExecutorRegistry(), VerifierRegistry()
builders = BuilderRegistry()
family = register(detectors=DetectorRegistry(catalog=load_scene_catalog()), builders=builders,
                  executors=executors, verifiers=verifiers)
assert "dialog.elevator" in family.claimed_scenes
assert "special.select_floor" in executors.refs()
assert "special.press_switch" in executors.refs()
assert "special.switch_done" in verifiers.refs()
print("Synthetic local data restored map/Fly/elevator/quiz/mansion/surf/prompt behavior")
'''
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=os.environ, timeout=20
    )
    assert result.returncode == 0, result.stderr
