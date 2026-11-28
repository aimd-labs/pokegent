"""JSON-like consequence facts for options.

Every function returns a plain dict of JSON types (str, int, float, bool, list, None) that goes
into ``Option.facts``. Facts, never recommendations: strategically bad options stay legal and
carry facts such as ``no_effect: true`` (CONTEXT.md, docs/contracts.md section 5.1).

``estimated_damage_pct_of_hp`` is a percentage of the defender's CURRENT HP (capped at 100);
``estimated_damage_pct_of_max_hp`` is the same damage as a share of its maximum HP.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from pokegent.games.gen3 import mechanics_constants as K
from pokegent.games.gen3 import mechanics_ids as I
from pokegent.games.gen3.firered.rom.rom_tables import GameTables, ItemData
from pokegent.games.gen3.mechanics.legality import move_legality, switch_legality
from pokegent.games.gen3.mechanics.views import BattlerView, FieldView
from pokegent.games.gen3.ruleset.catch import ball_multiplier, catch_odds, catch_probability
from pokegent.games.gen3.ruleset.damage import DamageEstimate, estimate_damage
from pokegent.games.gen3.ruleset.effects import traits_for

_TARGET_NAMES = {
    K.MOVE_TARGET_SELECTED: "one_foe",
    K.MOVE_TARGET_DEPENDS: "depends",
    K.MOVE_TARGET_USER_OR_SELECTED: "user_or_one",
    K.MOVE_TARGET_RANDOM: "random_foe",
    K.MOVE_TARGET_BOTH: "both_foes",
    K.MOVE_TARGET_USER: "user",
    K.MOVE_TARGET_FOES_AND_ALLY: "all_others",
    K.MOVE_TARGET_OPPONENTS_FIELD: "foes_field",
}


def pretty(name: str) -> str:
    """ROM names are upper case ("WATER GUN", "POKé BALL"): present them as Title Case."""
    return name.title().replace("Poké", "Poké")


def _pct(share: float | None) -> int | None:
    return None if share is None else int(round(share * 100))


def _range(lo: float, hi: float, digits: int = 0) -> list[Any]:
    if digits == 0:
        return [int(round(lo)), int(round(hi))]
    return [round(lo, digits), round(hi, digits)]


def _can_ko(est: DamageEstimate) -> str:
    """``no`` / ``maybe`` / ``likely``: whether one landed hit (no crit) can take the target out."""
    if est.damage_model in ("unknown", "variable"):
        return "maybe"
    if est.no_effect or est.damage_model == "none":
        return "no"
    if est.defender_hp is None:
        return "maybe"
    lo, hi = est.ko_probability
    if hi <= 0.0 and not est.ohko_possible:
        return "no"
    if lo >= 0.5:
        return "yes"
    return "maybe"


def _ev(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _ability_effects(
    battler: BattlerView, move_type: int, tables: GameTables, *, defender: bool = False
) -> list[str]:
    """Names of ability effects that can change this move, without exposing ability ids."""
    if battler.ability is not None:
        ids = (battler.ability,)
        possible = False
    elif battler.species is not None:
        try:
            ids = tuple(dict.fromkeys(tables.species(battler.species).abilities))
        except (KeyError, IndexError):
            ids = ()
        possible = True
    else:
        ids = ()
        possible = False
    relevant = {
        I.ABILITY_LEVITATE,
        I.ABILITY_WONDER_GUARD,
        I.ABILITY_VOLT_ABSORB,
        I.ABILITY_WATER_ABSORB,
        I.ABILITY_FLASH_FIRE,
        I.ABILITY_THICK_FAT,
        I.ABILITY_STURDY,
        I.ABILITY_HUGE_POWER,
        I.ABILITY_PURE_POWER,
        I.ABILITY_GUTS,
        I.ABILITY_BLAZE,
        I.ABILITY_OVERGROW,
        I.ABILITY_TORRENT,
        I.ABILITY_SWARM,
        I.ABILITY_HUSTLE,
        I.ABILITY_MARVEL_SCALE,
        I.ABILITY_INTIMIDATE,
    }
    type_relevant = {
        I.ABILITY_LEVITATE: move_type == K.TYPE_GROUND,
        I.ABILITY_VOLT_ABSORB: move_type == K.TYPE_ELECTRIC,
        I.ABILITY_WATER_ABSORB: move_type == K.TYPE_WATER,
        I.ABILITY_FLASH_FIRE: move_type == K.TYPE_FIRE,
        I.ABILITY_THICK_FAT: move_type in (K.TYPE_FIRE, K.TYPE_ICE),
        I.ABILITY_BLAZE: move_type == K.TYPE_FIRE,
        I.ABILITY_OVERGROW: move_type == K.TYPE_GRASS,
        I.ABILITY_TORRENT: move_type == K.TYPE_WATER,
        I.ABILITY_SWARM: move_type == K.TYPE_BUG,
    }
    out: list[str] = []
    for ability in ids:
        if ability not in relevant or (ability in type_relevant and not type_relevant[ability]):
            continue
        try:
            name = pretty(tables.ability_name(ability))
        except (KeyError, IndexError):
            name = "unknown ability"
        effect = {
            I.ABILITY_LEVITATE: "immune to Ground",
            I.ABILITY_WONDER_GUARD: "blocks non-super-effective damage",
            I.ABILITY_VOLT_ABSORB: "absorbs Electric",
            I.ABILITY_WATER_ABSORB: "absorbs Water",
            I.ABILITY_FLASH_FIRE: "blocks Fire and boosts Fire",
            I.ABILITY_THICK_FAT: "halves Fire/Ice damage",
            I.ABILITY_STURDY: "blocks OHKO moves",
            I.ABILITY_HUGE_POWER: "doubles Attack",
            I.ABILITY_PURE_POWER: "doubles Attack",
            I.ABILITY_GUTS: "ignores burn's Attack penalty and boosts Attack when statused",
            I.ABILITY_BLAZE: "boosts Fire power at low HP",
            I.ABILITY_OVERGROW: "boosts Grass power at low HP",
            I.ABILITY_TORRENT: "boosts Water power at low HP",
            I.ABILITY_SWARM: "boosts Bug power at low HP",
            I.ABILITY_HUSTLE: "boosts physical power but lowers accuracy",
            I.ABILITY_MARVEL_SCALE: "boosts Defense while statused",
            I.ABILITY_INTIMIDATE: "lowers the foe's Attack on switch-in",
        }[ability]
        out.append(f"{'possible ' if possible else ''}{name}: {effect}")
    return out


def _item_effects(
    battler: BattlerView, move_type: int, tables: GameTables, *, defender: bool = False
) -> list[str]:
    if battler.held_item in (None, 0):
        return []
    try:
        item = tables.item(battler.held_item)
    except (KeyError, IndexError):
        return []
    type_boosters = {
        I.HOLD_EFFECT_BUG_POWER: K.TYPE_BUG,
        I.HOLD_EFFECT_STEEL_POWER: K.TYPE_STEEL,
        I.HOLD_EFFECT_GROUND_POWER: K.TYPE_GROUND,
        I.HOLD_EFFECT_ROCK_POWER: K.TYPE_ROCK,
        I.HOLD_EFFECT_GRASS_POWER: K.TYPE_GRASS,
        I.HOLD_EFFECT_DARK_POWER: K.TYPE_DARK,
        I.HOLD_EFFECT_FIGHTING_POWER: K.TYPE_FIGHTING,
        I.HOLD_EFFECT_ELECTRIC_POWER: K.TYPE_ELECTRIC,
        I.HOLD_EFFECT_WATER_POWER: K.TYPE_WATER,
        I.HOLD_EFFECT_FLYING_POWER: K.TYPE_FLYING,
        I.HOLD_EFFECT_POISON_POWER: K.TYPE_POISON,
        I.HOLD_EFFECT_ICE_POWER: K.TYPE_ICE,
        I.HOLD_EFFECT_GHOST_POWER: K.TYPE_GHOST,
        I.HOLD_EFFECT_PSYCHIC_POWER: K.TYPE_PSYCHIC,
        I.HOLD_EFFECT_FIRE_POWER: K.TYPE_FIRE,
        I.HOLD_EFFECT_DRAGON_POWER: K.TYPE_DRAGON,
        I.HOLD_EFFECT_NORMAL_POWER: K.TYPE_NORMAL,
    }
    boosted_type = type_boosters.get(item.hold_effect)
    if boosted_type == move_type:
        amount = item.hold_effect_param or 10
        return [f"{pretty(item.name)}: boosts {K.TYPE_NAMES[move_type]} power {100 + amount}%"]
    effects = {
        I.HOLD_EFFECT_CHOICE_BAND: "boosts physical power 1.5x and locks the move",
        I.HOLD_EFFECT_LEFTOVERS: "restores 1/16 max HP after the turn",
        I.HOLD_EFFECT_QUICK_CLAW: "may act first",
    }
    if item.hold_effect in effects:
        return [f"{pretty(item.name)}: {effects[item.hold_effect]}"]
    return []


_STAGE_ATTR = {"def": "def_"}
_STAGE_LABEL = {
    "atk": "Attack",
    "def": "Defense",
    "spa": "Special Attack",
    "spd": "Special Defense",
    "spe": "Speed",
    "acc": "accuracy",
    "eva": "evasion",
}


def _stage_change_facts(
    changes: Sequence[tuple[str, str, int, int | None]],
    attacker: BattlerView,
    defender: BattlerView,
    estimate: DamageEstimate,
) -> tuple[list[dict[str, Any]], str, str | None]:
    """Apply ROM stat deltas to visible current stages and classify their remaining value."""
    out: list[dict[str, Any]] = []
    for who, stat, requested, chance in changes:
        battler = attacker if who == "self" else defender
        current = getattr(battler.stat_stages, _STAGE_ATTR.get(stat, stat))
        resulting = max(-6, min(6, current + requested))
        effective = resulting - current
        entry: dict[str, Any] = {
            "who": who,
            "stat": stat,
            "stages": requested,
            "chance_pct": chance,
            "current_stage": current,
            "resulting_stage": resulting,
            "effective_stages": effective,
        }
        if effective == 0:
            entry["no_further_effect"] = True
        elif current != 0 and (current > 0) == (requested > 0):
            entry["already_applied"] = True
        benefits_turn_order = stat == "spe" and (
            (who == "self" and requested > 0) or (who == "target" and requested < 0)
        )
        if benefits_turn_order and estimate.speed_attacker[0] > estimate.speed_defender[1]:
            entry["turn_order_already_decided"] = True
        out.append(entry)

    remaining = [entry for entry in out if not entry.get("no_further_effect")]
    valuable = [entry for entry in remaining if not entry.get("turn_order_already_decided")]
    if not remaining:
        entry = out[0]
        owner = "your" if entry["who"] == "self" else "the foe's"
        stat = _STAGE_LABEL.get(str(entry["stat"]), str(entry["stat"]))
        reason = f"{owner} {stat} is already at {entry['current_stage']:+d}; no further effect"
        return out, "none", reason
    if not valuable:
        reason = "you already act first; further Speed changes add no turn-order value"
        return out, "realized", reason
    return out, "available", None


def _stage_ratio(stage: int) -> float:
    numerator, denominator = K.STAT_STAGE_RATIOS[stage + 6]
    return numerator / denominator


def threat_facts(
    attacker: BattlerView,
    defender: BattlerView,
    move_ids: Sequence[int],
    tables: GameTables,
    field: FieldView,
    *,
    source: str,
) -> dict[str, Any] | None:
    """The foe's highest expected one-move threat against the selected player battler."""
    best: tuple[float, dict[str, Any]] | None = None
    for move_id in move_ids:
        if not move_id:
            continue
        try:
            move = tables.move(move_id)
            est = estimate_damage(attacker, defender, move_id, tables, field)
        except (KeyError, IndexError):
            continue
        if est.damage_model not in ("formula", "fixed") or est.max_share_of_max_hp is None:
            continue
        # A KO is impossible when even the largest damage roll is below the smallest
        # plausible current HP. Keep the displayed threat internally consistent with its
        # damage range even if a widened estimator supplied an over-broad probability envelope.
        ko_probability = est.ko_probability
        if est.defender_hp is not None and est.max_damage < est.defender_hp[0]:
            ko_probability = (0.0, 0.0)
        expected = ((est.min_share_of_max_hp or 0.0) + (est.max_share_of_max_hp or 0.0)) / 2
        expected *= (est.accuracy[0] + est.accuracy[1]) / 200
        current = None
        if est.min_share is not None and est.max_share is not None:
            current = [_pct(est.min_share), _pct(est.max_share)]
        entry: dict[str, Any] = {
            "name": pretty(move.name),
            "source": source,
            "damage_pct_of_max_hp": [
                _pct(est.min_share_of_max_hp),
                _pct(est.max_share_of_max_hp),
            ],
            "damage_pct_of_hp": current,
            "ko_chance_pct": _range(ko_probability[0] * 100, ko_probability[1] * 100),
            "can_ko": "no" if ko_probability == (0.0, 0.0) else _can_ko(est),
            "faster": est.faster,
            "priority": move.priority,
            "accuracy": (
                est.accuracy[0] if est.accuracy[0] == est.accuracy[1] else list(est.accuracy)
            ),
            "ability_effects": _ability_effects(attacker, est.move_type, tables),
            "item_effects": _item_effects(attacker, est.move_type, tables),
        }
        if best is None or expected > best[0]:
            best = (expected, entry)
    return best[1] if best else None


def move_option_facts(
    attacker: BattlerView,
    defender: BattlerView,
    slot: int,
    tables: GameTables,
    field: FieldView,
    opponents: Sequence[BattlerView] = (),
) -> dict[str, Any]:
    """Keys: move_id, name, slot, pp, max_pp, type, category, power, accuracy, effectiveness,
    stab, estimated_damage_pct_of_hp [lo, hi], can_ko "no|maybe|yes", faster "yes|no|unknown",
    no_effect, recoil, inflicts, uses_turn, restrictions, priority, uncertain, ...
    """
    sl = attacker.moves[slot]
    m = tables.move(sl.move_id)
    est = estimate_damage(attacker, defender, sl.move_id, tables, field)
    verdict = move_legality(attacker, slot, tables, field, opponents or (defender,))
    traits = traits_for(m.effect)

    facts: dict[str, Any] = {
        "move_id": m.move_id,
        "name": pretty(m.name),
        "slot": slot,
        "pp": sl.pp,
        "max_pp": sl.max_pp,
        "type": K.TYPE_NAMES[est.move_type],
        "category": None if (m.power == 0 and est.damage_model == "fixed") else est.category,
        "power": est.power if est.power is not None else (m.power or None),
        "accuracy": (
            None
            if est.always_hits
            else (est.accuracy[0] if est.accuracy[0] == est.accuracy[1] else list(est.accuracy))
        ),
        "never_misses": est.always_hits,
        "priority": m.priority,
        "target": _TARGET_NAMES.get(m.target, "other"),
        "uses_turn": True,
        "restrictions": [] if verdict.code == "ok" else [verdict.code],
    }
    if est.power is None and m.power:
        facts["power_varies"] = True
    if est.hits != (1, 1):
        facts["hits"] = list(est.hits)

    # -- effectiveness / immunity (damaging and typecalc'd moves only)
    damaging = est.damage_model in ("formula", "fixed")
    facts["effectiveness"] = est.effectiveness if (damaging or est.no_effect) else None
    facts["stab"] = est.stab
    facts["no_effect"] = est.no_effect
    if est.no_effect and est.no_effect_reason:
        facts["no_effect_reason"] = est.no_effect_reason

    # -- damage
    if damaging and est.damage_model != "unknown":
        lo = _pct(est.min_share)
        hi = _pct(est.max_share)
        if lo is not None and hi is not None:
            facts["estimated_damage_pct_of_hp"] = [min(lo, 100), min(hi, 100)]
            facts["estimated_damage_pct_of_max_hp"] = [
                _pct(est.min_share_of_max_hp),
                _pct(est.max_share_of_max_hp),
            ]
        else:
            facts["estimated_damage_pct_of_hp"] = None
        facts["estimated_damage"] = [est.min_damage, est.max_damage]
        facts["crit_damage"] = [est.crit_min, est.crit_max]
        facts["crit_chance_pct"] = _range(est.crit_chance[0] * 100, est.crit_chance[1] * 100)
        if est.hits_to_ko is not None:
            facts["hits_to_ko"] = [est.hits_to_ko[0], est.hits_to_ko[1]]
    else:
        facts["estimated_damage_pct_of_hp"] = None
        if est.damage_model in ("unknown", "variable"):
            facts["damage_estimate"] = "unavailable"
    facts["can_ko"] = _can_ko(est)
    facts["ko_chance_pct"] = _range(est.ko_probability[0] * 100, est.ko_probability[1] * 100)
    facts["faster"] = est.faster
    facts["ability_effects"] = (
        [
            *_ability_effects(attacker, est.move_type, tables),
            *_ability_effects(defender, est.move_type, tables, defender=True),
        ]
        if damaging
        else []
    )
    facts["item_effects"] = (
        [
            *_item_effects(attacker, est.move_type, tables),
            *_item_effects(defender, est.move_type, tables, defender=True),
        ]
        if damaging
        else []
    )

    # -- recoil / self-KO
    recoil: dict[str, Any] | None = None
    if est.recoil_fraction_of_damage:
        recoil = {"pct_of_damage_dealt": int(round(est.recoil_fraction_of_damage * 100))}
    if est.self_ko:
        recoil = {**(recoil or {}), "user_faints": True}
    facts["recoil"] = recoil

    # -- status / stat changes
    if est.inflicts_status and not est.status_blocked_by:
        entry: dict[str, Any] = {
            "status": est.inflicts_status,
            "chance_pct": est.status_chance,
        }
        facts["inflicts"] = entry
    else:
        facts["inflicts"] = None
    if est.status_blocked_by:
        facts["status_blocked_by"] = est.status_blocked_by
    if est.stat_changes:
        changes, status_value, reason = _stage_change_facts(
            est.stat_changes, attacker, defender, est
        )
        facts["stat_changes"] = changes
        facts["status_value"] = status_value
        if reason:
            facts["status_value_reason"] = reason
    elif facts["category"] == "status" and est.status_blocked_by:
        facts["status_value"] = "none"
        facts["status_value_reason"] = f"status has no effect ({est.status_blocked_by})"
    if traits.heals_fraction:
        facts["heals_pct_of_max_hp"] = int(traits.heals_fraction * 100)
    if traits.heals_with_weather:
        facts["heals_with_weather"] = True
    if traits.drain_fraction:
        facts["drains_pct_of_damage"] = int(traits.drain_fraction * 100)
    multi_turn = _multi_turn_value(m.effect, traits, facts)
    if multi_turn:
        facts["multi_turn_value"] = multi_turn
    if traits.sets_weather:
        facts["sets_weather"] = traits.sets_weather
    if traits.sets_side:
        facts["sets_side_condition"] = traits.sets_side
    if traits.protects:
        facts["protects_user"] = True
    if est.notes:
        facts["notes"] = list(est.notes)
    if est.uncertain_modifiers:
        facts["uncertain"] = list(est.uncertain_modifiers)
    return facts


def _multi_turn_value(effect: int, traits: Any, facts: Mapping[str, Any]) -> str | None:
    """Describe later-turn value from the ROM effect id, never from a move name."""
    if facts.get("status_value") in {"none", "realized"}:
        return None
    if effect == I.EFFECT_LEECH_SEED:
        return "drains ~1/8 of the foe's max HP per turn"
    if traits.drain_fraction:
        pct = int(round(traits.drain_fraction * 100))
        return f"restores ~{pct}% of damage dealt"
    if traits.primary_status and facts.get("inflicts"):
        return {
            K.STATUS_POISON: "damages the foe by ~1/8 max HP per turn",
            K.STATUS_TOXIC: "damage grows each later turn",
            K.STATUS_BURN: "halves physical damage and drains ~1/8 max HP per turn",
            K.STATUS_PARALYSIS: "cuts Speed by 75% and may prevent later actions",
            K.STATUS_SLEEP: "prevents the foe acting until it wakes",
            K.STATUS_FREEZE: "prevents the foe acting until it thaws",
            "confusion": "may make the foe hurt itself on later turns",
            "drowsy": "puts the foe to sleep after this turn",
            "leech_seed": "drains ~1/8 of the foe's max HP per turn",
        }.get(traits.primary_status)
    changes = facts.get("stat_changes")
    if traits.stat_changes and isinstance(changes, list) and changes:
        change = changes[0]
        if isinstance(change, Mapping):
            who, stat = change.get("who"), change.get("stat")
            current = change.get("current_stage")
            resulting = change.get("resulting_stage")
            if isinstance(current, int) and isinstance(resulting, int) and current != resulting:
                before, after = _stage_ratio(current), _stage_ratio(resulting)
                repeated = " more" if change.get("already_applied") else ""
                if who == "target" and stat in {"atk", "spa"} and resulting < current:
                    pct = int(round((1 - after / before) * 100))
                    return f"reduces its damage by ~{pct}%{repeated} for later turns"
                if who == "self" and stat in {"atk", "spa"} and resulting > current:
                    pct = int(round((after / before - 1) * 100))
                    return f"raises your damage by ~{pct}%{repeated} for later turns"
                if who == "self" and stat in {"def", "spd"} and resulting > current:
                    pct = int(round((1 - before / after) * 100))
                    return f"reduces matching damage taken by ~{pct}%{repeated} for later turns"
            if stat == "spe":
                return "changes who acts first on later turns"
        return "changes battle stats for later turns"
    return None


# ----------------------------------------------------------------------------------------------
# switching
# ----------------------------------------------------------------------------------------------


def switch_option_facts(
    active: BattlerView,
    candidate: BattlerView,
    opponent: BattlerView | None,
    tables: GameTables,
    field: FieldView,
    opponents: Sequence[BattlerView] = (),
    *,
    forced: bool = False,
) -> dict[str, Any]:
    """Facts about sending ``candidate`` in. ``forced`` = replacement after a faint.

    ``foe_stab_vs_candidate`` is a type-only proxy for the foe's STAB, not its actual moves.
    """
    verdict = switch_legality(
        active,
        candidate,
        tables,
        field,
        opponents or ([opponent] if opponent else []),
        forced=forced,
    )
    facts: dict[str, Any] = {
        "uses_turn": not forced,
        "forced_replacement": forced,
        "restrictions": [] if verdict.code == "ok" else [verdict.code],
    }
    if candidate.species is not None:
        facts["species"] = pretty(tables.species_name(candidate.species))
        types = candidate.types or tables.species(candidate.species).types
        facts["types"] = _type_names(types)
    facts["level"] = candidate.level
    facts["hp"] = candidate.hp
    facts["max_hp"] = candidate.max_hp
    if candidate.hp is not None and candidate.max_hp:
        facts["hp_pct"] = int(round(100 * candidate.hp / candidate.max_hp))
    facts["fainted"] = candidate.hp == 0
    facts["status"] = candidate.status
    facts["is_egg"] = candidate.is_egg
    if not forced:
        facts["foe_gets_free_attack"] = True  # the turn is spent switching (Gen 3)
    if opponent is None or candidate.species is None or candidate.hp == 0 or candidate.is_egg:
        return facts

    best: tuple[float, int, str] | None = None  # (effectiveness, power, name); first wins ties
    for sl in candidate.moves:
        if not sl.move_id:
            continue
        try:
            est = estimate_damage(candidate, opponent, sl.move_id, tables, field)
            m = tables.move(sl.move_id)
        except (KeyError, IndexError):
            continue
        if est.damage_model not in ("formula", "fixed") or est.effectiveness is None:
            continue
        key = (est.effectiveness, est.power if est.power is not None else (m.power or 0))
        if best is None or key > best[:2]:
            best = (*key, pretty(m.name))
    if best is not None:
        facts["best_move"] = {"name": best[2], "effectiveness": best[0]}
    if opponent.species is not None:  # unknown foe types stay unknown, never an exception
        try:
            foe_types = opponent.types or tables.species(opponent.species).types
            pairs = [(tables.type_chart.multiplier(t, *types), t) for t in dict.fromkeys(foe_types)]
            eff, ftype = max(pairs, key=lambda p: p[0])  # first wins ties
            facts["foe_stab_vs_candidate"] = {
                "effectiveness": eff,
                "foe_type": pretty(K.TYPE_NAMES[ftype]),
            }
        except (KeyError, IndexError, TypeError, ValueError):
            pass
    return facts


def _type_names(types: tuple[int, int]) -> list[str]:
    return [K.TYPE_NAMES[t] for t in dict.fromkeys(types)]


# ----------------------------------------------------------------------------------------------
# bag items
# ----------------------------------------------------------------------------------------------

_ESCAPE_ITEMS = {I.ITEM_POKE_DOLL, I.ITEM_FLUFFY_TAIL}
_HINTED_KINDS = frozenset({"wake_party", "escape"})  # kinds with no gItemEffectTable row


def _item_kind(item: ItemData) -> str:
    eff = item.effect
    if item.is_ball:
        return "ball"
    if item.item_id in _ESCAPE_ITEMS:
        return "escape"
    if item.item_id == I.ITEM_POKE_FLUTE:
        return "wake_party"
    if eff is None:
        return "other"
    if eff.revive:
        return "revive"
    if eff.heal_pp is not None:
        return "restore_pp"
    if eff.stat_boost or eff.focus_energy or eff.guard_spec:
        return "battle_boost"
    if eff.heals_hp:
        return "heal_hp"
    if eff.cures:
        return "cure_status"
    return "other"


def _whose(battler: BattlerView) -> str:
    name = battler.name or "Pokemon"
    return f"your {name}" if battler.is_player_side else f"the foe's {name}"


def _wake_party_facts(
    facts: dict[str, Any], battlers: Sequence[BattlerView] | None
) -> dict[str, Any]:
    """POKe FLUTE in battle (M4 run 1): it cures sleep on every Pokemon on the field, the foe's
    included, and the game spends the turn whether or not anyone was asleep (the run used it 80
    times with nobody asleep: the turn counter advanced each time and nothing else changed)."""
    if battlers is None:
        facts["would_have_effect"] = None  # who is asleep is not known here
        return facts
    asleep = [b for b in battlers if b.status == K.STATUS_SLEEP]
    facts["would_have_effect"] = bool(asleep)
    if asleep:
        facts["wakes"] = [_whose(b) for b in asleep]
    else:
        facts["reason"] = "nobody in the battle is asleep"
        facts["turn_still_used"] = True
    return facts


def item_option_facts(
    item_id: int,
    target: BattlerView | None,
    tables: GameTables,
    field: FieldView,
    *,
    quantity: int | None = None,
    battlers: Sequence[BattlerView] | None = None,
    kind_hint: str | None = None,
) -> dict[str, Any]:
    """Facts for a battle-usable bag item on ``target`` (heal amounts, status cures, revive).

    Rules: src/pokemon.c ``PokemonUseItemEffects`` (HP heal only if 0 < HP < max; revive only
    at 0 HP; a cure only if the target has that status; a stat booster only below +6).
    ``battlers`` are the Pokemon on the field (both sides): the POKe FLUTE has no target, it wakes
    every sleeping one of them, the foe's too, and does nothing when none sleeps. ``kind_hint``
    (``wake_party`` / ``escape``) lets the caller name the two kinds that have no effect row from
    its own ROM data (the battle family's item classes) instead of the item-id fallback below.
    """
    item = tables.item(item_id)
    facts: dict[str, Any] = {
        "item_id": item_id,
        "name": pretty(item.name),
        "quantity": quantity,
        "battle_usable": item.battle_usable,
        "kind": kind_hint if kind_hint in _HINTED_KINDS else _item_kind(item),
        "uses_turn": True,
    }
    if not item.battle_usable:
        facts["would_have_effect"] = False
        facts["reason"] = "not usable in battle"
        return facts
    if field.kind == K.KIND_LINK:
        facts["battle_usable"] = False
        facts["would_have_effect"] = False
        facts["reason"] = "items cannot be used in link battles"
        return facts
    eff = item.effect
    kind = facts["kind"]
    if kind == "wake_party":
        return _wake_party_facts(facts, battlers)
    if kind in ("ball", "escape", "other") or eff is None:
        if kind == "escape":
            facts["would_have_effect"] = field.kind == K.KIND_WILD
            if field.kind != K.KIND_WILD:
                facts["reason"] = "cannot run from this battle"
        return facts
    if target is None:
        facts["would_have_effect"] = None
        return facts

    facts["target_hp"] = target.hp
    facts["target_max_hp"] = target.max_hp
    facts["target_status"] = target.status
    effect_found = False
    reasons: list[str] = []
    hp, max_hp = target.hp, target.max_hp
    if eff.heals_hp or eff.revive:
        if hp is None or max_hp is None:
            facts["would_have_effect"] = None
            return facts
        if eff.revive:
            if hp != 0:
                reasons.append("target has not fainted")
            else:
                restore = max_hp // 2 or 1 if eff.heal_hp_half else max_hp
                facts["hp_restored"] = restore
                facts["hp_after"] = restore
                effect_found = True
        else:
            if hp == 0:
                reasons.append("target has fainted")
            elif hp == max_hp:
                reasons.append("target has full HP")
            else:
                if eff.heal_hp_full:
                    amount = max_hp - hp
                elif eff.heal_hp_half:
                    amount = max(1, max_hp // 2)
                else:
                    amount = eff.heal_hp or 0
                healed = min(amount, max_hp - hp)
                facts["hp_restored"] = healed
                facts["hp_after"] = hp + healed
                facts["hp_pct_after"] = int(round(100 * (hp + healed) / max_hp))
                effect_found = True
    if eff.cures:
        have = set()
        if target.status != K.STATUS_NONE:
            have.add(K.STATUS_POISON if target.status == K.STATUS_TOXIC else target.status)
        if K.V_CONFUSION in target.volatile:
            have.add("confusion")
        if K.V_INFATUATION in target.volatile:
            have.add("infatuation")
        cured = sorted(eff.cures & have)
        facts["cures"] = sorted(eff.cures)
        if cured:
            facts["cures_current"] = cured
            effect_found = True
        elif not (eff.heals_hp or eff.revive):
            reasons.append("target has no status this item cures")
    if eff.heal_pp is not None:
        facts["restores_pp"] = "full" if eff.heal_pp == 0x7F else eff.heal_pp
        facts["restores_pp_scope"] = "all_moves" if eff.heal_pp_all_moves else "one_move"
        missing = [
            s
            for s in target.moves
            if s.move_id and s.pp is not None and s.max_pp and s.pp < s.max_pp
        ]
        if target.moves and not missing and all(s.max_pp for s in target.moves if s.move_id):
            reasons.append("no move is missing PP")
        else:
            effect_found = True
    if eff.stat_boost or eff.focus_energy or eff.guard_spec:
        if eff.stat_boost:
            facts["stat_changes"] = [
                {"stat": stat, "stages": stages} for stat, stages in eff.stat_boost
            ]
            stages = target.stat_stages
            maxed = [
                getattr(stages, "def_" if stat == "def" else stat) >= 6
                for stat, _ in eff.stat_boost
            ]
            if all(maxed):
                reasons.append("the stat is already at +6")
            else:
                effect_found = True
        if eff.focus_energy:
            if K.V_FOCUS_ENERGY in target.volatile:
                reasons.append("critical-hit boost is already active")
            else:
                facts["raises_crit_stage"] = 2
                effect_found = True
        if eff.guard_spec:
            if field.side_of(target).has(K.SIDE_MIST):
                reasons.append("Mist is already active")
            else:
                facts["protects_stats_turns"] = 5
                effect_found = True
    facts["would_have_effect"] = effect_found
    if not effect_found and reasons:
        facts["reason"] = "; ".join(reasons)
    return facts


# ----------------------------------------------------------------------------------------------
# balls
# ----------------------------------------------------------------------------------------------


def ball_option_facts(
    ball_item_id: int,
    target: BattlerView,
    tables: GameTables,
    field: FieldView,
    *,
    quantity: int | None = None,
    species_caught: bool | None = None,
    underwater: bool | None = None,
    safari_catch_factor: int | None = None,
) -> dict[str, Any]:
    """Gen-3 catch-rate formula (``Cmd_handleballthrow``) as a probability range per throw."""
    item = tables.item(ball_item_id)
    facts: dict[str, Any] = {
        "item_id": ball_item_id,
        "name": pretty(item.name),
        "quantity": quantity,
        "uses_turn": True,
        "kind": "ball",
    }
    if field.kind == K.KIND_TRAINER:
        facts.update(catchable=False, reason="a trainer's Pokemon cannot be caught")
        return facts
    if field.kind == K.KIND_GHOST:
        facts.update(catchable=False, reason="the ghost cannot be caught (unidentified)")
        return facts
    if field.kind == K.KIND_LINK:
        facts.update(catchable=False, reason="not possible in link battles")
        return facts
    facts["catchable"] = True
    if field.kind == K.KIND_TUTORIAL:
        facts.update(catch_chance_pct=[100, 100], note="scripted tutorial catch")
        return facts
    if ball_item_id == I.ITEM_MASTER_BALL:
        facts.update(catch_chance_pct=[100, 100], note="a Master Ball always catches")
        return facts
    if target.species is None:
        facts.update(catch_chance_pct=None, reason="species unknown")
        return facts
    species = tables.species(target.species)

    # catch rate
    if ball_item_id == I.ITEM_SAFARI_BALL:
        if safari_catch_factor is None:
            catch_rate_range = (3 * 1275 // 100, 20 * 1275 // 100)
        else:
            catch_rate_range = ((safari_catch_factor * 1275 // 100) & 0xFF,) * 2
    else:
        catch_rate_range = (species.catch_rate, species.catch_rate)
    facts["catch_rate"] = (
        species.catch_rate
        if catch_rate_range[0] == catch_rate_range[1] == species.catch_rate
        else list(catch_rate_range)
    )

    types = target.types or species.types
    level = target.level
    mult_lo, mult_hi = ball_multiplier(
        ball_item_id, types, level, field.turn, species_caught, underwater
    )

    # HP: exact or an interval from the HP bar
    hp_pairs: list[tuple[int, int]] = []
    if target.hp is not None and target.max_hp:
        hp_pairs.append((target.hp, target.max_hp))
    elif target.hp_pct is not None and level is not None:
        from pokegent.games.gen3.ruleset.damage import _enemy_stat_bounds

        mlo, mhi = _enemy_stat_bounds(species.base_hp, level, hp=True, ev_max=0)
        for mx in (mlo, mhi):
            for pct in (max(0.0, target.hp_pct - 100 / 48), min(100.0, target.hp_pct + 100 / 48)):
                hp_pairs.append((max(1, int(round(mx * pct / 100))), mx))
    else:
        facts.update(catch_chance_pct=None, reason="HP unknown")
        return facts

    status = target.status
    probs = []
    for catch_rate, mult, (hp, max_hp) in (
        (cr, mu, hp_pair)
        for cr in set(catch_rate_range)
        for mu in {mult_lo, mult_hi}
        for hp_pair in hp_pairs
    ):
        odds = catch_odds(catch_rate, mult, hp, max_hp, status)
        probs.append(catch_probability(odds))
    lo, hi = min(probs), max(probs)
    facts["catch_chance_pct"] = [int(math.floor(lo * 100)), int(math.ceil(hi * 100))]
    facts["ball_multiplier"] = mult_lo / 10 if mult_lo == mult_hi else [mult_lo / 10, mult_hi / 10]
    if target.hp_pct is not None and target.hp is None:
        facts["uncertain"] = ["hp_from_percent"]
    return facts
