"""Gen-3 damage, accuracy and turn-order estimator.

``estimate_damage(attacker, defender, move, tables, field) -> DamageEstimate``.

Facts, never recommendations. Every formula step below is re-derived from the pret/pokefirered
decompile (commit pinned in ``source-lock.md``) and cited in the comments:

* ``CalculateBaseDamage``                       src/pokemon.c
* ``Cmd_critcalc / damagecalc / typecalc / adjustnormaldamage``    src/battle_script_commands.c
* ``ApplyRandomDmgMultiplier`` (roll 85..100)   src/battle_script_commands.c
* ``Cmd_accuracycheck / AccuracyCalcHelper``    src/battle_script_commands.c
* ``GetWhoStrikesFirst``                        src/battle_main.c
* ``CalculateMonStats``                         src/pokemon.c (stat ranges of unseen enemies)

Order of the game for a damaging move (data/battle_scripts_1.s ``BattleScript_EffectHit``):
``critcalc, damagecalc, typecalc, adjustnormaldamage``. ``damagecalc`` is
``CalculateBaseDamage * crit * dmgMultiplier`` (x2 Charge, x1.5 Helping Hand); ``typecalc`` applies
STAB (x15/10) and then every matching chart entry with integer truncation after each step;
``adjustnormaldamage`` applies the random roll ``dmg * (100 - rand % 16) / 100`` (min 1).

Unknown inputs (``None`` on the views) never produce a guess: stats of an unseen enemy become a
range from base stats with IV 0..31, EV 0..``ENEMY_EV_MAX`` and nature x0.9..x1.1; an unknown
ability becomes the (at most two) abilities its species can have; an unknown held item becomes the
wild-item pool of the species (wild battle) or the damage-relevant item candidates (otherwise); an
unknown badge set or unknown Cloud Nine / Air Lock state becomes both outcomes. The result is the
envelope over all candidates and ``uncertain_modifiers`` names what widened it.

NOT modeled (the range is widened or marked, see the runtime contracts):
Focus Band / Endure survival,
Substitute absorbing the hit, Protect / Detect, Counter / Mirror Coat / Bide (damage depends on
the damage taken), Beat Up, Low Kick (weight), Present, Magnitude, Rollout / Fury Cutter chains,
Pursuit / Revenge conditions, two-turn move invulnerability of the user, Future Sight, Spit Up,
double battle redirection, Gen-3 weather damage and abilities that act at turn end.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from itertools import product

from pokegent.games.gen3 import mechanics_constants as K
from pokegent.games.gen3 import mechanics_ids as I
from pokegent.games.gen3.firered.rom.rom_tables import GameTables, MoveData, SpeciesData
from pokegent.games.gen3.mechanics.views import BattlerView, FieldView, MoveSlotView, StagesView
from pokegent.games.gen3.ruleset.effects import EffectTraits, traits_for

#: Enemy EVs are 0 in FireRed (CreateMon for wild and NPC parties never sets EVs). Raised only if
#: a caller passes a different bound.
ENEMY_EV_MAX = 0
ENEMY_IV_RANGE = (0, 31)
NATURE_RANGE = (90, 110)  # percent, src/pokemon.c ModifyStatByNature
ROLLS = tuple(range(85, 101))  # src/battle_script_commands.c ApplyRandomDmgMultiplier

# -- move classification (from the decompile's battle scripts) ---------------------------------
_FIXED_DAMAGE = {
    I.EFFECT_DRAGON_RAGE,
    I.EFFECT_SONICBOOM,
    I.EFFECT_LEVEL_DAMAGE,
    I.EFFECT_PSYWAVE,
    I.EFFECT_SUPER_FANG,
    I.EFFECT_ENDEAVOR,
    I.EFFECT_OHKO,
}
#: damage depends on something the views cannot carry; reported as variable, never invented
_VARIABLE_DAMAGE = {
    I.EFFECT_COUNTER,
    I.EFFECT_MIRROR_COAT,
    I.EFFECT_BIDE,
    I.EFFECT_FUTURE_SIGHT,
    I.EFFECT_BEAT_UP,
    I.EFFECT_PRESENT,
    I.EFFECT_NATURE_POWER,
    I.EFFECT_SPIT_UP,
}
#: variable base power with known bounds (min, max): src/battle_script_commands.c
_POWER_BOUNDS = {
    I.EFFECT_MAGNITUDE: (10, 150),  # magnitudedamagecalculation: 10, 30, 50, 70, 90, 110, 150
    I.EFFECT_ROLLOUT: (30, 480),  # 30 << (0..4), x2 when Defense Curl was used
    I.EFFECT_FURY_CUTTER: (10, 160),  # 10 << (0..4)
    I.EFFECT_LOW_KICK: (20, 120),  # weightdamagecalculation table
    I.EFFECT_TRIPLE_KICK: (10, 30),
}
_TWO_TURN = {
    I.EFFECT_RAZOR_WIND,
    I.EFFECT_SKY_ATTACK,
    I.EFFECT_SKULL_BASH,
    I.EFFECT_SOLAR_BEAM,
    I.EFFECT_SEMI_INVULNERABLE,
}
_HIGH_CRIT = {
    I.EFFECT_HIGH_CRITICAL,
    I.EFFECT_SKY_ATTACK,
    I.EFFECT_BLAZE_KICK,
    I.EFFECT_POISON_TAIL,
}
_RECOIL_25 = {I.EFFECT_RECOIL}  # src/battle_script_commands.c MOVE_EFFECT_RECOIL_25 = gHpDealt / 4
_RECOIL_33 = {I.EFFECT_DOUBLE_EDGE}  # MOVE_EFFECT_RECOIL_33 = gHpDealt / 3
#: hit ground-contact immunity exemptions for semi-invulnerable targets
_HITS_ON_AIR = {I.EFFECT_GUST, I.EFFECT_TWISTER, I.EFFECT_SKY_UPPERCUT}
_HITS_UNDERGROUND = {I.EFFECT_EARTHQUAKE, I.EFFECT_MAGNITUDE}

#: src/pokemon.c sHoldEffectToType
_TYPE_BOOST_ITEMS = {
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


# -- result type --------------------------------------------------------------------------------


@dataclass(frozen=True)
class DamageEstimate:
    move_id: int
    move_type: int  # after Hidden Power / Weather Ball
    category: str  # "physical" | "special" | "status"
    damage_model: str  # "formula" | "fixed" | "variable" | "none" | "unknown"
    power: int | None  # effective base power of the first hit; None if the formula is not used
    min_damage: int  # one non-critical use: lowest roll, narrowest assumptions, fewest hits
    max_damage: int  # one non-critical use: highest roll, widest assumptions, most hits
    crit_min: int
    crit_max: int
    min_share: float | None  # min_damage / the largest plausible current HP (None: HP unknown)
    max_share: float | None  # max_damage / the smallest plausible current HP (may exceed 1.0)
    min_share_of_max_hp: float | None
    max_share_of_max_hp: float | None
    defender_hp: tuple[int, int] | None  # plausible current HP (lo, hi)
    defender_max_hp: tuple[int, int] | None
    ohko_possible: bool  # max_damage >= the smallest plausible current HP
    ko_certain: bool  # min_damage >= the largest plausible current HP
    ko_probability: tuple[float, float]  # P(one landed non-crit use KOs), lo..hi
    hits_to_ko: tuple[int, int | None] | None  # (fewest, most; None = no guarantee) landed uses
    accuracy: tuple[int, int]  # percent with stat stages and modifiers
    always_hits: bool
    effectiveness: float  # 0, .25, .5, 1, 2, 4 (1.0 for status moves without a type effect)
    stab: bool
    no_effect: bool  # immune (type chart, Levitate, absorb ability, Wonder Guard, ...)
    no_effect_reason: str | None
    crit_chance: tuple[float, float]
    recoil_fraction_of_damage: float
    recoil_fraction_of_max_hp: float
    self_ko: bool
    inflicts_status: str | None
    status_chance: int | None  # percent (before accuracy)
    status_blocked_by: str | None
    stat_changes: tuple[tuple[str, str, int, int | None], ...]  # (who, stat, stages, chance%)
    priority: int
    faster: str  # "yes" | "no" | "unknown"
    speed_attacker: tuple[int, int]
    speed_defender: tuple[int, int]
    hits: tuple[int, int]  # multi-hit count range, (1, 1) for ordinary moves
    uncertain_modifiers: tuple[str, ...]
    notes: tuple[str, ...]


# -- helpers ------------------------------------------------------------------------------------


def _move_id(move: int | MoveSlotView) -> int:
    move_id = move.move_id if isinstance(move, MoveSlotView) else int(move)
    if move_id <= 0:
        raise ValueError("cannot estimate an empty move slot")
    return move_id


def _stat_stage_apply(value: int, stage: int) -> int:
    """src/pokemon.c APPLY_STAT_MOD: ``stat * ratio[0] / ratio[1]`` with truncation."""
    num, den = K.STAT_STAGE_RATIOS[stage + 6]
    return value * num // den


def _enemy_stat_bounds(base: int, level: int, *, hp: bool, ev_max: int) -> tuple[int, int]:
    """src/pokemon.c CalculateMonStats / CALC_STAT with IV 0..31, EV 0..ev_max, nature range."""
    iv_lo, iv_hi = ENEMY_IV_RANGE
    if hp:
        lo = ((2 * base + iv_lo) * level) // 100 + level + 10
        hi = ((2 * base + iv_hi + ev_max // 4) * level) // 100 + level + 10
        return lo, hi
    lo = ((((2 * base + iv_lo) * level) // 100) + 5) * NATURE_RANGE[0] // 100
    hi = ((((2 * base + iv_hi + ev_max // 4) * level) // 100) + 5) * NATURE_RANGE[1] // 100
    return lo, hi


@dataclass(frozen=True)
class _Mon:
    """One fully concrete battler for a single scenario of the calculation."""

    species: int
    level: int
    hp: int
    max_hp: int
    atk: int
    def_: int
    spa: int
    spd: int
    spe: int
    stages: StagesView
    status: str
    types: tuple[int, int]
    ability: int
    hold_effect: int
    hold_param: int
    volatile: frozenset[str]
    is_player: bool
    friendship: int | None = None
    ivs: tuple[int, ...] | None = None


@dataclass(frozen=True)
class _Resolved:
    """A battler view resolved to value ranges and candidate sets."""

    view: BattlerView
    species: SpeciesData | None
    level: int | None
    stats: dict[str, tuple[int, int]]  # atk/def/spa/spd/spe/hp/max_hp -> (lo, hi)
    hp: tuple[int, int] | None
    max_hp: tuple[int, int] | None
    types: tuple[int, int] | None
    abilities: tuple[int, ...]
    ability_unknown: bool
    items: tuple[tuple[int, int, int], ...]  # (item_id, hold_effect, hold_param) candidates
    item_unknown: bool
    unknowns: tuple[str, ...]


def _resolve(
    view: BattlerView, tables: GameTables, *, role: str, kind: str, move_type: int | None = None
) -> _Resolved:
    unknowns: list[str] = []
    species = (
        tables.species(view.species)
        if view.species is not None and 0 < view.species < len(tables.all_species)
        else None
    )
    level = view.level
    types: tuple[int, int] | None = view.types
    if types is None and species is not None:
        types = species.types

    stats: dict[str, tuple[int, int]] = {}
    if view.stats is not None:
        s = view.stats
        for key, val in (
            ("atk", s.atk),
            ("def", s.def_),
            ("spa", s.spa),
            ("spd", s.spd),
            ("spe", s.spe),
        ):
            stats[key] = (val, val)
    elif species is not None and level is not None:
        unknowns.append(f"{role}_stats_range")
        for key, base in (
            ("atk", species.base_atk),
            ("def", species.base_def),
            ("spa", species.base_spa),
            ("spd", species.base_spd),
            ("spe", species.base_spe),
        ):
            stats[key] = _enemy_stat_bounds(base, level, hp=False, ev_max=ENEMY_EV_MAX)
    # HP
    max_hp: tuple[int, int] | None = None
    if view.max_hp is not None:
        max_hp = (view.max_hp, view.max_hp)
    elif species is not None and level is not None:
        if species.species_id == I.SPECIES_SHEDINJA:
            max_hp = (1, 1)
        else:
            max_hp = _enemy_stat_bounds(species.base_hp, level, hp=True, ev_max=ENEMY_EV_MAX)
        unknowns.append(f"{role}_max_hp_range")
    hp: tuple[int, int] | None = None
    if view.hp is not None:
        hp = (view.hp, view.hp)
        if max_hp is None:
            max_hp = (max(view.hp, 1), max(view.hp, 1))
    elif view.hp_pct is not None and max_hp is not None:
        # The HP bar has 48 pixels: pixels = hp * 48 / max_hp, at least 1 while alive
        # (src/battle_interface.c GetScaledHPFraction). Invert it to an HP interval.
        pixels = round(view.hp_pct * 48 / 100)
        if view.hp_pct <= 0:
            hp = (0, 0)
        elif pixels >= 48:
            hp = max_hp  # 48 pixels only when the HP is full
        else:
            hp = (
                max(1, -(-pixels * max_hp[0] // 48)),
                max(1, -(-(pixels + 1) * max_hp[1] // 48) - 1),
            )
        unknowns.append(f"{role}_hp_from_percent")

    # ability candidates
    ability_unknown = False
    if view.ability is not None:
        abilities: tuple[int, ...] = (view.ability,)
    elif species is not None:
        cands = sorted({a for a in species.abilities if a != 0}) or [0]
        abilities = tuple(cands)
        ability_unknown = len(cands) > 1
        unknowns.append(f"{role}_ability_unknown")
    else:
        abilities = (0,)
        ability_unknown = True
        unknowns.append(f"{role}_ability_unknown")

    # item candidates
    item_unknown = False
    items: list[tuple[int, int, int]] = []
    if view.held_item is not None:
        info = tables.item(view.held_item) if 0 <= view.held_item < len(tables.items) else None
        items.append(
            (view.held_item, info.hold_effect if info else 0, info.hold_effect_param if info else 0)
        )
    else:
        item_unknown = True
        unknowns.append(f"{role}_item_unknown")
        items.append((0, 0, 0))
        if kind == K.KIND_WILD and species is not None:
            for item_id in {species.item_common, species.item_rare}:
                if item_id:
                    info = tables.item(item_id)
                    items.append((item_id, info.hold_effect, info.hold_effect_param))
        elif species is not None and move_type is not None:
            # Damage-relevant candidates only (the runtime contracts): type boost for the move type,
            # Choice Band for physical attackers, and the species-specific doublers.
            for item_id_, item in ((i.item_id, i) for i in tables.items):
                eff = item.hold_effect
                if eff == 0:
                    continue
                if _TYPE_BOOST_ITEMS.get(eff) == move_type and role == "attacker":
                    items.append((item_id_, eff, item.hold_effect_param))
                    break
            if role == "attacker" and K.is_physical_type(move_type):
                _add_item_by_effect(items, tables, I.HOLD_EFFECT_CHOICE_BAND)
            for eff, sp in (
                (I.HOLD_EFFECT_LIGHT_BALL, (I.SPECIES_PIKACHU,)),
                (I.HOLD_EFFECT_THICK_CLUB, (I.SPECIES_CUBONE, I.SPECIES_MAROWAK)),
                (I.HOLD_EFFECT_METAL_POWDER, (I.SPECIES_DITTO,)),
                (I.HOLD_EFFECT_DEEP_SEA_TOOTH, (I.SPECIES_CLAMPERL,)),
                (I.HOLD_EFFECT_DEEP_SEA_SCALE, (I.SPECIES_CLAMPERL,)),
                (I.HOLD_EFFECT_SOUL_DEW, (I.SPECIES_LATIAS, I.SPECIES_LATIOS)),
            ):
                if species.species_id in sp:
                    _add_item_by_effect(items, tables, eff)
    return _Resolved(
        view=view,
        species=species,
        level=level,
        stats=stats,
        hp=hp,
        max_hp=max_hp,
        types=types,
        abilities=abilities,
        ability_unknown=ability_unknown,
        items=tuple(dict.fromkeys(items)),
        item_unknown=item_unknown,
        unknowns=tuple(unknowns),
    )


def _add_item_by_effect(items: list[tuple[int, int, int]], tables: GameTables, effect: int) -> None:
    for item in tables.items:
        if item.hold_effect == effect:
            items.append((item.item_id, effect, item.hold_effect_param))
            return


# -- the integer core: CalculateBaseDamage ------------------------------------------------------


@dataclass(frozen=True)
class _Ctx:
    """Everything CalculateBaseDamage reads besides the two battlers (one scenario)."""

    move: MoveData
    move_type: int
    power: int
    crit: bool
    dmg_multiplier: int
    reflect: bool
    light_screen: bool
    double: bool
    foes_alive: int
    weather: str  # effective weather (already negated when Cloud Nine / Air Lock)
    flash_fire: bool
    mud_sport: bool
    water_sport: bool
    badge_atk: bool  # attacker is player side and owns Boulder badge
    badge_spa: bool
    badge_def: bool  # defender is player side and owns Soul badge
    badge_spd: bool
    plus_minus_partner: bool  # the "other" Plus/Minus ability is on the field
    helping_hand: bool
    charged: bool
    foresight: bool
    solar_beam_halved: bool


def base_damage(att: _Mon, dfn: _Mon, c: _Ctx) -> int:
    """Port of src/pokemon.c ``CalculateBaseDamage`` (the value returned, i.e. ``damage + 2``)."""
    move_type = c.move_type
    power = c.power
    attack, defense = att.atk, dfn.def_
    sp_attack, sp_defense = att.spa, dfn.spd

    if att.ability in (I.ABILITY_HUGE_POWER, I.ABILITY_PURE_POWER):
        attack *= 2

    # ShouldGetStatBadgeBoost: Boulder (atk), Soul (def), Volcano (spa and spd)
    if c.badge_atk:
        attack = (110 * attack) // 100
    if c.badge_def:
        defense = (110 * defense) // 100
    if c.badge_spa:
        sp_attack = (110 * sp_attack) // 100
    if c.badge_spd:
        sp_defense = (110 * sp_defense) // 100

    # sHoldEffectToType: attack * (param + 100) / 100 on the stat that matches the move type
    if _TYPE_BOOST_ITEMS.get(att.hold_effect) == move_type:
        if K.is_physical_type(move_type):
            attack = (attack * (att.hold_param + 100)) // 100
        else:
            sp_attack = (sp_attack * (att.hold_param + 100)) // 100

    if att.hold_effect == I.HOLD_EFFECT_CHOICE_BAND:
        attack = (150 * attack) // 100
    if att.hold_effect == I.HOLD_EFFECT_SOUL_DEW and att.species in (
        I.SPECIES_LATIAS,
        I.SPECIES_LATIOS,
    ):
        sp_attack = (150 * sp_attack) // 100
    if dfn.hold_effect == I.HOLD_EFFECT_SOUL_DEW and dfn.species in (
        I.SPECIES_LATIAS,
        I.SPECIES_LATIOS,
    ):
        sp_defense = (150 * sp_defense) // 100
    if att.hold_effect == I.HOLD_EFFECT_DEEP_SEA_TOOTH and att.species == I.SPECIES_CLAMPERL:
        sp_attack *= 2
    if dfn.hold_effect == I.HOLD_EFFECT_DEEP_SEA_SCALE and dfn.species == I.SPECIES_CLAMPERL:
        sp_defense *= 2
    if att.hold_effect == I.HOLD_EFFECT_LIGHT_BALL and att.species == I.SPECIES_PIKACHU:
        sp_attack *= 2
    if dfn.hold_effect == I.HOLD_EFFECT_METAL_POWDER and dfn.species == I.SPECIES_DITTO:
        defense *= 2
    if att.hold_effect == I.HOLD_EFFECT_THICK_CLUB and att.species in (
        I.SPECIES_CUBONE,
        I.SPECIES_MAROWAK,
    ):
        attack *= 2
    if dfn.ability == I.ABILITY_THICK_FAT and move_type in (K.TYPE_FIRE, K.TYPE_ICE):
        sp_attack //= 2
    if att.ability == I.ABILITY_HUSTLE:
        attack = (150 * attack) // 100
    if att.ability in (I.ABILITY_PLUS, I.ABILITY_MINUS) and c.plus_minus_partner:
        sp_attack = (150 * sp_attack) // 100
    if att.ability == I.ABILITY_GUTS and att.status != K.STATUS_NONE:
        attack = (150 * attack) // 100
    if dfn.ability == I.ABILITY_MARVEL_SCALE and dfn.status != K.STATUS_NONE:
        defense = (150 * defense) // 100
    if move_type == K.TYPE_ELECTRIC and c.mud_sport:
        power //= 2
    if move_type == K.TYPE_FIRE and c.water_sport:
        power //= 2
    low_hp = att.hp <= att.max_hp // 3
    if (
        (move_type == K.TYPE_GRASS and att.ability == I.ABILITY_OVERGROW)
        or (move_type == K.TYPE_FIRE and att.ability == I.ABILITY_BLAZE)
        or (move_type == K.TYPE_WATER and att.ability == I.ABILITY_TORRENT)
        or (move_type == K.TYPE_BUG and att.ability == I.ABILITY_SWARM)
    ) and low_hp:
        power = (150 * power) // 100

    if c.move.effect == I.EFFECT_EXPLOSION:  # Self-Destruct / Explosion halve Defense
        defense //= 2

    damage = 0
    if K.is_physical_type(move_type):
        if c.crit:  # a crit ignores the attacker's drops and the defender's boosts
            damage = _stat_stage_apply(attack, att.stages.atk) if att.stages.atk > 0 else attack
        else:
            damage = _stat_stage_apply(attack, att.stages.atk)
        damage = damage * power
        damage *= 2 * att.level // 5 + 2
        if c.crit:
            helper = _stat_stage_apply(defense, dfn.stages.def_) if dfn.stages.def_ < 0 else defense
        else:
            helper = _stat_stage_apply(defense, dfn.stages.def_)
        damage = damage // helper
        damage //= 50
        if att.status == K.STATUS_BURN and att.ability != I.ABILITY_GUTS:
            damage //= 2
        if c.reflect and not c.crit:
            if c.double and c.foes_alive == 2:
                damage = 2 * (damage // 3)
            else:
                damage //= 2
        if c.double and c.move.target == K.MOVE_TARGET_BOTH and c.foes_alive == 2:
            damage //= 2
        if damage == 0:
            damage = 1

    if move_type == K.TYPE_MYSTERY:
        damage = 0

    if K.is_special_type(move_type):
        if c.crit:
            damage = (
                _stat_stage_apply(sp_attack, att.stages.spa) if att.stages.spa > 0 else sp_attack
            )
        else:
            damage = _stat_stage_apply(sp_attack, att.stages.spa)
        damage = damage * power
        damage *= 2 * att.level // 5 + 2
        if c.crit:
            helper = (
                _stat_stage_apply(sp_defense, dfn.stages.spd) if dfn.stages.spd < 0 else sp_defense
            )
        else:
            helper = _stat_stage_apply(sp_defense, dfn.stages.spd)
        damage = damage // helper
        damage //= 50
        if c.light_screen and not c.crit:
            if c.double and c.foes_alive == 2:
                damage = 2 * (damage // 3)
            else:
                damage //= 2
        if c.double and c.move.target == K.MOVE_TARGET_BOTH and c.foes_alive == 2:
            damage //= 2
        # WEATHER_HAS_EFFECT2 is resolved by the caller into c.weather
        if c.weather == K.WEATHER_RAIN:
            if move_type == K.TYPE_FIRE:
                damage //= 2
            elif move_type == K.TYPE_WATER:
                damage = (15 * damage) // 10
        if c.solar_beam_halved:  # any weather but sun halves Solar Beam
            damage //= 2
        if c.weather == K.WEATHER_SUN:
            if move_type == K.TYPE_FIRE:
                damage = (15 * damage) // 10
            elif move_type == K.TYPE_WATER:
                damage //= 2
        if c.flash_fire and move_type == K.TYPE_FIRE:
            damage = (15 * damage) // 10

    return damage + 2


def damage_rolls(att: _Mon, dfn: _Mon, c: _Ctx, chart_apply) -> list[int]:
    """The 16 final damage values (rolls 85..100) of one hit: damagecalc, typecalc, roll."""
    dmg = base_damage(att, dfn, c)
    dmg = dmg * (2 if c.crit else 1) * c.dmg_multiplier  # Cmd_damagecalc
    if c.charged and c.move.type == K.TYPE_ELECTRIC:
        dmg *= 2
    if c.helping_hand:
        dmg = dmg * 15 // 10
    if c.move.move_id != I.MOVE_STRUGGLE:  # Cmd_typecalc returns early for Struggle
        if c.move_type in att.types:  # IS_BATTLER_OF_TYPE: STAB
            dmg = dmg * 15 // 10
        if dfn.ability == I.ABILITY_LEVITATE and c.move_type == K.TYPE_GROUND:
            return [0] * len(ROLLS)
        dmg = chart_apply(dmg, c.move_type, dfn.types[0], dfn.types[1], foresight=c.foresight)
    out = []
    for r in ROLLS:  # ApplyRandomDmgMultiplier
        d = dmg
        if d != 0:
            d = d * r // 100
            if d == 0:
                d = 1
        out.append(d)
    return out


# ================================================================================================
# Move plans: dynamic type / power / multi-hit / fixed damage
# ================================================================================================


@dataclass
class _Dyn:
    mtype: int
    model: str  # formula | fixed | variable | none | unknown
    powers_min: tuple[int, ...] = ()
    powers_max: tuple[int, ...] = ()
    mult_min: int = 1  # sDMG_MULTIPLIER
    mult_max: int = 1
    notes: list[str] = field(default_factory=list)
    uncertain: list[str] = field(default_factory=list)


def _weather_options(f: FieldView, unc: list[str]) -> list[str]:
    """Effective weather: Cloud Nine / Air Lock negate it (WEATHER_HAS_EFFECT)."""
    if f.weather == K.WEATHER_NONE:
        return [K.WEATHER_NONE]
    negated = f.weather_negated
    if f.field_abilities is not None:  # the abilities on the field decide (WEATHER_HAS_EFFECT)
        negated = bool({I.ABILITY_CLOUD_NINE, I.ABILITY_AIR_LOCK} & f.field_abilities)
    if negated is True:
        return [K.WEATHER_NONE]
    if negated is False:
        return [f.weather]
    unc.append("weather_negation_unknown")
    return [f.weather, K.WEATHER_NONE]


def _hidden_power(ivs: tuple[int, ...]) -> tuple[int, int]:
    """src/battle_script_commands.c Cmd_hiddenpowercalc (ivs = hp, atk, def, spe, spa, spd)."""
    hp, atk, dfn, spe, spa, spd = ivs
    power_bits = (
        ((hp & 2) >> 1)
        | ((atk & 2) << 0)
        | ((dfn & 2) << 1)
        | ((spe & 2) << 2)
        | ((spa & 2) << 3)
        | ((spd & 2) << 4)
    )
    type_bits = (
        ((hp & 1) << 0)
        | ((atk & 1) << 1)
        | ((dfn & 1) << 2)
        | ((spe & 1) << 3)
        | ((spa & 1) << 4)
        | ((spd & 1) << 5)
    )
    power = (40 * power_bits) // 63 + 30
    mtype = ((K.NUMBER_OF_MON_TYPES - 3) * type_bits) // 63 + 1
    if mtype >= K.TYPE_MYSTERY:
        mtype += 1
    return power, mtype


def _flail_power(hp: int, max_hp: int) -> int:
    """sFlailHpScaleToPowerTable with GetScaledHPFraction(hp, max_hp, 48)."""
    frac = hp * 48 // max_hp
    if frac == 0 and hp > 0:
        frac = 1
    for threshold, power in ((1, 200), (4, 150), (9, 100), (16, 80), (32, 40), (48, 20)):
        if frac <= threshold:
            return power
    return 20


def _dynamic(
    m: MoveData,
    att: BattlerView,
    dfn: BattlerView,
    f: FieldView,
    weather: str,
    att_hp: tuple[int, int] | None,
    att_max: tuple[int, int] | None,
) -> _Dyn:
    eff = m.effect
    mtype = m.type
    if eff in _FIXED_DAMAGE:
        return _Dyn(mtype, "fixed")
    if eff in _VARIABLE_DAMAGE:
        d = _Dyn(mtype, "variable")
        d.uncertain.append(f"variable_damage:{eff}")
        return d
    if m.power == 0:
        return _Dyn(mtype, "none")

    power = m.power
    d = _Dyn(mtype, "formula", (power,), (power,))
    if eff == I.EFFECT_MULTI_HIT:
        d.powers_min, d.powers_max = (power,) * 2, (power,) * 5
    elif eff in (I.EFFECT_DOUBLE_HIT, I.EFFECT_TWINEEDLE):
        d.powers_min = d.powers_max = (power,) * 2
    elif eff == I.EFFECT_TRIPLE_KICK:
        d.powers_min, d.powers_max = (10,), (10, 20, 30)
        d.uncertain.append("triple_kick_hits")
    elif eff in (I.EFFECT_RETURN, I.EFFECT_FRUSTRATION):
        if att.friendship is None:
            lo, hi = (1, 102) if eff == I.EFFECT_RETURN else (1, 102)
            d.uncertain.append("friendship_unknown")
        else:
            val = (
                10 * att.friendship // 25
                if eff == I.EFFECT_RETURN
                else 10 * (255 - att.friendship) // 25
            )
            lo = hi = val or power  # a dynamic power of 0 falls back to the table power
        d.powers_min, d.powers_max = (lo,), (hi,)
    elif eff == I.EFFECT_FLAIL:
        if att_hp is None or att_max is None:
            d.powers_min, d.powers_max = (20,), (200,)
            d.uncertain.append("attacker_hp_unknown")
        else:
            lo = _flail_power(att_hp[1], max(1, att_max[0]))  # more HP -> lower power
            hi = _flail_power(att_hp[0], max(1, att_max[1]))
            d.powers_min, d.powers_max = (min(lo, hi),), (max(lo, hi),)
    elif eff == I.EFFECT_ERUPTION:
        if att_hp is None or att_max is None:
            d.powers_min, d.powers_max = (1,), (150,)
            d.uncertain.append("attacker_hp_unknown")
        else:  # Cmd_scaledamagebyhealthratio: hp * power / maxHP, minimum 1
            d.powers_min = (max(1, att_hp[0] * power // max(1, att_max[1])),)
            d.powers_max = (max(1, att_hp[1] * power // max(1, att_max[0])),)
    elif eff == I.EFFECT_FACADE:
        if att.status in (K.STATUS_POISON, K.STATUS_BURN, K.STATUS_PARALYSIS, K.STATUS_TOXIC):
            d.mult_min = d.mult_max = 2
    elif eff == I.EFFECT_SMELLINGSALT:
        if dfn.status == K.STATUS_PARALYSIS and K.V_SUBSTITUTE not in dfn.volatile:
            d.mult_min = d.mult_max = 2
    elif eff == I.EFFECT_REVENGE:
        d.mult_min, d.mult_max = 1, 2
        d.uncertain.append("revenge_needs_damage_taken_this_turn")
    elif eff in _POWER_BOUNDS:
        lo, hi = _POWER_BOUNDS[eff]
        d.powers_min, d.powers_max = (lo,), (hi,)
        d.uncertain.append(f"variable_power:{eff}")
    elif eff == I.EFFECT_HIDDEN_POWER:
        if att.ivs is None:
            return _Dyn(mtype, "unknown", uncertain=["hidden_power_ivs_unknown"])
        hp_power, hp_type = _hidden_power(att.ivs)
        d.mtype = hp_type
        d.powers_min = d.powers_max = (hp_power,)
    elif eff == I.EFFECT_WEATHER_BALL:
        if weather != K.WEATHER_NONE:
            d.mult_min = d.mult_max = 2
            d.mtype = {
                K.WEATHER_RAIN: K.TYPE_WATER,
                K.WEATHER_SANDSTORM: K.TYPE_ROCK,
                K.WEATHER_SUN: K.TYPE_FIRE,
                K.WEATHER_HAIL: K.TYPE_ICE,
            }[weather]
    elif eff in (I.EFFECT_GUST, I.EFFECT_TWISTER) and K.V_ON_AIR in dfn.volatile:
        d.mult_min = d.mult_max = 2
    elif eff in (I.EFFECT_EARTHQUAKE, I.EFFECT_MAGNITUDE) and K.V_UNDERGROUND in dfn.volatile:
        d.mult_min = d.mult_max = 2
    elif eff == I.EFFECT_FLINCH_MINIMIZE_HIT and K.V_MINIMIZED in dfn.volatile:
        d.mult_min = d.mult_max = 2
    elif (
        m.move_id == I.MOVE_SURF or m.move_id == I.MOVE_WHIRLPOOL
    ) and K.V_UNDERWATER in dfn.volatile:
        d.mult_min = d.mult_max = 2
    elif eff == I.EFFECT_SOLAR_BEAM:
        d.notes.append("two_turn")
    return d


# ================================================================================================
# Scenario evaluation
# ================================================================================================

_SOUND_MOVES = {
    I.MOVE_GROWL,
    I.MOVE_ROAR,
    I.MOVE_SING,
    I.MOVE_SUPERSONIC,
    I.MOVE_SCREECH,
    I.MOVE_SNORE,
    I.MOVE_UPROAR,
    I.MOVE_METAL_SOUND,
    I.MOVE_GRASS_WHISTLE,
    I.MOVE_HYPER_VOICE,
}  # src/battle_util.c sSoundMovesTable
_TYPECALC_STATUS = {I.EFFECT_PARALYZE}  # status moves whose script runs typecalc (Thunder Wave)


@dataclass
class _Rec:
    """Outcome of one scenario."""

    key: dict[str, int]  # dimension name -> index of the chosen candidate
    lo: int  # non-crit, min plan, lowest roll
    hi: int  # non-crit, max plan, highest roll
    clo: int
    chi: int
    rolls_min: list[list[int]]
    rolls_max: list[list[int]]
    effectiveness: float
    no_effect: bool
    reason: str | None


def _mon_from(
    res: _Resolved,
    *,
    stat_pick: int,
    ability: int,
    item: tuple[int, int, int],
    hp_pick: int,
    is_player: bool,
) -> _Mon:
    def pick(key: str) -> int:
        lo, hi = res.stats[key]
        return lo if stat_pick == 0 else hi

    hp = res.hp[hp_pick] if res.hp is not None else (res.max_hp[1] if res.max_hp else 1)
    max_hp = res.max_hp[hp_pick] if res.max_hp is not None else max(hp, 1)
    view = res.view
    return _Mon(
        species=view.species or 0,
        level=res.level or 1,
        hp=hp,
        max_hp=max(max_hp, hp, 1),
        atk=pick("atk"),
        def_=pick("def"),
        spa=pick("spa"),
        spd=pick("spd"),
        spe=pick("spe"),
        stages=view.stat_stages,
        status=view.status,
        types=res.types or (K.TYPE_NORMAL, K.TYPE_NORMAL),
        ability=ability,
        hold_effect=item[1],
        hold_param=item[2],
        volatile=view.volatile,
        is_player=is_player,
        friendship=view.friendship,
        ivs=view.ivs,
    )


def _wonder_guard_blocks(effectiveness: float) -> bool:
    """Wonder Guard lets only net super-effective hits through (Cmd_typecalc)."""
    return effectiveness <= 1.0


def _evaluate(
    att: _Mon,
    dfn: _Mon,
    m: MoveData,
    dyn: _Dyn,
    base_ctx: _Ctx,
    chart,
    key: dict[str, int],
) -> _Rec:
    """One scenario: per-hit rolls for the min and max plan, immunity checks, crit variants."""
    mtype = dyn.mtype
    reason: str | None = None
    # --- immunities decided before any damage (attackcanceler / typecalc)
    absorbed = (
        (dfn.ability == I.ABILITY_VOLT_ABSORB and mtype == K.TYPE_ELECTRIC and m.power != 0)
        or (dfn.ability == I.ABILITY_WATER_ABSORB and mtype == K.TYPE_WATER and m.power != 0)
        or (
            dfn.ability == I.ABILITY_FLASH_FIRE
            and mtype == K.TYPE_FIRE
            and dfn.status != K.STATUS_FREEZE
        )
    )
    if absorbed:
        reason = "absorbed by the ability"
    if dfn.ability == I.ABILITY_SOUNDPROOF and m.move_id in _SOUND_MOVES:
        absorbed, reason = True, "blocked by Soundproof"
    uses_chart = m.move_id != I.MOVE_STRUGGLE
    eff = 1.0
    if uses_chart:
        if dfn.ability == I.ABILITY_LEVITATE and mtype == K.TYPE_GROUND:
            eff, reason = 0.0, reason or "Levitate"
        else:
            eff = chart.multiplier(mtype, dfn.types[0], dfn.types[1], foresight=base_ctx.foresight)
            if eff == 0.0:
                reason = reason or "type"
    wonder = (
        dfn.ability == I.ABILITY_WONDER_GUARD
        and m.power != 0
        and uses_chart
        and _wonder_guard_blocks(eff)
    )
    if wonder and eff != 0.0:
        reason = reason or "Wonder Guard"
    no_effect = absorbed or eff == 0.0 or wonder

    def total(powers: tuple[int, ...], mult: int, crit: bool) -> list[list[int]]:
        out = []
        for p in powers:
            ctx = replace(base_ctx, power=p, dmg_multiplier=mult, crit=crit, move_type=mtype)
            out.append(damage_rolls(att, dfn, ctx, chart.apply))
        return out

    if no_effect:
        zero = [[0] * len(ROLLS) for _ in dyn.powers_min]
        zero_max = [[0] * len(ROLLS) for _ in dyn.powers_max]
        return _Rec(key, 0, 0, 0, 0, zero, zero_max, eff, True, reason)
    rolls_min = total(dyn.powers_min, dyn.mult_min, False)
    rolls_max = total(dyn.powers_max, dyn.mult_max, False)
    crolls_min = total(dyn.powers_min, dyn.mult_min, True)
    crolls_max = total(dyn.powers_max, dyn.mult_max, True)
    return _Rec(
        key,
        lo=sum(r[0] for r in rolls_min),
        hi=sum(r[-1] for r in rolls_max),
        clo=sum(r[0] for r in crolls_min),
        chi=sum(r[-1] for r in crolls_max),
        rolls_min=rolls_min,
        rolls_max=rolls_max,
        effectiveness=eff,
        no_effect=False,
        reason=None,
    )


def _ko_probability(roll_lists: list[list[int]], hp: int) -> float:
    """P(sum of independent uniform rolls >= hp). Each list has 16 equiprobable values."""
    if hp <= 0:
        return 1.0
    dist: dict[int, float] = {0: 1.0}
    for rolls in roll_lists:
        nxt: dict[int, float] = {}
        weight = 1.0 / len(rolls)
        for total_so_far, p in dist.items():
            for r in rolls:
                nxt[total_so_far + r] = nxt.get(total_so_far + r, 0.0) + p * weight
        dist = nxt
    return float(sum(p for total_, p in dist.items() if total_ >= hp))


# ================================================================================================
# Accuracy, crit, speed
# ================================================================================================


def _accuracy_range(
    m: MoveData,
    A: _Resolved,
    D: _Resolved,
    f: FieldView,
    weather_opts: list[str],
    dyn: _Dyn,
) -> tuple[tuple[int, int], bool, list[str]]:
    """src/battle_script_commands.c Cmd_accuracycheck / AccuracyCalcHelper."""
    notes: list[str] = []
    dv = D.view
    # semi-invulnerable target (AccuracyCalcHelper)
    exempt = (
        (K.V_ON_AIR in dv.volatile and m.effect not in _HITS_ON_AIR)
        or (K.V_UNDERGROUND in dv.volatile and m.effect not in _HITS_UNDERGROUND)
        or (K.V_UNDERWATER in dv.volatile and m.move_id not in (I.MOVE_SURF, I.MOVE_WHIRLPOOL))
    )
    targets_foe = m.target in (
        K.MOVE_TARGET_SELECTED,
        K.MOVE_TARGET_BOTH,
        K.MOVE_TARGET_RANDOM,
        K.MOVE_TARGET_FOES_AND_ALLY,
        K.MOVE_TARGET_DEPENDS,
    )
    if exempt and targets_foe:
        notes.append("target_semi_invulnerable")
        return (0, 0), False, notes
    if m.accuracy == 0 or m.effect in (I.EFFECT_ALWAYS_HIT, I.EFFECT_VITAL_THROW):
        return (100, 100), True, notes
    if K.V_ALWAYS_HITS in dv.volatile:
        notes.append("lock_on")
        return (100, 100), True, notes
    if m.effect == I.EFFECT_OHKO:
        al, dl = A.level, D.level
        if al is None or dl is None:
            return (0, 100), False, ["level_unknown"]
        if al < dl:
            return (0, 0), False, ["attacker_level_below_target"]
        chance = m.accuracy + (al - dl)
        pct = max(0, min(100, chance - 1))  # Random() % 100 + 1 < chance
        return (pct, pct), False, notes
    lo, hi = 101, -1
    ast, est = A.view.stat_stages.acc, D.view.stat_stages.eva
    foresight = K.V_FORESIGHT in dv.volatile
    buff = (ast + 6) if foresight else (ast + 6 - est)
    buff = max(0, min(12, buff))
    physical = K.is_physical_type(dyn.mtype)
    for weather, a_ab, d_ab, d_item in product(weather_opts, A.abilities, D.abilities, D.items):
        base_acc = m.accuracy
        if weather == K.WEATHER_SUN and m.effect == I.EFFECT_THUNDER:
            base_acc = 50
        if weather == K.WEATHER_RAIN and m.effect == I.EFFECT_THUNDER:
            lo, hi = min(lo, 100), max(hi, 100)  # AccuracyCalcHelper: Thunder never misses in rain
            continue
        num, den = K.ACCURACY_STAGE_RATIOS[buff]
        calc = num * base_acc // den
        if a_ab == I.ABILITY_COMPOUND_EYES:
            calc = calc * 130 // 100
        if weather == K.WEATHER_SANDSTORM and d_ab == I.ABILITY_SAND_VEIL:
            calc = calc * 80 // 100
        if a_ab == I.ABILITY_HUSTLE and physical:
            calc = calc * 80 // 100
        if d_item[1] == I.HOLD_EFFECT_EVASION_UP:
            calc = calc * (100 - d_item[2]) // 100
        pct = max(0, min(100, calc))  # hit if Random() % 100 + 1 <= calc
        lo, hi = min(lo, pct), max(hi, pct)
    return (lo, hi), False, notes


def _crit_range(m: MoveData, A: _Resolved, D: _Resolved, dyn: _Dyn) -> tuple[float, float]:
    """src/battle_script_commands.c Cmd_critcalc (Gen-3: x2, chance 1/16, 1/8, 1/4, 1/3, 1/2)."""
    if dyn.model != "formula":
        return (0.0, 0.0)
    lo, hi = 1.0, 0.0
    for item, d_ab in product(A.items, D.abilities):
        eff = item[1]
        stage = (
            2 * (K.V_FOCUS_ENERGY in A.view.volatile)
            + (m.effect in _HIGH_CRIT)
            + (eff == I.HOLD_EFFECT_SCOPE_LENS)
            + 2 * (eff == I.HOLD_EFFECT_LUCKY_PUNCH and A.view.species == I.SPECIES_CHANSEY)
            + 2 * (eff == I.HOLD_EFFECT_STICK and A.view.species == I.SPECIES_FARFETCHD)
        )
        stage = min(stage, len(K.CRIT_CHANCE_DENOMINATORS) - 1)
        p = (
            0.0
            if d_ab in (I.ABILITY_BATTLE_ARMOR, I.ABILITY_SHELL_ARMOR)
            else 1 / (K.CRIT_CHANCE_DENOMINATORS[stage])
        )
        lo, hi = min(lo, p), max(hi, p)
    return (lo, hi)


def _speed_range(
    R: _Resolved, f: FieldView, weather_opts: list[str], badge_opts: list[bool]
) -> tuple[tuple[int, int], bool]:
    """src/battle_main.c GetWhoStrikesFirst, one battler. Returns ((lo, hi), has_quick_claw)."""
    lo, hi = 10**9, 0
    quick_claw = False
    spe_lo, spe_hi = R.stats["spe"]
    stage = R.view.stat_stages.spe
    for weather, ab, item, badge, spe in product(
        weather_opts, R.abilities, R.items, badge_opts, (spe_lo, spe_hi)
    ):
        mult = (
            2
            if (ab == I.ABILITY_SWIFT_SWIM and weather == K.WEATHER_RAIN)
            or (ab == I.ABILITY_CHLOROPHYLL and weather == K.WEATHER_SUN)
            else 1
        )
        speed = spe * mult
        speed = _stat_stage_apply(speed, stage)
        if badge and R.view.is_player_side and f.kind != K.KIND_LINK:
            speed = speed * 110 // 100  # Thunder badge
        if item[1] == I.HOLD_EFFECT_MACHO_BRACE:
            speed //= 2
        if R.view.status == K.STATUS_PARALYSIS:
            speed //= 4
        if item[1] == I.HOLD_EFFECT_QUICK_CLAW:
            quick_claw = True
        lo, hi = min(lo, speed), max(hi, speed)
    return (lo, hi), quick_claw


def _badge_options(f: FieldView, unc: list[str]) -> list[tuple[bool, bool, bool, bool]]:
    """(atk Boulder, def Soul, spa/spd Volcano flags) as owned-badge tuples, or both outcomes."""
    if f.kind == K.KIND_LINK:
        return [(False, False, False, False)]
    if f.badges is None:
        unc.append("badge_boosts_unknown")
        return [(False, False, False, False), (True, True, True, True)]
    b = f.badges
    return [
        (
            K.BADGE_BOULDER in b,
            K.BADGE_SOUL in b,
            K.BADGE_VOLCANO in b,
            K.BADGE_VOLCANO in b,
        )
    ]


# ================================================================================================
# status / effect facts
# ================================================================================================

_STATUS_ABILITY_BLOCK = {
    K.STATUS_SLEEP: (I.ABILITY_INSOMNIA, I.ABILITY_VITAL_SPIRIT),
    K.STATUS_PARALYSIS: (I.ABILITY_LIMBER,),
    K.STATUS_POISON: (I.ABILITY_IMMUNITY,),
    K.STATUS_TOXIC: (I.ABILITY_IMMUNITY,),
    K.STATUS_FREEZE: (I.ABILITY_MAGMA_ARMOR,),
    K.STATUS_BURN: (I.ABILITY_WATER_VEIL,),
    "confusion": (I.ABILITY_OWN_TEMPO,),
}
_STATUS_TYPE_IMMUNE = {
    K.STATUS_BURN: (K.TYPE_FIRE,),
    K.STATUS_FREEZE: (K.TYPE_ICE,),
    K.STATUS_POISON: (K.TYPE_POISON, K.TYPE_STEEL),
    K.STATUS_TOXIC: (K.TYPE_POISON, K.TYPE_STEEL),
}


def _status_blocker(
    status: str, D: _Resolved, f: FieldView, *, secondary: bool, unc: list[str]
) -> str | None:
    major = status in (
        K.STATUS_SLEEP,
        K.STATUS_POISON,
        K.STATUS_BURN,
        K.STATUS_FREEZE,
        K.STATUS_PARALYSIS,
        K.STATUS_TOXIC,
    )
    if major and D.view.status != K.STATUS_NONE:
        return f"target already has a status ({D.view.status})"
    if status == "confusion" and K.V_CONFUSION in D.view.volatile:
        return "target is already confused"
    if D.types is not None and any(t in _STATUS_TYPE_IMMUNE.get(status, ()) for t in D.types):
        return "target type is immune"
    side = f.side_of(D.view)
    if (major or status == "confusion") and side.has(K.SIDE_SAFEGUARD):
        return "Safeguard on the target side"
    if not secondary and K.V_SUBSTITUTE in D.view.volatile:
        return "target has a Substitute"
    blockers = _STATUS_ABILITY_BLOCK.get(status, ())
    if secondary and I.ABILITY_SHIELD_DUST in D.abilities:
        blockers = (*blockers, I.ABILITY_SHIELD_DUST)
    if blockers:
        hits = [a for a in D.abilities if a in blockers]
        if hits and len(hits) == len(D.abilities):
            return "target ability blocks it"
        if hits:
            unc.append("status_may_be_blocked_by_ability")
    return None


# ================================================================================================
# the estimator
# ================================================================================================


def _unknown_estimate(
    m: MoveData, mtype: int, category: str, reasons: list[str], notes: list[str]
) -> DamageEstimate:
    return DamageEstimate(
        move_id=m.move_id,
        move_type=mtype,
        category=category,
        damage_model="unknown",
        power=None,
        min_damage=0,
        max_damage=0,
        crit_min=0,
        crit_max=0,
        min_share=None,
        max_share=None,
        min_share_of_max_hp=None,
        max_share_of_max_hp=None,
        defender_hp=None,
        defender_max_hp=None,
        ohko_possible=False,
        ko_certain=False,
        ko_probability=(0.0, 1.0),
        hits_to_ko=None,
        accuracy=(0, 100),
        always_hits=False,
        effectiveness=1.0,
        stab=False,
        no_effect=False,
        no_effect_reason=None,
        crit_chance=(0.0, 1.0),
        recoil_fraction_of_damage=0.0,
        recoil_fraction_of_max_hp=0.0,
        self_ko=False,
        inflicts_status=None,
        status_chance=None,
        status_blocked_by=None,
        stat_changes=(),
        priority=m.priority,
        faster="unknown",
        speed_attacker=(0, 0),
        speed_defender=(0, 0),
        hits=(1, 1),
        uncertain_modifiers=tuple(dict.fromkeys(reasons)),
        notes=tuple(notes),
    )


def estimate_damage(
    attacker: BattlerView,
    defender: BattlerView,
    move: int | MoveSlotView,
    tables: GameTables,
    field: FieldView,
) -> DamageEstimate:
    """Estimate one use of ``move`` by ``attacker`` on ``defender`` (see the module docstring)."""
    m = tables.move(_move_id(move))
    chart = tables.type_chart
    unc: list[str] = []
    notes: list[str] = []
    kind = field.kind

    weather_opts = _weather_options(field, unc)
    A = _resolve(attacker, tables, role="attacker", kind=kind, move_type=m.type)
    D = _resolve(defender, tables, role="defender", kind=kind, move_type=m.type)
    dyn = _dynamic(m, attacker, defender, field, weather_opts[0], A.hp, A.max_hp)
    if dyn.mtype != m.type:
        A = _resolve(attacker, tables, role="attacker", kind=kind, move_type=dyn.mtype)
        D = _resolve(defender, tables, role="defender", kind=kind, move_type=dyn.mtype)
    notes.extend(dyn.notes)
    unc.extend(dyn.uncertain)
    unc.extend(u for u in D.unknowns if u.endswith(("_hp_from_percent", "_max_hp_range")))

    category = (
        "status"
        if dyn.model == "none"
        else ("physical" if K.is_physical_type(dyn.mtype) else "special")
    )
    # -- accuracy, crit, speed, effect facts (independent of the damage scenarios)
    accuracy, always_hits, acc_notes = _accuracy_range(m, A, D, field, weather_opts, dyn)
    notes.extend(acc_notes)
    crit_chance = _crit_range(m, A, D, dyn)
    badge_opts = _badge_options(field, unc)
    thunder_opts = [K.BADGE_THUNDER in field.badges] if field.badges is not None else [False, True]
    sa, qa = _speed_range_safe(A, field, weather_opts, thunder_opts)
    sd, qd = _speed_range_safe(D, field, weather_opts, thunder_opts)
    faster = _who_is_faster(m.priority, sa, sd, qa, qd)

    traits = traits_for(m.effect)
    inflicts, chance, blocked = _status_facts(m, traits, A, D, field, dyn, unc)
    stat_changes = _stat_change_facts(m, traits, A)

    stab = dyn.model == "formula" and m.move_id != I.MOVE_STRUGGLE and dyn.mtype in (A.types or ())
    recoil_dmg = 0.25 if m.effect in _RECOIL_25 else (1 / 3 if m.effect in _RECOIL_33 else 0.0)
    if recoil_dmg and attacker.ability == I.ABILITY_ROCK_HEAD:
        recoil_dmg = 0.0
    elif recoil_dmg and attacker.ability is None and I.ABILITY_ROCK_HEAD in A.abilities:
        unc.append("recoil_may_be_prevented_by_rock_head")
    recoil_hp = 0.0
    if m.effect == I.EFFECT_RECOIL_IF_MISS:
        notes.append("crashes_on_miss")
    if m.effect in _TWO_TURN:
        notes.append("charge_turn")
    if m.effect == I.EFFECT_RECHARGE:
        notes.append("must_recharge")
    if m.effect == I.EFFECT_RAMPAGE:
        notes.append("locked_for_2_to_3_turns")
    hits = (len(dyn.powers_min) or 1, len(dyn.powers_max) or 1)

    common = dict(
        move_id=m.move_id,
        move_type=dyn.mtype,
        category=category,
        accuracy=accuracy,
        always_hits=always_hits,
        stab=stab,
        crit_chance=crit_chance,
        recoil_fraction_of_damage=recoil_dmg,
        recoil_fraction_of_max_hp=recoil_hp,
        self_ko=traits.self_ko,
        inflicts_status=inflicts,
        status_chance=chance,
        status_blocked_by=blocked,
        stat_changes=stat_changes,
        priority=m.priority,
        faster=faster,
        speed_attacker=sa,
        speed_defender=sd,
        hits=hits,
        notes=tuple(notes),
    )

    # -- models that bypass the formula
    if dyn.model == "none":
        eff, no_eff, reason = 1.0, False, None
        if m.effect in _TYPECALC_STATUS and D.types is not None:
            eff = chart.multiplier(dyn.mtype, *D.types, foresight=K.V_FORESIGHT in D.view.volatile)
            no_eff = eff == 0.0
            reason = "type" if no_eff else None
        return _finish_none(common, D, eff, no_eff, reason, unc)

    if dyn.model in ("variable", "unknown"):
        return _blank(common, D, dyn.model, unc)

    if dyn.model == "fixed":
        return _fixed_estimate(m, dyn, A, D, field, tables, common, unc)

    # -- formula model
    missing = []
    if A.level is None or not A.stats or A.types is None:
        missing.append("attacker_species_or_level_unknown")
    if D.types is None or not D.stats:
        missing.append("defender_species_unknown")
    if missing:
        return _blank(common, D, "unknown", unc + missing)
    return _formula_estimate(m, dyn, A, D, field, tables, weather_opts, badge_opts, common, unc)


# ------------------------------------------------------------------------------------------------
# helpers of the estimator
# ------------------------------------------------------------------------------------------------


def _speed_range_safe(
    R: _Resolved, f: FieldView, weather_opts: list[str], badge_opts: list[bool]
) -> tuple[tuple[int, int], bool]:
    if "spe" not in R.stats:
        return (0, 0), False
    return _speed_range(R, f, weather_opts, badge_opts)


def _who_is_faster(
    priority: int, sa: tuple[int, int], sd: tuple[int, int], quick_a: bool, quick_d: bool
) -> str:
    """src/battle_main.c GetWhoStrikesFirst; the foe's chosen move is assumed priority 0."""
    if sa == (0, 0) or sd == (0, 0):
        return "unknown"
    if priority > 0:
        return "yes"
    if priority < 0:
        return "no"
    if sa[0] > sd[1]:
        return "unknown" if quick_d else "yes"
    if sa[1] < sd[0]:
        return "unknown" if quick_a else "no"
    return "unknown"  # overlap, or an exact speed tie (random in the game)


def _effective_chance(m: MoveData, A: _Resolved) -> int | None:
    """src/battle_script_commands.c Cmd_seteffectwithchance: ``Random() % 100 <= chance``."""
    base = m.secondary_chance
    if base == 0:
        return None
    if A.abilities == (I.ABILITY_SERENE_GRACE,):
        base *= 2
    return min(100, base + 1)


def _status_facts(
    m: MoveData,
    traits: EffectTraits,
    A: _Resolved,
    D: _Resolved,
    f: FieldView,
    dyn: _Dyn,
    unc: list[str],
) -> tuple[str | None, int | None, str | None]:
    if traits.primary_status is not None:
        status = traits.primary_status
        return status, 100, _status_blocker(status, D, f, secondary=False, unc=unc)
    if traits.secondary_status is not None and dyn.model == "formula":
        status = traits.secondary_status
        chance = _effective_chance(m, A)
        single = status.split("/")[0] if "/" in status else status
        blocker = (
            _status_blocker(single, D, f, secondary=True, unc=unc) if "/" not in status else None
        )
        return status, chance, blocker
    return None, None, None


def _stat_change_facts(
    m: MoveData, traits: EffectTraits, A: _Resolved
) -> tuple[tuple[str, str, int, int | None], ...]:
    out = []
    for who, stat, stages, certain in traits.stat_changes:
        chance = None if certain else _effective_chance(m, A)
        out.append((who, stat, stages, chance))
    return tuple(out)


def _blank(common: dict, D: _Resolved, model: str, unc: list[str]) -> DamageEstimate:
    """No damage number is available: the model says why; speed, accuracy and effects remain."""
    return DamageEstimate(
        damage_model=model,
        power=None,
        min_damage=0,
        max_damage=0,
        crit_min=0,
        crit_max=0,
        min_share=None,
        max_share=None,
        min_share_of_max_hp=None,
        max_share_of_max_hp=None,
        defender_hp=D.hp,
        defender_max_hp=D.max_hp,
        ohko_possible=False,
        ko_certain=False,
        ko_probability=(0.0, 1.0),
        hits_to_ko=None,
        effectiveness=1.0,
        no_effect=False,
        no_effect_reason=None,
        uncertain_modifiers=tuple(dict.fromkeys(unc)),
        **common,
    )


def _finish_none(
    common: dict,
    D: _Resolved,
    eff: float,
    no_eff: bool,
    reason: str | None,
    unc: list[str],
) -> DamageEstimate:
    return DamageEstimate(
        damage_model="none",
        power=None,
        min_damage=0,
        max_damage=0,
        crit_min=0,
        crit_max=0,
        min_share=0.0 if D.hp else None,
        max_share=0.0 if D.hp else None,
        min_share_of_max_hp=0.0 if D.max_hp else None,
        max_share_of_max_hp=0.0 if D.max_hp else None,
        defender_hp=D.hp,
        defender_max_hp=D.max_hp,
        ohko_possible=False,
        ko_certain=False,
        ko_probability=(0.0, 0.0),
        hits_to_ko=None,
        effectiveness=eff,
        no_effect=no_eff,
        no_effect_reason=reason,
        uncertain_modifiers=tuple(dict.fromkeys(unc)),
        **common,
    )


def _shares(
    lo: int, hi: int, D: _Resolved
) -> tuple[float | None, float | None, float | None, float | None]:
    if D.hp is None or D.max_hp is None:
        return None, None, None, None
    hp_lo, hp_hi = max(1, D.hp[0]), max(1, D.hp[1])
    mx_lo, mx_hi = max(1, D.max_hp[0]), max(1, D.max_hp[1])
    return lo / hp_hi, hi / hp_lo, lo / mx_hi, hi / mx_lo


def _hits_to_ko(lo: int, hi: int, D: _Resolved) -> tuple[int, int | None] | None:
    if D.hp is None or hi <= 0:
        return None
    fewest = max(1, -(-D.hp[0] // hi))
    most = None if lo <= 0 else max(1, -(-D.hp[1] // lo))
    return fewest, most


def _fixed_estimate(
    m: MoveData,
    dyn: _Dyn,
    A: _Resolved,
    D: _Resolved,
    f: FieldView,
    tables: GameTables,
    common: dict,
    unc: list[str],
) -> DamageEstimate:
    """DRAGON_RAGE, SONICBOOM, LEVEL_DAMAGE, PSYWAVE, SUPER_FANG, ENDEAVOR, OHKO.

    The scripts run ``typecalc`` for the immunity check, clear the effectiveness flags and set
    the damage directly (data/battle_scripts_1.s); STAB and the roll do not apply.
    """
    eff_id = m.effect
    chart = tables.type_chart
    mtype = dyn.mtype
    if D.types is None:
        return _blank(common, D, "unknown", unc + ["defender_species_unknown"])
    effectiveness = chart.multiplier(mtype, *D.types, foresight=K.V_FORESIGHT in D.view.volatile)
    reasons: list[str] = []
    immune_by_ability = [
        a
        for a in D.abilities
        if (a == I.ABILITY_LEVITATE and mtype == K.TYPE_GROUND)
        or (a == I.ABILITY_WONDER_GUARD and effectiveness <= 1.0)
        or (a == I.ABILITY_STURDY and eff_id == I.EFFECT_OHKO)
    ]
    no_effect = effectiveness == 0.0 or (
        bool(immune_by_ability) and len(immune_by_ability) == len(D.abilities)
    )
    if immune_by_ability and not no_effect:
        unc.append("defender_ability_unknown")
        reasons.append("ability may block it")
    reason = "type" if effectiveness == 0.0 else ("ability" if no_effect else None)

    hp = D.hp
    level = A.level
    lo = hi = 0
    if eff_id == I.EFFECT_DRAGON_RAGE:
        lo = hi = 40
    elif eff_id == I.EFFECT_SONICBOOM:
        lo = hi = 20
    elif eff_id == I.EFFECT_LEVEL_DAMAGE:
        if level is None:
            return _blank(common, D, "unknown", unc + ["attacker_level_unknown"])
        lo = hi = level
    elif eff_id == I.EFFECT_PSYWAVE:
        if level is None:
            return _blank(common, D, "unknown", unc + ["attacker_level_unknown"])
        lo, hi = level * 50 // 100, level * 150 // 100  # level * (10k + 50) / 100, k = 0..10
        lo = max(lo, 0)
    elif eff_id == I.EFFECT_SUPER_FANG:
        if hp is None:
            return _blank(common, D, "unknown", unc + ["defender_hp_unknown"])
        lo, hi = max(1, hp[0] // 2), max(1, hp[1] // 2)
    elif eff_id == I.EFFECT_ENDEAVOR:
        if hp is None or A.hp is None:
            return _blank(common, D, "unknown", unc + ["hp_unknown"])
        lo, hi = max(0, hp[0] - A.hp[1]), max(0, hp[1] - A.hp[0])
    elif eff_id == I.EFFECT_OHKO:
        if hp is None:
            return _blank(common, D, "unknown", unc + ["defender_hp_unknown"])
        lo, hi = hp
    if no_effect:
        lo = hi = 0
    s_lo, s_hi, m_lo, m_hi = _shares(lo, hi, D)
    ko_lo = ko_hi = 0.0
    if hp is not None and not no_effect:
        ko_lo = 1.0 if lo >= hp[1] else 0.0
        ko_hi = 1.0 if hi >= hp[0] else 0.0
    return DamageEstimate(
        damage_model="fixed",
        power=None,
        min_damage=lo,
        max_damage=hi,
        crit_min=lo,
        crit_max=hi,
        min_share=s_lo,
        max_share=s_hi,
        min_share_of_max_hp=m_lo,
        max_share_of_max_hp=m_hi,
        defender_hp=D.hp,
        defender_max_hp=D.max_hp,
        ohko_possible=bool(hp is not None and hi >= hp[0] and hi > 0),
        ko_certain=bool(hp is not None and lo >= hp[1] and lo > 0),
        ko_probability=(ko_lo, ko_hi),
        hits_to_ko=_hits_to_ko(lo, hi, D),
        effectiveness=effectiveness,
        no_effect=no_effect,
        no_effect_reason=reason,
        uncertain_modifiers=tuple(dict.fromkeys(unc)),
        **{**common, "stab": False, "crit_chance": (0.0, 0.0)},
    )


def _formula_estimate(
    m: MoveData,
    dyn: _Dyn,
    A: _Resolved,
    D: _Resolved,
    f: FieldView,
    tables: GameTables,
    weather_opts: list[str],
    badge_opts: list[tuple[bool, bool, bool, bool]],
    common: dict,
    unc: list[str],
) -> DamageEstimate:
    chart = tables.type_chart
    av, dv = A.view, D.view
    d_side = f.side_of(dv)
    foes_alive_opts: list[int] = [2]
    if f.double:
        alive = (f.opponent_side if av.is_player_side else f.player_side).alive_count
        foes_alive_opts = [alive] if alive is not None else [1, 2]
        if alive is None:
            unc.append("foes_alive_unknown")

    plus_minus_opts = [False]
    if av.ability in (I.ABILITY_PLUS, I.ABILITY_MINUS) or (
        av.ability is None and set(A.abilities) & {I.ABILITY_PLUS, I.ABILITY_MINUS}
    ):
        if f.field_abilities is None:
            plus_minus_opts = [False, True]
            unc.append("plus_minus_partner_unknown")
        else:
            plus_minus_opts = [bool(f.field_abilities & {I.ABILITY_PLUS, I.ABILITY_MINUS}) and True]

    stat_ranges_a = len({A.stats[k] for k in ("atk", "spa")}) >= 0 and any(
        lo != hi for lo, hi in A.stats.values()
    )
    stat_ranges_d = any(lo != hi for lo, hi in D.stats.values())
    hp_ranges_a = A.hp is not None and A.hp[0] != A.hp[1]

    dims: dict[str, list[int]] = {
        "a_stats": [0, 1] if stat_ranges_a else [0],
        "d_stats": [0, 1] if stat_ranges_d else [0],
        "a_ab": list(range(len(A.abilities))),
        "d_ab": list(range(len(D.abilities))),
        "a_item": list(range(len(A.items))),
        "d_item": list(range(len(D.items))),
        "badge": list(range(len(badge_opts))),
        "weather": list(range(len(weather_opts))),
        "a_hp": [0, 1] if hp_ranges_a else [0],
        "pm": list(range(len(plus_minus_opts))),
        "alive": list(range(len(foes_alive_opts))),
    }
    names = list(dims)
    recs: list[_Rec] = []
    is_player_a, is_player_d = av.is_player_side, dv.is_player_side
    for combo in product(*(dims[n] for n in names)):
        key = dict(zip(names, combo, strict=True))
        att = _mon_from(
            A,
            stat_pick=key["a_stats"],
            ability=A.abilities[key["a_ab"]],
            item=A.items[key["a_item"]],
            hp_pick=key["a_hp"],
            is_player=is_player_a,
        )
        dfn = _mon_from(
            D,
            stat_pick=key["d_stats"],
            ability=D.abilities[key["d_ab"]],
            item=D.items[key["d_item"]],
            hp_pick=0,
            is_player=is_player_d,
        )
        b_atk, b_def, b_spa, b_spd = badge_opts[key["badge"]]
        weather = weather_opts[key["weather"]]
        ctx = _Ctx(
            move=m,
            move_type=dyn.mtype,
            power=dyn.powers_min[0],
            crit=False,
            dmg_multiplier=1,
            reflect=d_side.has(K.SIDE_REFLECT),
            light_screen=d_side.has(K.SIDE_LIGHT_SCREEN),
            double=f.double,
            foes_alive=foes_alive_opts[key["alive"]],
            weather=weather,
            flash_fire=K.V_FLASH_FIRE in av.volatile,
            mud_sport=f.mud_sport,
            water_sport=f.water_sport,
            badge_atk=b_atk and is_player_a,
            badge_spa=b_spa and is_player_a,
            badge_def=b_def and is_player_d,
            badge_spd=b_spd and is_player_d,
            plus_minus_partner=plus_minus_opts[key["pm"]],
            helping_hand=K.V_HELPING_HAND in av.volatile,
            charged=K.V_CHARGED in av.volatile,
            foresight=K.V_FORESIGHT in dv.volatile,
            solar_beam_halved=(
                m.effect == I.EFFECT_SOLAR_BEAM
                and weather in (K.WEATHER_RAIN, K.WEATHER_SANDSTORM, K.WEATHER_HAIL)
            ),
        )
        recs.append(_evaluate(att, dfn, m, dyn, ctx, chart, key))

    labels = {
        "a_stats": "attacker_stats_range",
        "d_stats": "defender_stats_range",
        "a_ab": "attacker_ability_unknown",
        "d_ab": "defender_ability_unknown",
        "a_item": "attacker_item_unknown",
        "d_item": "defender_item_unknown",
        "badge": "badge_boosts_unknown",
        "weather": "weather_negation_unknown",
        "a_hp": "attacker_hp_range",
        "pm": "plus_minus_partner_unknown",
        "alive": "foes_alive_unknown",
    }

    def envelope(rs: list[_Rec]) -> tuple[int, int, int, int]:
        return (
            min(r.lo for r in rs),
            max(r.hi for r in rs),
            min(r.clo for r in rs),
            max(r.chi for r in rs),
        )

    full = envelope(recs)
    # A dimension is reported only if it actually widened the result.
    for name in names:
        if len(dims[name]) > 1:
            fixed = [r for r in recs if r.key[name] == 0]
            if envelope(fixed) != full:
                unc.append(labels[name])
    # purge flags added earlier that did not matter for this move
    relevant = {
        labels[n]
        for n in names
        if len(dims[n]) > 1 and envelope([r for r in recs if r.key[n] == 0]) != full
    }
    stale = {labels[n] for n in names} - relevant
    unc[:] = [u for u in unc if u not in stale]

    lo, hi, clo, chi = full
    no_effect = all(r.no_effect for r in recs)
    any_no_effect = any(r.no_effect for r in recs)
    reason = next((r.reason for r in recs if r.no_effect), None)
    if any_no_effect and not no_effect:
        unc.append("may_have_no_effect")
    effs = [r.effectiveness for r in recs if not r.no_effect] or [0.0]
    effectiveness = effs[0] if len(set(effs)) == 1 else (min(effs) if min(effs) else max(effs))
    if no_effect:
        effectiveness = recs[0].effectiveness

    hp = D.hp
    s_lo, s_hi, m_lo, m_hi = _shares(lo, hi, D)
    if hp is not None:
        kp_lo = min(_ko_probability(r.rolls_min, hp[1]) for r in recs)
        kp_hi = max(_ko_probability(r.rolls_max, hp[0]) for r in recs)
        ohko = hi >= hp[0] and hi > 0
        certain = lo >= hp[1] and lo > 0
    else:
        kp_lo, kp_hi, ohko, certain = 0.0, 1.0, False, False
    return DamageEstimate(
        damage_model="formula",
        power=dyn.powers_min[0] if dyn.powers_min == dyn.powers_max else None,
        min_damage=lo,
        max_damage=hi,
        crit_min=clo,
        crit_max=chi,
        min_share=s_lo,
        max_share=s_hi,
        min_share_of_max_hp=m_lo,
        max_share_of_max_hp=m_hi,
        defender_hp=D.hp,
        defender_max_hp=D.max_hp,
        ohko_possible=ohko,
        ko_certain=certain,
        ko_probability=(kp_lo, kp_hi),
        hits_to_ko=_hits_to_ko(lo, hi, D),
        effectiveness=effectiveness,
        no_effect=no_effect,
        no_effect_reason=reason,
        uncertain_modifiers=tuple(dict.fromkeys(unc)),
        **common,
    )
