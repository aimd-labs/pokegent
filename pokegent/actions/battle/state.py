"""Typed battle state block and the pure decoders for the battle structs (spec 4.3 ``battle``).

Layouts come from the pret/pokefirered decompile at the commit pinned in ``source-lock.md``
(``include/pokemon.h`` struct BattlePokemon, ``include/battle.h`` struct DisableStruct and
BattleStruct, ``include/constants/battle.h``). Nothing here touches memory; :mod:`reader` does.

Two rules shape the models:

* The block is gated on ``gMain.inBattle``. Outside a battle ``BattleBlock.active`` is False and
  carries no battler data at all: battle RAM is stale in the overworld (m21 trap 3).
* Discovery policy (CONTEXT.md invariant 8): an opponent mon's exact HP, stats, moves, ability and
  held item are not visible to the player. They are decoded because the mechanics library may need
  them, but only inside :attr:`Battler.technical`. Everything on :class:`BattlerView` and in option
  facts is player-observable (species, level, HP percent, status, visible stat stages).
"""

from __future__ import annotations

import struct
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pokegent.games.gen3.firered.rom.text import decode_gen3_text

SCHEMA = "battle/1.0"

MAX_BATTLERS = 4
MOVE_SLOTS = 4

# include/constants/battle.h: BattleAction values written to gChosenActionByBattler.
B_ACTION_USE_MOVE = 0
B_ACTION_USE_ITEM = 1
B_ACTION_SWITCH = 2
B_ACTION_RUN = 3

# -- struct BattlePokemon (include/pokemon.h, size 0x58) ---------------------------------------
BM_SIZE = 0x58
BM_SPECIES = 0x00
BM_STATS = 0x02  # attack, defense, speed, spAttack, spDefense (u16 each)
BM_MOVES = 0x0C
BM_IVS = 0x14
BM_STAGES = 0x18  # s8[8], stored with +6 bias (6 = neutral)
BM_ABILITY = 0x20
BM_TYPE1 = 0x21
BM_TYPE2 = 0x22
BM_PP = 0x24
BM_HP = 0x28
BM_LEVEL = 0x2A
BM_MAX_HP = 0x2C
BM_ITEM = 0x2E
BM_NICKNAME = 0x30  # 11 bytes
BM_PP_BONUSES = 0x3B
BM_PERSONALITY = 0x48
BM_STATUS1 = 0x4C
BM_STATUS2 = 0x50

#: index -> stat of ``statStages`` (include/constants/pokemon.h STAT_*)
STAGE_NAMES = ("hp", "attack", "defense", "speed", "sp_attack", "sp_defense", "accuracy", "evasion")

# -- struct DisableStruct (include/battle.h, size 0x1C) ----------------------------------------
DS_SIZE = 0x1C
DS_DISABLED_MOVE = 0x04  # u16
DS_ENCORED_MOVE = 0x06  # u16
DS_PROTECT_USES = 0x08
DS_SUBSTITUTE_HP = 0x0A
DS_DISABLE_TIMER = 0x0B  # low nibble
DS_ENCORE_TIMER = 0x0E  # low nibble
DS_TAUNT_TIMER = 0x13  # low nibble
DS_BATTLER_PREVENTING_ESCAPE = 0x14
DS_IS_FIRST_TURN = 0x16

# -- struct BattleStruct (include/battle.h): only the offsets we read --------------------------
BS_CHOSEN_MOVE_POSITIONS = 0x80
BS_MOVE_TARGET = 0x0C
BS_EXP_GETTER_MON_ID = 0x10
BS_SAFARI_ROCK_COUNTER = 0x79
BS_SAFARI_BAIT_COUNTER = 0x7A
BS_SAFARI_ESCAPE_FACTOR = 0x7B
BS_SAFARI_CATCH_FACTOR = 0x7C
BS_RUN_TRIES = 0x6C
BS_ABSENT_BATTLER_FLAGS = 0x91
BS_CHOICED_MOVE = 0xC8  # u16[4]
BS_READ_SIZE = 0xD0

# -- struct BattleScripting (include/battle.h, offsets match constants/battle_script_commands.h)
SCR_BATTLE_STYLE = 0x1D
SCR_LVLUPBOX_STATE = 0x1E
SCR_LEARNMOVE_STATE = 0x1F

# -- gBattleCommunication entries (constants/battle_script_commands.h) -------------------------
COMM_MULTIUSE_STATE = 0
COMM_CURSOR_POSITION = 1
COMM_MSG_DISPLAY = 7

# -- battle script opcodes (gBattleScriptingCommandsTable order, src/battle_script_commands.c) --
OP_PRINTSTRING = 0x10
OP_WAITMESSAGE = 0x12
OP_GETEXP = 0x23
OP_PAUSE = 0x39
OP_END2 = 0x3E
OP_OPENPARTYSCREEN = 0x50
OP_SWITCHHANDLEORDER = 0x51
OP_YESNOBOXLEARNMOVE = 0x5A
OP_YESNOBOXSTOPLEARNING = 0x5B
OP_YESNOBOX = 0x67
OP_DRAWLVLUPBOX = 0x6C
OP_TRYGIVECAUGHTMONNICK = 0xF3
OP_DISPLAYDEXINFO = 0xF2

# -- statuses (include/constants/battle.h) -----------------------------------------------------
STATUS1_SLEEP = 0x7
STATUS1_POISON = 1 << 3
STATUS1_BURN = 1 << 4
STATUS1_FREEZE = 1 << 5
STATUS1_PARALYSIS = 1 << 6
STATUS1_TOXIC = 1 << 7
STATUS2_CONFUSION = 0x7
STATUS2_FLINCHED = 1 << 3
STATUS2_UPROAR = 0x70
STATUS2_BIDE = 0x300
STATUS2_LOCK_CONFUSE = 0xC00
STATUS2_MULTIPLETURNS = 1 << 12
STATUS2_WRAPPED = 0xE000
STATUS2_INFATUATION = 0xF0000
STATUS2_FOCUS_ENERGY = 1 << 20
STATUS2_TRANSFORMED = 1 << 21
STATUS2_RECHARGE = 1 << 22
STATUS2_RAGE = 1 << 23
STATUS2_SUBSTITUTE = 1 << 24
STATUS2_ESCAPE_PREVENTION = 1 << 26
STATUS2_NIGHTMARE = 1 << 27
STATUS2_CURSED = 1 << 28
STATUS2_FORESIGHT = 1 << 29
STATUS2_TORMENT = 1 << 31
STATUS3_ROOTED = 1 << 10
STATUS3_IMPRISONED_OTHERS = 1 << 13
STATUS3_SEMI_INVULNERABLE = (1 << 6) | (1 << 7) | (1 << 18)

# gSideStatuses bits (include/constants/battle.h SIDE_STATUS_*). SPIKES and SAFEGUARD are proven by
# the corpus (the runtime contractsb); REFLECT, LIGHTSCREEN and MIST
# are decompile
# constants no corpus state exercises.
SIDE_REFLECT = 1 << 0
SIDE_LIGHTSCREEN = 1 << 1
SIDE_SPIKES = 1 << 4
SIDE_SAFEGUARD = 1 << 5
SIDE_MIST = 1 << 8
# struct SideTimer (include/battle.h, 12 bytes per side)
ST_SIZE = 12
ST_REFLECT_TIMER = 0
ST_LIGHTSCREEN_TIMER = 2
ST_MIST_TIMER = 4
ST_SAFEGUARD_TIMER = 6
ST_SPIKES_AMOUNT = 10

B_WEATHER_RAIN = 0x07
B_WEATHER_SANDSTORM = 0x18
B_WEATHER_SUN = 0x60
B_WEATHER_HAIL = 0x80

OUTCOME_NAMES = {
    1: "won",
    2: "lost",
    3: "drew",
    4: "ran",
    5: "player_teleported",
    6: "mon_fled",
    7: "caught",
    8: "no_safari_balls",
    9: "forfeited",
    10: "mon_teleported",
}

BattleKind = Literal[
    "wild",
    "trainer",
    "double",
    "safari",
    "ghost",
    "tutorial",
    "link",
    "other_unsupported",
]

# Battles whose decisions are local to this cartridge.  Link battles deliberately stay outside
# the autonomous adapter: a local key press is a protocol decision shared with another console.
LOCAL_BATTLE_KINDS = frozenset({"wild", "trainer", "double", "safari", "ghost", "tutorial"})


class _F(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MoveSlot(_F):
    slot: int
    move_id: int
    pp: int


class DisableInfo(_F):
    disabled_move: int = 0
    disable_timer: int = 0
    encored_move: int = 0
    encore_timer: int = 0
    taunt_timer: int = 0
    substitute_hp: int = 0
    protect_uses: int = 0


class Battler(_F):
    """One ``gBattleMons`` entry plus the per-battler status words. Full RAM truth.

    For the opponent this is the technical (hidden-information) record; use :class:`BattlerView`
    for anything a model may see.
    """

    battler: int
    side: Literal["player", "opponent"]
    present: bool  # not flagged absent (gAbsentBattlerFlags)
    species: int
    level: int
    hp: int
    max_hp: int
    attack: int
    defense: int
    speed: int
    sp_attack: int
    sp_defense: int
    #: ``statStages`` as signed stages -6..+6 in :data:`STAGE_NAMES` order
    stat_stages: tuple[int, ...]
    status1: int
    status2: int
    status3: int
    type1: int
    type2: int
    ability: int
    item: int
    moves: tuple[MoveSlot, ...]
    pp_bonuses: int
    #: Gen-3 encoded nickname decoded at the RAM reader boundary; raw game bytes must not enter
    #: a Pydantic model because JSON serialization assumes UTF-8 for ``bytes`` fields.
    nickname: str = ""
    personality: int = 0
    disable: DisableInfo = Field(default_factory=DisableInfo)
    last_move: int = 0  # gLastMoves[battler]
    chosen_move: int = 0  # gChosenMoveByBattler[battler]
    # : gChosenActionByBattler[battler]: B_ACTION_USE_MOVE, B_ACTION_USE_ITEM,
    # B_ACTION_SWITCH, B_ACTION_RUN (stale between turns)
    chosen_action: int = 0
    choiced_move: int = 0  # gBattleStruct->choicedMove[battler] (Choice Band lock)

    @property
    def alive(self) -> bool:
        return self.hp > 0

    @property
    def hp_percent(self) -> int:
        """HP bar fill, 0..100; at least 1 while alive (the bar never reads empty above 0 HP)."""
        if self.max_hp <= 0:
            return 0
        if self.hp <= 0:
            return 0
        return max(1, min(100, (self.hp * 100) // self.max_hp))

    @property
    def types(self) -> tuple[int, ...]:
        return (self.type1,) if self.type1 == self.type2 else (self.type1, self.type2)


class SideInfo(_F):
    """One side's field conditions: ``gSideStatuses[side]`` plus its ``gSideTimers[side]``.

    Side 0 is the player's, side 1 the opponent's (``battler & 1``)."""

    status: int = 0
    reflect_timer: int = 0
    lightscreen_timer: int = 0
    mist_timer: int = 0
    safeguard_timer: int = 0
    spikes_layers: int = 0

    @property
    def reflect(self) -> bool:
        return bool(self.status & SIDE_REFLECT)

    @property
    def light_screen(self) -> bool:
        return bool(self.status & SIDE_LIGHTSCREEN)

    @property
    def mist(self) -> bool:
        return bool(self.status & SIDE_MIST)

    @property
    def safeguard(self) -> bool:
        return bool(self.status & SIDE_SAFEGUARD)

    @property
    def spikes(self) -> bool:
        return bool(self.status & SIDE_SPIKES)


def decode_sides(statuses: bytes, timers: bytes) -> tuple[SideInfo, SideInfo]:
    """``u16 gSideStatuses[2]`` (4 bytes) and ``struct SideTimer gSideTimers[2]`` (24 bytes)."""
    sides = []
    for side in range(2):
        base = side * ST_SIZE
        sides.append(
            SideInfo(
                status=int.from_bytes(statuses[side * 2 : side * 2 + 2], "little"),
                reflect_timer=timers[base + ST_REFLECT_TIMER],
                lightscreen_timer=timers[base + ST_LIGHTSCREEN_TIMER],
                mist_timer=timers[base + ST_MIST_TIMER],
                safeguard_timer=timers[base + ST_SAFEGUARD_TIMER],
                spikes_layers=timers[base + ST_SPIKES_AMOUNT],
            )
        )
    return sides[0], sides[1]


class EnemyPartyInfo(_F):
    """What the player can count: how many opposing mons exist / are still up (trainer balls)."""

    size: int
    alive: int


class OpponentTrainer(_F):
    trainer_id: int | None = None
    class_name: str = "unknown"
    name: str = "unknown"


class MoveInfo(_F):
    """ROM facts of one move (``gBattleMoves`` row + name)."""

    move_id: int
    name: str | None = None
    type: int = 0
    power: int = 0
    accuracy: int = 0
    pp: int = 0
    priority: int = 0
    target: int = 0
    effect: int = 0
    secondary_chance: int = 0


class ItemEffectInfo(_F):
    """What ``gItemEffectTable[item]`` does when the item is used (the traits the item classes of
    ``references/battle_item_classes.yaml`` select by; decoded by
    ``games.gen3.firered.rom_tables``)."""

    heals_hp: bool = False
    revive: bool = False
    #: statuses and volatile conditions it cures: ``poison`` (also clears toxic), ``burn``,
    #: ``sleep``, ``freeze``, ``paralysis``, ``confusion``, ``infatuation``
    cures: tuple[str, ...] = ()
    heal_pp: bool = False
    #: ``(stat, stages)`` with stat in atk/def/spe/spa/acc
    stat_boost: tuple[tuple[str, int], ...] = ()
    focus_energy: bool = False
    guard_spec: bool = False
    sacred_ash: bool = False


class ItemInfo(_F):
    """ROM facts of one item (``gItems`` row)."""

    item_id: int
    name: str | None = None
    pocket: int = 0
    battle_usage: int = 0
    #: the battle handler class derived from the generated ROM table:
    #: ``medicine`` / ``pp_restore`` / ``sacred_ash`` / ``stat_booster`` / ``ball`` /
    #: ``poke_doll`` / ``poke_flute`` / ``berry_pouch`` / ``none`` (not usable in battle) /
    #: ``None`` (unclassified)
    role: str | None = None
    hold_effect: int = 0
    #: ``None``: the item has no ``gItemEffectTable`` row, or the table is not readable here
    effect: ItemEffectInfo | None = None


class BattleLookups(_F):
    """Names and ROM rows for exactly the ids this snapshot mentions.

    Captured with the snapshot so builders stay pure functions of the snapshot (replay): a builder
    never reads the ROM. Missing entries mean "ROM not readable here": callers degrade to ids.
    """

    species: dict[int, str] = Field(default_factory=dict)
    species_national_dex: dict[int, int] = Field(default_factory=dict)
    #: ROM-derived type ids for species mentioned by this snapshot. Kept separate from names so
    #: learn-move facts can use the learner's species even when it is benched.
    species_types: dict[int, tuple[int, int]] = Field(default_factory=dict)
    species_gender_ratio: dict[int, int] = Field(default_factory=dict)
    species_base_stats: dict[int, tuple[int, int, int, int, int, int]] = Field(default_factory=dict)
    abilities: dict[int, str] = Field(default_factory=dict)
    moves: dict[int, MoveInfo] = Field(default_factory=dict)
    items: dict[int, ItemInfo] = Field(default_factory=dict)


class BattleBlock(_F):
    schema_version: Literal["battle/1.0"] = SCHEMA
    #: gMain.inBattle (the only gate; stale battle RAM is never exposed when False)
    active: bool
    #: False: some read failed. ``read_errors`` explains; consumers treat everything as unknown.
    valid: bool = True
    read_errors: tuple[str, ...] = ()
    type_flags: int = 0
    kind: BattleKind = "wild"
    kind_reason: str = ""
    supported: bool = True
    battlers_count: int = 0
    battlers: tuple[Battler, ...] = ()
    #: The dispatcher battler at the instant of the atomic read.  In doubles this is not always 0.
    active_battler: int | None = None
    #: Player-side battlers whose controller is accepting input now.  This is the authoritative
    #: chooser set for doubles and scripted controllers; an empty tuple means no tactical input.
    input_battlers: tuple[int, ...] = ()
    #: Party slot backing each battler (``gBattlerPartyIndexes``), in battler order.
    party_indexes: tuple[int, ...] = ()
    #: Selected move target per battler from BattleStruct; stale outside move commitment.
    move_targets: tuple[int, ...] = ()
    absent_flags: int = 0
    #: gBattlerPositions per battler (profile symbol, not verified: no behaviour depends on it)
    positions: tuple[int, ...] = ()
    enemy_party: EnemyPartyInfo | None = None
    opponent_trainer: OpponentTrainer | None = None
    weather: tuple[str, ...] = ()
    #: field conditions of side 0 (player) and side 1 (opponent); None = not read (unknown)
    sides: tuple[SideInfo, SideInfo] | None = None
    outcome: int = 0
    script_ptr: int = 0
    # : the battle script opcode at ``gBattlescriptCurrInstr`` (None: pointer not in ROM /
    # unreadable)
    script_op: int | None = None
    #: ``PokedexScreenData.state`` while a newly caught species is being registered. Pret's
    #: ``Task_DexScreen_RegisterMonToPokedex`` reads A/B only in state 11.
    dex_registration_state: int | None = None
    main_func: int = 0
    comm: tuple[int, ...] = ()
    lvlup_state: int = 0
    learn_move_state: int = 0
    battle_style: int = 0  # 0 SHIFT, 1 SET
    move_to_learn: int = 0
    learn_move_party_id: int = 0  # BattleStruct.expGetterMonId
    number_of_moves: int = 0  # gNumberOfMovesToChoose (non-empty move slots of the chooser)
    #: gDisplayedStringBattle: the last battle text, exactly as shown to the player
    message: str = ""
    #: gBattleResults.battleTurnCounter (turns completed in this battle, saturates at 255)
    turn: int = 0
    last_used_item: int = 0  # gLastUsedItem (set when a bag item is chosen)
    #: Safari-only state. ``safari_balls`` is None until a profile supplies gNumSafariBalls.
    safari_balls: int | None = None
    safari_rock_turns: int = 0
    safari_bait_turns: int = 0
    safari_escape_factor: int = 0
    safari_catch_factor: int = 0
    run_tries: int = 0
    #: gRandomTurnNumber: a fresh random value at every turn boundary (a turn marker, not a counter)
    turn_marker: int = 0
    lookups: BattleLookups = Field(default_factory=BattleLookups)

    @property
    def player_battlers(self) -> tuple[Battler, ...]:
        """Present player-side battlers, including both active Pokemon in a double battle."""
        return tuple(b for b in self.battlers if b.side == "player" and b.present)

    @property
    def opponent_battlers(self) -> tuple[Battler, ...]:
        """Present opponent-side battlers, including both targets in a double battle."""
        return tuple(b for b in self.battlers if b.side == "opponent" and b.present)

    @property
    def decision_battler(self) -> int | None:
        """The player battler whose controller owns the current question, if there is one."""
        if self.input_battlers:
            return self.input_battlers[0]
        if self.active_battler in tuple(b.battler for b in self.player_battlers):
            return self.active_battler
        return None

    # -- helpers ---------------------------------------------------------------------------
    def battler_at(self, index: int) -> Battler | None:
        for b in self.battlers:
            if b.battler == index:
                return b
        return None

    @property
    def player(self) -> Battler | None:
        return self.battler_at(0)

    @property
    def opponent(self) -> Battler | None:
        return self.battler_at(1)

    @property
    def yes_no_cursor(self) -> int | None:
        """0 = YES, 1 = NO while a battle yes/no box is waiting; meaningless otherwise."""
        return self.comm[COMM_CURSOR_POSITION] if len(self.comm) > COMM_CURSOR_POSITION else None


# -- pure decoders ------------------------------------------------------------------------------


def status_names(status1: int) -> tuple[str, ...]:
    names = []
    if status1 & STATUS1_SLEEP:
        names.append("sleep")
    if status1 & STATUS1_POISON:
        names.append("poison")
    if status1 & STATUS1_TOXIC:
        names.append("toxic")
    if status1 & STATUS1_BURN:
        names.append("burn")
    if status1 & STATUS1_FREEZE:
        names.append("freeze")
    if status1 & STATUS1_PARALYSIS:
        names.append("paralysis")
    return tuple(names)


def volatile_names(status2: int, status3: int) -> tuple[str, ...]:
    names = []
    for mask, name in (
        (STATUS2_CONFUSION, "confusion"),
        (STATUS2_FLINCHED, "flinched"),
        (STATUS2_UPROAR, "uproar"),
        (STATUS2_BIDE, "bide"),
        (STATUS2_LOCK_CONFUSE, "thrash_lock"),
        (STATUS2_MULTIPLETURNS, "multi_turn_move"),
        (STATUS2_WRAPPED, "wrapped"),
        (STATUS2_INFATUATION, "infatuated"),
        (STATUS2_FOCUS_ENERGY, "focus_energy"),
        (STATUS2_TRANSFORMED, "transformed"),
        (STATUS2_RECHARGE, "recharging"),
        (STATUS2_RAGE, "rage"),
        (STATUS2_SUBSTITUTE, "substitute"),
        (STATUS2_ESCAPE_PREVENTION, "escape_prevented"),
        (STATUS2_NIGHTMARE, "nightmare"),
        (STATUS2_CURSED, "cursed"),
        (STATUS2_FORESIGHT, "foresight"),
        (STATUS2_TORMENT, "tormented"),
    ):
        if status2 & mask:
            names.append(name)
    if status3 & STATUS3_ROOTED:
        names.append("rooted")
    if status3 & STATUS3_SEMI_INVULNERABLE:
        names.append("semi_invulnerable")
    return tuple(names)


def weather_names(word: int) -> tuple[str, ...]:
    names = []
    if word & B_WEATHER_RAIN:
        names.append("rain")
    if word & B_WEATHER_SANDSTORM:
        names.append("sandstorm")
    if word & B_WEATHER_SUN:
        names.append("sun")
    if word & B_WEATHER_HAIL:
        names.append("hail")
    return tuple(names)


def decode_battle_mon(raw: bytes) -> dict:
    """Fields of one 0x58-byte ``struct BattlePokemon`` (no side / status words)."""
    if len(raw) < BM_SIZE:
        raise ValueError(f"battle mon needs {BM_SIZE} bytes, got {len(raw)}")
    species = struct.unpack_from("<H", raw, BM_SPECIES)[0]
    attack, defense, speed, sp_attack, sp_defense = struct.unpack_from("<5H", raw, BM_STATS)
    moves = struct.unpack_from("<4H", raw, BM_MOVES)
    stages = tuple(b - 6 for b in raw[BM_STAGES : BM_STAGES + 8])
    pps = raw[BM_PP : BM_PP + MOVE_SLOTS]
    hp = struct.unpack_from("<H", raw, BM_HP)[0]
    max_hp = struct.unpack_from("<H", raw, BM_MAX_HP)[0]
    item = struct.unpack_from("<H", raw, BM_ITEM)[0]
    return {
        "species": species,
        "level": raw[BM_LEVEL],
        "hp": hp,
        "max_hp": max_hp,
        "attack": attack,
        "defense": defense,
        "speed": speed,
        "sp_attack": sp_attack,
        "sp_defense": sp_defense,
        "stat_stages": stages,
        "status1": struct.unpack_from("<I", raw, BM_STATUS1)[0],
        "status2": struct.unpack_from("<I", raw, BM_STATUS2)[0],
        "type1": raw[BM_TYPE1],
        "type2": raw[BM_TYPE2],
        "ability": raw[BM_ABILITY],
        "item": item,
        "moves": tuple(MoveSlot(slot=i, move_id=moves[i], pp=pps[i]) for i in range(MOVE_SLOTS)),
        "pp_bonuses": raw[BM_PP_BONUSES],
        "nickname": decode_gen3_text(raw[BM_NICKNAME : BM_NICKNAME + 11]),
        "personality": struct.unpack_from("<I", raw, BM_PERSONALITY)[0],
    }


def decode_disable_struct(raw: bytes) -> DisableInfo:
    if len(raw) < DS_SIZE:
        raise ValueError(f"disable struct needs {DS_SIZE} bytes, got {len(raw)}")
    return DisableInfo(
        disabled_move=struct.unpack_from("<H", raw, DS_DISABLED_MOVE)[0],
        encored_move=struct.unpack_from("<H", raw, DS_ENCORED_MOVE)[0],
        protect_uses=raw[DS_PROTECT_USES],
        substitute_hp=raw[DS_SUBSTITUTE_HP],
        disable_timer=raw[DS_DISABLE_TIMER] & 0x0F,
        encore_timer=raw[DS_ENCORE_TIMER] & 0x0F,
        taunt_timer=raw[DS_TAUNT_TIMER] & 0x0F,
    )


def classify_battle(
    flags: int, *, player_variant: str | None = None
) -> tuple[BattleKind, str, bool]:
    """``(kind, reason, supported)`` from ``gBattleTypeFlags`` (and the player controller variant).

    ``supported`` is True for every local FireRed battle controller.  Link play remains outside
    the autonomous adapter because its inputs are synchronised with a second console.
    """
    bit = lambda n: bool(flags & (1 << n))  # noqa: E731
    if bit(1) or bit(5):
        return "link", "link battle (gBattleTypeFlags LINK)", False
    if bit(7):
        return "safari", "Safari Zone battle", True
    if bit(9) or bit(16) or player_variant in ("oak_old_man", "pokedude"):
        return "tutorial", "scripted tutorial battle (Old Man / Pokedude / Oak controller)", True
    if bit(15) and not bit(13):
        return "ghost", "Pokemon Tower ghost without the Silph Scope", True
    if bit(0) or bit(6):
        return "double", "double or multi battle", True
    if bit(8) or bit(11) or bit(19):
        return "other_unsupported", "battle tower / e-reader / trainer tower battle", False
    if bit(3):
        return "trainer", "trainer battle", True
    return "wild", "wild battle", True
