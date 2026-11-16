"""What a move's effect id does besides raw damage (status, stat changes, healing, field).

Pure lookup tables keyed by the decompile's ``EFFECT_*`` ids
(``games.gen3.firered.mechanics_ids``); the move
data itself (power, type, chance) always comes from the ROM tables. Sources:
``data/battle_scripts_1.s`` (``gBattleScriptsForMoveEffects``) and
``SetMoveEffect`` in ``src/battle_script_commands.c``. Only effects whose consequence is
unambiguous are listed; everything else yields no trait (and therefore no fact), never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass

from pokegent.games.gen3 import mechanics_constants as K
from pokegent.games.gen3 import mechanics_ids as I

SELF = "self"
TARGET = "target"

#: effect -> primary status inflicted by a pure status move (always attempted, accuracy only)
PRIMARY_STATUS: dict[int, str] = {
    I.EFFECT_SLEEP: K.STATUS_SLEEP,
    I.EFFECT_POISON: K.STATUS_POISON,
    I.EFFECT_PARALYZE: K.STATUS_PARALYSIS,
    I.EFFECT_TOXIC: K.STATUS_TOXIC,
    I.EFFECT_WILL_O_WISP: K.STATUS_BURN,
    I.EFFECT_CONFUSE: "confusion",
    I.EFFECT_SWAGGER: "confusion",
    I.EFFECT_FLATTER: "confusion",
    I.EFFECT_TEETER_DANCE: "confusion",
    I.EFFECT_YAWN: "drowsy",
    I.EFFECT_ATTRACT: "infatuation",
    I.EFFECT_LEECH_SEED: "leech_seed",
}

#: effect -> secondary status of a damaging move; chance comes from secondaryEffectChance
SECONDARY_STATUS: dict[int, str] = {
    I.EFFECT_POISON_HIT: K.STATUS_POISON,
    I.EFFECT_BURN_HIT: K.STATUS_BURN,
    I.EFFECT_FREEZE_HIT: K.STATUS_FREEZE,
    I.EFFECT_PARALYZE_HIT: K.STATUS_PARALYSIS,
    I.EFFECT_FLINCH_HIT: "flinch",
    I.EFFECT_CONFUSE_HIT: "confusion",
    I.EFFECT_TWINEEDLE: K.STATUS_POISON,
    I.EFFECT_POISON_FANG: K.STATUS_TOXIC,
    I.EFFECT_BLAZE_KICK: K.STATUS_BURN,
    I.EFFECT_THAW_HIT: K.STATUS_BURN,  # Flame Wheel / Sacred Fire: thaws the user, may burn
    I.EFFECT_FLINCH_MINIMIZE_HIT: "flinch",
    I.EFFECT_TWISTER: "flinch",
    I.EFFECT_SKY_ATTACK: "flinch",
    I.EFFECT_FAKE_OUT: "flinch",
    I.EFFECT_TRI_ATTACK: "burn/freeze/paralysis",
}

#: effect -> ((who, stat, stages), ...) for a status move; chance None = always
STAT_STATUS_MOVES: dict[int, tuple[tuple[str, str, int], ...]] = {
    I.EFFECT_ATTACK_UP: ((SELF, "atk", 1),),
    I.EFFECT_DEFENSE_UP: ((SELF, "def", 1),),
    I.EFFECT_SPEED_UP: ((SELF, "spe", 1),),
    I.EFFECT_SPECIAL_ATTACK_UP: ((SELF, "spa", 1),),
    I.EFFECT_SPECIAL_DEFENSE_UP: ((SELF, "spd", 1),),
    I.EFFECT_ACCURACY_UP: ((SELF, "acc", 1),),
    I.EFFECT_EVASION_UP: ((SELF, "eva", 1),),
    I.EFFECT_ATTACK_UP_2: ((SELF, "atk", 2),),
    I.EFFECT_DEFENSE_UP_2: ((SELF, "def", 2),),
    I.EFFECT_SPEED_UP_2: ((SELF, "spe", 2),),
    I.EFFECT_SPECIAL_ATTACK_UP_2: ((SELF, "spa", 2),),
    I.EFFECT_SPECIAL_DEFENSE_UP_2: ((SELF, "spd", 2),),
    I.EFFECT_ACCURACY_UP_2: ((SELF, "acc", 2),),
    I.EFFECT_EVASION_UP_2: ((SELF, "eva", 2),),
    I.EFFECT_ATTACK_DOWN: ((TARGET, "atk", -1),),
    I.EFFECT_DEFENSE_DOWN: ((TARGET, "def", -1),),
    I.EFFECT_SPEED_DOWN: ((TARGET, "spe", -1),),
    I.EFFECT_ACCURACY_DOWN: ((TARGET, "acc", -1),),
    I.EFFECT_EVASION_DOWN: ((TARGET, "eva", -1),),
    I.EFFECT_ATTACK_DOWN_2: ((TARGET, "atk", -2),),
    I.EFFECT_DEFENSE_DOWN_2: ((TARGET, "def", -2),),
    I.EFFECT_SPEED_DOWN_2: ((TARGET, "spe", -2),),
    I.EFFECT_SPECIAL_ATTACK_DOWN_2: ((TARGET, "spa", -2),),
    I.EFFECT_SPECIAL_DEFENSE_DOWN_2: ((TARGET, "spd", -2),),
    I.EFFECT_ACCURACY_DOWN_2: ((TARGET, "acc", -2),),
    I.EFFECT_EVASION_DOWN_2: ((TARGET, "eva", -2),),
    I.EFFECT_BULK_UP: ((SELF, "atk", 1), (SELF, "def", 1)),
    I.EFFECT_CALM_MIND: ((SELF, "spa", 1), (SELF, "spd", 1)),
    I.EFFECT_DRAGON_DANCE: ((SELF, "atk", 1), (SELF, "spe", 1)),
    I.EFFECT_COSMIC_POWER: ((SELF, "def", 1), (SELF, "spd", 1)),
    I.EFFECT_TICKLE: ((TARGET, "atk", -1), (TARGET, "def", -1)),
    I.EFFECT_SWAGGER: ((TARGET, "atk", 2),),
    I.EFFECT_FLATTER: ((TARGET, "spa", 1),),
    I.EFFECT_BELLY_DRUM: ((SELF, "atk", 12),),  # to +6 at the cost of half max HP
}

#: effect -> stat changes of a DAMAGING move: (who, stat, stages, certain)
STAT_HIT_EFFECTS: dict[int, tuple[tuple[str, str, int, bool], ...]] = {
    I.EFFECT_ATTACK_DOWN_HIT: ((TARGET, "atk", -1, False),),
    I.EFFECT_DEFENSE_DOWN_HIT: ((TARGET, "def", -1, False),),
    I.EFFECT_SPEED_DOWN_HIT: ((TARGET, "spe", -1, False),),
    I.EFFECT_SPECIAL_ATTACK_DOWN_HIT: ((TARGET, "spa", -1, False),),
    I.EFFECT_SPECIAL_DEFENSE_DOWN_HIT: ((TARGET, "spd", -1, False),),
    I.EFFECT_ACCURACY_DOWN_HIT: ((TARGET, "acc", -1, False),),
    I.EFFECT_EVASION_DOWN_HIT: ((TARGET, "eva", -1, False),),
    I.EFFECT_DEFENSE_UP_HIT: ((SELF, "def", 1, False),),
    I.EFFECT_ATTACK_UP_HIT: ((SELF, "atk", 1, False),),
    I.EFFECT_ALL_STATS_UP_HIT: tuple(
        (SELF, s, 1, False) for s in ("atk", "def", "spe", "spa", "spd")
    ),
    I.EFFECT_OVERHEAT: ((SELF, "spa", -2, True),),
    I.EFFECT_SUPERPOWER: ((SELF, "atk", -1, True), (SELF, "def", -1, True)),
}

#: effect -> fraction of max HP restored (Recover, Softboiled, Milk Drink: 1/2)
HEAL_HALF = {I.EFFECT_RESTORE_HP, I.EFFECT_SOFTBOILED}
HEAL_WEATHER = {I.EFFECT_MORNING_SUN, I.EFFECT_SYNTHESIS, I.EFFECT_MOONLIGHT}
#: effect -> fraction of damage dealt returned to the user
DRAIN = {I.EFFECT_ABSORB: 0.5, I.EFFECT_DREAM_EATER: 0.5}

SETS_WEATHER = {
    I.EFFECT_RAIN_DANCE: K.WEATHER_RAIN,
    I.EFFECT_SUNNY_DAY: K.WEATHER_SUN,
    I.EFFECT_SANDSTORM: K.WEATHER_SANDSTORM,
    I.EFFECT_HAIL: K.WEATHER_HAIL,
}
SETS_SIDE = {
    I.EFFECT_REFLECT: K.SIDE_REFLECT,
    I.EFFECT_LIGHT_SCREEN: K.SIDE_LIGHT_SCREEN,
    I.EFFECT_SAFEGUARD: K.SIDE_SAFEGUARD,
    I.EFFECT_MIST: K.SIDE_MIST,
    I.EFFECT_SPIKES: K.SIDE_SPIKES,
}
PROTECTS = {I.EFFECT_PROTECT, I.EFFECT_ENDURE}
#: effects whose sole purpose is to run away or phaze: no damage, special rules
PHAZES = {I.EFFECT_ROAR}
#: user faints (Self-Destruct, Explosion; Memento)
SELF_KO = {I.EFFECT_EXPLOSION, I.EFFECT_MEMENTO}
#: two-turn moves (charge turn first); Skull Bash / Solar Beam / Razor Wind / Sky Attack / Fly / Dig
CHARGE = {
    I.EFFECT_RAZOR_WIND,
    I.EFFECT_SKY_ATTACK,
    I.EFFECT_SKULL_BASH,
    I.EFFECT_SOLAR_BEAM,
    I.EFFECT_SEMI_INVULNERABLE,
}
RECHARGE = {I.EFFECT_RECHARGE}  # Hyper Beam: must recharge after a hit
RAMPAGE = {I.EFFECT_RAMPAGE}  # Thrash / Outrage / Petal Dance: locked 2-3 turns, then confused
MULTI_TURN_LOCK = {I.EFFECT_ROLLOUT, I.EFFECT_UPROAR, I.EFFECT_RAMPAGE}


@dataclass(frozen=True)
class EffectTraits:
    primary_status: str | None = None
    secondary_status: str | None = None
    stat_changes: tuple[tuple[str, str, int, bool], ...] = ()  # (who, stat, stages, certain)
    heals_fraction: float | None = None
    heals_with_weather: bool = False
    drain_fraction: float | None = None
    sets_weather: str | None = None
    sets_side: str | None = None
    protects: bool = False
    self_ko: bool = False
    charge_turn: bool = False
    recharge: bool = False
    rampage: bool = False
    phazes: bool = False


def traits_for(effect: int) -> EffectTraits:
    return EffectTraits(
        primary_status=PRIMARY_STATUS.get(effect),
        secondary_status=SECONDARY_STATUS.get(effect),
        stat_changes=tuple(
            (who, stat, stages, True) for who, stat, stages in STAT_STATUS_MOVES.get(effect, ())
        )
        or STAT_HIT_EFFECTS.get(effect, ()),
        heals_fraction=0.5 if effect in HEAL_HALF else (1.0 if effect == I.EFFECT_REST else None),
        heals_with_weather=effect in HEAL_WEATHER,
        drain_fraction=DRAIN.get(effect),
        sets_weather=SETS_WEATHER.get(effect),
        sets_side=SETS_SIDE.get(effect),
        protects=effect in PROTECTS,
        self_ko=effect in SELF_KO,
        charge_turn=effect in CHARGE,
        recharge=effect in RECHARGE,
        rampage=effect in RAMPAGE,
        phazes=effect in PHAZES,
    )
