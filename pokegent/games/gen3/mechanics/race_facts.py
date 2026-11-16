"""Whitelisted battle-option facts for the exchange model.

This module is intentionally not imported by ``options.render``.  The battle-family owner can
register the renderer after this work lands; keeping the registration out of this branch avoids a
cross-owner change while fixing the facts contract here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pokegent.games.gen3 import mechanics_constants as K
from pokegent.games.gen3.firered.rom.rom_tables import GameTables
from pokegent.games.gen3.mechanics.matchup import Matchup
from pokegent.games.gen3.mechanics.views import BattlerView, FieldView, MoveSlotView


def _percent(value: float | None) -> int | None:
    return None if value is None else int(round(max(0.0, min(1.0, value)) * 100))


def race_facts(
    ours: BattlerView,
    foe: BattlerView,
    tables: GameTables,
    field: FieldView,
    *,
    option: str = "attack",
    move: int | MoveSlotView | None = None,
    switch_in: BattlerView | None = None,
    forced: bool = False,
    foe_model: str | None = None,
) -> dict[str, dict[str, int | float | bool | None]]:
    """Return the scalar-only ``{"race": ...}`` facts for one legal battle option.

    ``option`` is one of ``attack``, ``idle``, ``status`` or ``switch``.  The model is
    descriptive: this function never removes an option or tells the decider which fact is good.
    """
    model = foe_model or ("wild" if field.kind == K.KIND_WILD else "trainer")
    matchup = Matchup(ours, foe, tables, field, model)
    if option == "switch":
        if switch_in is None:
            raise ValueError("switch_in is required for a switch race")
        result = matchup.switch(switch_in, forced=forced)
    elif option == "status":
        if move is None:
            raise ValueError("move is required for a status race")
        result = matchup.status(move)
    elif option == "idle":
        result = matchup.idle()
    else:
        result = matchup.attack(move)
    values: dict[str, int | float | bool | None] = {
        "p_win_pct": _percent(result.p_win),
        "we_strike_first": result.first_probability >= 0.5,
        "foe_ko_chance_pct": _percent(result.p_foe_falls),
        "p_ko_us_now_pct": _percent(result.p_ko_us_now),
        "turns_to_win": result.my_turns,
        "turns_to_lose": result.foe_turns,
        "idle_turn": option in {"idle", "status"},
    }
    if option == "switch":
        values["switch_in_acts_pct"] = _percent(result.p_acts)
    return {"race": values}


def render_race(facts: Mapping[str, Any] | None) -> str | None:
    """Render only the whitelisted race scalars as factual plain English."""
    if not isinstance(facts, Mapping):
        return None
    race = facts.get("race", facts)
    if not isinstance(race, Mapping):
        return None
    phrases: list[str] = []
    first = race.get("we_strike_first")
    if first is True:
        phrases.append("you strike first")
    elif first is False:
        phrases.append("you strike second")
    win = race.get("p_win_pct")
    turns = race.get("turns_to_win")
    if isinstance(win, int) and isinstance(turns, (int, float)) and not isinstance(turns, bool):
        phrases.append(f"wins in about {turns:.1f} turns, about {win}% of the time")
    elif isinstance(win, int):
        phrases.append(f"wins about {win}% of the time")
    falls = race.get("p_ko_us_now_pct")
    if isinstance(falls, int) and falls:
        phrases.append(f"the foe can knock you out now about {falls}% of the time")
    acts = race.get("switch_in_acts_pct")
    if isinstance(acts, int):
        phrases.append(f"the switch-in acts about {acts}% of the time")
    return "; ".join(phrases) or None


def from_battle_view(
    view: Any,
    provider: Any,
    *,
    option: str = "attack",
    slot: int | None = None,
    switch_slot: int | None = None,
    forced: bool = False,
    foe_model: str | None = None,
) -> dict[str, dict[str, int | float | bool | None]]:
    """Build race facts at the existing battle-family seam.

    ``provider`` is the existing ``MechanicsFacts`` adapter.  The later integration should call
    this immediately after its ordinary option facts are built, passing ``option="attack"`` for
    ``move:<slot>``, ``option="switch"`` for ``switch:<slot>`` / ``send_out:<slot>``, and
    ``option="idle"`` for a stay option.  Hidden opponent technical fields are never read.
    """
    ours = provider.player_battler(view)
    foe = provider.opponent_battler(view)
    field = provider.field_view(view)
    move: int | MoveSlotView | None = None
    candidate: BattlerView | None = None
    if option in {"attack", "status"}:
        if slot is None:
            raise ValueError("slot is required for an attack or status race")
        move = ours.moves[slot]
    if option == "switch":
        if switch_slot is None:
            raise ValueError("switch_slot is required for a switch race")
        mon = view.mon(switch_slot)
        if mon is None:
            raise ValueError(f"unknown party slot {switch_slot}")
        candidate = provider.bench_battler(view, mon)
    return race_facts(
        ours,
        foe,
        provider.tables,
        field,
        option=option,
        move=move,
        switch_in=candidate,
        forced=forced,
        foe_model=foe_model,
    )


__all__ = ["from_battle_view", "race_facts", "render_race"]
