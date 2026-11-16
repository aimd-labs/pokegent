"""The ``state`` block of ``/telemetry``: party, map, battle, badges from one snapshot.

Pure functions of a :class:`~pokegent.games.gen3.firered.reader.schemas.StateSnapshot` (what the
runner
perceived last) and a :class:`~pokegent.dashboard.names.NameBook`. Shapes follow the pre-fork
stream dashboard (``docs/addons.md`` lists each key and what is not known here).

Observable-only rule (CONTEXT.md invariant 8): the opponent's exact HP is hidden information, so
``battle.enemy.hp`` is the HP bar in percent and ``max_hp`` is 100; its moves are never listed.
"""

from __future__ import annotations

import re
from typing import Any

from pokegent.actions.battle.state import BattleBlock, Battler, status_names
from pokegent.dashboard.names import NameBook, type_name
from pokegent.games.gen3.firered.maps import map_names
from pokegent.games.gen3.firered.reader.schemas import PartyMember, StateSnapshot

BADGE_NAMES = (
    "Boulder",
    "Cascade",
    "Thunder",
    "Rainbow",
    "Soul",
    "Marsh",
    "Volcano",
    "Earth",
)
#: Legacy species_id is limited to 1..251; national_dex supplies the full sprite number.
MAX_SPRITE_SPECIES = 251
_WORD_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[A-Za-z])(?=[0-9])")


#: pret drops accents and apostrophes: the words of map names that need them back
_WORDS = {
    "Pokemon": "Pokémon",
    "Loreleis": "Lorelei's",
    "Brunos": "Bruno's",
    "Agathas": "Agatha's",
    "Lances": "Lance's",
    "Champions": "Champion's",
    "Copycats": "Copycat's",
    "Wardens": "Warden's",
    "Bills": "Bill's",
}


def humanize_map(name: str | None) -> str | None:
    """``Route4_PokemonCenter_1F`` -> ``Route 4 Pokémon Center 1F``."""
    if not name:
        return None
    words: list[str] = []
    for part in name.split("_"):
        if re.fullmatch(r"B?\d+F", part):
            words.append(part)
            continue
        words.extend(_WORD_SPLIT.sub(" ", part).split())
    return " ".join(_WORDS.get(w, w) for w in words)


def status_text(status1: int) -> str:
    names = status_names(status1)
    return names[0].upper() if names else "OK"


def _species_id(species: int) -> int | None:
    return species if 1 <= species <= MAX_SPRITE_SPECIES else None


def _species_label(species: int, is_egg: bool, names: NameBook) -> str:
    if is_egg:
        return "EGG"
    name = names.species_name(species)
    return name.upper() if name else f"SPECIES {species}"


def party_row(mon: PartyMember, names: NameBook) -> dict[str, Any]:
    species = _species_label(mon.species, mon.is_egg, names)
    types = () if mon.is_egg else names.species_types(mon.species)
    return {
        "slot": mon.slot,
        "species_id": None if mon.is_egg else _species_id(mon.species),
        "national_dex": None if mon.is_egg else names.national_dex(mon.species),
        "species": species,
        "nickname": mon.nickname or species,
        "level": mon.level,
        "hp": mon.hp,
        "max_hp": mon.max_hp,
        "status": status_text(mon.status),
        "type1": types[0] if types else None,
        "type2": types[1] if len(types) > 1 else None,
        "types": list(types),
        "moves": [
            {"name": names.move_name(m.move_id) or f"MOVE {m.move_id}", "pp": m.pp}
            for m in mon.moves
            if m.move_id
        ],
    }


def map_block(snapshot: StateSnapshot) -> dict[str, Any]:
    player = snapshot.player
    if player is None:
        return {}
    pret = map_names.map_name(player.map_group, player.map_num)
    return {
        "id": f"{player.map_group}:{player.map_num}",
        "group": player.map_group,
        "number": player.map_num,
        "name": humanize_map(pret) or f"Map {player.map_group}:{player.map_num}",
        "pret_name": pret,
        "position": {"x": player.x, "y": player.y},
    }


def badges_block(snapshot: StateSnapshot) -> list[str]:
    player = snapshot.player
    if player is None:
        return []
    return [BADGE_NAMES[n - 1] for n in sorted(set(player.badges)) if 1 <= n <= len(BADGE_NAMES)]


def _active_party_index(battler: Battler, party: tuple[PartyMember, ...]) -> int | None:
    for i, mon in enumerate(party):
        if mon.personality == battler.personality and mon.species == battler.species:
            return i
    for i, mon in enumerate(party):  # a Transformed mon changes species: fall back to the slot
        if mon.personality == battler.personality:
            return i
    return None


def _battler_name(block: BattleBlock, battler: Battler, names: NameBook) -> str:
    name = block.lookups.species.get(battler.species) or names.species_name(battler.species)
    return name.upper() if name else f"SPECIES {battler.species}"


def _battler_types(battler: Battler) -> list[str]:
    seen = [type_name(battler.type1), type_name(battler.type2)]
    return [t for t in dict.fromkeys(seen) if t]


def _player_moves(block: BattleBlock, battler: Battler, names: NameBook) -> list[dict[str, Any]]:
    moves: list[dict[str, Any]] = []
    for slot in battler.moves:
        if not slot.move_id:
            continue
        info = block.lookups.moves.get(slot.move_id)
        name = (info.name if info else None) or names.move_name(slot.move_id)
        moves.append(
            {
                "name": name or f"MOVE {slot.move_id}",
                "type": type_name(info.type) if info else None,
                "power": info.power if info else None,
                "pp": slot.pp,
            }
        )
    return moves


def battle_block(snapshot: StateSnapshot, names: NameBook) -> dict[str, Any]:
    block: BattleBlock | None = getattr(snapshot, "battle", None)
    if block is None or not block.active:
        return {"in_battle": False, "is_trainer": False}
    out: dict[str, Any] = {
        "in_battle": True,
        "is_trainer": block.kind == "trainer",
        "kind": block.kind,
        "turn": block.turn,
        "weather": list(block.weather),
        "valid": block.valid,
        "active_party_indexes": [],
        "player": None,
        "enemy": None,
    }
    if block.enemy_party is not None:
        out["enemy_party"] = {"size": block.enemy_party.size, "alive": block.enemy_party.alive}
    mine, foe = block.player, block.opponent
    if mine is not None:
        index = _active_party_index(mine, snapshot.party)
        name = _battler_name(block, mine, names)
        out["active_party_indexes"] = [] if index is None else [index]
        out["player"] = {
            "party_index": index,
            "species": name,
            "nickname": snapshot.party[index].nickname if index is not None else name,
            "level": mine.level,
            "hp": mine.hp,
            "max_hp": mine.max_hp,
            "status": status_text(mine.status1),
            "types": _battler_types(mine),
            "moves": _player_moves(block, mine, names),
        }
    if foe is not None:
        name = _battler_name(block, foe, names)
        types = _battler_types(foe)
        out["enemy"] = {
            "species_id": _species_id(foe.species),
            "national_dex": names.national_dex(foe.species),
            "species": name,
            "nickname": name,
            "level": foe.level,
            "hp": foe.hp_percent,  # the bar in percent: the exact HP is hidden information
            "max_hp": 100,
            "hp_percent": foe.hp_percent,
            "status": status_text(foe.status1),
            "type1": types[0] if types else None,
            "type2": types[1] if len(types) > 1 else None,
            "types": types,
        }
    return out


def state_block(snapshot: StateSnapshot, names: NameBook) -> dict[str, Any]:
    """The ``state`` object of the telemetry (an empty shell for an unusable snapshot)."""
    if not snapshot.usable or snapshot.player is None:
        return {
            "valid": False,
            "badges": [],
            "party": [],
            "battle": {"in_battle": False, "is_trainer": False},
        }
    player = snapshot.player
    map_info = map_block(snapshot)
    # Cleared save blocks can have valid pointers during the intro. Until a name exists,
    # zero-filled IDs, clocks, money and flags are not an initialized trainer card.
    trainer_ready = bool(player.name.strip())
    return {
        "valid": True,
        "badges": badges_block(snapshot),
        "map": map_info,
        "position": map_info.get("position"),
        "player": {
            "name": player.name if trainer_ready else None,
            "trainer_id": player.tid if trainer_ready else None,
            "money": player.money if trainer_ready else None,
            "facing": player.facing,
            "movement": player.movement,
            "play_time": (
                player.play_time.model_dump() if trainer_ready and player.play_time else None
            ),
        },
        "party": [party_row(m, names) for m in snapshot.party],
        "battle": battle_block(snapshot, names),
        "pokedex": {
            "seen": snapshot.pokedex.seen_count if trainer_ready and snapshot.pokedex else None,
            "caught": snapshot.pokedex.owned_count if trainer_ready and snapshot.pokedex else None,
        },
    }


__all__ = [
    "BADGE_NAMES",
    "badges_block",
    "battle_block",
    "humanize_map",
    "map_block",
    "party_row",
    "state_block",
    "status_text",
]
