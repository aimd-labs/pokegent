"""Pure legality functions for moves and switches.

Unknown inputs give ``Legality.UNKNOWN``, never ``LEGAL``. Hidden information that could only
make an action *fail* (an opposing trapping ability the species may have) gives ``CONDITIONAL``:
the input is allowed, the outcome is uncertain (docs/contracts.md section 2).

Rules are taken from the decompile (pret/pokefirered, commit pinned in ``source-lock.md``):

* move selection: ``CheckMoveLimitations`` / ``TrySetCantSelectMoveBattleScript``
  (src/battle_util.c): PP, Disable, Torment, Taunt, Imprison, Encore, Choice Band;
  ``AreAllMovesUnusable`` -> Struggle.
* switching: ``B_ACTION_SWITCH`` in ``HandleTurnActionSelectionState`` (src/battle_main.c):
  wrapped / escape prevention (Mean Look, Block) / Ingrain, Shadow Tag, Arena Trap (not Flying
  or Levitate), Magnet Pull (Steel); the party menu refuses the active mon, fainted mons and eggs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pokegent.games.gen3 import mechanics_constants as K
from pokegent.games.gen3 import mechanics_ids as I
from pokegent.games.gen3.firered.rom.rom_tables import GameTables
from pokegent.games.gen3.mechanics.views import BattlerView, FieldView
from pokegent.options.models import Legality


@dataclass(frozen=True)
class Verdict:
    legality: Legality
    reason: str  # English, concise
    code: str  # machine-readable: "ok", "no_pp", "disabled", "taunted", "tormented", ...

    @property
    def chooseable(self) -> bool:
        return self.legality.chooseable


def _ok(reason: str = "usable") -> Verdict:
    return Verdict(Legality.LEGAL, reason, "ok")


def _no(code: str, reason: str) -> Verdict:
    return Verdict(Legality.UNAVAILABLE, reason, code)


def _unknown(code: str, reason: str) -> Verdict:
    return Verdict(Legality.UNKNOWN, reason, code)


def _holds_choice_band(att: BattlerView, tables: GameTables) -> bool | None:
    if att.held_item is None:
        return None
    if att.held_item == 0:
        return False
    return tables.item(att.held_item).hold_effect == I.HOLD_EFFECT_CHOICE_BAND


def move_legality(
    attacker: BattlerView,
    slot: int,
    tables: GameTables,
    field: FieldView | None = None,
    opponents: Sequence[BattlerView] = (),
) -> Verdict:
    """Can ``attacker`` select the move in ``slot`` (0..3) this turn?"""
    if not 0 <= slot < len(attacker.moves):
        return _no("no_move", "there is no move in this slot")
    sl = attacker.moves[slot]
    if sl.move_id == 0:
        return _no("no_move", "the slot is empty")
    move = tables.move(sl.move_id)
    unknown = attacker.unknown

    # A multi-turn lock (rampage, Rollout, charge turn, Hyper Beam recharge) decides the move.
    if K.V_RECHARGING in attacker.volatile:
        return _no("recharging", "the Pokemon must recharge this turn")
    if attacker.locked_move_id is not None:
        if sl.move_id == attacker.locked_move_id:
            return Verdict(Legality.LEGAL, "locked into this move", "locked_move")
        return _no("locked_move", "locked into another move")

    # No PP (CheckMoveLimitations: MOVE_LIMITATION_PP). Unknown PP is not "has PP".
    if sl.pp is None:
        return _unknown("unknown_pp", "the remaining PP is unknown")
    if sl.pp == 0:
        return _no("no_pp", "no PP left")

    if sl.disabled or attacker.disabled_move_id == sl.move_id:
        return _no("disabled", "disabled")
    if "disable" in unknown:
        return _unknown("unknown_state", "Disable state could not be read")

    tormented = K.V_TORMENT in attacker.volatile or sl.torment_blocked
    if (
        tormented
        and (sl.torment_blocked or attacker.last_move_id == sl.move_id)
        and (sl.move_id != I.MOVE_STRUGGLE)
    ):
        return _no("tormented", "Torment forbids using the same move twice in a row")
    if "torment" in unknown:
        return _unknown("unknown_state", "Torment state could not be read")

    if (K.V_TAUNT in attacker.volatile or sl.taunt_blocked) and move.power == 0:
        return _no("taunted", "Taunt forbids status moves")
    if "taunt" in unknown and move.power == 0:
        return _unknown("unknown_state", "Taunt state could not be read")

    if sl.imprisoned:
        return _no("imprisoned", "a foe's Imprison blocks this move")
    foes = [o for o in opponents if o.is_player_side != attacker.is_player_side]
    for foe in foes:
        if K.V_IMPRISON in foe.volatile and any(s.move_id == sl.move_id for s in foe.moves):
            return _no("imprisoned", "a foe's Imprison blocks this move")
    if "imprison" in unknown:
        return _unknown("unknown_state", "Imprison state could not be read")

    if K.V_ENCORE in attacker.volatile and attacker.encored_move_id not in (None, sl.move_id):
        return _no("encored", "Encore forces another move")
    if sl.choice_blocked:
        return _no("choice_locked", "a Choice item locks another move")
    locked = attacker.choice_locked_move_id
    if locked is not None and locked != sl.move_id:
        holds = _holds_choice_band(attacker, tables)
        if holds is None:
            return _unknown("unknown_state", "the held item is unknown; a Choice lock may apply")
        if holds:
            return _no("choice_locked", "the Choice item locks the first move used")
    if "choice" in unknown:
        return _unknown("unknown_state", "Choice lock state could not be read")
    return _ok()


def struggle_forced(
    attacker: BattlerView,
    tables: GameTables,
    field: FieldView | None = None,
    opponents: Sequence[BattlerView] = (),
) -> Verdict:
    """Legality of the Struggle option: LEGAL exactly when every move slot is unusable.

    ``AreAllMovesUnusable`` (src/battle_util.c): all four slots blocked by PP, Disable, Torment,
    Taunt, Imprison, Encore or Choice. UNKNOWN while an unreadable slot could still be usable.
    """
    verdicts = [
        move_legality(attacker, i, tables, field, opponents) for i in range(len(attacker.moves))
    ]
    if any(v.legality.chooseable for v in verdicts):
        return _no("moves_available", "at least one move can still be selected")
    if not verdicts or any(v.legality is Legality.UNKNOWN for v in verdicts):
        return _unknown("unknown_state", "a move's usability could not be determined")
    return Verdict(Legality.LEGAL, "no move can be selected; Struggle is forced", "struggle")


def _foe_trap_candidates(
    foes: Sequence[BattlerView], tables: GameTables
) -> tuple[list[tuple[BattlerView, int]], list[tuple[BattlerView, int]]]:
    """(known trapping abilities, possible trapping abilities) among the opposing battlers."""
    known: list[tuple[BattlerView, int]] = []
    possible: list[tuple[BattlerView, int]] = []
    trapping = (I.ABILITY_SHADOW_TAG, I.ABILITY_ARENA_TRAP, I.ABILITY_MAGNET_PULL)
    for foe in foes:
        if foe.ability is not None:
            if foe.ability in trapping:
                known.append((foe, foe.ability))
        elif foe.species is not None and 0 < foe.species < len(tables.all_species):
            for ability in tables.species(foe.species).abilities:
                if ability in trapping:
                    possible.append((foe, ability))
    return known, possible


def switch_legality(
    active: BattlerView,
    candidate: BattlerView,
    tables: GameTables,
    field: FieldView | None = None,
    opponents: Sequence[BattlerView] = (),
    *,
    forced: bool = False,
) -> Verdict:
    """Can ``active`` be replaced by ``candidate``?

    ``forced`` marks a replacement after a faint (Send out): trapping does not apply then.
    """
    if candidate.is_egg:
        return _no("egg", "an Egg cannot battle")
    if candidate.species is None or candidate.hp is None:
        return _unknown("unknown_candidate", "the party member could not be read")
    if candidate.hp == 0:
        return _no("fainted", "the Pokemon has fainted")
    if candidate.position is not None:
        return _no("already_active", "this Pokemon is already in the battle")

    if forced:
        return _ok("replacement for a fainted Pokemon")

    vol = active.volatile
    if K.V_TRAPPED in vol or K.V_ROOTED in vol:
        return _no("trapped", "the active Pokemon cannot switch out (bound, Mean Look or Ingrain)")
    if "trapped" in active.unknown:
        return _unknown("unknown_state", "whether the active Pokemon is trapped could not be read")

    foes = [o for o in opponents if o.is_player_side != active.is_player_side]
    known, possible = _foe_trap_candidates(foes, tables)

    def applies(ability: int) -> bool:
        if ability == I.ABILITY_SHADOW_TAG:
            return True
        types = active.types
        if types is None and active.species is not None:
            types = tables.species(active.species).types
        if ability == I.ABILITY_ARENA_TRAP:
            if types is None or active.ability is None:
                return True  # cannot prove the exemption; report as possible
            return K.TYPE_FLYING not in types and active.ability != I.ABILITY_LEVITATE
        if ability == I.ABILITY_MAGNET_PULL:
            return types is None or K.TYPE_STEEL in types
        return False

    for _foe, ability in known:
        if applies(ability):
            return _no("trapped", f"{tables.ability_name(ability).title()} prevents switching")
    for _foe, ability in possible:
        if applies(ability):
            return Verdict(
                Legality.CONDITIONAL,
                f"the opposing Pokemon may have {tables.ability_name(ability).title()}",
                "maybe_trapped",
            )
    return _ok("can switch")
