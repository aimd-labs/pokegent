"""FireRed/Gen-3 catch arithmetic (``Cmd_handleballthrow``)."""

from __future__ import annotations

import math

from pokegent.games.gen3 import mechanics_constants as K
from pokegent.games.gen3 import mechanics_ids as I

_BALL_FIXED = {
    I.ITEM_ULTRA_BALL: 20,
    I.ITEM_GREAT_BALL: 15,
    I.ITEM_POKE_BALL: 10,
    I.ITEM_SAFARI_BALL: 15,
    I.ITEM_LUXURY_BALL: 10,
    I.ITEM_PREMIER_BALL: 10,
}


def catch_probability(odds: int) -> float:
    """Return the probability that one Gen-3 throw succeeds for pre-shake ``odds``."""
    if odds <= 0:
        return 0.0
    if odds > 254:
        return 1.0
    shake = math.isqrt(math.isqrt(16711680 // odds))
    if shake == 0:
        return 1.0
    threshold = 1048560 // shake
    return min(1.0, threshold / 65536) ** 4


def ball_multiplier(
    ball: int,
    target_types: tuple[int, int] | None,
    level: int | None,
    turn: int,
    species_caught: bool | None,
    underwater: bool | None,
) -> tuple[int, int]:
    """Return the x10 ball multiplier range used by the FireRed catch script."""
    if ball in _BALL_FIXED:
        return _BALL_FIXED[ball], _BALL_FIXED[ball]
    if ball == I.ITEM_NET_BALL:
        if target_types is None:
            return 10, 30
        return (30, 30) if K.TYPE_WATER in target_types or K.TYPE_BUG in target_types else (10, 10)
    if ball == I.ITEM_DIVE_BALL:
        if underwater is None:
            return 10, 35
        return (35, 35) if underwater else (10, 10)
    if ball == I.ITEM_NEST_BALL:
        if level is None:
            return 10, 39
        mult = max(10, 40 - level) if level < 40 else 10
        return mult, mult
    if ball == I.ITEM_REPEAT_BALL:
        if species_caught is None:
            return 10, 30
        return (30, 30) if species_caught else (10, 10)
    if ball == I.ITEM_TIMER_BALL:
        mult = min(40, turn + 10)
        return mult, mult
    return 10, 10


def catch_odds(
    catch_rate: int, multiplier_x10: int, hp: int, max_hp: int, status: int | None
) -> int:
    """Calculate the integer pre-shake odds from ``Cmd_handleballthrow``."""
    odds = (catch_rate * multiplier_x10 // 10) * (max_hp * 3 - hp * 2) // (3 * max_hp)
    if status in (K.STATUS_SLEEP, K.STATUS_FREEZE):
        odds *= 2
    elif status in (K.STATUS_POISON, K.STATUS_BURN, K.STATUS_PARALYSIS, K.STATUS_TOXIC):
        odds = odds * 15 // 10
    return odds
