"""Player-readable team facts used while preparing for the Elite Four.

This module is deliberately read-only: names, move types, and effectiveness all come from the
same ROM tables used by battle mechanics.  The returned strings contain no game-internal ids.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from pokegent.games.gen3 import mechanics_constants as K
from pokegent.games.gen3.mechanics.team_planning import E4_TRAINERS, render_member_fact


@lru_cache(maxsize=4)
def _tables_from_rom_path(path: str) -> Any:
    if not path or not os.path.isfile(path):
        return None
    try:
        from pokegent.games.gen3.firered.rom.rom_tables import GameTables

        return GameTables.from_rom_file(path)
    except Exception:
        return None


def _tables_from_env() -> Any:
    """Load the ROM tables once per ROM path, rather than once per party frame."""
    return _tables_from_rom_path(os.environ.get("FIRERED_ROM_PATH", ""))


@lru_cache(maxsize=4)
def _e4_teams_from_rom(path: str) -> dict[str, tuple[Any, ...]]:
    if not path or not os.path.isfile(path):
        return {}
    try:
        from pokegent.games.gen3.firered.rom.rom_data import (
            identify_rom_file,
            load_profiles,
            require_supported,
        )
        from pokegent.games.gen3.firered.rom.trainer import TrainerTable

        profiles = load_profiles()
        profile = require_supported(identify_rom_file(path, profiles), profiles)
        with open(path, "rb") as handle:
            table = TrainerTable(handle.read(), profile)
        return {name: table.party(trainer_id).members for name, trainer_id in E4_TRAINERS.items()}
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def _type_name(tables: Any, type_id: int) -> str:
    try:
        return str(tables.type_name(type_id)).lower()
    except (AttributeError, KeyError, TypeError, ValueError):
        return K.TYPE_NAMES[type_id] if 0 <= type_id < len(K.TYPE_NAMES) else "unknown"


def party_facts(
    snapshot: Any, tables: Any = None, *, opponent: str | None = None
) -> tuple[str, ...]:
    """Return one concise, readable line per party member."""
    tables = tables if tables is not None else _tables_from_env()
    lines: list[str] = []
    e4_teams = _e4_teams_from_rom(os.environ.get("FIRERED_ROM_PATH", ""))
    for mon in snapshot.party:
        try:
            species = tables.species(mon.species) if tables is not None else None
            types = species.types if species is not None else ()
            type_text = (
                "/".join(_type_name(tables, t) for t in dict.fromkeys(types))
                if tables is not None
                else ""
            )
            moves = []
            for move in mon.moves:
                if not move.move_id:
                    continue
                if tables is None:
                    moves.append("unknown move (unknown type)")
                else:
                    data = tables.move(move.move_id)
                    moves.append(f"{data.name.title()} ({_type_name(tables, data.type)})")
            move_text = ", ".join(moves) or "no known moves"
            name = mon.nickname or (species.name if species is not None else "Pokemon")
            line = f"{name} Lv{mon.level}"
            if type_text:
                line += f" {type_text}"
            line += f" HP {mon.hp}/{mon.max_hp}; moves: {move_text}"
            if tables is not None and hasattr(mon, "stats"):
                line = render_member_fact(
                    mon,
                    tables,
                    next_name="",
                    e4_teams=e4_teams,
                )
            lines.append(line)
        except (AttributeError, KeyError, TypeError, ValueError):
            lines.append(
                f"{getattr(mon, 'nickname', '') or 'Pokemon'} Lv{getattr(mon, 'level', '?')} "
                f"HP {getattr(mon, 'hp', '?')}/{getattr(mon, 'max_hp', '?')}; "
                "moves: unknown"
            )
    return tuple(lines)
