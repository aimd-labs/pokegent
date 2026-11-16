"""RAM/ROM-backed party facts shared by the party and PC surfaces."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import SimpleNamespace
from typing import Any

from pokegent.games.gen3 import mechanics_constants as K
from pokegent.games.gen3.mechanics.views import BattlerView, FieldView, MoveSlotView, StatsView
from pokegent.games.gen3.ruleset.damage import estimate_damage

E4_TRAINERS = {"Lorelei": 410, "Bruno": 411, "Agatha": 412, "Lance": 413}


def _name(tables: Any, method: str, value: int, fallback: str = "unknown") -> str:
    try:
        return str(getattr(tables, method)(value)).title()
    except (AttributeError, KeyError, IndexError, TypeError, ValueError):
        return fallback


def _level_from_experience(experience: int, growth: int) -> int:
    def required(level: int) -> int:
        if growth == 0:
            return level**3
        if growth == 1:
            if level <= 50:
                return level**3 * (100 - level) // 50
            if level <= 68:
                return level**3 * (150 - level) // 100
            if level <= 98:
                return level**3 * ((1911 - 10 * level) // 3) // 500
            return level**3 * (160 - level) // 100
        if growth == 2:
            return (
                level**3 * ((level + 1) // 3 + 24) // 50
                if level <= 15
                else (
                    level**3 * (level + 14) // 50
                    if level <= 35
                    else level**3 * (level // 2 + 32) // 50
                )
            )
        if growth == 3:
            return 6 * level**3 // 5 - 15 * level**2 + 100 * level - 140
        if growth == 4:
            return 4 * level**3 // 5
        return 5 * level**3 // 4

    level = 1
    while level < 100 and required(level + 1) <= experience:
        level += 1
    return level


def _member_ability(mon: Any, species: Any) -> int | None:
    ability_num = getattr(mon, "ability_num", None)
    abilities = getattr(species, "abilities", ())
    if ability_num is None or not 0 <= int(ability_num) < len(abilities):
        return None
    ability = int(abilities[int(ability_num)])
    return ability or None


def _normalise_mon(mon: Any, tables: Any) -> Any:
    if hasattr(mon, "level") and hasattr(mon, "stats"):
        return mon
    species = tables.species(int(mon.species))
    if not hasattr(species, "growth_rate") or not hasattr(species, "base_hp"):
        return mon
    level = _level_from_experience(int(getattr(mon, "experience", 0)), species.growth_rate)
    ivs = getattr(mon, "ivs", None)
    evs = getattr(mon, "evs", None)
    values = {
        key: int(getattr(ivs, key, 0)) + int(getattr(evs, key, 0)) // 4
        for key in ("hp", "attack", "defense", "speed", "sp_attack", "sp_defense")
    }
    stats = SimpleNamespace(
        hp=(2 * species.base_hp + values.get("hp", 0)) * level // 100 + level + 10,
        attack=(2 * species.base_atk + values["attack"]) * level // 100 + 5,
        defense=(2 * species.base_def + values["defense"]) * level // 100 + 5,
        speed=(2 * species.base_spe + values["speed"]) * level // 100 + 5,
        sp_attack=(2 * species.base_spa + values["sp_attack"]) * level // 100 + 5,
        sp_defense=(2 * species.base_spd + values["sp_defense"]) * level // 100 + 5,
    )
    return SimpleNamespace(
        **{
            name: getattr(mon, name, default)
            for name, default in {
                "species": mon.species,
                "nickname": "",
                "held_item": 0,
                "ability_num": 0,
                "pp_bonuses": 0,
                "friendship": 0,
                "moves": (),
            }.items()
        },
        level=level,
        hp=stats.hp,
        max_hp=stats.hp,
        stats=stats,
    )


def _member_view(mon: Any, tables: Any) -> BattlerView:
    mon = _normalise_mon(mon, tables)
    species = tables.species(int(mon.species))
    stats = getattr(mon, "stats", None)
    stat_view = None
    if stats is not None:
        stat_view = StatsView(
            atk=int(stats.attack),
            def_=int(stats.defense),
            spa=int(stats.sp_attack),
            spd=int(stats.sp_defense),
            spe=int(stats.speed),
        )
    moves = tuple(
        MoveSlotView(
            move_id=int(move.move_id),
            pp=int(getattr(move, "pp", 0)),
            max_pp=int(move.pp) if getattr(move, "pp", None) is not None else None,
        )
        for move in getattr(mon, "moves", ())
        if int(getattr(move, "move_id", 0))
    )
    return BattlerView(
        species=int(mon.species),
        level=int(mon.level),
        hp=int(mon.hp),
        max_hp=int(mon.max_hp),
        stats=stat_view,
        types=species.types,
        ability=_member_ability(mon, species),
        held_item=int(mon.held_item),
        moves=moves,
        is_player_side=True,
        friendship=int(getattr(mon, "friendship", 0)),
        ivs=None,
        name=str(getattr(mon, "nickname", "") or species.name),
    )


def _enemy_view(row: Any, tables: Any) -> BattlerView:
    species_id = _row_species(row)
    level = _row_level(row)
    species = tables.species(species_id)
    moves = tuple(MoveSlotView(move_id=int(move)) for move in getattr(row, "moves", ()) if move)
    return BattlerView(
        species=species_id,
        level=level,
        hp=None,
        max_hp=None,
        types=species.types,
        held_item=int(getattr(row, "held_item", 0)),
        moves=moves,
        is_player_side=False,
        name=species.name,
    )


def _percent(estimate: Any) -> str:
    if estimate.max_share_of_max_hp is None:
        return "unknown"
    return f"{estimate.max_share_of_max_hp * 100:.0f}%"


def _row_level(row: Any) -> int:
    value = getattr(row, "level", None)
    return int(value if value is not None else row[1] if isinstance(row, tuple) else 1)


def _row_species(row: Any) -> int:
    value = getattr(row, "species", None)
    return int(value if value is not None else row[0] if isinstance(row, tuple) else 0)


def _mon_level(mon: Any, tables: Any) -> int:
    """Read a party level or derive a boxed level exactly from RAM EXP plus ROM growth."""
    level = getattr(mon, "level", None)
    if level is not None:
        return int(level)
    species = tables.species(int(mon.species))
    return _level_from_experience(int(mon.experience), int(species.growth_rate))


def speed_tiers(mon: Any, teams: Mapping[str, Iterable[Any]], tables: Any) -> dict[str, str]:
    """Compare a member's exact speed against each E4 ace's ROM-derived speed range."""
    mine = _member_view(mon, tables)
    result: dict[str, str] = {}
    for name, members in teams.items():
        ace = max(members, key=_row_level, default=None)
        if ace is None or not mine.moves:
            result[name] = "unknown"
            continue
        foe = _enemy_view(ace, tables)
        field = FieldView(kind=K.KIND_TRAINER, badges=frozenset(range(1, 9)))
        faster = estimate_damage(mine, foe, mine.moves[0], tables, field).faster
        result[name] = f"{faster} vs {name}'s ace"
    return result


def render_member_fact(
    mon: Any,
    tables: Any,
    *,
    next_name: str,
    next_team: Iterable[Any] = (),
    e4_teams: Mapping[str, Iterable[Any]] | None = None,
) -> str:
    """Render one member without exposing ROM/RAM ids."""
    mon = _normalise_mon(mon, tables)
    species = tables.species(int(mon.species))
    stats = getattr(mon, "stats", None)
    stat_text = ""
    if stats is not None:
        values = (
            stats.hp,
            stats.attack,
            stats.defense,
            stats.sp_attack,
            stats.sp_defense,
            stats.speed,
        )
        stat_text = "; stats HP/Atk/Def/SpA/SpD/Spe " + "/".join(map(str, values))
    ability_id = _member_ability(mon, species)
    ability = (
        _name(tables, "ability_name", ability_id, "none") if ability_id is not None else "none"
    )
    item = (
        _name(tables, "item_name", int(getattr(mon, "held_item", 0)), "none")
        if getattr(mon, "held_item", 0)
        else "none"
    )
    moves = []
    for slot in getattr(mon, "moves", ()):
        if not int(getattr(slot, "move_id", 0)):
            continue
        try:
            data = tables.move(int(slot.move_id))
            category = (
                "physical" if data.is_physical else "special" if data.is_special else "status"
            )
            max_pp = (
                int(data.pp)
                + int(data.pp)
                * 20
                * ((int(getattr(mon, "pp_bonuses", 0)) >> (2 * len(moves))) & 3)
                // 100
            )
            type_name = _name(tables, "type_name", data.type)
            moves.append(
                f"{data.name.title()} {type_name} {data.power or 'status'} "
                f"{category} PP {slot.pp}/{max_pp}"
            )
        except (AttributeError, KeyError, IndexError, TypeError, ValueError):
            pass
    type_text = "/".join(_name(tables, "type_name", t) for t in dict.fromkeys(species.types))
    label = getattr(mon, "nickname", "") or species.name.title()
    text = (
        f"{label} Lv{mon.level} {type_text} HP {getattr(mon, 'hp', '?')}/"
        f"{getattr(mon, 'max_hp', '?')}{stat_text}; "
        f"ability {ability}; "
        f"held item {item}; moves: {', '.join(moves) or 'none'}"
    )
    damage_ready = hasattr(tables, "all_species") and hasattr(tables, "items")
    if e4_teams and damage_ready and hasattr(mon, "stats"):
        tiers = speed_tiers(mon, e4_teams, tables)
        text += "; E4 speed " + ", ".join(f"{name} {value}" for name, value in tiers.items())
    return text
