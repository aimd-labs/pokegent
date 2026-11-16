"""Option legality for ``battle.action`` (spec 5.2, 6.D): hard game rules only, from RAM.

Rules mirror the pret decompile (``src/battle_util.c`` ``CheckMoveLimitations`` /
``TrySetCantSelectMoveBattleScript`` / ``AreAllMovesUnusable``, ``src/battle_main.c``
``IsRunningFromBattleImpossible``, the ``B_ACTION_SWITCH`` branch of
``HandleTurnActionSelectionState`` and, for bag items, ``src/pokemon.c`` ``PokemonUseItemEffects``
through the item classes of ``references/battle_item_classes.yaml``). Strategically bad is NOT
illegal: a resisted attack or a ball at a strong mon stays LEGAL, the provider's facts say so. A use
that PROVABLY changes nothing (a Potion on a full-HP mon, the POKe FLUTE with nobody asleep, an X
item at +6) is UNAVAILABLE with the game rule as its reason: the game either refuses it ("It won't
have any effect") or spends the turn on nothing (M4 run 1, the runtime contracts
row 11). UNKNOWN is
returned whenever a rule needs an input this snapshot cannot supply (a Taunt rule without the move's
power, an item whose battle handler is not classified); it is never LEGAL.

The opponent's ability, moves and the Imprison state are read from the opponent's technical record,
because the GAME enforces them. The verdict reason given to the model is generic ("blocked by the
opponent"), never the ability or move list itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pokegent.actions.battle import item_classes as IC
from pokegent.actions.battle import state as S
from pokegent.actions.battle.view import BattleView, MonView, sleepers_on_the_field
from pokegent.options.models import Legality

MOVE_STRUGGLE = 165
MOVE_UNAVAILABLE = 0xFFFF
ITEM_CHOICE_BAND = 186
HOLD_EFFECT_CHOICE_BAND = 29
HOLD_EFFECT_CAN_ALWAYS_RUN = 37
ABILITY_SHADOW_TAG = 23
ABILITY_LEVITATE = 26
ABILITY_MAGNET_PULL = 42
ABILITY_RUN_AWAY = 50
ABILITY_ARENA_TRAP = 71
TYPE_FLYING = 2
TYPE_STEEL = 8

#: stat code of ``ItemEffectInfo.stat_boost`` -> index in ``Battler.stat_stages`` (``STAGE_NAMES``)
BOOST_STAGE_INDEX = {"atk": 1, "def": 2, "spe": 3, "spa": 4, "acc": 6}
MAX_STAGE = 6


def active_player(view: BattleView) -> S.Battler | None:
    """The player battler which owns this question (battler 2 on a second double turn)."""
    battler = view.block.battler_at(view.player.battler)
    return battler if battler is not None and battler.side == "player" else view.block.player


@dataclass(frozen=True)
class Verdict:
    legality: Legality
    reason: str | None = None
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def chooseable(self) -> bool:
        return self.legality.chooseable


LEGAL = Verdict(Legality.LEGAL)


def unavailable(reason: str, **facts: Any) -> Verdict:
    return Verdict(Legality.UNAVAILABLE, reason, dict(facts))


def unknown(reason: str, **facts: Any) -> Verdict:
    return Verdict(Legality.UNKNOWN, reason, dict(facts))


def unsupported(reason: str, **facts: Any) -> Verdict:
    return Verdict(Legality.UNSUPPORTED, reason, dict(facts))


# -- moves -----------------------------------------------------------------------------------------


def _imprisoned(view: BattleView, move_id: int) -> bool:
    """``GetImprisonedMovesCount``: an opposing battler under Imprison knows the move."""
    player = active_player(view)
    for b in view.block.battlers:
        if player is None or b.side == player.side:
            continue
        if b.status3 & S.STATUS3_IMPRISONED_OTHERS and any(m.move_id == move_id for m in b.moves):
            return True
    return False


def move_verdicts(view: BattleView) -> tuple[list[Verdict | None], bool]:
    """Per move slot of the player's active mon (``None`` for an empty slot) and the struggle flag.

    ``struggle`` is True when every slot is unusable: the game then uses Struggle by itself after
    FIGHT (``BattleScript_NoMovesLeft``) and offers no move choice.
    """
    me = active_player(view)
    if me is None:
        return [None] * S.MOVE_SLOTS, False
    t = view.tables
    out: list[Verdict | None] = []
    for slot in range(S.MOVE_SLOTS):
        move = me.moves[slot].move_id
        pp = me.moves[slot].pp
        if move == 0:
            out.append(None)
            continue
        if pp == 0:
            out.append(unavailable("no_pp"))
            continue
        if move == me.disable.disabled_move:
            out.append(unavailable("disabled", disabled_turns=me.disable.disable_timer))
            continue
        if me.status2 & S.STATUS2_TORMENT and move == me.last_move and move != MOVE_STRUGGLE:
            out.append(unavailable("tormented"))
            continue
        if me.disable.taunt_timer:
            data = t.move_data(move) if t else None
            if data is None:
                out.append(unknown("taunted_move_power_unreadable"))
                continue
            if data.power == 0:
                out.append(unavailable("taunted", taunt_turns=me.disable.taunt_timer))
                continue
        if _imprisoned(view, move):
            out.append(unavailable("imprisoned"))
            continue
        if me.disable.encore_timer and me.disable.encored_move != move:
            out.append(unavailable("encored", encored_move=me.disable.encored_move))
            continue
        if (
            _holds_choice_band(view)
            and me.choiced_move not in (0, MOVE_UNAVAILABLE)
            and me.choiced_move != move
        ):
            out.append(unavailable("choice_locked", locked_move=me.choiced_move))
            continue
        out.append(LEGAL)
    usable = [v for v in out if v is not None and v.chooseable]
    struggle = not usable and all(v is None or v.legality is Legality.UNAVAILABLE for v in out)
    return out, struggle


def _holds_choice_band(view: BattleView) -> bool:
    me = active_player(view)
    if me is None:
        return False
    if me.item == ITEM_CHOICE_BAND:
        return True
    info = view.block.lookups.items.get(me.item)
    return bool(info and info.hold_effect == HOLD_EFFECT_CHOICE_BAND)


# -- trapping --------------------------------------------------------------------------------------


def trap_cause(view: BattleView) -> str | None:
    """Why the player's mon cannot switch out or run (``None``: free to leave).

    Mirrors ``HandleTurnActionSelectionState`` (switch) and ``IsRunningFromBattleImpossible`` (run);
    the two share the same conditions except the Run Away / Smoke Ball exemptions, handled in
    :func:`escape_verdict`.
    """
    me = active_player(view)
    if me is None:
        return "unknown"
    if me.status2 & (S.STATUS2_WRAPPED | S.STATUS2_ESCAPE_PREVENTION):
        return "held_by_move"
    if me.status3 & S.STATUS3_ROOTED:
        return "rooted"
    for b in view.block.battlers:
        if b.side == me.side:
            continue
        if b.ability == ABILITY_SHADOW_TAG:
            return "opponent_ability"
        if (
            b.ability == ABILITY_ARENA_TRAP
            and TYPE_FLYING not in me.types
            and me.ability != ABILITY_LEVITATE
        ):
            return "opponent_ability"
        if b.ability == ABILITY_MAGNET_PULL and TYPE_STEEL in me.types:
            return "opponent_ability"
    return None


def switch_verdict(view: BattleView, slot: int, *, forced: bool) -> Verdict:
    mon = view.mon(slot)
    if mon is None:
        return unavailable("no_such_party_slot")
    # TrySwitchInPokemon checks the live party HP before it checks whether the
    # member is already in battle.  During a fainted replacement the party
    # struct can still contain the pre-faint HP for one frame; gBattleMons is
    # the authoritative current HP for an active member in that frame.
    if mon.active:
        active = [
            b
            for b in view.block.player_battlers
            if b.personality == mon.personality and b.species == mon.species
        ]
        if len(active) != 1 or not view.block.valid:
            return unknown("active_hp_unreadable")
        if active[0].hp <= 0:
            return unavailable("fainted")
    elif mon.fainted:
        return unavailable("fainted")
    if mon.active:
        # The game refuses every currently deployed player mon, even during a
        # forced replacement.  In doubles this also excludes the partner.
        return unavailable("already_in_battle")
    if mon.is_egg:
        return unavailable("egg")
    if not forced:
        cause = trap_cause(view)
        if cause == "unknown":
            return unknown("trap_state_unreadable")
        if cause:
            return unavailable("trapped", cause=cause)
    return LEGAL


def escape_verdict(view: BattleView) -> Verdict:
    if view.is_trainer:
        return unavailable("trainer_battle")
    if view.kind == "ghost":
        return Verdict(Legality.LEGAL, None, {"guaranteed": True})
    if view.kind not in ("wild", "double"):
        return unsupported(f"{view.kind}_battle")
    me = active_player(view)
    if me is None:
        return unknown("player_battler_unreadable")
    info = view.block.lookups.items.get(me.item)
    if (info and info.hold_effect == HOLD_EFFECT_CAN_ALWAYS_RUN) or me.ability == ABILITY_RUN_AWAY:
        return Verdict(Legality.LEGAL, None, {"guaranteed": True})
    cause = trap_cause(view)
    if cause == "unknown":
        return unknown("trap_state_unreadable")
    if cause:
        return unavailable("trapped", cause=cause)
    return Verdict(Legality.CONDITIONAL, None, {"guaranteed": False})


# -- items -----------------------------------------------------------------------------------------

#: bag pocket name -> pocket index in the battle bag UI (0 items, 1 key items, 2 poke balls)
MENU_POCKET = {"items": 0, "key_items": 1, "poke_balls": 2}

#: ``PokemonUseItemEffects`` cure code -> the status names of ``MonView.status`` / the volatile
#: names of the active Pokemon it clears (a poison cure also clears toxic)
CURE_STATUS = {
    "poison": ("poison", "toxic"),
    "burn": ("burn",),
    "sleep": ("sleep",),
    "freeze": ("freeze",),
    "paralysis": ("paralysis",),
}
CURE_VOLATILE = {"confusion": "confusion", "infatuation": "infatuated"}


def item_role(view: BattleView, item_id: int) -> str | None:
    info = view.block.lookups.items.get(item_id)
    return info.role if info else None


def item_classes(view: BattleView, item_id: int) -> tuple[IC.ItemClass, ...]:
    """The offered item-use classes of a bag item (empty: it has none)."""
    classes = IC.load().classes_of(view.block.lookups.items.get(item_id))
    return tuple(c for c in classes if c.offered)


def _item_verdict(cls: IC.ItemClass, code: str | None, **facts: object) -> Verdict:
    facts = {"item_class": cls.id, **facts}
    if code is None:
        return Verdict(Legality.LEGAL, None, facts)
    return unavailable(code, **facts)


def _member_code(
    view: BattleView, mon: MonView, cls: IC.ItemClass, effect: S.ItemEffectInfo
) -> str | None:
    """Why a party-target class cannot change ``mon`` (``None``: it can), per
    ``PokemonUseItemEffects``."""
    if cls.rule == "fainted":  # the Revive family only works on a fainted Pokemon
        return None if mon.fainted else "not_fainted"
    if mon.fainted:
        return "fainted"
    # the Pokemon on the field is read from its battle record (gBattleMons), the rest from the party
    hp, max_hp = (view.player.hp, view.player.max_hp) if mon.active else (mon.hp, mon.max_hp)
    if cls.rule == "hurt":
        return None if hp is None or max_hp is None or hp < max_hp else "full_hp"
    if cls.rule == "has_status":
        have = set(mon.status) | (set(view.player.status) if mon.active else set())
        volatile = set(view.player.volatile) if mon.active else set()
        for cure in effect.cures:
            if have & set(CURE_STATUS.get(cure, ())) or CURE_VOLATILE.get(cure) in volatile:
                return None
        return "no_status_to_cure"
    return None


def _boost_possible(view: BattleView, effect: S.ItemEffectInfo) -> bool | None:
    """Can an X item / Dire Hit / Guard Spec. change anything now? ``None``: not readable."""
    me = active_player(view)
    if me is None:
        return None
    for stat, _stages in effect.stat_boost:
        index = BOOST_STAGE_INDEX.get(stat)
        if index is None or index >= len(me.stat_stages):
            return None
        if me.stat_stages[index] < MAX_STAGE:
            return True
    if effect.focus_energy and "focus_energy" not in view.player.volatile:
        return True
    if effect.guard_spec:
        sides = view.block.sides
        if sides is None:
            return None
        return not sides[0].mist
    return False if (effect.stat_boost or effect.focus_energy) else None


def item_target_verdicts(view: BattleView, item_id: int) -> list[tuple[int, Verdict]]:
    """``[(target_slot, verdict)]`` for a bag item; target ``-1`` means "no target (active mon)".

    The item's handler and effect bytes come from generated ROM data; the class rules come from
    ``references/battle_item_classes.yaml``. One entry per candidate target for party-target
    classes. The list is empty only when the item has no business in the options at all (not in the
    bag, a ball: those are THROW_BALL, no battle use). A use that provably changes nothing is
    UNAVAILABLE with the game rule as reason; what cannot be proven stays LEGAL.
    """
    qty = view.bag.get(item_id, 0)
    if qty <= 0:
        return []
    pocket = view.bag_pocket.get(item_id, "")
    info = view.block.lookups.items.get(item_id)
    role = info.role if info else None
    if role == "ball" or pocket == "poke_balls":
        return []
    if pocket not in MENU_POCKET:
        return []  # TM/HM case and berry pouch contents are not battle bag entries
    if role == "none":
        return []  # no battle use: it does not appear in the battle options at all
    if role is None:
        return [(-1, unknown("item_battle_handler_unclassified"))]
    if not IC.load().classes_of(info):
        return [(-1, unknown("item_class_unreadable"))]  # its effect bytes did not select a class
    classes = item_classes(view, item_id)
    if not classes:
        return []  # a class that is not offered (a key item): no option at all
    first = classes[0]
    if first.rule == "unsupported":
        return [(-1, unsupported(f"{role}_item_needs_extra_menu_steps", item_class=first.id))]
    if first.target == "party_member":
        assert info is not None and info.effect is not None
        out: list[tuple[int, Verdict]] = []
        for mon in view.party:
            if mon.is_egg:
                out.append((mon.slot, unavailable("egg", item_class=first.id)))
                continue
            codes = [_member_code(view, mon, cls, info.effect) for cls in classes]
            if None in codes:
                out.append((mon.slot, _item_verdict(first, None)))
                continue
            reason = " and ".join(dict.fromkeys(c for c in codes if c))
            out.append((mon.slot, _item_verdict(first, reason.replace(" ", "_"))))
        return out
    return [(-1, _active_verdict(view, first, info))]


def _active_verdict(view: BattleView, cls: IC.ItemClass, info: S.ItemInfo | None) -> Verdict:
    if cls.rule == "someone_asleep":
        return _item_verdict(cls, None if sleepers_on_the_field(view) else "nobody_asleep")
    if cls.rule == "wild_only":
        return _item_verdict(cls, "trainer_battle" if view.is_trainer else None)
    if cls.rule == "boost_possible":
        possible = _boost_possible(view, info.effect) if info and info.effect else None
        return _item_verdict(cls, "already_in_effect" if possible is False else None)
    return _item_verdict(cls, None)


def ball_verdict(view: BattleView, ball_id: int) -> Verdict:
    if view.bag.get(ball_id, 0) <= 0:
        return unavailable("none_left")
    if view.is_trainer:
        return unavailable("trainer_battle")
    if view.kind != "wild":
        return unsupported(f"{view.kind}_battle")
    return Verdict(Legality.CONDITIONAL, None, {"catch_uncertain": True})
