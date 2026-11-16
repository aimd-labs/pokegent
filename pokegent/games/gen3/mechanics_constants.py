"""Small hand-written vocabulary of the FireRed mechanics ruleset.

This module holds the *shapes* of the vocabulary: type ids, status names, weather names, battle
kinds,
move-target masks. Every numeric value cites the pret/pokefirered header it comes from (commit
pinned in ``source-lock.md``).
"""

from __future__ import annotations

# -- types (include/constants/pokemon.h) ---------------------------------------------------
TYPE_NORMAL = 0
TYPE_FIGHTING = 1
TYPE_FLYING = 2
TYPE_POISON = 3
TYPE_GROUND = 4
TYPE_ROCK = 5
TYPE_BUG = 6
TYPE_GHOST = 7
TYPE_STEEL = 8
TYPE_MYSTERY = 9  # the "???" type; also the split point of IS_TYPE_PHYSICAL / IS_TYPE_SPECIAL
TYPE_FIRE = 10
TYPE_WATER = 11
TYPE_GRASS = 12
TYPE_ELECTRIC = 13
TYPE_PSYCHIC = 14
TYPE_ICE = 15
TYPE_DRAGON = 16
TYPE_DARK = 17
NUMBER_OF_MON_TYPES = 18

#: include/battle_main.h: TYPE_FORESIGHT / TYPE_ENDTABLE markers inside gTypeEffectiveness
TYPE_FORESIGHT = 0xFE
TYPE_ENDTABLE = 0xFF

#: English names (gTypeNames in src/battle_main.c; the in-game abbreviations are not used)
TYPE_NAMES: tuple[str, ...] = (
    "normal", "fighting", "flying", "poison", "ground", "rock", "bug", "ghost", "steel",
    "???", "fire", "water", "grass", "electric", "psychic", "ice", "dragon", "dark",
)  # fmt: skip

# include/battle_main.h
TYPE_MUL_NO_EFFECT = 0
TYPE_MUL_NOT_EFFECTIVE = 5
TYPE_MUL_NORMAL = 10
TYPE_MUL_SUPER_EFFECTIVE = 20


def is_physical_type(move_type: int) -> bool:
    """include/battle.h IS_TYPE_PHYSICAL: ``moveType < TYPE_MYSTERY`` (Gen-3 split BY TYPE)."""
    return move_type < TYPE_MYSTERY


def is_special_type(move_type: int) -> bool:
    """include/battle.h IS_TYPE_SPECIAL: ``moveType > TYPE_MYSTERY``."""
    return move_type > TYPE_MYSTERY


# -- stats ----------------------------------------------------------------------------------
STAT_NAMES = ("atk", "def", "spa", "spd", "spe")
STAGE_NAMES = (
    "atk",
    "def",
    "spe",
    "spa",
    "spd",
    "acc",
    "eva",
)  # order of include/constants/pokemon.h
DEFAULT_STAT_STAGE = 6  # game-internal 0..12; the views use signed -6..+6
MIN_STAT_STAGE = 0
MAX_STAT_STAGE = 12

#: src/pokemon.c gStatStageRatios[13][2] (index = stage + 6); verified against the ROM in tests
STAT_STAGE_RATIOS: tuple[tuple[int, int], ...] = (
    (10, 40), (10, 35), (10, 30), (10, 25), (10, 20), (10, 15),
    (10, 10),
    (15, 10), (20, 10), (25, 10), (30, 10), (35, 10), (40, 10),
)  # fmt: skip

#: src/battle_script_commands.c sAccuracyStageRatios[13] = {dividend, divisor}
ACCURACY_STAGE_RATIOS: tuple[tuple[int, int], ...] = (
    (33, 100), (36, 100), (43, 100), (50, 100), (60, 100), (75, 100),
    (1, 1),
    (133, 100), (166, 100), (2, 1), (233, 100), (133, 50), (3, 1),
)  # fmt: skip

#: src/battle_script_commands.c sCriticalHitChance[]: one crit in N (N = 16, 8, 4, 3, 2)
CRIT_CHANCE_DENOMINATORS = (16, 8, 4, 3, 2)

# -- status (views use names; include/constants/battle.h STATUS1_* are the game's bits) -------
STATUS_NONE = "none"
STATUS_SLEEP = "sleep"
STATUS_POISON = "poison"
STATUS_BURN = "burn"
STATUS_FREEZE = "freeze"
STATUS_PARALYSIS = "paralysis"
STATUS_TOXIC = "toxic"
MAJOR_STATUSES = (
    STATUS_NONE, STATUS_SLEEP, STATUS_POISON, STATUS_BURN, STATUS_FREEZE, STATUS_PARALYSIS,
    STATUS_TOXIC,
)  # fmt: skip

#: STATUS1_* bits of include/constants/battle.h (gBattleMons[].status1 / Pokemon.status)
STATUS1_SLEEP_MASK = 0x7
STATUS1_POISON = 0x8
STATUS1_BURN = 0x10
STATUS1_FREEZE = 0x20
STATUS1_PARALYSIS = 0x40
STATUS1_TOXIC_POISON = 0x80

# -- volatile conditions the library understands (BattlerView.volatile) ----------------------
V_CONFUSION = "confusion"
V_FLINCH = "flinch"
V_FOCUS_ENERGY = "focus_energy"
V_SUBSTITUTE = "substitute"
V_LEECH_SEED = "leech_seed"
V_FORESIGHT = "foresight"  # Foresight / Odor Sleuth target: ghost immunities dropped
V_TAUNT = "taunt"
V_TORMENT = "torment"
V_ENCORE = "encore"
V_DISABLE = "disable"
V_IMPRISON = "imprison"  # this battler used Imprison
V_INFATUATION = "infatuation"
V_CHARGED = "charged_up"  # Charge: next Electric move x2
V_HELPING_HAND = "helping_hand"
V_FLASH_FIRE = "flash_fire"  # Flash Fire boost active
V_TRAPPED = "trapped"  # wrapped / bound / Mean Look / Block (cannot switch)
V_ROOTED = "ingrain"
V_RECHARGING = "recharging"  # Hyper Beam recharge turn
V_LOCKED_MOVE = "locked_move"  # rampage / Rollout / two-turn charge in progress
V_UNDERGROUND = "underground"
V_ON_AIR = "on_air"
V_UNDERWATER = "underwater"
V_MINIMIZED = "minimized"
V_DEFENSE_CURL = "defense_curl"
V_ALWAYS_HITS = "always_hits"  # Lock-On / Mind Reader on this target

# -- weather (include/constants/battle.h B_WEATHER_*) ----------------------------------------
WEATHER_NONE = "none"
WEATHER_RAIN = "rain"
WEATHER_SANDSTORM = "sandstorm"
WEATHER_SUN = "sun"
WEATHER_HAIL = "hail"
WEATHERS = (WEATHER_NONE, WEATHER_RAIN, WEATHER_SANDSTORM, WEATHER_SUN, WEATHER_HAIL)

# -- battle kinds (FieldView.kind) ----------------------------------------------------------
KIND_WILD = "wild"
KIND_TRAINER = "trainer"
KIND_SAFARI = "safari"
KIND_GHOST = "ghost"  # Pokemon Tower ghost without the Silph Scope (BATTLE_TYPE_GHOST)
KIND_TUTORIAL = "tutorial"  # Old Man / Pokedude scripted battle
KIND_LINK = "link"
KIND_UNKNOWN = "unknown"
KINDS = (KIND_WILD, KIND_TRAINER, KIND_SAFARI, KIND_GHOST, KIND_TUTORIAL, KIND_LINK, KIND_UNKNOWN)

# -- battle positions (include/constants/battle.h B_POSITION_*) ------------------------------
B_POSITION_PLAYER_LEFT = 0
B_POSITION_OPPONENT_LEFT = 1
B_POSITION_PLAYER_RIGHT = 2
B_POSITION_OPPONENT_RIGHT = 3

# -- move targets (include/constants/battle.h MOVE_TARGET_*) ---------------------------------
MOVE_TARGET_SELECTED = 0
MOVE_TARGET_DEPENDS = 1 << 0
MOVE_TARGET_USER_OR_SELECTED = 1 << 1
MOVE_TARGET_RANDOM = 1 << 2
MOVE_TARGET_BOTH = 1 << 3
MOVE_TARGET_USER = 1 << 4
MOVE_TARGET_FOES_AND_ALLY = 1 << 5
MOVE_TARGET_OPPONENTS_FIELD = 1 << 6

# -- move flags (include/constants/battle.h FLAG_*) ------------------------------------------
FLAG_MAKES_CONTACT = 1 << 0
FLAG_PROTECT_AFFECTED = 1 << 1
FLAG_MAGICCOAT_AFFECTED = 1 << 2
FLAG_SNATCH_AFFECTED = 1 << 3
FLAG_MIRROR_MOVE_AFFECTED = 1 << 4
FLAG_KINGS_ROCK_AFFECTED = 1 << 5

# -- side conditions (names; SideView.conditions) --------------------------------------------
SIDE_REFLECT = "reflect"
SIDE_LIGHT_SCREEN = "light_screen"
SIDE_SAFEGUARD = "safeguard"
SIDE_MIST = "mist"
SIDE_SPIKES = "spikes"

# -- badges (include/constants/flags.h FLAG_BADGE01_GET .. FLAG_BADGE08_GET) -----------------
BADGE_BOULDER = 1  # Brock: x1.1 Attack of the player's battlers
BADGE_CASCADE = 2
BADGE_THUNDER = 3  # Lt. Surge: x1.1 Speed (src/battle_main.c GetWhoStrikesFirst)
BADGE_RAINBOW = 4
BADGE_SOUL = 5  # Koga: x1.1 Defense of the player's battlers
BADGE_MARSH = 6
BADGE_VOLCANO = 7  # Blaine: x1.1 Sp. Atk and Sp. Def of the player's battlers
BADGE_EARTH = 8
