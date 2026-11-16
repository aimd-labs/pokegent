"""Facts of a learn-move choice: the new move against each known move (no preference inside).

Sources, best first: the static ROM tables (``SystemFlowOptions.game_tables()``), the battle block's
lookups when the evolution scene sits behind a battle (``snapshot.battle``), else the ids only. The
new move's id exists only in the battle block (``gMoveToLearn``); a scene entered from the field (a
Rare Candy) has no way to read it and says so. PP Up bonuses are counted in the maximum PP.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pokegent.games.gen3.firered.reader.schemas import PartyMember, StateSnapshot

#: moves ``IsHMMove2`` refuses to forget (src/pokemon.c ``sHMMoves``: Cut, Fly, Surf, Strength,
# Flash,
#: Rock Smash, Waterfall, Dive). The game itself answers "HM moves can't be forgotten"; an
# option for
#: them would only cost a wasted round trip, and invariant 7 forbids discarding HMs.
HM_MOVE_IDS = frozenset({15, 19, 57, 70, 148, 249, 127, 291})


@dataclass(frozen=True)
class MoveFacts:
    move_id: int
    name: str | None
    type: str | None
    power: int | None
    accuracy: int | None
    pp: int | None
    max_pp: int | None

    def as_fact(self) -> dict[str, Any]:
        out: dict[str, Any] = {"move_id": self.move_id, "name": self.name or f"move {self.move_id}"}
        for key in ("type", "power", "accuracy", "pp", "max_pp"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


def battle_block(snapshot: StateSnapshot):
    block = getattr(snapshot, "battle", None)
    return block if block is not None and getattr(block, "valid", False) else None


def move_facts(
    move_id: int,
    *,
    tables: Any,
    block: Any,
    pp: int | None = None,
    pp_bonus: int = 0,
) -> MoveFacts:
    name = mtype = None
    power = accuracy = base_pp = None
    if tables is not None:
        try:
            row = tables.move(move_id)
            name, power, accuracy, base_pp = row.name, row.power, row.accuracy, row.pp
            mtype = tables.type_name(row.type)
        except Exception:  # an id outside the table: unknown, never a crash
            name = mtype = power = accuracy = base_pp = None
    if name is None and block is not None:
        info = block.lookups.moves.get(move_id)
        if info is not None:
            name, power, accuracy, base_pp = info.name, info.power, info.accuracy, info.pp
            try:
                from pokegent.actions.battle.romtables import RomTables

                mtype = RomTables.type_name(info.type).lower()
            except Exception:
                mtype = None
    max_pp = base_pp + (base_pp // 5) * pp_bonus if base_pp is not None else None
    return MoveFacts(move_id, name, mtype, power, accuracy, pp, max_pp)


def species_name(species: int, *, tables: Any, block: Any) -> str:
    if tables is not None:
        try:
            return str(tables.species_name(species)).title()
        except Exception:
            pass
    if block is not None:
        name = block.lookups.species.get(species)
        if name:
            return str(name).title()
    return f"species {species}"


def party_member(snapshot: StateSnapshot, party_id: int) -> PartyMember | None:
    if snapshot.usable and 0 <= party_id < len(snapshot.party):
        return snapshot.party[party_id]
    return None


def known_moves(member: PartyMember, *, tables: Any, block: Any) -> list[MoveFacts]:
    out = []
    for slot, mv in enumerate(member.moves):
        if not mv.move_id:
            continue
        bonus = (member.pp_bonuses >> (2 * slot)) & 3
        out.append(move_facts(mv.move_id, tables=tables, block=block, pp=mv.pp, pp_bonus=bonus))
    return out


def new_move_id(snapshot: StateSnapshot) -> int | None:
    """``gMoveToLearn`` from the battle block; ``None`` when there is no block to read it from."""
    block = battle_block(snapshot)
    if block is None or not block.move_to_learn:
        return None
    return int(block.move_to_learn)


def describe(m: MoveFacts) -> str:
    bits = [m.name or f"move {m.move_id}"]
    detail = []
    if m.type:
        detail.append(m.type)
    if m.power is not None:
        detail.append(f"power {m.power}" if m.power else "no power")
    if m.accuracy is not None:
        detail.append(f"accuracy {m.accuracy}" if m.accuracy else "never misses")
    if m.pp is not None:
        detail.append(f"PP {m.pp}/{m.max_pp}" if m.max_pp is not None else f"PP {m.pp}")
    elif m.max_pp is not None:
        detail.append(f"PP {m.max_pp}")
    return bits[0] + (f" ({', '.join(detail)})" if detail else "")


def describe_fact(m: dict[str, Any]) -> str:
    """Render a JSON-safe move fact for option text and telemetry consumers."""
    bits = [str(m.get("name") or f"move {m.get('move_id', '?')}")]
    detail = []
    if m.get("type"):
        detail.append(str(m["type"]))
    if isinstance(m.get("power"), int):
        detail.append(f"power {m['power']}" if m["power"] else "no power")
    if isinstance(m.get("pp"), int):
        max_pp = m.get("max_pp")
        detail.append(f"PP {m['pp']}/{max_pp}" if isinstance(max_pp, int) else f"PP {m['pp']}")
    elif isinstance(m.get("max_pp"), int):
        detail.append(f"PP {m['max_pp']}")
    return bits[0] + (f" ({', '.join(detail)})" if detail else "")
