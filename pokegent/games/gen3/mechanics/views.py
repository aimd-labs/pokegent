"""Read-only views of a battle that the mechanics functions consume (API frozen in M2b).

Plain frozen dataclasses: no emulator, no RAM, no I/O. The caller (battle reader / option
builder) fills them from what it is *allowed to know*; the mechanics code never reaches for
more. Conventions:

* ``None`` always means **unknown**, never "no". A function that needs an unknown input either
  widens its answer (damage estimator: ``uncertain_modifiers``) or answers ``UNKNOWN``
  (legality). Defaults of the volatile flags are "no/empty" because that is what the game's
  RAM says when nothing is active; the caller lists categories it could not read in
  ``BattlerView.unknown``.
* Stat stages are signed ``-6..+6`` (the game stores ``0..12`` with 6 = neutral).
* Ability ids, item ids, species ids, move ids and type ids are the decompile's numbers
  (``games.gen3.firered.mechanics_ids``, ``games.gen3.firered.mechanics_constants``).
* An opposing battler may be built from what is visible on screen: ``species``, ``level``,
  ``hp_pct``; stats / ability / item / moves ``None``. The estimator then derives *ranges* from
  base stats (IV 0..31, EV 0, neutral-or-boosted nature) instead of leaking RAM it should not
  use (CONTEXT.md invariant 8).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from pokegent.games.gen3 import mechanics_constants as K


@dataclass(frozen=True)
class StatsView:
    """Battle stats (the numbers gBattleMons holds: before badge/item/ability modifiers)."""

    atk: int
    def_: int
    spa: int
    spd: int
    spe: int

    def as_dict(self) -> dict[str, int]:
        return {
            "atk": self.atk,
            "def": self.def_,
            "spa": self.spa,
            "spd": self.spd,
            "spe": self.spe,
        }


@dataclass(frozen=True)
class StagesView:
    """Signed stat stages -6..+6; ``acc``/``eva`` are the accuracy/evasion stages."""

    atk: int = 0
    def_: int = 0
    spa: int = 0
    spd: int = 0
    spe: int = 0
    acc: int = 0
    eva: int = 0

    def __post_init__(self) -> None:
        for name in ("atk", "def_", "spa", "spd", "spe", "acc", "eva"):
            value = getattr(self, name)
            if not -6 <= value <= 6:
                raise ValueError(f"stage {name}={value} outside -6..+6")


@dataclass(frozen=True)
class MoveSlotView:
    """One move slot. ``pp`` ``None`` = unknown. The flags are what the caller read from RAM;
    :mod:`legality` additionally derives restrictions from the battler-level state.
    """

    move_id: int  # 0 = empty slot
    pp: int | None = None
    max_pp: int | None = None
    disabled: bool = False  # Disable names this move (gDisableStructs.disabledMove)
    taunt_blocked: bool = False  # Taunt is active and this move is a status move
    torment_blocked: bool = False  # Torment is active and this was the last move used
    choice_blocked: bool = False  # a Choice item locks a different move
    imprisoned: bool = False  # a foe used Imprison and knows this move


@dataclass(frozen=True)
class BattlerView:
    """A battler or a bench member. Fields beyond the ones in the freeze spec are optional."""

    species: int | None
    level: int | None
    hp: int | None  # current HP; None = unknown (use hp_pct)
    max_hp: int | None
    stats: StatsView | None = None  # None = unknown: ranges are derived from base stats
    stat_stages: StagesView = field(default_factory=StagesView)
    status: str = K.STATUS_NONE  # one of constants.MAJOR_STATUSES
    sleep_turns: int | None = None  # remaining sleep turns if known
    volatile: frozenset[str] = frozenset()  # constants.V_* names
    types: tuple[int, int] | None = None  # None = take from species (Roost-like changes: n/a)
    ability: int | None = None  # None = unknown; 0 = no ability
    held_item: int | None = None  # None = unknown; 0 = none
    moves: tuple[MoveSlotView, ...] = ()
    is_player_side: bool = True
    position: int | None = None  # constants.B_POSITION_*; None for a benched mon
    # --- optional extras -------------------------------------------------------------------
    hp_pct: float | None = None  # 0..100, used when hp/max_hp are unknown (enemy HP bar)
    friendship: int | None = None  # Return / Frustration
    ivs: tuple[int, int, int, int, int, int] | None = None  # hp, atk, def, spe, spa, spd
    is_egg: bool = False
    last_move_id: int | None = None  # for Torment and Encore
    locked_move_id: int | None = None  # rampage / Rollout / charge turn in progress
    choice_locked_move_id: int | None = None  # Choice Band lock (first move used since switch-in)
    disabled_move_id: int | None = None
    encored_move_id: int | None = None
    unknown: frozenset[str] = frozenset()  # volatile categories the caller could not read
    name: str | None = None  # display only

    # -- conveniences -------------------------------------------------------------------------
    @property
    def is_fainted(self) -> bool:
        return self.hp == 0

    def has_volatile(self, name: str) -> bool:
        return name in self.volatile

    def with_(self, **changes: Any) -> BattlerView:
        return replace(self, **changes)


@dataclass(frozen=True)
class SideView:
    """Conditions on one side of the field."""

    conditions: frozenset[str] = frozenset()  # constants.SIDE_*
    spikes_layers: int = 0
    alive_count: int | None = None  # battlers of this side still in the fight (double-battle rules)

    def has(self, name: str) -> bool:
        return name in self.conditions


@dataclass(frozen=True)
class FieldView:
    """Field-wide facts. ``kind`` is one of constants.KINDS; ``double`` is separate."""

    kind: str = K.KIND_UNKNOWN
    double: bool = False
    weather: str = K.WEATHER_NONE
    weather_negated: bool | None = False  # Cloud Nine / Air Lock on the field; None = unknown
    turn: int = 0
    player_side: SideView = field(default_factory=SideView)
    opponent_side: SideView = field(default_factory=SideView)
    #: badge numbers 1..8 the player owns (constants.BADGE_*); None = unknown (boost uncertain)
    badges: frozenset[int] | None = None
    field_abilities: frozenset[int] | None = None  # abilities of all active battlers (Plus/Minus)
    mud_sport: bool = False
    water_sport: bool = False
    #: Safari Zone only: balls left / bait / rock counters are read by the battle reader
    safari_balls_left: int | None = None

    def side_of(self, battler: BattlerView) -> SideView:
        return self.player_side if battler.is_player_side else self.opponent_side

    def with_(self, **changes: Any) -> FieldView:
        return replace(self, **changes)
