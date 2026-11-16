#!/usr/bin/env python3
"""Fail CI when distributable ROM-derived data is checked into the repository."""

from __future__ import annotations

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

    item_classes = root / "references" / "battle_item_classes.yaml"
    if item_classes.is_file():
        text = item_classes.read_text(encoding="utf-8")
        for token in ("reference_item:", "reference_name:", "rom_tables:"):
            if token in text:
                bad.append(f"references/battle_item_classes.yaml contains {token}")
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
