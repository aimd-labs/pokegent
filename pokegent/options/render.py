"""Render the consequence facts of an option as concise English for the model (contract v1.2).

Why this exists: ``to_system_one_payload`` hands System One only ``criteria {option_id: text}``.
The facts that carry a decision (damage range, KO chance, who moves first, what a potion restores,
how far a walk is) live in ``Option.facts`` and were invisible to the model. ``criterion_text``
appends the output of :func:`render_facts` so that every family gets the same, deterministic
wording without inventing its own.

Rules
* Whitelist, not dump: only keys with a registered renderer are shown. Internal values (ids, slots,
  addresses, argument names) never reach the text, and the payload leak guard still runs after.
* Facts, not advice: the wording states consequences ("may KO", "moves second"), never "best".
* Deterministic: renderers run in a fixed order; the same facts give the same text, bytes for bytes.
* Defensive: a fact of an unexpected shape is skipped (counted in :data:`STATS`), never an
  exception;
  the contract tests pin the real shapes the families produce.
* Bounded: the text is cut at a phrase boundary at ``max_chars``; the most decision-relevant phrases
  come first, so the cut drops the least relevant ones.

Families may add renderers for their own keys with :func:`register_fact_renderer`.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from pokegent.actions._exec.contracts import MenuEntry, MenuModel

#: renderers: name -> function(facts) -> phrases. Order = priority (first = most relevant).
FactRenderer = Callable[[Mapping[str, Any]], Sequence[str]]

DEFAULT_MAX_CHARS = 600

#: diagnostics only (never part of a decision): number of renderer failures on odd shapes
STATS: dict[str, int] = {"renderer_errors": 0}

_RENDERERS: list[tuple[str, FactRenderer]] = []


def route_blocker_label(value: Any) -> str:
    """Turn a route requirement into text suitable for the model-facing payload."""
    token = str(value)
    parts = token.split(":")
    if len(parts) >= 3 and parts[0] == "boulder_plan":
        return {
            "switch": "a Strength boulder puzzle (switch not yet pressed)",
            "hole": "a Strength boulder puzzle (hole not yet cleared)",
            "warp": "a Strength boulder puzzle (exit not yet cleared)",
            "edge": "a Strength boulder puzzle (exit not yet cleared)",
        }.get(parts[2], "a Strength boulder puzzle")
    return {
        "surf": "Surf required",
        "cut": "Cut required",
        "strength": "Strength required",
    }.get(token.casefold(), token if ":" not in token else "a gated route requirement")


def route_blockers(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    return [route_blocker_label(value) for value in values]


def _field_option_kind(facts: Mapping[str, Any]) -> str | None:
    target = facts.get("target")
    allowed = {
        "edge",
        "warp",
        "sign",
        "npc",
        "item",
        "obstacle",
        "grass",
        "tile",
        "surf",
        "landing",
        "fly",
        "elevator",
        "gate",
    }
    if isinstance(target, Mapping) and target.get("kind") in allowed:
        return str(target["kind"])
    if facts.get("prompt_kind") == "field_move":
        return "field_move"
    if facts.get("mode") in ("on_foot", "biking") and "item_id" in facts:
        return "bike"
    return None


def _field_prerequisites(facts: Mapping[str, Any]) -> str:
    route_needs = facts.get("route_needs")
    if (
        facts.get("route_relation") == "unreachable"
        and isinstance(route_needs, (list, tuple))
        and route_needs
    ):
        return "unmet (needs " + " and ".join(route_blockers(route_needs)) + ")"
    capability = facts.get("capability")
    if capability == "ready":
        return "met"
    if capability == "missing":
        requirement = facts.get("requirement")
        if isinstance(requirement, Mapping) and requirement.get("kind") == "key_item":
            name = requirement.get("name")
            if isinstance(name, str) and name:
                return f"unmet (needs {name} in the Bag)"
        needs = facts.get("needs")
        if isinstance(needs, Mapping):
            move, badge = needs.get("move"), needs.get("badge")
            requirements = [str(x) for x in (move, badge) if isinstance(x, str) and x]
            if requirements:
                return "unmet (" + " and ".join(requirements) + ")"
        return "unmet"
    if capability == "unknown" or facts.get("reason") == "capability_unknown":
        return "unknown"
    return "met"


def _encounter_phrase(value: Any) -> str | None:
    if not isinstance(value, (list, tuple)):
        return None
    rows = [row for row in value if isinstance(row, Mapping)]
    labels = []
    for row in rows[:8]:
        name = row.get("name", "unknown")
        levels = row.get("levels")
        level_text = (
            f" Lv {levels[0]}-{levels[1]}"
            if isinstance(levels, (list, tuple)) and len(levels) == 2
            else ""
        )
        chance = row.get("chance")
        rate_text = (
            f" {round(float(chance) * 100):.0f}%" if isinstance(chance, (int, float)) else ""
        )
        status = (
            "owned"
            if row.get("owned") is True
            else "new"
            if row.get("new") is True
            else "seen"
            if row.get("seen") is True
            else "unseen"
            if row.get("unseen") is True
            else "Pokedex unknown"
        )
        labels.append(f"{name}{level_text}{rate_text} ({status})")
    if not labels:
        return None
    extra = f" +{len(rows) - len(labels)} more" if len(rows) > len(labels) else ""
    return "wild encounters: " + ", ".join(labels) + extra


def route_objective_slot(facts: Mapping[str, Any]) -> str | None:
    """The ``Objective:`` slot for an option navigation annotated, else ``None``.

    Options outside the seven-slot field contract (boulder plans, HM teaching, ...) still
    carry navigation's route facts; this keeps the agent-facing text from dropping them.
    """
    if facts.get("route_relation") is None and facts.get("next_route_step") is not True:
        return None
    return _field_objective(facts)


def _field_objective(facts: Mapping[str, Any]) -> str:
    if facts.get("objective_unknown") is True:
        return "unknown"
    if facts.get("objective_no_route") is True:
        return "active but no route: no target map was given"
    relation = facts.get("route_relation")
    if relation in ("next route step", "on route", "off route"):
        return str(relation)
    if relation == "unreachable":
        route_needs = facts.get("route_needs")
        if isinstance(route_needs, (list, tuple)) and route_needs:
            return "unreachable: needs " + " and ".join(route_blockers(route_needs))
        return "unreachable"
    progression_relation = facts.get("progression_relation")
    if facts.get("next_route_step") is True:
        objective = "next route step"
    elif facts.get("on_route") is True:
        objective = "on route"
    elif facts.get("serves_goal") is True:
        objective = "serves"
    elif facts.get("serves_goal") is False:
        objective = "does not serve"
    else:
        objective = "no objective"
    if isinstance(progression_relation, str) and progression_relation:
        return f"{objective}; Progression: {progression_relation}"
    return objective


def _field_effect(kind: str, facts: Mapping[str, Any], label: str, effect: str | None) -> str:
    if kind == "field_move":
        move = facts.get("field_move") or "the field move"
        action = "use" if effect is None or effect.startswith("Uses") else "do not use"
        return f"{action} {str(move).upper()}"
    if kind == "bike":
        return "ride the Bicycle" if facts.get("mode") == "on_foot" else "dismount the Bicycle"
    return {
        "warp": "travel through the warp",
        "edge": "travel to the connected map",
        "sign": "read the sign",
        "npc": "talk to the person",
        "item": "pick up the item",
        "obstacle": "clear the obstacle",
        "grass": "walk through tall grass",
        "tile": "walk to the tile",
        "surf": "surf onto the water",
        "landing": "step ashore",
        "fly": "fly to the destination",
        "elevator": "choose the elevator floor",
        "gate": "pass through the gate",
    }.get(kind, label.rstrip("."))


def render_field_criterion(
    *,
    label: str,
    facts: Mapping[str, Any],
    effect_description: str | None = None,
    irreversible: bool = False,
    repetition: str | None = None,
) -> str | None:
    """Render the fixed seven-slot contract used by field options and field-move prompts."""
    kind = _field_option_kind(facts)
    if kind is None:
        return None
    steps = _num(facts.get("steps"))
    travel = "unknown" if steps is None else f"{_fmt(steps)} travel step{'s' if steps != 1 else ''}"
    no_effect_streak = _num(facts.get("nothing_changed_streak"))
    if steps == 0 and no_effect_streak:
        travel += f" (but nothing changed last {_fmt(no_effect_streak)} times)"
    if kind == "field_move":
        action_cost = "one dialog choice"
    elif kind == "bike":
        action_cost = "one menu action"
    else:
        action_cost = "one field action"
    talked_before = facts.get("talked_before") is True or (
        kind == "npc" and isinstance(repetition, str) and "nothing changed" in repetition
    )
    if talked_before:
        novelty = "nothing new (already talked)"
    elif facts.get("already_read") is True or facts.get("no_new_information") is True:
        novelty = "already read/completed"
    elif kind == "sign":
        novelty = "new information expected"
    elif kind == "npc":
        novelty = "unknown"
    else:
        novelty = "n/a"
    outcome = _field_effect(kind, facts, label, effect_description)
    details: list[str] = []
    destination = facts.get("destination")
    if isinstance(destination, str) and destination:
        details.append(f"leads to {destination}")
    elif kind == "obstacle":
        unlocks = facts.get("unlocks")
        if isinstance(unlocks, str) and unlocks:
            details.append(f"opens {unlocks}")
    if facts.get("edge_bounced"):
        details.append(
            f"crossed here {_fmt(_num(facts['edge_bounced']) or 0)} times without getting closer"
        )
    if facts.get("bounce_count"):
        details.append(
            f"tried this person {_fmt(_num(facts['bounce_count']) or 0)} times without progress"
        )
    route_needs = facts.get("route_needs")
    if isinstance(route_needs, (list, tuple)) and route_needs:
        details.append("route blocked by " + " and ".join(route_blockers(route_needs)))
    gap_text = _r_field_move_gap(facts)
    if gap_text:
        details.extend(gap_text)
    if facts.get("blocked_by_npc") is True:
        details.append("blocked by a person in the way")
    if facts.get("script_tiles"):
        details.append("crosses a scripted event tile")
    if facts.get("declinable") is True:
        decline = facts.get("decline_outcome")
        details.append(
            "a YES/NO prompt can be declined"
            + (f" ({decline})" if isinstance(decline, str) and decline else "")
        )
    if facts.get("trigger_untested") is True:
        details.append("trigger is untested")
    if facts.get("already_visited") is True:
        novelty = "already visited"
    encounter_text = _encounter_phrase(facts.get("encounters"))
    if encounter_text:
        details.append(encounter_text)
    risk = (
        "wild"
        if kind == "grass" or _num(facts.get("grass_tiles")) or _num(facts.get("encounter_tiles"))
        else (
            "forced trainer battles if accepted"
            if facts.get("forced_battle") is True
            else ("trainer" if facts.get("trainer") is True else "none")
        )
    )
    slots = [
        f"Objective: {_field_objective(facts)}",
    ]
    if repetition:
        slots.append(repetition.rstrip(". "))
    slots.extend(
        [
            f"Cost: {travel}; action cost: {action_cost}",
            f"Prerequisites: {_field_prerequisites(facts)}",
            f"Novelty: {novelty}",
            "Outcome: " + "; ".join([outcome, *details]),
            f"Risk: {risk}; reversible "
            f"{'no' if irreversible or facts.get('irreversible') is True else 'yes'}",
        ]
    )
    return f"{label}: " + "; ".join(slots) + "."


def render_item_criterion(
    *,
    label: str,
    facts: Mapping[str, Any],
    irreversible: bool = False,
    repetition: str | None = None,
) -> str | None:
    """Render generic item options through the fixed seven-slot vocabulary."""
    if facts.get("domain") != "item":
        return None
    effects = _str_list(facts.get("effect_classes"))
    objective = ", ".join(effects) if effects else "use item"
    if facts.get("next_route_step") is True:
        # Navigation marked this item (e.g. teaching a needed HM) as the next route step.
        objective = f"next route step ({objective})"
    target = facts.get("target_name")
    if isinstance(facts.get("move_name"), str):
        target = f"{target or 'the Pokemon'} / {facts['move_name']}"
    cost = facts.get("cost") if isinstance(facts.get("cost"), str) else "unknown"
    prerequisites = facts.get("prerequisites")
    outcome = facts.get("outcome")
    slots = [f"Objective: {objective}"]
    if repetition:
        slots.append(repetition.rstrip(". "))
    slots.extend(
        [
            f"Target: {target or 'none'}",
            f"Cost: {cost}; {facts.get('quantity', '?')} in the bag",
            f"Prerequisites: {prerequisites or 'unknown'}",
            "Novelty: n/a",
            f"Outcome: {outcome or 'unknown'}",
            "Risk: none; reversible "
            f"{'no' if irreversible or facts.get('irreversible') is True else 'yes'}",
        ]
    )
    return f"{label}: " + "; ".join(slots) + "."


def render_adapter_choice(
    *,
    label: str,
    facts: Mapping[str, Any],
    repetition: str | None = None,
) -> str | None:
    """Render a live menu choice with the same seven slots System One and agent receive."""
    if facts.get("menu_choice") is not True:
        return None
    objective = facts.get("menu_objective")
    cost = facts.get("menu_cost")
    prerequisites = facts.get("menu_prerequisites")
    novelty = facts.get("menu_novelty")
    outcome = facts.get("menu_outcome")
    risk = facts.get("menu_risk")
    reversible = facts.get("menu_reversible") is not False
    slots = [f"Objective: {objective if isinstance(objective, str) else 'not stated'}"]
    slots.append(repetition.rstrip(". ") if repetition else "Repetition: not chosen here yet")
    slots.extend(
        (
            f"Cost: {cost if isinstance(cost, str) else 'one menu action'}",
            "Prerequisites: " + (prerequisites if isinstance(prerequisites, str) else "enabled"),
            f"Novelty: {novelty if isinstance(novelty, str) else 'n/a'}",
            f"Outcome: {outcome if isinstance(outcome, str) else 'select this entry'}",
            f"Risk: {risk if isinstance(risk, str) else 'none'}; reversible "
            f"{'yes' if reversible else 'no'}",
        )
    )
    return f"{label}: " + "; ".join(slots) + "."


def render_adapter_criterion(model: "MenuModel", entry: "MenuEntry") -> str:
    """Render a generic adapter entry through the same seven-slot criterion vocabulary.

    Adapter models are not registered in PR1.  This hook is intentionally a pure bridge so a
    later adapter can use the established renderer without changing option text or family code.
    """
    del model
    rendered = render_adapter_choice(
        label=entry.label,
        facts={
            "menu_choice": True,
            "menu_objective": "adapter choice",
            "menu_cost": "one menu action",
            "menu_prerequisites": "enabled" if entry.enabled else "disabled",
            "menu_novelty": "n/a",
            "menu_outcome": entry.effect or "select this entry",
            "menu_risk": "none",
            "menu_reversible": True,
        },
    )
    assert rendered is not None
    return rendered


def register_fact_renderer(
    name: str, fn: FactRenderer, *, before: str | None = None, replace: bool = False
) -> None:
    """Add a renderer (at the end, or before the renderer named ``before``)."""
    names = [n for n, _ in _RENDERERS]
    if name in names:
        if not replace:
            raise ValueError(f"fact renderer {name!r} is already registered")
        _RENDERERS[names.index(name)] = (name, fn)
        return
    if before is None:
        _RENDERERS.append((name, fn))
        return
    if before not in names:
        raise ValueError(f"unknown renderer {before!r}")
    _RENDERERS.insert(names.index(before), (name, fn))


def renderer_names() -> tuple[str, ...]:
    return tuple(n for n, _ in _RENDERERS)


# -- small helpers -------------------------------------------------------------------------------


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def _pair(v: Any) -> tuple[float, float] | None:
    """``[lo, hi]`` (or a single number) as an ordered pair."""
    if isinstance(v, (list, tuple)) and len(v) == 2:
        lo, hi = _num(v[0]), _num(v[1])
        if lo is not None and hi is not None:
            return (min(lo, hi), max(lo, hi))
        return None
    n = _num(v)
    return (n, n) if n is not None else None


def _fmt(n: float) -> str:
    return str(int(round(n))) if abs(n - round(n)) < 1e-9 else f"{n:.2f}".rstrip("0").rstrip(".")


def _range(v: Any, unit: str = "") -> str | None:
    p = _pair(v)
    if p is None:
        return None
    lo, hi = p
    return f"{_fmt(lo)}{unit}" if lo == hi else f"{_fmt(lo)}-{_fmt(hi)}{unit}"


def _word(v: Any) -> str:
    return str(v).replace("_", " ").strip()


def _str_list(v: Any) -> list[str]:
    if isinstance(v, str):
        return [_word(v)] if v.strip() else []
    if isinstance(v, (list, tuple)):
        return [_word(x) for x in v if isinstance(x, str) and x.strip()]
    return []


# -- battle: moves -------------------------------------------------------------------------------


def _r_move_effect(f: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    if f.get("no_effect") is True:
        reason = f.get("no_effect_reason")
        out.append(
            "no effect" + (f" ({_word(reason)})" if isinstance(reason, str) and reason else "")
        )
        return out
    eff = _num(f.get("effectiveness"))
    if eff is not None and eff != 1.0:
        if eff == 0:
            out.append("no effect (immune)")
        elif eff > 1:
            out.append(f"super effective (x{_fmt(eff)})")
        else:
            out.append(f"not very effective (x{_fmt(eff)})")
    if f.get("stab") is True:
        out.append("same-type attack bonus")
    return out


def _r_heal_risk(f: Mapping[str, Any]) -> list[str]:
    warning = f.get("heal_faint_warning")
    risk = f.get("heal_risk")
    out = [str(warning)] if isinstance(warning, str) and warning else []
    if isinstance(risk, str) and risk:
        out.append(risk)
    return out


def _r_move_damage(f: Mapping[str, Any]) -> list[str]:
    if f.get("no_effect") is True:
        return []
    return _r_foe_threat(f)


def move_verdict(f: Mapping[str, Any]) -> str | None:
    """One concise, model-first consequence line for a battle move."""
    if f.get("no_effect") is True:
        return "No effect."
    if f.get("category") == "status" or f.get("no_damage") is True:
        if f.get("status_value") == "none":
            return "No damage; no further effect."
        if f.get("status_value") == "realized":
            return "No damage; its useful effect is already realised."
        value = f.get("multi_turn_value")
        return (
            f"No damage this turn; {value}."
            if isinstance(value, str) and value
            else "No damage; does not help win this turn."
        )
    damage = _range(f.get("estimated_damage_pct_of_hp"), "%")
    if not damage:
        return None
    ko = f.get("can_ko") in ("yes", "likely")
    chance = _range(f.get("ko_chance_pct"), "%")
    if ko and chance and chance != "0%":
        hits = _range(f.get("hits_to_ko"))
        suffix = f"; needs {hits} hit{'s' if hits != '1' else ''}" if hits else ""
        return f"Knocks the foe out this turn (about {chance}){suffix}."
    hits = _range(f.get("hits_to_ko"))
    if hits:
        return f"Does {damage} damage; needs {hits} hit{'s' if hits != '1' else ''}."
    return f"Does {damage} damage."


def _r_speed(f: Mapping[str, Any]) -> list[str]:
    if isinstance(f.get("race"), Mapping):
        return []  # race already renders who strikes first
    v = f.get("faster")
    if v is True or v == "yes":
        return ["acts first"]
    if v is False or v == "no":
        return ["acts second"]
    if v == "unknown":
        return ["speed tie risk"]
    return []


def _r_priority(f: Mapping[str, Any]) -> list[str]:
    p = _num(f.get("priority"))
    if p is not None and p != 0:
        return [f"priority {'+' if p > 0 else ''}{_fmt(p)}"]
    return []


def _r_move_details(f: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    accuracy = _range(f.get("accuracy"), "%")
    if accuracy:
        out.append(f"accuracy after stages about {accuracy}")
    crit = _range(f.get("crit_damage"), " damage")
    if crit and f.get("no_effect") is not True:
        out.append(f"critical hit about {crit}")
    for key in ("ability_effects", "item_effects"):
        for effect in _str_list(f.get(key)):
            out.append(effect)
    notes = set(_str_list(f.get("notes")))
    if "charge_turn" in notes:
        out.append("needs a charge turn")
    if "must_recharge" in notes:
        out.append("must recharge next turn")
    if "crashes_on_miss" in notes:
        out.append("recoils if it misses")
    return out


def _r_foe_threat(f: Mapping[str, Any]) -> list[str]:
    threat = f.get("foe_threat")
    if not isinstance(threat, Mapping) or not isinstance(threat.get("name"), str):
        return []
    bits: list[str] = []
    dmg = _range(threat.get("damage_pct_of_hp"), "% of current HP")
    if dmg:
        bits.append(dmg)
    ko = _range(threat.get("ko_chance_pct"), "%")
    if ko and ko != "0%":
        bits.append(f"KO chance {ko}")
    if threat.get("can_ko") in ("yes", "likely"):
        bits.append("can KO")
    elif threat.get("can_ko") == "maybe":
        bits.append("may KO")
    if threat.get("faster") in ("yes", True):
        bits.append("moves first")
    elif threat.get("faster") in ("no", False):
        bits.append("moves second")
    priority = _num(threat.get("priority"))
    if priority is not None and priority:
        bits.append(f"priority {'+' if priority > 0 else ''}{_fmt(priority)}")
    source = threat.get("source")
    label = f"foe threat: {threat['name']}"
    if isinstance(source, str) and source:
        label += f" ({source})"
    return [label + (f" ({', '.join(bits)})" if bits else "")]


def _r_move_side_effects(f: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    value = f.get("multi_turn_value")
    if f.get("category") != "status" and isinstance(value, str) and value:
        out.append(value)
    inf = f.get("inflicts")
    if isinstance(inf, Mapping) and inf.get("status"):
        chance = _num(inf.get("chance_pct"))
        out.append(f"may cause {_word(inf['status'])}" + (f" ({_fmt(chance)}%)" if chance else ""))
    elif isinstance(inf, str) and inf:
        out.append(f"may cause {_word(inf)}")
    changes = f.get("stat_changes")
    if isinstance(changes, (list, tuple)):
        for c in changes[:3]:
            if not isinstance(c, Mapping):
                continue
            stages, stat = _num(c.get("stages")), c.get("stat")
            if stages is None or not isinstance(stat, str):
                continue
            who = "the foe's" if c.get("who") == "target" else "your"
            current = _num(c.get("current_stage"))
            if c.get("no_further_effect") is True:
                if current is not None:
                    out.append(
                        f"{who} {_word(stat)} is already at {_fmt(current)}; no further effect"
                    )
                else:
                    out.append(f"{who} {_word(stat)} cannot change further")
                continue
            if c.get("turn_order_already_decided") is True:
                out.append("you already act first; further Speed changes add no turn-order value")
                continue
            chance = _num(c.get("chance_pct"))
            out.append(
                f"{'raises' if stages > 0 else 'lowers'} {who} {_word(stat)} by {_fmt(abs(stages))}"
                + (f" ({_fmt(chance)}% chance)" if chance else "")
            )
    rec = f.get("recoil")
    if isinstance(rec, Mapping):
        pct = _num(rec.get("pct_of_damage_dealt", rec.get("pct")))
        if pct:
            out.append(f"recoil {_fmt(pct)}% of the damage dealt")
    elif _num(rec):
        out.append(f"recoil {_fmt(float(rec))}")
    if f.get("user_faints") is True:
        out.append("the user faints")
    crit = _range(f.get("crit_chance_pct"), "%")
    if (
        crit
        and crit != "0%"
        and f.get("no_effect") is not True
        and f.get("estimated_damage_pct_of_hp")
    ):
        out.append(f"{crit} critical-hit chance")
    return out


def _r_restrictions(f: Mapping[str, Any]) -> list[str]:
    r = _str_list(f.get("restrictions"))
    return [f"restricted: {', '.join(r)}"] if r else []


# -- battle: switches, items, balls --------------------------------------------------------------


def _r_battle_progress(f: Mapping[str, Any]) -> list[str]:
    value = f.get("battle_progress")
    return [value] if isinstance(value, str) and value else []


def _r_switch(f: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    types = _str_list(f.get("types"))
    if types and "species" in f:
        out.append(f"{'/'.join(types)} type")
    if f.get("foe_gets_free_attack") is True:
        out.append("the foe gets a free attack while you switch")
    return out


def _r_switch_offer(f: Mapping[str, Any]) -> list[str]:
    incoming = f.get("incoming_foe")
    out = [f"incoming foe: {incoming}"] if isinstance(incoming, str) and incoming else []
    candidates = f.get("switch_candidates")
    if isinstance(candidates, (list, tuple)):
        for candidate in candidates[:2]:
            if not isinstance(candidate, Mapping):
                continue
            name = candidate.get("species") or candidate.get("name")
            if not isinstance(name, str):
                continue
            race = candidate.get("race")
            win = race.get("p_win_pct") if isinstance(race, Mapping) else None
            suffix = f", wins about {win}%" if isinstance(win, int) else ""
            out.append(f"switch-in {name}{suffix}")
    return out


_TURN_ITEM_KINDS = (
    "heal_hp", "revive", "cure_status", "wake_party", "restore_pp", "battle_boost", "escape",
)  # fmt: skip


def _r_item(f: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    if f.get("would_have_effect") is False:
        why = f.get("reason") or f.get("no_effect_reason")
        none = "would have no effect"
        out.append(none + (f" ({_word(why)})" if isinstance(why, str) and why else ""))
        if f.get("turn_still_used") is True:  # the game accepts it: the turn is gone for nothing
            out.append("the turn is still used")
        return out
    restored, after = _num(f.get("hp_restored")), _num(f.get("hp_after"))
    if restored:
        text = f"restores {_fmt(restored)} HP"
        pct = _num(f.get("hp_pct_after"))
        if after is not None:
            text += f" (to {_fmt(after)}" + (f", {_fmt(pct)}%)" if pct is not None else ")")
        out.append(text)
    cures = _str_list(f.get("cures"))
    if cures:
        out.append(f"cures {', '.join(cures)}")
    if f.get("kind") == "revive":
        out.append("revives a fainted Pokemon")
    wakes = _str_list(f.get("wakes"))
    if wakes:
        out.append(f"wakes up {' and '.join(wakes)}")
    pp, scope = f.get("restores_pp"), f.get("restores_pp_scope")
    if pp is not None and f.get("kind") == "restore_pp":
        amount = "all" if pp == "full" else _fmt(_num(pp) or 0)
        out.append(
            f"restores {amount} PP of {'every move' if scope == 'all_moves' else 'one move'}"
        )
    crit = _num(f.get("raises_crit_stage"))
    if crit:
        out.append(f"raises the critical-hit stage by {_fmt(crit)}")
    guard = _num(f.get("protects_stats_turns"))
    if guard:
        out.append(f"protects your stats from being lowered for {_fmt(guard)} turns")
    if f.get("kind") == "escape" and f.get("would_have_effect") is True:
        out.append("escapes from the wild battle for sure")
    if f.get("uses_turn") is True and f.get("kind") in _TURN_ITEM_KINDS:
        out.append("uses the turn")
    return out


def _r_ball(f: Mapping[str, Any]) -> list[str]:
    if f.get("catchable") is False:
        return ["cannot be caught"]
    chance = _range(f.get("catch_chance_pct"), "%")
    return [f"catch chance about {chance}"] if chance else []


# -- field ---------------------------------------------------------------------------------------


def _r_prompt(f: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    relation = f.get("objective_relation")
    if relation == "does_not_serve_current_objective":
        out.append("does not serve the current objective")
    elif f.get("serves_goal") is True:
        out.append("serves the current objective")
    elif f.get("serves_goal") is False:
        out.append("does not serve the current objective")
    cost = f.get("wrong_answer_cost")
    if isinstance(cost, str) and cost:
        out.append(f"a wrong answer costs a {cost}")
    if f.get("other_fossil_gone") is True:
        out.append("the other fossil is gone after this choice")
    return out


def _r_field(f: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    obstacle = f.get("obstacle")
    obstacle_names = {
        "cut_tree": "the tree",
        "rock_smash": "the rock",
        "boulder": "the boulder",
        "snorlax": "the sleeping Snorlax",
        "fossil": "this fossil",
    }
    if obstacle in obstacle_names:
        requirement = f.get("requirement")
        if isinstance(requirement, Mapping) and requirement.get("kind") == "key_item":
            name = requirement.get("name")
            if isinstance(name, str) and name:
                out.append(f"needs {name} in the Bag")
        needs = f.get("needs")
        if isinstance(needs, Mapping):
            move = needs.get("move")
            badge = needs.get("badge")
            if isinstance(move, str) and isinstance(badge, str):
                out.append(f"needs a Pokemon that knows {move} and the {badge} Badge")
            elif isinstance(move, str):
                out.append(f"needs a Pokemon that knows {move}")
        unlocks = f.get("unlocks")
        if isinstance(unlocks, str) and unlocks:
            out.append(f"opens the area on {unlocks}")
        if obstacle == "fossil" or f.get("irreversible") is True:
            if obstacle == "fossil":
                out.append("the other fossil is gone after this choice")
            elif f.get("consequence") == "wakes a battle":
                out.append("irreversible: wakes a battle")
    patch_size = _num(f.get("patch_size"))
    if patch_size is not None:
        out.append(f"grass patch has {_fmt(patch_size)} tiles")
    bound = _num(f.get("bound_steps"))
    if bound is not None:
        out.append(f"walks between two grass tiles for at most {_fmt(bound)} steps")
    encounter_table = f.get("encounter_table")
    if isinstance(encounter_table, (list, tuple)) and encounter_table:
        names = [str(name) for name in encounter_table if isinstance(name, str)]
        if names:
            out.append("encounters include " + ", ".join(names))
    encounter_text = _encounter_phrase(f.get("encounters"))
    if encounter_text:
        out.append(encounter_text)
    steps = _num(f.get("steps"))
    if steps is not None:
        out.append(f"{_fmt(steps)} step{'s' if steps != 1 else ''} away")
    dest = f.get("destination")
    if isinstance(dest, str) and dest:
        out.append(f"leads to {dest}")
    elif isinstance(dest, Mapping) and dest.get("name"):
        out.append(f"leads to {dest['name']}")
    if f.get("serves_goal") is True and not f.get("prompt_kind"):
        out.append("serves the current objective")
    grass = _num(f.get("grass_tiles", f.get("grass")))
    if grass:
        out.append(
            f"crosses {_fmt(grass)} tall-grass tile{'s' if grass != 1 else ''} "
            "(wild encounters possible)"
        )
    encounter_tiles = _num(f.get("encounter_tiles"))
    if encounter_tiles and (not grass or encounter_tiles > grass):
        out.append(
            f"crosses {_fmt(encounter_tiles)} encounter-risk tile"
            f"{'s' if encounter_tiles != 1 else ''} (wild encounters possible)"
        )
    if f.get("blocked_by_npc") is True:
        out.append("currently blocked by a person in the way")
    ledges = _num(f.get("ledges"))
    if ledges:
        out.append("includes a one-way ledge jump")
    elif f.get("one_way") is True:
        out.append("one-way")
    if f.get("already_visited") is True:
        out.append("already visited")
    if f.get("declined_before") is True:
        out.append("declined before for this objective on this map")
    forced_tiles = _num(f.get("forced_tiles"))
    if forced_tiles:
        out.append("crosses forced-movement tiles and lands at the predicted exit")
    fail = f.get("last_failure")
    if isinstance(fail, str) and fail:
        out.append(f"the last attempt failed: {_word(fail)}")
    return out


def _r_field_move_gap(f: Mapping[str, Any]) -> list[str]:
    """Render the adapter's narrow field-move gap from fixed templates only."""
    gap = f.get("field_move_gap")
    if not isinstance(gap, Mapping):
        return []
    move, badge = gap.get("move"), gap.get("badge")
    if not isinstance(move, str) or not isinstance(badge, str):
        return []
    move, badge = _word(move)[:40], _word(badge)[:40]
    carrier = gap.get("carrier_missing") is True
    missing_badge = gap.get("badge_missing") is True
    if carrier and missing_badge:
        return [f"no party member knows {move}; {badge} Badge not obtained"]
    if carrier:
        return [f"no party member knows {move}; {badge} Badge obtained"]
    if missing_badge:
        return [f"{badge} Badge not obtained; party has {move}"]
    return []


for _name, _fn in (
    ("heal_risk", _r_heal_risk),
    ("move_effect", _r_move_effect),
    ("move_damage", _r_move_damage),
    ("speed", _r_speed),
    ("priority", _r_priority),
    ("move_details", _r_move_details),
    ("move_side_effects", _r_move_side_effects),
    ("restrictions", _r_restrictions),
    ("field_move_gap", _r_field_move_gap),
    ("battle_progress", _r_battle_progress),
    ("switch_offer", _r_switch_offer),
    ("switch", _r_switch),
    ("item", _r_item),
    ("ball", _r_ball),
    ("field", _r_field),
    ("prompt", _r_prompt),
):
    register_fact_renderer(_name, _fn)


# -- entry point ---------------------------------------------------------------------------------


def render_facts(
    facts: Mapping[str, Any] | None,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    battle_move: bool = False,
) -> str:
    """Phrases joined by ``"; "`` (no trailing period), most relevant first.

    Output is cut at ``max_chars``.
    """
    if not facts:
        return ""
    phrases: list[str] = []
    for _name, fn in _RENDERERS:
        try:
            rendered = [p for p in fn(facts) if p]
            if battle_move and _name == "race" and rendered:
                # Move verdicts own win/KO conclusions; retain only turn order.
                rendered = [rendered[0].split("; ", 1)[0]]
            phrases.extend(rendered)
        except Exception:  # noqa: BLE001 - a bad shape must never stop a decision
            STATS["renderer_errors"] += 1
    out: list[str] = []
    used = 0
    for p in phrases:
        extra = len(p) + (2 if out else 0)
        if used + extra > max_chars:
            break
        out.append(p)
        used += extra
    return "; ".join(out)
