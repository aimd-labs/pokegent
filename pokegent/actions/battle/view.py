"""``BattleView``: the read-only, player-observable battle state facts providers work on.

The mechanics library (``pokegent.games.gen3.mechanics.facts``) is built in parallel and has its own
``BattlerView``/``FieldView`` types. This family depends on it only through the
:class:`MoveFactsProvider` Protocol below, which the integrator adapts. :class:`BattleView` is the
family's own, complete and plain description of "the battle right now":

* the player's active mon with everything RAM knows (the player may see its stats, moves, PP);
* the opponent's active mon as the player sees it: species, level, HP percent, status, visible
  stat stages and types. Its exact HP, stats, moves, ability and item are NOT on the view; they
  sit in
  ``opponent_technical`` (tagged, for the mechanics library to use or ignore; model-facing facts
  must
  not echo them);
* the player's party (from the core snapshot) and bag;
* the field (weather, battle kind) and the ROM tables for names and move data.

:class:`BasicFacts` is the plain fallback provider: names, types, PP, power and accuracy straight
from RAM/ROM, nothing computed (no damage, no effectiveness).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pokegent.actions.battle import state as S
from pokegent.actions.battle.lookups import Lookups
from pokegent.actions.battle.romtables import RomTables
from pokegent.games.gen3.firered.reader.schemas import PartyMember, StateSnapshot


@dataclass(frozen=True)
class MonView:
    """One member of the player's party."""

    slot: int
    species: int
    name: str
    level: int
    hp: int
    max_hp: int
    status: tuple[str, ...]
    is_egg: bool
    moves: tuple[tuple[int, int], ...]  # (move_id, pp), empty slots omitted
    personality: int
    held_item: int
    active: bool = False  # currently on the field
    # --- own-party facts for the mechanics adapter (additive, M2.5) ---------------------------
    #: (attack, defense, speed, sp_attack, sp_defense) from the party struct; None = not read
    stats: tuple[int, int, int, int, int] | None = None
    ability_num: int | None = None  # 0/1: which of the species' two abilities
    friendship: int | None = None

    @property
    def fainted(self) -> bool:
        return self.hp <= 0

    @property
    def hp_percent(self) -> int:
        return 0 if self.hp <= 0 or self.max_hp <= 0 else max(1, (self.hp * 100) // self.max_hp)


@dataclass(frozen=True)
class BattlerView:
    """An active battler. For the opponent only observable fields are filled."""

    battler: int
    side: str
    species: int
    name: str
    level: int
    hp_percent: int
    status: tuple[str, ...]
    volatile: tuple[str, ...]
    stat_stages: dict[str, int]  # visible via messages and the in-battle stat arrows
    types: tuple[str, ...]
    # player side only (None for the opponent):
    hp: int | None = None
    max_hp: int | None = None
    ability: str | None = None
    held_item: int | None = None
    moves: tuple[tuple[int, int, int], ...] = ()  # (move_id, pp, max_pp), empty slots omitted
    stats: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class BattleView:
    kind: str
    is_trainer: bool
    weather: tuple[str, ...]
    player: BattlerView
    opponent: BattlerView
    party: tuple[MonView, ...]
    #: bag quantities by item id (all pockets)
    bag: dict[int, int]
    #: pocket name by item id present in the bag
    bag_pocket: dict[int, str]
    block: S.BattleBlock
    tables: Lookups | None
    # : HIDDEN information about the opponent (exact HP/stats/moves/ability/item). Technical use
    # only.
    opponent_technical: dict[str, Any] = field(default_factory=dict)
    enemy_party_alive: int | None = None
    enemy_party_size: int | None = None
    #: badge numbers 1..8 the player owns (core snapshot, FLAG_BADGE0x_GET); None = not known
    badges: tuple[int, ...] | None = None

    def mon(self, slot: int) -> MonView | None:
        for m in self.party:
            if m.slot == slot:
                return m
        return None


@runtime_checkable
class MoveFactsProvider(Protocol):
    """Facts for the options of ``battle.action`` (owned by this family, implemented elsewhere).

    Every method returns a JSON-like ``dict`` (str keys; int/float/str/bool/list/dict values). The
    builder merges it into ``Option.facts``; a missing or raising provider degrades to
    :class:`BasicFacts` (facts are advice, legality never depends on them).
    """

    def move_facts(self, view: BattleView, slot: int) -> dict: ...

    def switch_facts(self, view: BattleView, slot: int) -> dict: ...

    def item_facts(self, view: BattleView, item_id: int, target_slot: int) -> dict: ...

    def ball_facts(self, view: BattleView, ball_id: int) -> dict: ...

    def safari_facts(self, view: BattleView) -> dict: ...

    def escape_facts(self, view: BattleView) -> dict: ...


# -- building a view ---------------------------------------------------------------------------


def _mon_name(m: PartyMember, tables: Lookups | None) -> str:
    if m.nickname:
        return m.nickname
    species = tables.species_name(m.species) if tables else None
    return species or f"species {m.species}"


def _stage_dict(stages: tuple[int, ...]) -> dict[str, int]:
    return {n: s for n, s in zip(S.STAGE_NAMES, stages, strict=False) if s != 0 and n != "hp"}


def make_view(snapshot: StateSnapshot, block: S.BattleBlock) -> BattleView:
    """Assemble the view. ``block`` must be an active, valid, supported-or-not battle block."""
    tables = Lookups(block.lookups)
    chooser = block.decision_battler
    player = block.battler_at(chooser) if chooser is not None else None
    if player is None or player.side != "player":
        player = block.player
    opponent = block.battler_at(player.battler ^ 1) if player is not None else None
    if opponent is None or opponent.side != "opponent" or not opponent.present:
        opponent = next(iter(block.opponent_battlers), None)
    if player is None or opponent is None:
        raise ValueError("battle block has no player/opponent battler")

    def species_name(species: int) -> str:
        return (tables.species_name(species) if tables else None) or f"species {species}"

    def type_names(b: S.Battler) -> tuple[str, ...]:
        return tuple(RomTables.type_name(t) for t in b.types)

    def pp_max(b: S.Battler, slot: int) -> int:
        move = b.moves[slot]
        data = tables.move_data(move.move_id) if tables else None
        if data is None:
            return move.pp
        bonus = (b.pp_bonuses >> (2 * slot)) & 3
        return data.pp + (data.pp * 20 * bonus) // 100

    party: list[MonView] = []
    for m in snapshot.party:
        party.append(
            MonView(
                slot=m.slot,
                species=m.species,
                name=_mon_name(m, tables),
                level=m.level,
                hp=m.hp,
                max_hp=m.max_hp,
                status=S.status_names(m.status),
                is_egg=m.is_egg,
                moves=tuple((mv.move_id, mv.pp) for mv in m.moves if mv.move_id),
                personality=m.personality,
                held_item=m.held_item,
                active=any(
                    m.personality == active.personality and m.species == active.species
                    for active in block.player_battlers
                ),
                stats=(
                    m.stats.attack,
                    m.stats.defense,
                    m.stats.speed,
                    m.stats.sp_attack,
                    m.stats.sp_defense,
                ),
                ability_num=m.ability_num,
                friendship=m.friendship,
            )
        )

    nick = None
    for mon in party:
        if mon.active:
            nick = mon.name
    pview = BattlerView(
        battler=player.battler,
        side="player",
        species=player.species,
        name=nick or species_name(player.species),
        level=player.level,
        hp_percent=player.hp_percent,
        status=S.status_names(player.status1),
        volatile=S.volatile_names(player.status2, player.status3),
        stat_stages=_stage_dict(player.stat_stages),
        types=type_names(player),
        hp=player.hp,
        max_hp=player.max_hp,
        ability=(tables.ability_name(player.ability) if tables else None),
        held_item=player.item,
        moves=tuple((m.move_id, m.pp, pp_max(player, m.slot)) for m in player.moves if m.move_id),
        stats={
            "attack": player.attack,
            "defense": player.defense,
            "speed": player.speed,
            "sp_attack": player.sp_attack,
            "sp_defense": player.sp_defense,
        },
    )
    oview = BattlerView(
        battler=opponent.battler,
        side="opponent",
        species=opponent.species,
        name=species_name(opponent.species),
        level=opponent.level,
        hp_percent=opponent.hp_percent,
        status=S.status_names(opponent.status1),
        volatile=S.volatile_names(opponent.status2, opponent.status3),
        stat_stages=_stage_dict(opponent.stat_stages),
        types=type_names(opponent),
    )
    technical = {
        "hp": opponent.hp,
        "max_hp": opponent.max_hp,
        "stats": {
            "attack": opponent.attack,
            "defense": opponent.defense,
            "speed": opponent.speed,
            "sp_attack": opponent.sp_attack,
            "sp_defense": opponent.sp_defense,
        },
        "personality": opponent.personality,
        "moves": [(m.move_id, m.pp) for m in opponent.moves if m.move_id],
        "ability": opponent.ability,
        "item": opponent.item,
        "status2": opponent.status2,
        "status3": opponent.status3,
        "_tag": "technical: not observable by the player; never put this into model-facing text",
    }

    bag: dict[int, int] = {}
    bag_pocket: dict[int, str] = {}
    for pocket in snapshot.bag:
        for item in pocket.items:
            bag[item.item_id] = bag.get(item.item_id, 0) + item.quantity
            bag_pocket[item.item_id] = pocket.pocket

    return BattleView(
        kind=block.kind,
        is_trainer=bool(block.type_flags & (1 << 3)),
        weather=block.weather,
        player=pview,
        opponent=oview,
        party=tuple(party),
        bag=bag,
        bag_pocket=bag_pocket,
        block=block,
        tables=tables,
        opponent_technical=technical,
        enemy_party_alive=block.enemy_party.alive if block.enemy_party else None,
        enemy_party_size=block.enemy_party.size if block.enemy_party else None,
        badges=tuple(snapshot.player.badges) if snapshot.player is not None else None,
    )


#: pure HP healers and the status each status healer cures (include/constants/items.h ids)
_HP_HEALERS = frozenset({13, 19, 20, 21, 22, 26, 27, 28, 29, 30, 31, 32, 44})
_STATUS_CURES = {
    14: {"poison", "toxic"},
    15: {"burn"},
    16: {"freeze"},
    17: {"sleep"},
    18: {"paralysis"},
}
_FULL_HEAL = 23


def _no_effect(item_id: int, mon: MonView) -> bool | None:
    """Would the item do nothing to this mon? ``None``: not known from the id alone."""
    if item_id in _HP_HEALERS and item_id != 19:
        return mon.hp >= mon.max_hp
    if item_id in _STATUS_CURES:
        return not (set(mon.status) & _STATUS_CURES[item_id])
    if item_id == _FULL_HEAL:
        return not mon.status
    if item_id == 19:  # FULL RESTORE: HP and status
        return mon.hp >= mon.max_hp and not mon.status
    return None


def sleepers_on_the_field(view: BattleView) -> list[str]:
    """The sleeping battlers, as the player reads them ("your X", "the foe's Y")."""
    out = []
    if "sleep" in view.player.status:
        out.append(f"your {view.player.name}")
    if "sleep" in view.opponent.status:
        out.append(f"the foe's {view.opponent.name}")
    return out


# -- the fallback provider -----------------------------------------------------------------------


class BasicFacts:
    """Names, types, PP, power and accuracy; nothing computed. The default provider."""

    def move_facts(self, view: BattleView, slot: int) -> dict:
        moves = view.player.moves
        # ``slot`` indexes gBattleMons[0].moves (empty slots included); ``moves`` omits empty ones.
        player = view.block.battler_at(view.player.battler)
        raw = player.moves[slot] if player else None
        if raw is None or raw.move_id == 0:
            return {"slot": slot, "empty": True}
        facts: dict[str, Any] = {"slot": slot, "move_id": raw.move_id, "pp": raw.pp}
        for move_id, pp, pp_max in moves:
            if move_id == raw.move_id and pp == raw.pp:
                facts["max_pp"] = pp_max
                break
        t = view.tables
        if t is not None:
            name = t.move_name(raw.move_id)
            if name:
                facts["name"] = name
            data = t.move_data(raw.move_id)
            if data is not None:
                facts.update(
                    type=data.type_name,
                    power=data.power,
                    accuracy=data.accuracy,
                    priority=data.priority,
                    category=(
                        "status"
                        if data.power == 0
                        else "physical"
                        if data.is_physical
                        else "special"
                    ),
                )
        return facts

    def switch_facts(self, view: BattleView, slot: int) -> dict:
        mon = view.mon(slot)
        if mon is None:
            return {"slot": slot, "empty": True}
        return {
            "slot": slot,
            "name": mon.name,
            "level": mon.level,
            "hp": mon.hp,
            "max_hp": mon.max_hp,
            "hp_percent": mon.hp_percent,
            "status": list(mon.status),
            "fainted": mon.fainted,
            "is_egg": mon.is_egg,
        }

    def item_facts(self, view: BattleView, item_id: int, target_slot: int) -> dict:
        facts: dict[str, Any] = {"item_id": item_id, "quantity": view.bag.get(item_id, 0)}
        if view.tables is not None:
            name = view.tables.item_name(item_id)
            if name:
                facts["name"] = name
        info = view.block.lookups.items.get(item_id)
        if info is not None and info.role == "poke_flute":  # no target: it wakes the field
            sleepers = sleepers_on_the_field(view)
            facts["would_have_effect"] = bool(sleepers)
            if sleepers:
                facts["wakes"] = sleepers
            else:
                facts.update(reason="nobody in the battle is asleep", turn_still_used=True)
        mon = view.mon(target_slot) if target_slot >= 0 else None
        if mon is not None:
            facts.update(
                target=mon.name,
                target_slot=target_slot,
                target_hp=mon.hp,
                target_max_hp=mon.max_hp,
                target_status=list(mon.status),
            )
            no_effect = _no_effect(item_id, mon)
            if no_effect is not None:
                facts["no_effect"] = no_effect
        return facts

    def ball_facts(self, view: BattleView, ball_id: int) -> dict:
        facts: dict[str, Any] = {
            "item_id": ball_id,
            "quantity": view.bag.get(ball_id, 0),
            "target": view.opponent.name,
            "target_level": view.opponent.level,
            "target_hp_percent": view.opponent.hp_percent,
            "target_status": list(view.opponent.status),
        }
        if view.tables is not None:
            name = view.tables.item_name(ball_id)
            if name:
                facts["name"] = name
        return facts

    def safari_facts(self, view: BattleView) -> dict:
        return {
            **self.ball_facts(view, 5),
            "quantity": view.block.safari_balls,
            "safari_catch_factor": view.block.safari_catch_factor,
            "safari_escape_factor": view.block.safari_escape_factor,
        }

    def escape_facts(self, view: BattleView) -> dict:
        return {
            "attempts": view.block.run_tries,
            "escape_chance_pct": 100 if view.kind == "ghost" else None,
        }
