"""FireRed wild encounter data generated privately from the user's verified ROM."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class Encounter:
    map: str
    method: str
    species: int
    probability: float
    min_level: int
    max_level: int


@lru_cache(maxsize=1)
def load_wild_table(path: str | Path | None = None) -> tuple[Encounter, ...]:
    if path is None:
        from pokegent.games.gen3.firered.rom.rom_cache import ensure

        source = ensure(None) / "wild_encounters.json"
    else:
        source = Path(path)
    raw = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"generated wild encounter table is not a list: {source}")
    return tuple(Encounter(**row) for row in raw)


def by_species(table: tuple[Encounter, ...]) -> dict[int, tuple[Encounter, ...]]:
    result: dict[int, list[Encounter]] = {}
    for row in table:
        result.setdefault(row.species, []).append(row)
    return {species: tuple(rows) for species, rows in result.items()}
