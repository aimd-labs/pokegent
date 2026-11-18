#!/usr/bin/env python3
"""Fail CI when distributable ROM-derived data is checked into the repository."""

from __future__ import annotations

import ast
import sys
import unicodedata
from pathlib import Path, PurePosixPath

FORBIDDEN_PREFIXES = (
    "pokegent/games/gen3/firered/tables/",
    "references/prefork/knowledge/",
    "references/prefork/world/",
    "pokegent/dashboard/legacy_static/static/assets/pokemon/",
    "pokegent/dashboard/legacy_static/static/assets/items/",
    "pokegent/dashboard/legacy_static/static/assets/trainers/",
)
FORBIDDEN_BASENAMES = {
    "pret_map_groups.json",
    "fly_destinations.json",
    "field_surf.yaml",
    "prompt_rules.yaml",
    "elevators.yaml",
    "cinnabar_quiz.yaml",
    "puzzles.yaml",
    "gen3_tables.json",
    "runtime_tables.json",
    "wild_encounters.json",
    "boulders.yaml",
    "regions.json",
    "regions.PROVENANCE.txt",
}
ROM_EXTENSIONS = {".gba", ".gb", ".gbc", ".sav", ".srm", ".state", ".zip", ".7z"}
BINARY_ALLOWLIST: frozenset[str] = frozenset()
MIN_LITERAL_IDS = 64


def literal_ids(node: ast.AST) -> set[int] | None:
    """Read only integer literals, ID-indexed containers and literal collection wrappers.

    Never execute source. Symbolic keys, text, computed values and comprehensions are
    outside this deliberately narrow heuristic; they need ordinary content review.
    """
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return {node.value}
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        if isinstance(node.operand, ast.Constant) and type(node.operand.value) is int:
            value = node.operand.value
            return {-value if isinstance(node.op, ast.USub) else value}
    if isinstance(node, ast.Dict):
        if any(not isinstance(key, ast.Constant) or type(key.value) is not int
               for key in node.keys):
            return None
        children = node.values
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        children = node.elts
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"frozenset", "set", "list", "tuple"}
        and len(node.args) == 1
        and not node.keywords
    ):
        return literal_ids(node.args[0])
    else:
        return None
    result: set[int] = set()
    for child in children:
        values = literal_ids(child)
        if values is None:
            return None
        result.update(values)
    return result


def python_table_violations(text: str, relative: str) -> tuple[str, ...]:
    """Flag literal tables with at least 64 distinct integer payload IDs.

    This catches flat ID lists/mappings and nested ID-to-walkability sets regardless
    of filename or variable name. Dictionary keys do not inflate the ID count.
    Named protocol/layout facts and text decoders are not numeric-only ID tables;
    repeated small layout permutations also stay below the distinct-ID threshold.
    This is a review tripwire, not proof of a table's origin or a complete data scan.
    """
    try:
        tree = ast.parse(text, filename=relative)
    except SyntaxError as error:
        return (f"{relative}:{error.lineno} cannot parse Python for data inspection",)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Dict, ast.List, ast.Tuple, ast.Set, ast.Call)):
            values = literal_ids(node)
            if values is not None and len(values) >= MIN_LITERAL_IDS:
                bad.append(f"{relative}:{node.lineno} contains a large literal integer-ID table")
    return tuple(bad)


def tracked_files(root: Path) -> tuple[str, ...]:
    ignored_dirs = {
        ".git", ".agents", ".aws", ".codex", ".venv",
        "__pycache__", ".ruff_cache", ".pytest_cache",
    }
    return tuple(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not ignored_dirs.intersection(path.relative_to(root).parts)
    )


def violations(root: Path) -> tuple[str, ...]:
    bad: list[str] = []
    for relative in tracked_files(root):
        if not (root / relative).is_file():
            continue
        path = PurePosixPath(relative)
        if relative.startswith(FORBIDDEN_PREFIXES):
            bad.append(relative)
        elif path.suffix.lower() in ROM_EXTENSIONS or path.suffix.lower().startswith(".ss"):
            bad.append(relative)
        elif path.name in FORBIDDEN_BASENAMES:
            bad.append(relative)

        if relative not in BINARY_ALLOWLIST:
            try:
                text = (root / relative).read_text(encoding="utf-8")
                binary = any(
                    unicodedata.category(char) == "Cc" and char not in "\n\r\t\f"
                    for char in text
                )
            except UnicodeDecodeError:
                binary = True
            if binary:
                bad.append(f"{relative} is an unexpected binary")
            elif path.suffix.lower() == ".py":
                bad.extend(python_table_violations(text, relative))

    item_classes = root / "references" / "battle_item_classes.yaml"
    packaged_item_classes = root / "pokegent" / "references" / "battle_item_classes.yaml"
    for item_classes_path in (item_classes, packaged_item_classes):
        if item_classes_path.is_file():
            text = item_classes_path.read_text(encoding="utf-8")
            badge = f"{item_classes_path.relative_to(root)} contains {{token}}"
            for token in ("reference_item:", "reference_name:", "rom_tables:"):
                if token in text:
                    bad.append(badge.format(token=token))
    return tuple(sorted(set(bad)))


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    bad = violations(root)
    if bad:
        print("ROM-derived distributable files are forbidden:", file=sys.stderr)
        for path in bad:
            print(f"  {path}", file=sys.stderr)
        return 1
    print("ROM-derived data guard: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
