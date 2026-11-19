"""Opt-in ROM-backed fallback, adapted from the MIT pokevon_agent DexFallback.

Encounter data stays in the user's private ROM cache; no story or town tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pokegent.games.gen3.firered.rom.wild import Encounter, by_species
from pokegent.goals.validate import normalize_map_name


class FallbackUnavailable(ValueError):
    """Current facts cannot support a fallback objective safely."""


@dataclass
class DexFallback:
    table: tuple[Encounter, ...]

    def from_snapshot(self, snapshot) -> dict[str, Any]:
        if not snapshot.usable or snapshot.pokedex is None or not snapshot.party:
            raise FallbackUnavailable("fallback requires usable party and Pokedex RAM")
        party = [member for member in snapshot.party if not member.is_egg]
        # Fork capture eligibility is HP-only; it is not the stronger heal completion probe.
        healthy = bool(party) and all(member.max_hp > 0 and member.hp >= member.max_hp
                                      for member in party)
        balls = sum(item.quantity for pocket in snapshot.bag for item in pocket.items
                    if item.item_id in {2, 3, 4})
        current_map = snapshot.map.identity.name if snapshot.map is not None else None
        return self.source(set(snapshot.pokedex.owned), current_map=current_map,
                           party_healthy=healthy, ball_count=balls)

    def source(
        self, owned: set[int], *, current_map: str | None = None,
        party_healthy: bool = True, ball_count: int = 10,
    ) -> dict[str, Any]:
        choices = []
        for species, rows in by_species(self.table).items():
            if not 1 <= species <= 151 or species in owned:
                continue
            supported = [row for row in rows if row.method == "land"
                         and 0 < row.probability <= 1 and row.map]
            if not supported:
                continue
            row = min(supported, key=lambda item: (-item.probability, item.map, item.method))
            local = (current_map is not None
                     and normalize_map_name(row.map) == normalize_map_name(current_map))
            cost = (0 if local else 10) + 1 / row.probability
            choices.append((cost, species, row))
        if not choices:
            raise FallbackUnavailable("no unowned species with a supported land encounter")
        if not party_healthy:
            raise FallbackUnavailable("heal prerequisite unresolved; supply an explicit objective")
        if ball_count < 10:
            raise FallbackUnavailable(
                "restock prerequisite unresolved; supply an explicit objective")
        _, species, row = min(choices, key=lambda choice: choice[:2])
        return {
            "objective_id": f"dex_{species}", "kind": "train",
            "target": {"map": row.map},
            "success_probes": [{"kind": "dex_owned", "species": species}],
            "rationale": f"Pokedex: catch species {species} at {row.map} ({row.method}).",
            "set_by": "fallback:dex",
        }
