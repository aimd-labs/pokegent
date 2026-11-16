"""``MechanicsFacts``: the battle family's :class:`MoveFactsProvider` on the mechanics library.

The battle family describes "the battle right now" as a plain :class:`BattleView` and asks a
provider for the facts of each option (``Option.facts``). The mechanics library
(``pokegent.games.gen3.mechanics.facts``) computes them from its own ``BattlerView`` /
``FieldView``.
This module is the translation, nothing else; it decides no strategy and never adds an option.

What goes into the library views (CONTEXT.md invariant 8: no omniscient facts to the model)

========================  =====================================================================
player's active battler   everything RAM knows: stats, exact HP, stages, ability id, held item,
                          PP (max PP from the ROM move PP and the PP-bonus bits), Disable /
                          Encore / Taunt state, Torment, last move, Choice lock
opponent's active mon     what the player sees: species, level, HP bar percent, major status,
                          visible volatile effects, stat stages. ``hp``/``stats``/``ability``/
                          ``held_item`` stay ``None``, so the estimator derives RANGES from base
                          stats (IV 0..31, EV 0, nature 0.9..1.1) and the species' possible
                          abilities. ``BattleView.opponent_technical`` is used only by the
                          configured ``guide`` policy to select runtime-ROM/RAM move and held-item
                          knowledge; it is never copied into model-facing fields.
bench members             own party: stats, status, moves, friendship, ability by ``ability_num``
field                     kind (wild/trainer/...), weather from ``gBattleWeather`` (verified by the
                          battle family), badges from the badge flags of the core snapshot, turn
                          from ``gBattleResults``; weather negation unknown while a weather is up
========================  =====================================================================

Side conditions (Reflect, Light Screen, Mist, Safeguard, Spikes) come from the battle block's
``sides`` (``gSideStatuses`` / ``gSideTimers``, profile symbols verified in M2.5 on the corpus,
the runtime contractsb). When the block has no ``sides`` (a profile
without those symbols,
a failed read) the library still has no "unknown" mode for them, so this adapter computes every
damaging move twice, once without and once with Reflect + Light Screen on the opponent's side, and
reports the ENVELOPE; ``uncertain`` then lists ``opponent_screens_unread`` when that widened the
result.

A provider that raises is handled by the builder (it falls back to ``BasicFacts``); this module
therefore does not swallow errors.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from pokegent.actions.battle import legality as L
from pokegent.actions.battle import state as S
from pokegent.actions.battle.view import BattleView, MonView
from pokegent.games.gen3 import mechanics_constants as K
from pokegent.games.gen3.firered.reader.memory_port import MemoryPort
from pokegent.games.gen3.firered.rom.rom_data import RomProfile
from pokegent.games.gen3.firered.rom.rom_tables import GameTables
from pokegent.games.gen3.mechanics.facts import (
    ball_option_facts,
    item_option_facts,
    move_option_facts,
    switch_option_facts,
    threat_facts,
)
from pokegent.games.gen3.mechanics.views import (
    BattlerView,
    FieldView,
    MoveSlotView,
    SideView,
    StagesView,
    StatsView,
)


def _register_race_renderer() -> None:
    """``race`` facts reach the model through the fact renderer (contracts section 11)."""
    from pokegent.games.gen3.mechanics.race_facts import render_race
    from pokegent.options.render import register_fact_renderer

    def phrases(facts: Any) -> list[str]:  # the renderer protocol is a list of phrases
        text = render_race(facts)
        return [text] if text else []

    try:
        register_fact_renderer("race", phrases, before="speed")
    except ValueError:  # registered already (module imported twice under another name)
        pass


_register_race_renderer()

#: ``BattleBlock.kind`` -> ``FieldView.kind``
_KIND = {
    "wild": K.KIND_WILD,
    "trainer": K.KIND_TRAINER,
    "safari": K.KIND_SAFARI,
    "ghost": K.KIND_GHOST,
    "tutorial": K.KIND_TUTORIAL,
    "link": K.KIND_LINK,
}

#: battle-family volatile names (``state.volatile_names``) -> library names. Only effects the
#: player can see (a text box says so) are mapped for the opponent.
_VOLATILE = {
    "confusion": K.V_CONFUSION,
    "flinched": K.V_FLINCH,
    "focus_energy": K.V_FOCUS_ENERGY,
    "substitute": K.V_SUBSTITUTE,
    "tormented": K.V_TORMENT,
    "infatuated": K.V_INFATUATION,
    "recharging": K.V_RECHARGING,
    "foresight": K.V_FORESIGHT,
    "rooted": K.V_ROOTED,
}

#: stage index of ``Battler.stat_stages`` (``state.STAGE_NAMES``) -> ``StagesView`` field
_STAGE_FIELD = {
    "attack": "atk",
    "defense": "def_",
    "speed": "spe",
    "sp_attack": "spa",
    "sp_defense": "spd",
    "accuracy": "acc",
    "evasion": "eva",
}

#: item-use class (references/battle_item_classes.yaml) -> ``item_option_facts`` kind, for the kinds
#: that have no gItemEffectTable row
_KIND_OF_CLASS = {"wake": "wake_party", "escape": "escape"}

#: result keys that are ``[low, high]`` ranges and must widen to the envelope of two estimates
_RANGE_KEYS = (
    "estimated_damage_pct_of_hp",
    "estimated_damage_pct_of_max_hp",
    "estimated_damage",
    "crit_damage",
    "hits_to_ko",
)
_SCREENS = frozenset({K.SIDE_REFLECT, K.SIDE_LIGHT_SCREEN})


# -- translation helpers ------------------------------------------------------------------


def major_status(names: tuple[str, ...]) -> str:
    """One of ``constants.MAJOR_STATUSES`` from the battle family's status names."""
    for name in (K.STATUS_TOXIC, K.STATUS_POISON, K.STATUS_SLEEP, K.STATUS_BURN):
        if name in names:
            return name
    if K.STATUS_FREEZE in names:
        return K.STATUS_FREEZE
    if K.STATUS_PARALYSIS in names:
        return K.STATUS_PARALYSIS
    return K.STATUS_NONE


def stages_view(stages: tuple[int, ...] | dict[str, int]) -> StagesView:
    """Signed stages. ``tuple`` = ``Battler.stat_stages`` order; ``dict`` = the observable view."""
    values: dict[str, int] = {}
    if isinstance(stages, dict):
        for name, value in stages.items():
            if name in _STAGE_FIELD:
                values[_STAGE_FIELD[name]] = value
    else:
        for name, value in zip(S.STAGE_NAMES, stages, strict=False):
            if name in _STAGE_FIELD:
                values[_STAGE_FIELD[name]] = value
    return StagesView(**{k: max(-6, min(6, v)) for k, v in values.items()})


def _volatiles(names: tuple[str, ...]) -> frozenset[str]:
    return frozenset(_VOLATILE[n] for n in names if n in _VOLATILE)


def max_pp(tables: GameTables, move_id: int, bonuses: int, slot: int) -> int | None:
    """ROM base PP plus the PP-UP bits (+20 % of the base per bonus)."""
    if move_id <= 0:
        return None
    try:
        base = tables.move(move_id).pp
    except (IndexError, KeyError):
        return None
    return base + (base * 20 * ((bonuses >> (2 * slot)) & 3)) // 100


def _side_view(info: S.SideInfo) -> SideView:
    conditions = set()
    if info.reflect:
        conditions.add(K.SIDE_REFLECT)
    if info.light_screen:
        conditions.add(K.SIDE_LIGHT_SCREEN)
    if info.mist:
        conditions.add(K.SIDE_MIST)
    if info.safeguard:
        conditions.add(K.SIDE_SAFEGUARD)
    if info.spikes and info.spikes_layers:
        conditions.add(K.SIDE_SPIKES)
    return SideView(conditions=frozenset(conditions), spikes_layers=info.spikes_layers)


# -- the provider --------------------------------------------------------------------------


class MechanicsFacts:
    """Implements ``MoveFactsProvider`` (``move_facts``, ``switch_facts``, ``item_facts``,
    ``ball_facts``) with ``pokegent.games.gen3.mechanics.facts``.

    ``tables`` is the static game data (read once from the ROM image, ``from_port`` /
    ``from_rom_file``). ``widen_unread_screens=False`` trusts "no screens" (tests, or once the
    side-condition symbols are verified).
    """

    def __init__(
        self,
        tables: GameTables,
        *,
        widen_unread_screens: bool = True,
        opponent_knowledge: str = "guide",
    ) -> None:
        self.tables = tables
        self.widen_unread_screens = widen_unread_screens
        if opponent_knowledge not in ("guide", "fair_play"):
            raise ValueError("opponent_knowledge must be guide or fair_play")
        self.opponent_knowledge = opponent_knowledge

    # -- construction -------------------------------------------------------------------------

    @classmethod
    def from_port(cls, port: MemoryPort, profile: RomProfile, **kw: Any) -> MechanicsFacts:
        return cls(GameTables.from_port(port, profile), **kw)

    @classmethod
    def from_rom_file(
        cls,
        path: str | os.PathLike[str] | None = None,
        profile_id: str | None = None,
        **kw: Any,
    ) -> MechanicsFacts:
        return cls(GameTables.from_rom_file(path, profile_id), **kw)

    # -- views ----------------------------------------------------------------------------------

    def player_battler(self, view: BattleView) -> BattlerView:
        """The active player battler with everything RAM knows."""
        b = view.block.battler_at(view.player.battler)
        if b is None:
            raise ValueError("battle block has no player battler")
        moves = tuple(
            MoveSlotView(
                move_id=m.move_id,
                pp=m.pp if m.move_id else None,
                max_pp=max_pp(self.tables, m.move_id, b.pp_bonuses, m.slot),
            )
            for m in b.moves
        )
        names = S.status_names(b.status1)
        volatile = set(_volatiles(S.volatile_names(b.status2, b.status3)))
        if b.disable.taunt_timer:
            volatile.add(K.V_TAUNT)
        if b.disable.encore_timer and b.disable.encored_move:
            volatile.add(K.V_ENCORE)
        if b.disable.disable_timer and b.disable.disabled_move:
            volatile.add(K.V_DISABLE)
        if b.status2 & S.STATUS2_WRAPPED or b.status2 & S.STATUS2_ESCAPE_PREVENTION:
            volatile.add(K.V_TRAPPED)
        if b.status3 & S.STATUS3_IMPRISONED_OTHERS:
            volatile.add(K.V_IMPRISON)
        return BattlerView(
            species=b.species,
            level=b.level,
            hp=b.hp,
            max_hp=b.max_hp,
            stats=StatsView(
                atk=b.attack, def_=b.defense, spa=b.sp_attack, spd=b.sp_defense, spe=b.speed
            ),
            stat_stages=stages_view(b.stat_stages),
            status=major_status(names),
            sleep_turns=(b.status1 & S.STATUS1_SLEEP) or None,
            volatile=frozenset(volatile),
            types=(b.type1, b.type2),
            ability=b.ability,
            held_item=b.item,
            moves=moves,
            is_player_side=True,
            position=K.B_POSITION_PLAYER_LEFT,
            last_move_id=b.last_move or None,
            choice_locked_move_id=b.choiced_move or None,
            disabled_move_id=(b.disable.disabled_move if b.disable.disable_timer else None) or None,
            encored_move_id=(b.disable.encored_move if b.disable.encore_timer else None) or None,
            name=view.player.name,
        )

    def opponent_battler(self, view: BattleView) -> BattlerView:
        """The opposing mon as the player sees it. Hidden fields are ``None`` (ranges)."""
        o = view.opponent
        species = self.tables.species(o.species) if o.species is not None else None
        # Species types are public game knowledge.  A fixed ability is public too; otherwise
        # leave it unknown so the damage estimator widens instead of leaking technical RAM.
        abilities = species.abilities if species is not None else ()
        ability = abilities[0] if len(abilities) == 1 or abilities[0] == abilities[1] else None
        return BattlerView(
            species=o.species,
            level=o.level,
            hp=None,
            max_hp=None,
            hp_pct=float(o.hp_percent),
            stats=None,
            stat_stages=stages_view(o.stat_stages),
            status=major_status(o.status),
            volatile=_volatiles(o.volatile),
            types=species.types if species is not None else None,
            ability=ability,
            held_item=None,
            moves=(),
            is_player_side=False,
            position=K.B_POSITION_OPPONENT_LEFT,
            name=o.name,
        )

    def bench_battler(self, view: BattleView, mon: MonView) -> BattlerView:
        """A party member as a library view. The active one is the live battler instead."""
        if mon.active:
            return self.player_battler(view)
        stats = None
        if mon.stats is not None:
            atk, dfn, spe, spa, spd = mon.stats
            stats = StatsView(atk=atk, def_=dfn, spa=spa, spd=spd, spe=spe)
        ability = None
        if mon.ability_num is not None and 0 <= mon.species < 412:
            abilities = self.tables.species(mon.species).abilities
            ability = abilities[mon.ability_num] if mon.ability_num < len(abilities) else None
        return BattlerView(
            species=mon.species,
            level=mon.level,
            hp=mon.hp,
            max_hp=mon.max_hp,
            stats=stats,
            status=major_status(mon.status),
            ability=ability,
            held_item=mon.held_item,
            moves=tuple(MoveSlotView(move_id=mid, pp=pp) for mid, pp in mon.moves),
            is_player_side=True,
            friendship=mon.friendship,
            is_egg=mon.is_egg,
            name=mon.name,
        )

    def field_view(self, view: BattleView, *, opponent_screens: bool = False) -> FieldView:
        block = view.block
        weather = view.weather[0] if view.weather else K.WEATHER_NONE
        player = block.battler_at(view.player.battler)
        # Cloud Nine / Air Lock: known for the player's mon; unknown for the foe while a weather
        # is up (the foe's ability is hidden), so a weather-sensitive estimate widens.
        negated: bool | None = False
        if weather != K.WEATHER_NONE:
            from pokegent.games.gen3 import mechanics_ids as I

            own = player.ability if player is not None else None
            negated = True if own in (I.ABILITY_CLOUD_NINE, I.ABILITY_AIR_LOCK) else None
        if block.sides is not None:
            player_side, opponent_side = (_side_view(s) for s in block.sides)
        else:
            player_side = SideView()
            opponent_side = SideView(conditions=_SCREENS if opponent_screens else frozenset())
        return FieldView(
            kind=_KIND.get(block.kind, K.KIND_UNKNOWN),
            double=block.kind == "double",
            weather=weather,
            weather_negated=negated,
            turn=block.turn,
            player_side=player_side,
            opponent_side=opponent_side,
            badges=frozenset(view.badges) if view.badges is not None else None,
        )

    # -- the MoveFactsProvider protocol --------------------------------------------------------

    def _race(self, view: BattleView, **kw: Any) -> dict:
        """The exchange model's ``{"race": ...}`` facts for one option (the runtime contracts).

        An optional aid: descriptive numbers (who strikes first, how the fight ends), never an
        option removed or a choice advised. Any failure just means no ``race`` key.
        """
        try:
            from pokegent.games.gen3.mechanics.race_facts import from_battle_view

            return from_battle_view(view, self, **kw)
        except Exception:
            return {}

    def move_facts(self, view: BattleView, slot: int) -> dict:
        me = self.player_battler(view)
        foe = self.opponent_battler(view)
        facts = self._widened(
            view,
            lambda f: move_option_facts(me, foe, slot, self.tables, f, opponents=(foe,)),
        )
        threat = self._threat(view, me)
        if threat is not None:
            facts["foe_threat"] = threat
        option = "status" if facts.get("category") == "status" else "attack"
        return {**facts, **self._race(view, option=option, slot=slot)}

    def switch_facts(self, view: BattleView, slot: int) -> dict:
        mon = view.mon(slot)
        if mon is None:
            return {"slot": slot, "empty": True}
        active = self.player_battler(view)
        candidate = self.bench_battler(view, mon)
        foe = self.opponent_battler(view)
        forced = active.hp == 0  # the active mon fainted: this is the replacement screen
        facts = self._widened(
            view,
            lambda f: switch_option_facts(
                active, candidate, foe, self.tables, f, opponents=(foe,), forced=forced
            ),
        )
        threat = self._threat(view, candidate)
        if threat is not None:
            facts["foe_threat"] = threat
        return {**facts, **self._race(view, option="switch", switch_slot=slot, forced=forced)}

    def item_facts(self, view: BattleView, item_id: int, target_slot: int) -> dict:
        mon = view.mon(target_slot) if target_slot >= 0 else None
        target = self.bench_battler(view, mon) if mon is not None else None
        me = self.player_battler(view)
        classes = L.item_classes(view, item_id)
        if target is None and any(c.rule == "boost_possible" for c in classes):
            target = me  # X items, Dire Hit and Guard Spec. take no menu target: the active mon
        # the two kinds without an effect row come from the ROM handler class, not from item ids
        hint = next((_KIND_OF_CLASS[c.id] for c in classes if c.id in _KIND_OF_CLASS), None)
        return item_option_facts(
            item_id,
            target,
            self.tables,
            self.field_view(view),
            quantity=view.bag.get(item_id),
            battlers=(me, self.opponent_battler(view)),  # the POKe FLUTE wakes the whole field
            kind_hint=hint,
        )

    def ball_facts(self, view: BattleView, ball_id: int) -> dict:
        return ball_option_facts(
            ball_id,
            self.opponent_battler(view),
            self.tables,
            self.field_view(view),
            quantity=view.bag.get(ball_id),
        )

    def safari_facts(self, view: BattleView) -> dict:
        """Safari Ball probability from the live catch factor, not the species' base rate."""
        return ball_option_facts(
            5,
            self.opponent_battler(view),
            self.tables,
            self.field_view(view),
            quantity=view.block.safari_balls,
            safari_catch_factor=view.block.safari_catch_factor or None,
        )

    def escape_facts(self, view: BattleView) -> dict:
        """Gen-3 run probability envelope from own speed and the visible foe species/level."""
        if view.kind == "ghost":
            return {"escape_chance_pct": [100.0, 100.0], "guaranteed": True}
        me = self.player_battler(view)
        foe = self.opponent_battler(view)
        if me.stats is None or foe.species is None or foe.level is None:
            return {"escape_chance_pct": None, "attempts": view.block.run_tries}
        base = self.tables.species(foe.species).base_spe
        raw_lo = ((2 * base) * foe.level) // 100 + 5
        raw_hi = ((2 * base + 31) * foe.level) // 100 + 5
        foe_lo = max(1, (raw_lo * 90) // 100)
        foe_hi = max(1, (raw_hi * 110) // 100)

        def chance(foe_speed: int) -> float:
            if me.stats is not None and me.stats.spe >= foe_speed:
                return 100.0
            factor = min(255, (me.stats.spe * 128) // foe_speed + view.block.run_tries * 30)
            return round(100 * factor / 256, 1)

        chances = sorted((chance(foe_hi), chance(foe_lo)))
        return {
            "escape_chance_pct": chances,
            "attempts": view.block.run_tries,
            "own_speed": me.stats.spe,
            "foe_speed_range": [foe_lo, foe_hi],
        }

    # -- opponent knowledge and threats ---------------------------------------------------------

    def _known_opponent(self, view: BattleView) -> tuple[BattlerView, tuple[int, ...], str]:
        """Return only the move/item knowledge allowed by the configured policy."""
        visible = self.opponent_battler(view)
        block_mon = view.block.battler_at(view.opponent.battler)
        last = block_mon.last_move if block_mon is not None else 0
        technical = view.opponent_technical
        if self.opponent_knowledge == "fair_play":
            ids = (last,) if last else ()
            source = "seen in this battle"
        else:
            raw = technical.get("moves", ())
            ids = tuple(dict.fromkeys(int(pair[0]) for pair in raw if pair and pair[0]))
            if last:
                ids = tuple(dict.fromkeys((*ids, last)))
            source = "ROM battle data"
        item = None
        if self.opponent_knowledge == "guide" and view.is_trainer:
            candidate = technical.get("item")
            item = candidate if isinstance(candidate, int) else None
        foe = replace(
            visible,
            moves=tuple(MoveSlotView(move_id=move_id, pp=None) for move_id in ids),
            held_item=item,
        )
        return foe, ids, source

    def _threat(self, view: BattleView, target: BattlerView) -> dict[str, Any] | None:
        foe, moves, source = self._known_opponent(view)
        if not moves:
            return None
        return threat_facts(foe, target, moves, self.tables, self.field_view(view), source=source)

    # -- the screens envelope ------------------------------------------------------------------

    def _widened(self, view: BattleView, compute: Callable[[FieldView], dict]) -> dict:
        plain = compute(self.field_view(view))
        if view.block.sides is not None or not self.widen_unread_screens:
            return plain  # the screens were read (or the caller trusts "none")
        screened = compute(self.field_view(view, opponent_screens=True))
        merged = merge_envelope(plain, screened)
        if merged != plain:
            merged["uncertain"] = [*merged.get("uncertain", []), "opponent_screens_unread"]
        return merged


# -- envelope of two estimates ------------------------------------------------------------


def _widen_pair(a: Any, b: Any) -> Any:
    """[low, high] -> the envelope; ``None`` as an upper bound ("no guarantee") wins."""
    if not (isinstance(a, list) and isinstance(b, list) and len(a) == len(b) == 2):
        return a
    lows = [x for x in (a[0], b[0]) if x is not None]
    low = min(lows) if lows else None
    high = None if a[1] is None or b[1] is None else max(a[1], b[1])
    return [low, high]


def merge_envelope(plain: dict, screened: dict) -> dict:
    """``plain`` with every range key widened to cover ``screened`` too (copy; inputs untouched)."""
    out = dict(plain)
    for key in _RANGE_KEYS:
        if key in plain and key in screened and plain[key] != screened[key]:
            out[key] = _widen_pair(plain[key], screened[key])
    if plain.get("can_ko") != screened.get("can_ko") and "can_ko" in plain:
        out["can_ko"] = "maybe"
    return out
