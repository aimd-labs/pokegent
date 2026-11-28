"""Compact, human-readable planning view for the loopback agent bridge."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from pokegent.games.gen3.firered.maps import map_names
from pokegent.games.gen3.mechanics.team_planning import _mon_level


def _name(tables: Any, method: str, value: int, fallback: str = "unknown") -> str:
    try:
        return str(getattr(tables, method)(value)).title()
    except (AttributeError, KeyError, IndexError, TypeError, ValueError):
        return fallback


def _map_label(key: str | None) -> str | None:
    if not key:
        return key
    label = re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", key.replace("_", " "))
    return (
        label.replace("Pokemon", "Pokémon")
        .replace("Pallet Town Professor", "Pallet Town, Professor")
        .replace("Professor Oaks Lab", "Professor Oak's Lab")
    )


def _status(value: int) -> str:
    return next(
        (
            name
            for bit, name in (
                (0x80, "toxic"),
                (0x40, "paralysis"),
                (0x20, "freeze"),
                (0x10, "burn"),
                (0x08, "poisoned"),
                (0x07, "sleep"),
            )
            if value & bit
        ),
        "none" if not value else "unknown",
    )


BRIEFING_MAX_BYTES = 6144


def trim_briefing(result: dict[str, Any], *, max_bytes: int = BRIEFING_MAX_BYTES) -> dict[str, Any]:
    """Apply stable field caps, then drop optional detail while preserving the core."""
    existing = result.get("diagnostics")
    diagnostics = existing if isinstance(existing, dict) else {"trimmed": []}
    diagnostics.setdefault("trimmed", [])
    result["diagnostics"] = diagnostics
    trimmed = diagnostics["trimmed"]

    def mark(field: str) -> None:
        if field not in trimmed:
            trimmed.append(field)

    def size() -> int:
        return len(json.dumps(result, sort_keys=True, ensure_ascii=True).encode("utf-8"))

    # These caps are part of the public briefing shape, even when the byte budget is not tight.
    events = result.get("events")
    if isinstance(events, list) and len(events) > 5:
        result["events"] = events[-5:]
        mark("events")
    side = result.get("side_objectives")
    if isinstance(side, dict):
        changed = False
        for key in ("uncaught_species", "nearby_items", "unfought_trainers"):
            values = side.get(key)
            if isinstance(values, list) and len(values) > 6:
                side[key] = values[:6]
                changed = True
        if changed:
            mark("side_objectives")
    bag = result.get("bag")
    if isinstance(bag, dict):
        changed = False
        for key, values in bag.items():
            if isinstance(values, list) and len(values) > 5:
                bag[key] = values[:5]
                changed = True
        if changed:
            mark("bag")
    pc = result.get("pc_highlights")
    if isinstance(pc, list) and len(pc) > 6:
        result["pc_highlights"] = pc[:6]
        mark("pc_highlights")
    team = result.get("team")
    if isinstance(team, list):
        changed = False
        for member in team:
            if isinstance(member, dict) and isinstance(member.get("moves"), list):
                if len(member["moves"]) > 4:
                    member["moves"] = member["moves"][:4]
                    changed = True
        if changed:
            mark("team.moves")

    storage = result.get("pc")
    if isinstance(storage, dict) and isinstance(storage.get("stored_mons"), list):
        if len(storage["stored_mons"]) > 30:
            storage["stored_mons"] = storage["stored_mons"][:30]
            mark("pc.stored_mons")

    def drop_events() -> bool:
        if result.get("events"):
            result["events"] = []
            mark("events")
            return True
        return False

    def drop_side() -> bool:
        if result.get("side_objectives"):
            result["side_objectives"] = {}
            mark("side_objectives")
            return True
        return False

    def reduce_bag() -> bool:
        bag = result.get("bag")
        if not isinstance(bag, dict):
            return False
        optional = {key: value for key, value in bag.items() if key != "key_items" and value}
        if optional:
            result["bag"] = {"key_items": bag.get("key_items", [])}
            mark("bag")
            return True
        return False

    def reduce_pc() -> bool:
        pc = result.get("pc_highlights")
        if isinstance(pc, list) and len(pc) > 6:
            result["pc_highlights"] = pc[:6]
            mark("pc_highlights")
            return True
        return False

    def drop_moves() -> bool:
        team = result.get("team")
        if isinstance(team, list) and any(
            member.get("moves") for member in team if isinstance(member, dict)
        ):
            for member in team:
                if isinstance(member, dict):
                    member.pop("moves", None)
            mark("team.moves")
            return True
        return False

    def reduce_storage() -> bool:
        storage = result.get("pc")
        mons = storage.get("stored_mons") if isinstance(storage, dict) else None
        if not isinstance(mons, list) or len(mons) <= 6:
            return False
        storage["stored_mons"] = mons[: max(6, len(mons) // 2)]
        mark("pc.stored_mons")
        return True

    def shorten_diagnostics() -> bool:
        changed = False

        def shorten(value: Any) -> Any:
            nonlocal changed
            if isinstance(value, str) and len(value) > 200:
                changed = True
                return value[:200]
            if isinstance(value, list):
                return [shorten(item) for item in value]
            if isinstance(value, dict):
                return {key: shorten(item) for key, item in value.items()}
            return value

        shortened = shorten(result["diagnostics"])
        # Keep the original dict alive: the exhausted-reductions path writes cap_raised_to
        # through the outer diagnostics reference below.
        result["diagnostics"].clear()
        result["diagnostics"].update(shortened)
        result["diagnostics"]["trimmed"] = trimmed
        if changed:
            mark("diagnostics")
        return changed

    def drop_story_facts() -> bool:
        if "story_facts" not in result:
            return False
        del result["story_facts"]
        mark("story_facts")
        return True

    reductions = (
        drop_story_facts,
        drop_events,
        drop_side,
        reduce_bag,
        reduce_pc,
        drop_moves,
        reduce_storage,
        shorten_diagnostics,
    )
    while size() > max_bytes:
        if not any(reduction() for reduction in reductions):
            diagnostics["cap_raised_to"] = size()
            break
    return result


def _table_facts(snapshot: Any, tables: Any) -> dict[str, Any]:
    """Facts: in-game trades the party can do and tutor moves it could learn (keys absent when
    a table is unreadable). Party indexes only; the Pokedex test reads snapshot.pokedex.owned."""
    out: dict[str, Any] = {}
    if tables is None:
        return out
    party = [
        (i, int(mon.species))
        for i, mon in enumerate(getattr(snapshot, "party", ()))
        if getattr(mon, "is_egg", None) is not True
    ]
    owned = getattr(getattr(snapshot, "pokedex", None), "owned", None)
    try:
        table = tables.ingame_trade_table()
        trades = []
        for offered, wanted in table or ():
            dex = tables.national_dex(offered)
            members = [i for i, species in party if species == wanted]
            if owned is None or dex is None or dex in owned or not members:
                continue
            trades.append(
                {
                    "give": tables.species_name(wanted).title(),
                    "get": tables.species_name(offered).title(),
                    "party_members": members,
                }
            )
        if trades:
            out["trades_available"] = trades
        learnable = []
        for index in range(15):
            move_id = tables.tutor_move(index)
            members = [
                i for i, s in party if move_id and tables.can_learn_tutor(s, move_id) is True
            ]
            if members:
                learnable.append(
                    {"move": _name(tables, "move_name", move_id), "party_members": members}
                )
        if learnable:
            out["tutor_moves_learnable"] = learnable
    except (AttributeError, KeyError, IndexError, TypeError, ValueError):
        pass
    return out


def build_briefing(
    snapshot: Any,
    *,
    tables: Any = None,
    world: Any = None,
    objective: Any = None,
    events: list[dict[str, Any]] | None = None,
    story_facts: Mapping[str, Any] | None = None,
    trim: bool = True,
) -> dict[str, Any]:
    """Build only facts already present in the snapshot/ROM tables.

    The output is deliberately bounded and contains names rather than RAM/ROM identifiers.
    """
    player = getattr(snapshot, "player", None)
    location = None
    if player is not None:
        try:
            location = map_names.map_name(player.map_group, player.map_num)
        except (KeyError, TypeError, ValueError):
            location = "unknown"
    diagnostics: list[str] = []
    team = []
    for mon in getattr(snapshot, "party", ()):
        species = _name(tables, "species_name", int(mon.species), f"species {mon.species}")
        nickname = str(getattr(mon, "nickname", "") or "").strip()
        if getattr(mon, "is_egg", None) is True:
            # An Egg cannot battle: species, moves and HP would read as a usable member.
            team.append({"species": species, "level": mon.level, "egg": True, "types": [],
                         "moves": [], "status": "none", "held_item": None})
            continue
        types = []
        if tables:
            types = [
                _name(tables, "type_name", t)
                for t in getattr(tables.species(int(mon.species)), "types", ())
            ]
        moves = []
        for slot in getattr(mon, "moves", ()):
            if not slot.move_id:
                continue
            move = tables.move(int(slot.move_id)) if tables else None
            moves.append(
                {
                    "name": _name(tables, "move_name", slot.move_id),
                    "type": _name(tables, "type_name", getattr(move, "type", 0)),
                    "power": getattr(move, "power", None),
                    "pp": slot.pp,
                }
            )
        member = {
            "species": species,
            "level": mon.level,
            "types": types,
            "moves": moves,
            "hp": [mon.hp, mon.max_hp],
            "status": _status(mon.status),
            "held_item": (
                _name(tables, "item_name", mon.held_item, "none") if mon.held_item else None
            ),
        }
        if nickname and nickname.casefold() != species.casefold():
            member["nickname"] = nickname
        team.append(member)
    pokedex = getattr(snapshot, "pokedex", None)
    party_size = len(team)
    storage = getattr(snapshot, "storage", None)
    stored_mons = []
    if storage is not None:
        for box in getattr(storage, "boxes", ()):
            for mon in box:
                if mon is None:
                    continue
                level = "unknown"
                try:
                    level = _mon_level(mon, tables) if tables is not None else "unknown"
                except (AttributeError, KeyError, IndexError, TypeError, ValueError):
                    pass
                stored_mons.append(
                    {
                        "species": _name(
                            tables, "species_name", int(mon.species), f"species {mon.species}"
                        ),
                        "level": level,
                    }
                )
    bag = {}
    for pocket in getattr(snapshot, "bag", ()):
        bag[pocket.pocket] = [
            {"item": _name(tables, "item_name", item.item_id), "quantity": item.quantity}
            for item in sorted(pocket.items, key=lambda item: item.quantity, reverse=True)
        ]
    world_view = None
    if world is not None:
        try:
            current = snapshot
            if current.map is None and player is not None:
                mapped = world.map_reader.read_static(player.map_group, player.map_num)
                if mapped.usable:
                    current = snapshot.with_map(mapped.state)
            world_view = world.view(current)
        except Exception:
            world_view = None
    active = objective.model_dump(mode="json") if hasattr(objective, "model_dump") else objective
    result = {
        "schema": "briefing/1",
        "location": _map_label(location),
        "team": team,
        "party_size": party_size,
        "party_free_slots": max(0, 6 - party_size),
        "pokedex": {
            "owned": pokedex.owned_count if pokedex is not None else "unknown",
            "seen": pokedex.seen_count if pokedex is not None else "unknown",
        },
        "pc": {
            "stored_count": len(stored_mons) if storage is not None else "unknown",
            "stored_mons": stored_mons if storage is not None else "unknown",
        },
        "bag": bag,
        "money": getattr(player, "money", None),
        "badges": sorted(getattr(player, "badges", ())) if player is not None else "unknown",
        "events": list(events or [])[-5:],
        "objective": active,
        "diagnostics": diagnostics,
    }
    battle = getattr(snapshot, "battle", None)
    if getattr(battle, "kind", None) == "trainer":
        trainer = getattr(battle, "opponent_trainer", None)
        result["battle"] = {
            "opponent_trainer": {
                "trainer_id": getattr(trainer, "trainer_id", None),
                "class_name": getattr(trainer, "class_name", "unknown"),
                "name": getattr(trainer, "name", "unknown"),
            }
        }
    if world_view is not None:
        side = {
            "uncaught_species": [
                {"species": row.name, "levels": [row.min_level, row.max_level]}
                for row in world_view.dex.encounters
                if row.caught is False
            ],
            "nearby_items": [
                {"item": item.item_name, "quantity": item.quantity}
                for item in world_view.items
                if not item.collected
            ],
            "unfought_trainers": [
                {"level_max": max(npc.trainer_levels, default=None)}
                for npc in world_view.npcs
                if npc.trainer_id is not None and npc.trainer_defeated is False
            ],
        }
        side["nearby_items"] += [
            {"gift": npc.gift.name, "quantity": npc.gift.quantity}
            for npc in world_view.npcs
            if npc.gift is not None and npc.gift_received is not True
        ][: max(0, 6 - len(side["nearby_items"]))]
        result["side_objectives"] = side
        result["wild_encounters"] = [
            {
                "method": row.method,
                "species": row.name,
                "levels": [row.min_level, row.max_level],
                "chance": row.probability,
                "owned": row.caught,
                "seen": row.seen,
                "new": None if row.caught is None else row.caught is False,
                "unseen": row.seen is False,
            }
            for row in world_view.dex.encounters
        ]
    summary = f"At {_map_label(location) or 'an unknown place'} with {len(team)} Pokémon."
    if objective is not None:
        try:
            objective_text = objective.summary()
        except AttributeError:
            objective_text = None
        if objective_text:
            summary += f" Objective: {objective_text}."
    result["summary"] = summary
    # Keys are machine-facing navigation data and are deliberately kept out of readable fields.
    result["keys"] = {"location": location}
    if story_facts is not None:
        result["story_facts"] = dict(story_facts)
    result.update(_table_facts(snapshot, tables))
    return trim_briefing(result) if trim else result
