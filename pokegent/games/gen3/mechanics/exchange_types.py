"""Immutable value types shared by the exchange engine and matchup builders."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Strike:
    """One repeatable move use, including miss and failure-to-act outcomes."""

    damage: tuple[tuple[int, float], ...]
    accuracy: float = 1.0
    act_chance: float = 1.0
    priority: int = 0
    speed: tuple[int, int] = (0, 0)
    name: str = ""
    move_id: int | None = None
    expected_damage: float = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "expected_damage", sum(d * p for d, p in self.damage))

    @property
    def p_acts(self) -> float:
        return max(0.0, min(1.0, self.accuracy * self.act_chance))

    @property
    def p_damage(self) -> float:
        return sum(p for d, p in self.damage if d > 0)


@dataclass(frozen=True)
class Plan:
    """A repeatable sequence of strikes used by ``dp`` and ``exchange``."""

    strikes: tuple[Strike | None, ...]
    hp: int
    max_hp: int | None = None
    dot_damage: int = 0
    heal: int = 0
    delay_turns: int = 0
    free_hit: bool = False
    idle: bool = False

    def strike(self, turn: int) -> Strike | None:
        if not self.strikes:
            return None
        return self.strikes[min(turn, len(self.strikes) - 1)]

    @property
    def full_hp(self) -> int:
        return max(1, self.max_hp if self.max_hp is not None else self.hp)


@dataclass(frozen=True)
class DPResult:
    """Distribution of remaining HP after each turn of a plan."""

    turns: tuple[tuple[tuple[int, float], ...], ...]
    ko_probability: float
    expected_damage: float
    expected_turns: float | None

    @property
    def final(self) -> tuple[tuple[int, float], ...]:
        return self.turns[-1] if self.turns else ((0, 1.0),)


@dataclass(frozen=True)
class Exchange:
    """Facts from two plans racing for at most eight turns."""

    p_win: float
    p_ko_us_now: float
    my_turns: float | None
    foe_turns: float | None
    expected_hp_fraction_lost: float
    expected_foe_hp_fraction_lost: float
    p_foe_falls: float
    p_we_fall: float
    p_acts: float = 1.0
    first_probability: float = 0.5

    @property
    def gain(self) -> float:
        return self.expected_foe_hp_fraction_lost

    @property
    def loss(self) -> float:
        return self.expected_hp_fraction_lost
