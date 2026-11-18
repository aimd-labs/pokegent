"""Exercise the publication guard with authored numeric data, never game tables."""

import importlib.util
import itertools
from pathlib import Path

import pytest


@pytest.fixture
def guard():
    spec = importlib.util.spec_from_file_location(
        "rom_guard", Path(__file__).resolve().parents[1] / "tools/check_rom_data.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("directory", ["references", "pokegent/references"])
@pytest.mark.parametrize("token", ["reference_item:", "reference_name:", "rom_tables:"])
def test_guard_rejects_item_class_tokens_at_both_paths(tmp_path, guard, directory, token):
    relative = f"{directory}/battle_item_classes.yaml"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(f"{token} invented\n")
    assert guard.violations(tmp_path) == (f"{relative} contains {token}",)
    path.write_text("classes: []\n")
    assert guard.violations(tmp_path) == ()


@pytest.mark.parametrize("shape", [
    "list", "tuple", "set", "frozenset", "list_call", "tuple_call", "set_call",
    "mapping", "nested",
])
def test_guard_rejects_large_numeric_literal_tables(tmp_path, guard, shape):
    # Distinct invented IDs, deliberately unrelated to any game's ID space.
    values = list(range(4000, 4064))
    payloads = {
        "list": repr(values),
        "tuple": repr(tuple(values)),
        "set": "{" + ", ".join(map(str, values)) + "}",
        "frozenset": f"frozenset({values!r})",
        "list_call": f"list({tuple(values)!r})",
        "tuple_call": f"tuple({values!r})",
        "set_call": f"set({values!r})",
        "mapping": repr(dict(enumerate(values))),
        "nested": "{" + ", ".join(
            f"{row}: frozenset({values[row * 8:(row + 1) * 8]!r})" for row in range(8)
        ) + "}",
    }
    # A neutral name and annotation ensure neither path nor naming is the detector.
    source = (
        "raise RuntimeError('inspection must not execute source')\n"
        f"rows: object = {payloads[shape]}\n"
    )
    (tmp_path / "innocent.py").write_text(source)
    assert any(
        message.startswith("innocent.py:2 contains a large literal integer-ID table")
        for message in guard.violations(tmp_path)
    )


def test_guard_finds_nested_table_inside_function_without_executing_it(tmp_path, guard):
    values = list(range(5000, 5064))
    source = f"def unused():\n    return {{91: ({values[:32]!r}), 92: ({values[32:]!r})}}\n"
    (tmp_path / "nested.py").write_text(source)
    assert guard.violations(tmp_path) == (
        "nested.py:2 contains a large literal integer-ID table",
    )


@pytest.mark.parametrize("payload", [
    repr(list(range(4000, 4063))),  # Just below the distinct-ID boundary.
    repr([False, True] * 64),
    repr([7] * 128),  # Size alone must not reject a repeated layout.
    repr(list(itertools.permutations(range(4)))),  # Authored mathematical layout.
    repr({f"FIELD_{i}": i * 4 for i in range(80)}),  # Named offsets.
    repr({i: (f"op_{i}", i % 8) for i in range(80)}),  # Protocol operand layout.
    repr({i: chr(0x100 + i) for i in range(80)}),  # Text decoder.
    repr({i: i % 4 for i in range(80)}),  # Keys are indexes, not payload IDs.
    "[4000 + index for index in range(80)]",  # Computed data needs review.
])
def test_guard_allows_small_tables_and_protocol_layout_facts(tmp_path, guard, payload):
    (tmp_path / "formats.py").write_text(f"LAYOUT = {payload}\n")
    assert guard.violations(tmp_path) == ()


def test_guard_does_not_treat_comments_or_docstrings_as_tables(tmp_path, guard):
    table = repr(list(range(4000, 4064)))
    (tmp_path / "prose.py").write_text(f'"""Example notation: {table}"""\n# {table}\n')
    assert guard.violations(tmp_path) == ()


def test_guard_reports_unparseable_python_without_echoing_source(tmp_path, guard):
    (tmp_path / "broken.py").write_text("rows = [\n")
    assert guard.violations(tmp_path) == (
        "broken.py:1 cannot parse Python for data inspection",
    )
