"""Behavioral checks for a ROM-free public export using only invented data."""
import importlib.util
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from pokegent.actions.special import elevators, mansion, quiz
from pokegent.actions.special.register import register
from pokegent.games.gen3.firered.maps import map_names
from pokegent.state.catalog import load_scene_catalog


def test_missing_local_index_explains_setup(tmp_path):
    with pytest.raises(RuntimeError, match="POKEGENT_MAP_GROUPS"):
        map_names._load(str(tmp_path / "missing.json"))


def test_synthetic_index_supports_enumeration_and_lookup(tmp_path, monkeypatch):
    path = tmp_path / "synthetic-index.json"
    path.write_text(json.dumps({"group_order": ["Demo"], "groups": {"Demo": ["DemoRoom"]}}))
    monkeypatch.setenv("POKEGENT_MAP_GROUPS", str(path))
    assert map_names.group_sizes() == (1,)
    assert map_names.map_name(0, 0) == "DemoRoom"
    assert map_names.map_name(0, 1) is None
    assert map_names.find_map("demoroom") == (0, 0)


def test_special_actions_fail_closed_with_explanation(caplog):
    with caplog.at_level(logging.WARNING):
        family = register(detectors=None, builders=None, executors=None, verifiers=None)
    assert not family.claimed_scenes
    assert "Scripted special actions disabled" in caplog.text
    assert elevators.TABLE == {}
    assert quiz.QUESTIONS == ()
    state = SimpleNamespace(identity=SimpleNamespace(group=0, number=0))
    assert mansion.statues_on_map(state) == ()
    with pytest.raises(RuntimeError, match="Local puzzles.yaml data excluded"):
        mansion._data()


def test_public_catalog_retains_valid_interfaces():
    catalog = load_scene_catalog()
    assert catalog.get("field.free") is not None
    assert catalog.get("dialog.yes_no").has_yes_no
    assert catalog.get("system.unsupported_rom") is not None


def test_guard_catches_ignored_game_files_before_git_init(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "rom_guard", Path(__file__).resolve().parents[1] / "tools/check_rom_data.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / ".gitignore").write_text("*.gba\n")
    (tmp_path / "synthetic.gba").write_bytes(b"synthetic placeholder")
    assert module.violations(tmp_path) == ("synthetic.gba",)


def _load_guard():
    spec = importlib.util.spec_from_file_location(
        "rom_guard", Path(__file__).resolve().parents[1] / "tools/check_rom_data.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_packaged_item_classes_path_also_guarded(tmp_path):
    """After references/*.yaml moved into the package (pokegent/references/), the ROM-data
    guard must still refuse a synthetic reference_item marker at the NEW packaged path,
    not only at the legacy top-level path (issue #39 review probe)."""
    module = _load_guard()
    # New packaged path carries the forbidden ROM-derived marker -> violation flagged.
    new_dir = tmp_path / "pokegent" / "references"
    new_dir.mkdir(parents=True)
    (new_dir / "battle_item_classes.yaml").write_text("reference_item: invented\n")
    violations = module.violations(tmp_path)
    assert "pokegent/references/battle_item_classes.yaml" in violations[0]

    # Legacy top-level path still guarded (regression: refusal behavior preserved).
    legacy = tmp_path / "references"
    legacy.mkdir()
    (legacy / "battle_item_classes.yaml").write_text("reference_item: invented\n")
    violations = module.violations(tmp_path)
    assert "references/battle_item_classes.yaml" in violations[0]


@pytest.mark.parametrize("suffix", [".gb", ".gbc", ".zip", ".7z", ".ss3", ".ss42"])
def test_guard_rejects_game_and_archive_extensions_even_if_text(tmp_path, suffix):
    spec = importlib.util.spec_from_file_location(
        "rom_guard", Path(__file__).resolve().parents[1] / "tools/check_rom_data.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    filename = "synthetic" + suffix
    (tmp_path / filename).write_text("synthetic text")
    assert filename in module.violations(tmp_path)


@pytest.mark.parametrize("payload", [b"\x89PNG\r\n\x1a\n", b"text\x00binary", b"\x7fELF"])
def test_guard_rejects_disguised_binary_payloads(tmp_path, payload):
    spec = importlib.util.spec_from_file_location(
        "rom_guard", Path(__file__).resolve().parents[1] / "tools/check_rom_data.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "innocent.txt").write_bytes(payload)
    assert "innocent.txt is an unexpected binary" in module.violations(tmp_path)
