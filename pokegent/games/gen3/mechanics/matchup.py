"""Pure Strike and battle-action plan builders for the exchange engine."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace

from pokegent.games.gen3 import mechanics_constants as K
from pokegent.games.gen3.firered.rom.rom_tables import GameTables
from pokegent.games.gen3.mechanics.exchange import MULTI_HIT_COUNTS, exchange
from pokegent.games.gen3.mechanics.exchange_types import Exchange, Plan, Strike
from pokegent.games.gen3.mechanics.views import BattlerView, FieldView, MoveSlotView
from pokegent.games.gen3.ruleset.damage import DamageEstimate, estimate_damage
from pokegent.games.gen3.ruleset.effects import traits_for


def _status_act_chance(mon: BattlerView) -> float:
    if mon.status in (K.STATUS_SLEEP, K.STATUS_FREEZE):
        return 0.0 if mon.status == K.STATUS_FREEZE or (mon.sleep_turns or 1) > 0 else 1.0
    chance = 0.75 if mon.status == K.STATUS_PARALYSIS else 1.0
    if K.V_CONFUSION in mon.volatile:
        chance *= 0.5
    if K.V_INFATUATION in mon.volatile:
        chance *= 0.5
    if K.V_FLINCH in mon.volatile:
        chance = 0.0
    return chance


def _strike_distribution(
    est: DamageEstimate, *, accuracy: float, act: float
) -> tuple[tuple[int, float], ...]:
    if est.no_effect or est.damage_model == "none" or est.max_damage <= 0:
        return ((0, 1.0),)
    hits_lo, hits_hi = est.hits
    hit_count = hits_hi if hits_hi > 1 else 1
    one_max = max(1, round(est.max_damage / hit_count))
    rolls = tuple((max(1, one_max * roll // 100), 1 / 16) for roll in range(85, 101))
    counts = ((1, 1.0),) if hits_hi <= 1 else MULTI_HIT_COUNTS
    landed: dict[int, float] = defaultdict(float)
    for count, count_probability in counts:
        values = {0: 1.0}
        for _ in range(count):
            values = _convolve(values, rolls)
        for damage, probability in values.items():
            landed[damage] += count_probability * probability
    hit_probability = max(0.0, min(1.0, accuracy * act))
    result = {0: 1.0 - hit_probability}
    for damage, probability in landed.items():
        result[damage] = result.get(damage, 0.0) + hit_probability * probability
    return _clean_distribution(result)


def _clean_distribution(values: dict[int, float]) -> tuple[tuple[int, float], ...]:
    values = {max(0, int(k)): v for k, v in values.items() if v > 1e-12}
    total = sum(values.values())
    return ((0, 1.0),) if not total else tuple(sorted((k, v / total) for k, v in values.items()))


def _convolve(a: dict[int, float], b: tuple[tuple[int, float], ...]) -> dict[int, float]:
    out: dict[int, float] = defaultdict(float)
    for left, left_p in a.items():
        for right, right_p in b:
            out[left + right] += left_p * right_p
    return out


def make_strike(
    attacker: BattlerView,
    defender: BattlerView,
    move: int | MoveSlotView,
    tables: GameTables,
    field: FieldView,
) -> Strike:
    """Build a repeatable Strike through the canonical damage estimator."""
    estimate = estimate_damage(attacker, defender, move, tables, field)
    accuracy = (estimate.accuracy[0] + estimate.accuracy[1]) / 200
    act = _status_act_chance(attacker)
    move_id = move.move_id if isinstance(move, MoveSlotView) else move
    return Strike(
        damage=_strike_distribution(estimate, accuracy=accuracy, act=act),
        accuracy=accuracy,
        act_chance=act,
        priority=estimate.priority,
        speed=estimate.speed_attacker,
        name=tables.move(move_id).name,
        move_id=move_id,
    )


strike = make_strike


def _idle_strike() -> Strike:
    return Strike(((0, 1.0),), speed=(0, 0), name="idle")


def _plan_hp(mon: BattlerView, tables: GameTables) -> tuple[int, int]:
    maximum = mon.max_hp
    if maximum is None and mon.species is not None and mon.level is not None:
        species = tables.species(mon.species)
        maximum = ((2 * species.base_hp + 15) * mon.level) // 100 + mon.level + 10
    maximum = max(1, maximum or mon.hp or 1)
    current = mon.hp
    if current is None and mon.hp_pct is not None:
        current = max(1, round(maximum * max(0.0, min(100.0, mon.hp_pct)) / 100))
    return max(0, min(maximum, current if current is not None else maximum)), maximum


def _best_move(
    mon: BattlerView, foe: BattlerView, tables: GameTables, field: FieldView
) -> int | None:
    usable = [slot for slot in mon.moves if slot.move_id and slot.pp != 0]
    candidates = [s for s in usable if not tables.move(s.move_id).is_status] or usable
    if not candidates:
        return None
    return max(
        candidates, key=lambda s: make_strike(mon, foe, s, tables, field).expected_damage
    ).move_id


@dataclass(frozen=True)
class Matchup:
    """Factory for plans corresponding to attack, idle, switch, and status options."""

    ours: BattlerView
    foe: BattlerView
    tables: GameTables
    field: FieldView
    foe_model: str = "trainer"

    def _our_attack(self, move: int | MoveSlotView | None = None) -> Strike:
        chosen = (
            move if move is not None else _best_move(self.ours, self.foe, self.tables, self.field)
        )
        return (
            make_strike(self.ours, self.foe, chosen, self.tables, self.field)
            if chosen
            else _idle_strike()
        )

    def _foe_attack(self) -> Strike:
        usable = [slot for slot in self.foe.moves if slot.move_id and slot.pp != 0]
        if usable:
            strikes = [
                make_strike(self.foe, self.ours, slot, self.tables, self.field) for slot in usable
            ]
            if self.foe_model == "wild":
                return _mix_strikes(strikes)
            return max(strikes, key=lambda item: item.expected_damage)
        types = self.foe.types or (
            self.tables.species(self.foe.species).types if self.foe.species else ()
        )
        fallback = [
            MoveSlotView(move_data.move_id, pp=1)
            for move_data in self.tables.moves
            if move_data.type in types and move_data.power == 60 and not move_data.is_status
        ]
        if not fallback:
            return _idle_strike()
        strikes = [
            make_strike(self.foe, self.ours, slot, self.tables, self.field) for slot in fallback
        ]
        return (
            _mix_strikes(strikes)
            if self.foe_model == "wild"
            else max(strikes, key=lambda item: item.expected_damage)
        )

    def attack(self, move: int | MoveSlotView | None = None) -> Exchange:
        my_hp, my_max = _plan_hp(self.ours, self.tables)
        foe_hp, foe_max = _plan_hp(self.foe, self.tables)
        return exchange(
            Plan((self._our_attack(move),), my_hp, my_max),
            Plan((self._foe_attack(),), foe_hp, foe_max),
        )

    def idle(self) -> Exchange:
        return exchange(
            Plan((_idle_strike(),), *_plan_hp(self.ours, self.tables), idle=True),
            Plan((self._foe_attack(),), *_plan_hp(self.foe, self.tables)),
        )

    def switch(self, candidate: BattlerView, *, forced: bool = False) -> Exchange:
        matchup = Matchup(candidate, self.foe, self.tables, self.field, self.foe_model)
        plan = Plan(
            (matchup._our_attack(),), *_plan_hp(candidate, self.tables), free_hit=not forced
        )
        foe_plan = Plan((matchup._foe_attack(),), *_plan_hp(self.foe, self.tables))
        return exchange(plan, foe_plan)

    def status(self, move: int | MoveSlotView) -> Exchange:
        move_id = move.move_id if isinstance(move, MoveSlotView) else move
        data = self.tables.move(move_id)
        estimate = estimate_damage(self.ours, self.foe, move, self.tables, self.field)
        user_strike = Strike(
            ((0, 1.0),),
            accuracy=(estimate.accuracy[0] + estimate.accuracy[1]) / 200,
            act_chance=_status_act_chance(self.ours),
            priority=data.priority,
            speed=estimate.speed_attacker,
            name=data.name,
            move_id=move_id,
        )
        foe_strike = self._foe_attack()
        traits = traits_for(data.effect)
        if traits.primary_status == K.STATUS_PARALYSIS:
            foe_strike = replace(
                foe_strike,
                speed=(max(1, foe_strike.speed[0] // 4), max(1, foe_strike.speed[1] // 4)),
                act_chance=foe_strike.act_chance * 0.75,
            )
        elif traits.primary_status == K.STATUS_SLEEP:
            foe_plan = Plan((foe_strike,), *_plan_hp(self.foe, self.tables), delay_turns=2)
            return exchange(Plan((user_strike,), *_plan_hp(self.ours, self.tables)), foe_plan)
        elif traits.primary_status == "confusion" or traits.secondary_status == "confusion":
            foe_strike = replace(foe_strike, act_chance=foe_strike.act_chance * 0.5)
        foe_dot = 0
        if traits.primary_status == K.STATUS_BURN:
            foe_dot = max(1, _plan_hp(self.foe, self.tables)[1] // 8)
        elif traits.primary_status in (K.STATUS_POISON, K.STATUS_TOXIC):
            foe_dot = max(1, _plan_hp(self.foe, self.tables)[1] // 16)
        elif traits.primary_status == "leech_seed":
            foe_dot = max(1, _plan_hp(self.foe, self.tables)[1] // 8)
        return exchange(
            Plan(
                (user_strike,),
                *_plan_hp(self.ours, self.tables),
                heal=foe_dot if traits.primary_status == "leech_seed" else 0,
            ),
            Plan((foe_strike,), *_plan_hp(self.foe, self.tables), dot_damage=foe_dot),
        )


def _mix_strikes(strikes: tuple[Strike, ...] | list[Strike]) -> Strike:
    if not strikes:
        return _idle_strike()
    mixed: dict[int, float] = defaultdict(float)
    for item in strikes:
        for damage, probability in item.damage:
            mixed[damage] += probability / len(strikes)
    return Strike(
        _clean_distribution(mixed),
        accuracy=sum(s.accuracy for s in strikes) / len(strikes),
        act_chance=sum(s.act_chance for s in strikes) / len(strikes),
        priority=min(s.priority for s in strikes),
        speed=(min(s.speed[0] for s in strikes), max(s.speed[1] for s in strikes)),
        name="random move",
    )


__all__ = ["Matchup", "make_strike", "strike"]
