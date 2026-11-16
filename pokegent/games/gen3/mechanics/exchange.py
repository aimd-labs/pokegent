"""Pure Gen-3 battle exchange facts.

The damage chain remains in :mod:`pokegent.games.gen3.ruleset.damage`.  This module turns its
one-use result into a small, deterministic race model: who gets to act, how many attacks are
needed, and whether either side falls before the exchange is complete.  It deliberately does
not rank options or call a provider.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Sequence

from pokegent.games.gen3.mechanics.exchange_types import DPResult, Exchange, Plan, Strike

MAX_TURNS = 8
MULTI_HIT_COUNTS = ((2, 3 / 8), (3, 3 / 8), (4, 1 / 8), (5, 1 / 8))


def _clean_distribution(values: dict[int, float]) -> tuple[tuple[int, float], ...]:
    values = {max(0, int(k)): v for k, v in values.items() if v > 1e-12}
    total = sum(values.values())
    if not total:
        return ((0, 1.0),)
    return tuple(sorted((k, v / total) for k, v in values.items()))


def _convolve(a: dict[int, float], b: Iterable[tuple[int, float]]) -> dict[int, float]:
    out: dict[int, float] = defaultdict(float)
    for left, left_p in a.items():
        for right, right_p in b:
            out[left + right] += left_p * right_p
    return out


def _as_plan(value: Plan | Sequence[Strike | None], *, hp: int | None = None) -> Plan:
    if isinstance(value, Plan):
        return value
    strikes = tuple(value)
    return Plan(strikes, hp=1 if hp is None else hp)


def dp(plan: Plan | Sequence[Strike | None], hp: int, turns: int = MAX_TURNS) -> DPResult:
    """Apply a plan to a target HP value for up to eight turns.

    The first turn retains every discrete damage outcome (including exact KO mass); later turns
    are integer HP states, which is the required quantisation and keeps long races bounded.
    """
    if turns < 0 or turns > MAX_TURNS:
        raise ValueError(f"turns must be between 0 and {MAX_TURNS}")
    p = _as_plan(plan)
    states: dict[int, float] = {max(0, hp): 1.0}
    snapshots: list[tuple[tuple[int, float], ...]] = []
    expected_damage = 0.0
    expected_turns = 0.0
    active_probability = 1.0
    for turn in range(turns):
        strike = p.strike(turn)
        outcomes = ((0, 1.0),) if strike is None or turn < p.delay_turns else strike.damage
        next_states: dict[int, float] = defaultdict(float)
        turn_damage = 0.0
        for old_hp, probability in states.items():
            for damage, damage_p in outcomes:
                new_hp = max(0, old_hp - damage + p.heal)
                if p.dot_damage:
                    new_hp = max(0, new_hp - p.dot_damage)
                branch = probability * damage_p
                next_states[new_hp] += branch
                turn_damage += probability * damage_p * damage
        states = dict(next_states)
        total = sum(states.values()) or 1.0
        states = {h: v / total for h, v in states.items()}
        snapshots.append(tuple(sorted(states.items())))
        expected_damage += turn_damage
        p_alive = sum(v for h, v in states.items() if h > 0)
        expected_turns += active_probability
        active_probability = p_alive
    ko = 1.0 - sum(v for h, v in states.items() if h > 0) if states else 1.0
    return DPResult(tuple(snapshots), ko, expected_damage, expected_turns if turns else None)


def _speed_probability(a: Strike | None, b: Strike | None) -> float:
    if a is None or b is None:
        return 0.5
    if a.priority != b.priority:
        return 1.0 if a.priority > b.priority else 0.0
    alo, ahi = a.speed
    blo, bhi = b.speed
    if alo > bhi:
        return 1.0
    if ahi < blo:
        return 0.0
    if alo == ahi == blo == bhi:
        return 0.5
    # Unknown ranges are bounded by the view estimator.  Enumerating this small range keeps a
    # speed overlap deterministic and models a tie as a coin flip.
    a_values = range(max(1, alo), max(1, ahi) + 1)
    b_values = range(max(1, blo), max(1, bhi) + 1)
    count = total = 0
    for av in a_values:
        for bv in b_values:
            total += 1
            count += av > bv
            count += 0.5 if av == bv else 0
    return count / total if total else 0.5


def _damage_outcomes(strike: Strike | None) -> tuple[tuple[int, float], ...]:
    return ((0, 1.0),) if strike is None else strike.damage


def _turn_strike(plan: Plan, turn: int) -> Strike | None:
    return None if turn < plan.delay_turns else plan.strike(turn)


def _apply_side(
    hp: int, strike: Strike | None, probability: float, *, heal: int = 0, dot: int = 0
) -> dict[int, float]:
    out: dict[int, float] = defaultdict(float)
    for damage, damage_p in _damage_outcomes(strike):
        out[max(0, hp - damage + heal - dot)] += probability * damage_p
    return out


def exchange(
    ours: Plan | Sequence[Strike | None],
    foes: Plan | Sequence[Strike | None],
    firsts: bool | Sequence[bool | str] | None = None,
) -> Exchange:
    """Race two plans, resolving priority, speed, accuracy, status and first-turn KOs.

    ``firsts`` is an optional deterministic test override: ``True`` means ours acts first,
    ``False`` means the foe acts first.  A sequence supplies one value per turn.  With ``None``
    the Strike priority/speed fields are used and an exact tie is split 50/50.
    """
    mine, foe = _as_plan(ours), _as_plan(foes)
    my_hp, foe_hp = max(0, mine.hp), max(0, foe.hp)
    my_max, foe_max = mine.full_hp, foe.full_hp
    states: dict[tuple[int, int], float] = {(my_hp, foe_hp): 1.0}
    p_foe_falls = p_we_fall = p_ko_now = 0.0
    loss_hp = gain_hp = 0.0
    win_turn_sum = lose_turn_sum = 0.0
    win_mass = lose_mass = 0.0
    p_acts = _turn_strike(mine, 0).p_acts if _turn_strike(mine, 0) else 0.0
    first_probability = _speed_probability(_turn_strike(mine, 0), _turn_strike(foe, 0))

    def is_ours_first(turn: int) -> float:
        if isinstance(firsts, bool):
            return 1.0 if firsts else 0.0
        if firsts is not None and turn < len(firsts):
            item = firsts[turn]
            return 1.0 if item is True or item == "ours" else 0.0
        return _speed_probability(_turn_strike(mine, turn), _turn_strike(foe, turn))

    def terminal(my: int, enemy: int, probability: float, turn: int, *, first_turn: bool) -> None:
        nonlocal p_foe_falls, p_we_fall, p_ko_now, loss_hp, gain_hp
        nonlocal win_turn_sum, lose_turn_sum, win_mass, lose_mass
        if enemy <= 0:
            p_foe_falls += probability
            gain_hp += probability * (foe_max - max(0, enemy)) / foe_max
            win_turn_sum += probability * (turn + 1)
            win_mass += probability
        elif my <= 0:
            p_we_fall += probability
            loss_hp += probability * (my_max - max(0, my)) / my_max
            lose_turn_sum += probability * (turn + 1)
            lose_mass += probability
            if first_turn:
                p_ko_now += probability

    # A voluntary switch is spent on one unopposed foe action before turn one.
    if mine.free_hit:
        after_entry = _apply_side(my_hp, _turn_strike(foe, 0), 1.0, dot=0)
        states = {}
        for h, probability in after_entry.items():
            states[(h, foe_hp)] = states.get((h, foe_hp), 0.0) + probability
        p_acts = sum(p for (h, _), p in states.items() if h > 0) * (
            _turn_strike(mine, 0).p_acts if _turn_strike(mine, 0) else 0.0
        )

    for turn in range(MAX_TURNS):
        if not states:
            break
        next_states: dict[tuple[int, int], float] = defaultdict(float)
        for (my, enemy), state_probability in states.items():
            if my <= 0 or enemy <= 0:
                terminal(my, enemy, state_probability, turn, first_turn=turn == 0)
                continue
            ours_first = is_ours_first(turn)
            order_branches = ((True, ours_first), (False, 1.0 - ours_first))
            for ours_goes_first, order_probability in order_branches:
                if order_probability <= 0:
                    continue
                branch_probability = state_probability * order_probability
                first = _turn_strike(mine, turn) if ours_goes_first else _turn_strike(foe, turn)
                second = _turn_strike(foe, turn) if ours_goes_first else _turn_strike(mine, turn)
                after_first: dict[tuple[int, int], float] = defaultdict(float)
                for damage, damage_probability in _damage_outcomes(first):
                    nmy, nenemy = my, enemy
                    if ours_goes_first:
                        nenemy = max(0, nenemy - damage)
                    else:
                        nmy = max(0, nmy - damage)
                    after_first[(nmy, nenemy)] += branch_probability * damage_probability
                for (nmy, nenemy), first_probability_mass in after_first.items():
                    if nmy <= 0 or nenemy <= 0:
                        terminal(nmy, nenemy, first_probability_mass, turn, first_turn=turn == 0)
                        continue
                    for damage, damage_probability in _damage_outcomes(second):
                        fmy, fenemy = nmy, nenemy
                        if ours_goes_first:
                            fmy = max(0, fmy - damage)
                        else:
                            fenemy = max(0, fenemy - damage)
                        mass = first_probability_mass * damage_probability
                        if fmy <= 0 or fenemy <= 0:
                            terminal(fmy, fenemy, mass, turn, first_turn=turn == 0)
                        else:
                            next_states[(fmy, fenemy)] += mass
        states = dict(next_states)
        # End-of-turn damage/healing belongs to the standing side only.
        if states and (mine.dot_damage or mine.heal or foe.dot_damage or foe.heal):
            adjusted: dict[tuple[int, int], float] = defaultdict(float)
            for (my, enemy), probability in states.items():
                adjusted[
                    (
                        max(0, my + mine.heal - foe.dot_damage),
                        max(0, enemy + foe.heal - mine.dot_damage),
                    )
                ] += probability
            states = dict(adjusted)
    for (my, enemy), probability in states.items():
        loss_hp += probability * (my_max - my) / my_max
        gain_hp += probability * (foe_max - enemy) / foe_max
    draws = sum(states.values())
    p_win = p_foe_falls + draws * 0.5
    return Exchange(
        p_win=max(0.0, min(1.0, p_win)),
        p_ko_us_now=max(0.0, min(1.0, p_ko_now)),
        my_turns=(win_turn_sum / win_mass if win_mass else None),
        foe_turns=(lose_turn_sum / lose_mass if lose_mass else None),
        expected_hp_fraction_lost=max(0.0, min(1.0, loss_hp)),
        expected_foe_hp_fraction_lost=max(0.0, min(1.0, gain_hp)),
        p_foe_falls=max(0.0, min(1.0, p_foe_falls)),
        p_we_fall=max(0.0, min(1.0, p_we_fall)),
        p_acts=max(0.0, min(1.0, p_acts)),
        first_probability=first_probability,
    )


__all__ = [
    "DPResult",
    "Exchange",
    "MAX_TURNS",
    "MULTI_HIT_COUNTS",
    "Plan",
    "Strike",
    "dp",
    "exchange",
]


def __getattr__(name: str):
    if name in {"Matchup", "make_strike", "strike"}:
        from pokegent.games.gen3.mechanics.matchup import Matchup, make_strike, strike

        return {"Matchup": Matchup, "make_strike": make_strike, "strike": strike}[name]
    raise AttributeError(name)
