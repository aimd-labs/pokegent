"""Small, model-facing facts for deliberate wild catches.

The battle mechanics adapter remains the sole owner of damage and catch-rate math.  This module
only labels the active foe and adds catch tactics to those already computed facts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from pokegent.actions.battle.view import BattleView

_SPECIES_RE = re.compile(r"(?:dex_owned|species)[^0-9]{0,24}(\d+)", re.I)


def wanted_species(objective: Any) -> int | None:
    """Read the dex-owned species from either the future structured objective or its summary."""
    if isinstance(objective, Mapping):
        probes = objective.get("success_probes", ())
        for probe in probes:
            if isinstance(probe, Mapping) and probe.get("kind") == "dex_owned":
                value = probe.get("species")
                if isinstance(value, int) and value > 0:
                    return value
    if isinstance(objective, str):
        try:
            value = json.loads(objective)
        except (TypeError, ValueError):
            value = None
        if isinstance(value, Mapping):
            found = wanted_species(value)
            if found is not None:
                return found
        match = _SPECIES_RE.search(objective)
        if match:
            return int(match.group(1))
    return None


def snapshot_owned_species(snapshot: Any) -> frozenset[int] | None:
    """Return the owned national-dex numbers parsed by the FireRed snapshot reader."""
    pokedex = getattr(snapshot, "pokedex", None)
    value = getattr(pokedex, "owned", None) if pokedex is not None else None
    if value is None:
        return None
    try:
        return frozenset(int(x) for x in value if int(x) > 0)
    except (TypeError, ValueError):
        return None


def foe_facts(view: BattleView, snapshot: Any, objective: Any) -> dict[str, Any]:
    foe = view.opponent
    wanted = wanted_species(objective)
    owned = snapshot_owned_species(snapshot)
    dex = view.tables.national_dex(foe.species) if view.tables is not None else None
    new = None if owned is None or dex is None else dex not in owned
    state = (
        "wanted"
        if dex is not None and wanted == dex
        else "unknown"
        if new is None
        else "new"
        if new
        else "already owned"
    )
    facts: dict[str, Any] = {
        "wild_target": f"{foe.name} ({state})",
        "wanted_species": wanted,
        "foe_new_to_pokedex": new,
    }
    if dex is not None and wanted == dex:
        facts["wanted"] = True
        facts["wanted_text"] = "WANTED: this is the Pokemon the objective needs"
    elif wanted is not None:
        facts["wanted"] = None if dex is None else False
    return facts


def add_ball_facts(facts: dict[str, Any], all_balls: list[dict[str, Any]]) -> dict[str, Any]:
    """Add throw count and a factual sufficient-ball hint; never changes legality or ordering."""
    chance = facts.get("catch_chance_pct")
    if isinstance(chance, list) and chance and chance[0] is not None:
        p = max(float(chance[0]), 0.01) / 100
        facts["expected_balls"] = round(1 / p, 1)
    elif chance == [100, 100]:
        facts["expected_balls"] = 1.0
    quantity = facts.get("quantity")
    if isinstance(quantity, int):
        facts["balls_left"] = quantity
    return facts


def render(facts: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    if facts.get("wanted_text"):
        out.append(str(facts["wanted_text"]))
    elif facts.get("foe_new_to_pokedex") is True:
        out.append("NEW to the Pokedex")
    elif facts.get("foe_new_to_pokedex") is False:
        out.append("already owned in the Pokedex")
    if isinstance(facts.get("expected_balls"), (int, float)):
        out.append(f"expected balls {facts['expected_balls']}")
    if isinstance(facts.get("balls_left"), int):
        out.append(f"{facts['balls_left']} left")
    return out


def register_renderer() -> None:
    from pokegent.options.render import register_fact_renderer

    try:
        register_fact_renderer("catch", render, before="ball")
    except ValueError:
        pass


register_renderer()
