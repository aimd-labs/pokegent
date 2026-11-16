"""ROM/RAM-driven facts for non-walking, non-battle world interactions.

The adapter deliberately does not press buttons or plan paths.  It gives the field and shop
families identities and consequences for targets they already know how to reach and execute.
Immutable facts come from the supported ROM profile (map object/background events, event scripts,
trainer parties, mart lists, item/species tables and wild encounters); availability comes from the
current snapshot (flags, bag, money, party and Pokédex).
"""

from __future__ import annotations

import logging
import os
import struct
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pokegent.games.gen3 import mechanics_constants as MK
from pokegent.games.gen3.firered.maps import map_names
from pokegent.games.gen3.firered.maps.map_constants import (
    OBJ_EVENT_GFX_CLERK,
    OBJ_EVENT_GFX_NURSE,
)
from pokegent.games.gen3.firered.maps.map_data import MapReader, RomMapCache
from pokegent.games.gen3.firered.maps.map_schema import MapObject, MapState
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.games.gen3.firered.rom import constants as C
from pokegent.games.gen3.firered.rom.rom_data import (
    RomProfile,
    identify_rom_file,
    load_profiles,
    require_supported,
)
from pokegent.games.gen3.firered.rom.rom_tables import GameTables, ItemData, RomImagePort
from pokegent.games.gen3.firered.rom.script_scan import (
    Cmd,
    UnknownOpcode,
    decode_stream,
    reachable,
)

logger = logging.getLogger(__name__)

ROM_BASE = 0x0800_0000
TRAINER_FLAGS_START = 0x500
TRAINER_ENTRY_SIZE = 40
WILD_HEADER_SIZE = 20
_GIFT_STATE_LIMIT = 10_000
WILD_SLOTS = {"land": 12, "water": 5, "rock_smash": 5, "fishing": 10}
WILD_WEIGHTS = {
    "land": (20, 20, 10, 10, 10, 10, 5, 5, 4, 4, 1, 1),
    "water": (60, 30, 5, 4, 1),
    "rock_smash": (60, 30, 5, 4, 1),
    # Old Rod (2), Good Rod (3), Super Rod (5).  Species presence is the important dex fact.
    "fishing": (70, 30, 60, 20, 20, 40, 40, 15, 4, 1),
}


@dataclass(frozen=True)
class ItemOpportunity:
    source: Literal["object", "hidden"]
    ref: int
    x: int
    y: int
    item_id: int
    item_name: str
    quantity: int
    flag_id: int
    collected: bool | None
    knowledge: str
    owned: int
    new: bool
    useful: str


@dataclass(frozen=True)
class CollectSource:
    """A ROM-proven way to acquire one item, with its RAM availability status."""

    map_key: tuple[int, int]
    map_name: str
    object_id: int
    kind: Literal["item_ball", "npc"]
    x: int
    y: int
    item_id: int
    flag_id: int | None
    status: Literal["available", "used", "locked", "unknown"]
    spawned_by: int | None = None
    taken_flag_id: int | None = None
    spawner_map_key: tuple[int, int] | None = None
    spawner_map_name: str | None = None


@dataclass(frozen=True)
class PokemonGiftSource:
    """A ROM-proven NPC gift of one species, with live gating/status facts."""

    map_key: tuple[int, int]
    map_name: str
    object_id: int
    species: int
    species_name: str
    level: int | None
    gift_flag: int | None
    gate_flags: tuple[int, ...]
    gate_unset_flags: tuple[int, ...]
    gate_items: tuple[int, ...]
    gate_money: int | None
    gate_coins: int | None
    gate_unknown: bool
    status: Literal["available", "used", "locked", "unknown"]

    @property
    def gates(self) -> tuple[str, ...]:
        gates = [f"flag 0x{flag:X} set" for flag in self.gate_flags]
        gates.extend(f"flag 0x{flag:X} unset" for flag in self.gate_unset_flags)
        gates.extend(f"item {item}" for item in self.gate_items)
        if self.gate_money is not None:
            gates.append(f"money {self.gate_money}")
        if self.gate_coins is not None:
            gates.append(f"coins {self.gate_coins}")
        if self.gate_unknown:
            gates.append("unknown")
        return tuple(gates)


@dataclass(frozen=True)
class _GiftBranch:
    species: int
    level: int
    given_flag: int | None
    gate_flags: tuple[int, ...]
    gate_unset_flags: tuple[int, ...]
    gate_items: tuple[int, ...]
    gate_money: int | None
    gate_coins: int | None
    gate_unknown: bool = False
    source_addr: int = 0


@dataclass(frozen=True)
class _GateValue:
    kind: Literal["item", "money", "coins"]
    value: int | tuple[int, int] | None = None


@dataclass(frozen=True)
class _FlagTest:
    flag: int


@dataclass(frozen=True)
class _GateTest:
    gate: _GateValue
    threshold: int
    gate_on_left: bool


@dataclass(frozen=True)
class _EnumValue:
    values: frozenset[int]
    gift_records: tuple[int, ...] = ()


@dataclass(frozen=True)
class _EnumTest:
    value: _EnumValue
    threshold: int
    value_on_left: bool


@dataclass(frozen=True)
class Gift:
    kind: Literal["item", "pokemon"]
    id: int
    name: str
    quantity: int = 1
    level: int | None = None
    types: tuple[str, ...] = ()
    strength_vs_brock: str | None = None


@dataclass(frozen=True)
class PartyFact:
    name: str
    species: int
    level: int
    moves: tuple[str, ...]


@dataclass(frozen=True)
class NpcOpportunity:
    local_id: int
    x: int
    y: int
    role: str
    gift: Gift | None = None
    gift_flag: int | None = None
    gift_received: bool | None = None
    trainer_id: int | None = None
    trainer_class: int | None = None
    trainer_kind: str | None = None
    leader_badge: int | None = None
    trainer_defeated: bool | None = None
    trainer_levels: tuple[int, ...] = ()
    line_of_sight: bool = False
    story_gate: bool = False
    checked_flags: tuple[int, ...] = ()
    set_flags: tuple[int, ...] = ()
    mart: tuple[int, ...] = ()

    @property
    def approximate_level(self) -> int | None:
        return max(self.trainer_levels) if self.trainer_levels else None


@dataclass(frozen=True)
class ShopOffer:
    item_id: int
    name: str
    price: int
    owned: int
    need: str | None


@dataclass(frozen=True)
class EncounterOpportunity:
    method: str
    species: int
    name: str
    probability: float
    min_level: int
    max_level: int
    seen: bool | None
    caught: bool | None


@dataclass(frozen=True)
class DexFacts:
    seen_count: int | None
    caught_count: int | None
    encounters: tuple[EncounterOpportunity, ...]
    oak_next_count: int | None = None
    oak_next_reward: str | None = None

    @property
    def new_species(self) -> tuple[EncounterOpportunity, ...]:
        return tuple(row for row in self.encounters if row.seen is False)


@dataclass(frozen=True)
class WorldView:
    items: tuple[ItemOpportunity, ...]
    npcs: tuple[NpcOpportunity, ...]
    marts: tuple[tuple[int, tuple[ShopOffer, ...]], ...]
    dex: DexFacts
    party_need: tuple[str, ...]
    party_facts: tuple[PartyFact, ...] = ()

    def item_object(self, local_id: int) -> ItemOpportunity | None:
        return next(
            (item for item in self.items if item.source == "object" and item.ref == local_id), None
        )

    def hidden_item(self, bg_index: int) -> ItemOpportunity | None:
        return next(
            (item for item in self.items if item.source == "hidden" and item.ref == bg_index), None
        )

    def npc(self, local_id: int) -> NpcOpportunity | None:
        return next((npc for npc in self.npcs if npc.local_id == local_id), None)


def _rom_offset(pointer: int, size: int, rom: bytes) -> int | None:
    offset = pointer - ROM_BASE
    return offset if 0 <= offset and offset + size <= len(rom) else None


def _owned(snapshot: StateSnapshot, item_id: int) -> int:
    return sum(
        entry.quantity
        for pocket in snapshot.bag
        for entry in pocket.items
        if entry.item_id == item_id
    )


def _commands(rom: bytes, script_ptr: int | None) -> tuple[Cmd, ...]:
    if script_ptr is None:
        return ()
    try:
        return tuple(reachable(rom, script_ptr))
    except UnknownOpcode:
        # Some gifts branch into shared scripts containing commands outside this deliberately
        # small decoder. The complete entry stream still proves a direct givemon/additem/mart;
        # retain that prefix while refusing to infer anything from the unknown branch.
        try:
            offset = script_ptr - ROM_BASE if script_ptr >= ROM_BASE else script_ptr
            return tuple(decode_stream(rom, offset))
        except UnknownOpcode:
            return ()


def _vars(cmds: tuple[Cmd, ...]) -> dict[int, int]:
    """Read only the explicit variable-copy form used by real item gifts.

    Plain ``setvar`` values are often scratch state in shared scripts and must not be
    treated as an item identity.
    """
    out: dict[int, int] = {}
    for cmd in cmds:
        if cmd.name == "setorcopyvar" and len(cmd.operands) == 4:
            out[cmd.u16()] = cmd.u16(2)
    return out


def _gift(cmds: tuple[Cmd, ...], tables: GameTables) -> Gift | None:
    # Oak's three balls share one script.  The selected starter is carried in VAR_0x4001;
    # the script's literal species table is VAR_0x4002..VAR_0x4004.
    starter = next((cmd for cmd in cmds if cmd.name == "setvar" and cmd.u16() == 0x4001), None)
    if starter is not None:
        species = next(
            (cmd.u16(2) for cmd in cmds if cmd.name == "setvar" and cmd.u16() == 0x4002),
            None,
        )
        if species in range(1, len(tables.all_species)):
            data = tables.species(species)
            types = tuple(tables.type_name(t) for t in dict.fromkeys(data.types))
            multipliers = {
                target: max(
                    tables.type_chart.multiplier(attacker, MK.TYPE_ROCK, MK.TYPE_ROCK)
                    if target == "rock"
                    else tables.type_chart.multiplier(attacker, MK.TYPE_GROUND, MK.TYPE_GROUND)
                    for attacker in data.types
                )
                for target in ("rock", "ground")
            }
            strength = "strong vs Brock" if max(multipliers.values()) > 1 else "weak vs Brock"
            return Gift(
                "pokemon", species, data.name, level=5, types=types, strength_vs_brock=strength
            )
    for cmd in cmds:
        if cmd.name == "givemon" and len(cmd.operands) >= 3:
            species, level = cmd.u16(), cmd.operands[2]
            # Script operands may be VAR_* references (Oak's starter sequence uses VAR_0x4002),
            # not literal species. Only publish an identity the ROM table can prove.
            if 0 < species < len(tables.all_species):
                data = tables.species(species)
                return Gift(
                    "pokemon",
                    species,
                    data.name,
                    level=level,
                    types=tuple(tables.type_name(t) for t in dict.fromkeys(data.types)),
                )
        if cmd.name == "giveegg" and len(cmd.operands) >= 2:
            species = cmd.u16()
            if 0 < species < len(tables.all_species):
                data = tables.species(species)
                return Gift(
                    "pokemon",
                    species,
                    data.name,
                    level=1,
                    types=tuple(tables.type_name(t) for t in dict.fromkeys(data.types)),
                )
        if cmd.name == "additem" and len(cmd.operands) == 4:
            item_id, quantity = cmd.u16(), cmd.u16(2)
            if 0 < item_id < len(tables.items):
                return Gift("item", item_id, tables.item_name(item_id), quantity=quantity)
    values = _vars(cmds)
    item_id, quantity = values.get(0x8000), values.get(0x8001, 1)
    if item_id and 0 < item_id < len(tables.items):
        return Gift("item", item_id, tables.item_name(item_id), quantity=max(1, quantity))
    return None


def _compare(left: int, right: int) -> int:
    return 0 if left < right else 1 if left == right else 2


def _condition_holds(condition: int, comparison: int | None) -> bool | None:
    if comparison is None:
        return None
    return {
        0: comparison == 0,
        1: comparison == 1,
        2: comparison == 2,
        3: comparison in (0, 1),
        4: comparison in (1, 2),
        5: comparison != 1,
    }.get(condition)


def _compare_values(
    left: int | _GateValue | _EnumValue | None,
    right: int | _GateValue | _EnumValue | None,
) -> int | _GateTest | _EnumTest | None:
    if isinstance(left, int) and isinstance(right, int):
        return _compare(left, right)
    if isinstance(left, _GateValue) and isinstance(right, int):
        return _GateTest(left, right, True)
    if isinstance(left, int) and isinstance(right, _GateValue):
        return _GateTest(right, left, False)
    if isinstance(left, _EnumValue) and isinstance(right, int):
        return _EnumTest(left, right, True)
    if isinstance(left, int) and isinstance(right, _EnumValue):
        return _EnumTest(right, left, False)
    return None


def _gate_comparisons(test: _GateTest, met: bool) -> tuple[int, ...]:
    """Possible comparison results for a RAM-backed gate being met or unmet."""
    if test.gate.kind == "coins":
        results = (1, 2) if met else (0,)
    else:
        value = int(met)
        results = (_compare(value, test.threshold),)
    if test.gate_on_left:
        return results
    return tuple(2 - result for result in results)


def _branch_requirement(
    comparison: int | _FlagTest | _GateTest | _EnumTest | None,
    condition: int,
    taken: bool,
) -> tuple[bool, int | None, bool | None, _GateValue | None, bool]:
    """Resolve one conditional edge into a flag/quantity requirement.

    The result is ``(possible, flag, flag_must_be_set, gate, unknown)``.  A negative
    item/money/coin predicate cannot be represented as an acquisition prerequisite, so it is
    retained as an unknown gate rather than guessed away.
    """
    if isinstance(comparison, int):
        holds = _condition_holds(condition, comparison)
        return holds == taken, None, None, None, False
    if isinstance(comparison, _FlagTest):
        matches = {
            is_set for is_set in (False, True) if _condition_holds(condition, int(is_set)) == taken
        }
        if not matches:
            return False, None, None, None, False
        required = next(iter(matches)) if len(matches) == 1 else None
        return True, comparison.flag, required, None, False
    if isinstance(comparison, _GateTest):
        matches = {
            met
            for met in (False, True)
            if any(
                _condition_holds(condition, result) == taken
                for result in _gate_comparisons(comparison, met)
            )
        }
        if not matches:
            return False, None, None, None, False
        if matches == {True}:
            return True, None, None, comparison.gate, False
        return True, None, None, None, matches == {False}
    if isinstance(comparison, _EnumTest):
        return _narrow_enum(comparison, condition, taken) is not None, None, None, None, False
    return True, None, None, None, True


def _narrow_enum(test: _EnumTest, condition: int, taken: bool) -> _EnumValue | None:
    values = frozenset(
        value
        for value in test.value.values
        if _condition_holds(
            condition,
            _compare(value, test.threshold)
            if test.value_on_left
            else _compare(test.threshold, value),
        )
        == taken
    )
    return _EnumValue(values, test.value.gift_records) if values else None


def _simplify_gift_branches(records: list[_GiftBranch]) -> tuple[_GiftBranch, ...]:
    """Collapse equivalent flag-path cubes without crossing gift or given-flag identity."""
    groups: dict[tuple[object, ...], set[tuple[frozenset[int], frozenset[int]]]] = {}
    samples: dict[tuple[object, ...], _GiftBranch] = {}
    for record in records:
        key = (
            record.species,
            record.level,
            record.given_flag,
            record.gate_items,
            record.gate_money,
            record.gate_coins,
            record.gate_unknown,
            record.source_addr,
        )
        groups.setdefault(key, set()).add(
            (frozenset(record.gate_flags), frozenset(record.gate_unset_flags))
        )
        samples.setdefault(key, record)
    simplified: list[_GiftBranch] = []
    for key, cubes in groups.items():
        cubes = {cube for cube in cubes if not cube[0] & cube[1]}
        sample = samples[key]
        if sample.gate_unknown and cubes:
            # An unresolved condition means these are alternative possible execution paths, not
            # independently routable sources.  Retain only flag predicates common to every arm;
            # the unknown marker carries the unevaluable remainder without inventing a choice.
            cubes = {
                (
                    frozenset.intersection(*(cube[0] for cube in cubes)),
                    frozenset.intersection(*(cube[1] for cube in cubes)),
                )
            }
        changed = True
        while changed:
            changed = False
            reduced = {
                cube
                for cube in cubes
                if not any(
                    other != cube and other[0] <= cube[0] and other[1] <= cube[1] for other in cubes
                )
            }
            additions: set[tuple[frozenset[int], frozenset[int]]] = set()
            ordered = tuple(reduced)
            for index, left in enumerate(ordered):
                for right in ordered[index + 1 :]:
                    set_diff = left[0] ^ right[0]
                    unset_diff = left[1] ^ right[1]
                    if len(set_diff) != 1 or set_diff != unset_diff:
                        continue
                    flag = next(iter(set_diff))
                    if (flag in left[0]) == (flag in left[1]):
                        continue
                    additions.add((left[0] - {flag}, left[1] - {flag}))
            next_cubes = reduced | additions
            changed = next_cubes != cubes
            cubes = next_cubes
        simplified.extend(
            _GiftBranch(
                sample.species,
                sample.level,
                sample.given_flag,
                tuple(sorted(set_flags)),
                tuple(sorted(unset_flags)),
                sample.gate_items,
                sample.gate_money,
                sample.gate_coins,
                sample.gate_unknown,
                sample.source_addr,
            )
            for set_flags, unset_flags in sorted(
                cubes, key=lambda cube: (tuple(sorted(cube[0])), tuple(sorted(cube[1])))
            )
        )
    return tuple(simplified)


def _gift_branches(
    rom: bytes, script_ptr: int | None, tables: GameTables
) -> tuple[_GiftBranch, ...]:
    """Decode gift facts with variables and gates kept on their branch path."""
    if script_ptr is None:
        return ()
    offset = script_ptr - ROM_BASE if script_ptr >= ROM_BASE else script_ptr
    try:
        streams: dict[int, tuple[Cmd, ...]] = {}
        pending = [offset]
        while pending:
            start = pending.pop(0)
            if start in streams:
                continue
            stream = tuple(decode_stream(rom, start))
            streams[start] = stream
            pending.extend(c.target for c in stream if c.target is not None)
    except UnknownOpcode as exc:
        logger.warning("Skipping Pokemon gift script at %#x: %s", script_ptr, exc)
        return ()
    commands = {command.addr: command for stream in streams.values() for command in stream}
    terminals = {"end", "return", "gotostd", "endram", "returnram", "gotonative"}
    states = deque([(offset, (), (), None, (), (), (), None, None, False, (), (), ())])
    seen: set[tuple[object, ...]] = set()
    results: list[_GiftBranch] = []
    while states and len(seen) < _GIFT_STATE_LIMIT:
        (
            address,
            call_stack,
            variables,
            comparison,
            set_flags,
            unset_flags,
            checked_items,
            pending_money,
            pending_coins,
            unknown_gate,
            records,
            pending_records,
            failed_records,
        ) = states.popleft()
        command = commands.get(address)
        if command is None:
            continue
        key = (
            address,
            call_stack,
            variables,
            comparison,
            set_flags,
            unset_flags,
            checked_items,
            pending_money,
            pending_coins,
            unknown_gate,
            records,
            pending_records,
            failed_records,
        )
        if key in seen:
            continue
        seen.add(key)
        values = dict(variables)
        required_set = list(set_flags)
        required_unset = list(unset_flags)
        items = list(checked_items)
        money = pending_money
        coins = pending_coins
        unknown = unknown_gate
        branch_records = list(records)
        waiting = list(pending_records)
        failed = set(failed_records)
        next_comparison = comparison
        if command.name == "setvar" and len(command.operands) >= 4:
            values[command.u16()] = command.u16(2)
        elif command.name in {"copyvar", "setorcopyvar"} and len(command.operands) >= 4:
            destination, source = command.u16(), command.u16(2)
            if command.name == "setorcopyvar" and source < 0x4000:
                values[destination] = source
            elif source in values:
                values[destination] = values[source]
            else:
                values.pop(destination, None)
        elif command.name == "checkflag":
            next_comparison = _FlagTest(command.u16())
        elif command.name == "checkitem" and len(command.operands) >= 4:
            values[0x800D] = _GateValue("item", (command.u16(), command.u16(2)))
        elif command.name == "checkmoney":
            values[0x800D] = _GateValue("money", command.u32())
        elif command.name == "checkcoins":
            values[command.u16()] = _GateValue("coins")
        elif command.name == "compare_var_to_value" and len(command.operands) >= 4:
            next_comparison = _compare_values(values.get(command.u16()), command.u16(2))
        elif command.name == "compare_var_to_var" and len(command.operands) >= 4:
            next_comparison = _compare_values(values.get(command.u16()), values.get(command.u16(2)))
        elif command.name == "specialvar" and len(command.operands) >= 2:
            values.pop(command.u16(), None)
            next_comparison = None
        elif command.name in {
            "callstd",
            "special",
            "multichoice",
            "multichoicedefault",
            "multichoicegrid",
            "yesnobox",
            "checkplayergender",
            "random",
            "getpartysize",
        }:
            values.pop(0x800D, None)
            next_comparison = None
        elif command.name == "givemon" and len(command.operands) >= 3:
            operand = command.u16()
            species = values.get(operand) if operand >= 0x4000 else operand
            if isinstance(species, int) and 0 < species < len(tables.all_species):
                branch_records.append(
                    _GiftBranch(
                        species,
                        command.operands[2],
                        None,
                        tuple(dict.fromkeys(required_set)),
                        tuple(dict.fromkeys(required_unset)),
                        tuple(dict.fromkeys(items)),
                        money,
                        coins,
                        unknown,
                        command.addr,
                    )
                )
                record_index = len(branch_records) - 1
                waiting.append(record_index)
                values[0x800D] = _EnumValue(frozenset((0, 1, 2)), (record_index,))
            else:
                values.pop(0x800D, None)
            next_comparison = None
        elif command.name == "setflag":
            flag = command.u16()
            for index in waiting:
                if index not in failed:
                    branch_records[index] = _replace_gift_flag(branch_records[index], flag)
            waiting = []
        if command.name == "return" and call_stack:
            states.append(
                (
                    call_stack[-1],
                    call_stack[:-1],
                    tuple(sorted(values.items())),
                    next_comparison,
                    tuple(required_set),
                    tuple(required_unset),
                    tuple(items),
                    money,
                    coins,
                    unknown,
                    tuple(branch_records),
                    tuple(waiting),
                    tuple(failed),
                )
            )
            continue
        if command.name in terminals:
            results.extend(
                record for index, record in enumerate(branch_records) if index not in failed
            )
            continue
        next_address = command.addr + 1 + len(command.operands)
        edges: list[tuple[int, tuple[int, ...], bool | None]] = []
        if command.name == "call" and command.target is not None:
            edges.append((command.target, call_stack + (next_address,), None))
        elif command.name == "call_if" and command.target is not None:
            edges.append((command.target, call_stack + (next_address,), True))
            if next_address in commands:
                edges.append((next_address, call_stack, False))
        elif command.name in {"goto", "goto_if"} and command.target is not None:
            edges.append((command.target, call_stack, command.name == "goto_if" or None))
            if command.name == "goto_if" and next_address in commands:
                edges.append((next_address, call_stack, False))
        elif command.name not in terminals and next_address in commands:
            edges.append((next_address, call_stack, None))
        for next_address, stack, is_taken in edges:
            edge_set = list(required_set)
            edge_unset = list(required_unset)
            edge_items = list(items)
            edge_money = money
            edge_coins = coins
            edge_unknown = unknown
            edge_values = dict(values)
            edge_failed = set(failed)
            if is_taken is not None:
                possible, flag, must_set, gate, unresolved = _branch_requirement(
                    next_comparison, command.operands[0], is_taken
                )
                if not possible:
                    continue
                if flag is not None and must_set is not None:
                    target_flags = edge_set if must_set else edge_unset
                    if flag not in target_flags:
                        target_flags.append(flag)
                if gate is not None:
                    if gate.kind == "item":
                        item_id, _quantity = gate.value
                        if item_id not in edge_items:
                            edge_items.append(item_id)
                    elif gate.kind == "money":
                        edge_money = int(gate.value)
                    else:
                        edge_coins = next_comparison.threshold
                if isinstance(next_comparison, _EnumTest):
                    narrowed = _narrow_enum(next_comparison, command.operands[0], is_taken)
                    if narrowed is None:
                        continue
                    for var_id, value in tuple(edge_values.items()):
                        if value == next_comparison.value:
                            edge_values[var_id] = narrowed
                    if narrowed.values == {2}:
                        edge_failed.update(narrowed.gift_records)
                edge_unknown |= unresolved
            states.append(
                (
                    next_address,
                    stack,
                    tuple(sorted(edge_values.items())),
                    next_comparison,
                    tuple(edge_set),
                    tuple(edge_unset),
                    tuple(edge_items),
                    edge_money,
                    edge_coins,
                    edge_unknown,
                    tuple(branch_records),
                    tuple(waiting),
                    tuple(sorted(edge_failed)),
                )
            )
    if states:
        logger.warning(
            "Skipping Pokemon gift script at %#x after reaching the %d-state limit",
            script_ptr,
            _GIFT_STATE_LIMIT,
        )
        return ()
    normalized_records: list[_GiftBranch] = []
    for record in results:
        normalized = _GiftBranch(
            record.species,
            record.level,
            record.given_flag,
            record.gate_flags,
            tuple(flag for flag in record.gate_unset_flags if flag != record.given_flag),
            record.gate_items,
            record.gate_money,
            record.gate_coins,
            record.gate_unknown,
            record.source_addr,
        )
        normalized_records.append(normalized)
    return _simplify_gift_branches(normalized_records)


def _replace_gift_flag(record: _GiftBranch, flag: int) -> _GiftBranch:
    return _GiftBranch(
        record.species,
        record.level,
        flag,
        record.gate_flags,
        record.gate_unset_flags,
        record.gate_items,
        record.gate_money,
        record.gate_coins,
        record.gate_unknown,
        record.source_addr,
    )


def _flags(cmds: tuple[Cmd, ...], name: str) -> tuple[int, ...]:
    return tuple(sorted({cmd.u16() for cmd in cmds if cmd.name == name and len(cmd.operands) >= 2}))


class WorldAdapter:
    """Immutable ROM index plus a pure projection of one current RAM snapshot."""

    def __init__(
        self,
        rom: bytes,
        profile: RomProfile,
        tables: GameTables | None = None,
    ) -> None:
        self.rom = bytes(rom)
        self.profile = profile
        self.port = RomImagePort(self.rom)
        self.tables = tables if tables is not None else GameTables.from_rom_image(self.rom, profile)
        self.map_reader = MapReader(self.port, profile, cache=RomMapCache())
        self._wild = self._read_wild()
        self._collect_sources: dict[
            int,
            tuple[
                tuple[
                    int,
                    int,
                    str,
                    int,
                    str,
                    int,
                    int,
                    int | None,
                    int | None,
                    int | None,
                    tuple[int, int] | None,
                    str | None,
                ],
                ...,
            ],
        ] = {}
        self._pokemon_sources: tuple[tuple[int, int, str, int, _GiftBranch], ...] | None = None

    @classmethod
    def from_rom_file(
        cls,
        path: str | os.PathLike[str] | None = None,
    ) -> "WorldAdapter":
        rom_path = Path(path or os.environ["FIRERED_ROM_PATH"])
        profiles = load_profiles()
        profile = require_supported(identify_rom_file(rom_path, profiles), profiles)
        return cls(
            rom_path.read_bytes(),
            profile,
            tables=GameTables.from_rom_file(rom_path, profile.profile_id),
        )

    def view(self, snapshot: StateSnapshot) -> WorldView:
        state = snapshot.map
        if not snapshot.usable or state is None:
            return WorldView((), (), (), DexFacts(0, 0, ()), ())
        items = tuple(self._items(snapshot, state))
        npcs = tuple(self._npcs(snapshot, state))
        marts = tuple(
            (npc.local_id, tuple(self._shop_offers(snapshot, npc.mart))) for npc in npcs if npc.mart
        )
        party_facts = tuple(
            PartyFact(
                member.nickname or self.tables.species_name(member.species),
                member.species,
                member.level,
                tuple(self.tables.move_name(move.move_id) for move in member.moves if move.move_id),
            )
            for member in snapshot.party
        )
        return WorldView(
            items, npcs, marts, self._dex(snapshot, state), self.party_need(snapshot), party_facts
        )

    def collect_sources(self, snapshot: StateSnapshot, item_id: int) -> tuple[CollectSource, ...]:
        """Return every ROM-script source for ``item_id`` not already used in RAM."""
        rows = self._collect_sources_for(item_id)
        flags = snapshot.flags
        out = []
        for (
            group,
            number,
            name,
            object_id,
            kind,
            x,
            y,
            hide_flag,
            taken_flag,
            spawned_by,
            spawner_map_key,
            spawner_map_name,
        ) in rows:
            status = "unknown"
            if flags is not None:
                if taken_flag is not None and flags.is_set(taken_flag):
                    status = "used"
                elif (
                    hide_flag is not None
                    and flags.is_set(hide_flag)
                    and (kind == "npc" or spawned_by is not None)
                ):
                    status = "locked"
                elif taken_flag is not None and (hide_flag is None or not flags.is_set(hide_flag)):
                    status = "available"
            if status == "used":
                continue
            out.append(
                CollectSource(
                    (group, number),
                    name,
                    object_id,
                    kind,
                    x,
                    y,
                    item_id,
                    hide_flag,
                    status,
                    spawned_by,
                    taken_flag,
                    spawner_map_key,
                    spawner_map_name,
                )
            )
        return tuple(out)

    def pokemon_gift_sources(
        self,
        snapshot: StateSnapshot,
        species: int | None,
        *,
        include_used: bool = False,
    ) -> tuple[PokemonGiftSource, ...]:
        """Return ROM ``givemon`` sources, optionally retaining exhausted rows for display."""
        if self._pokemon_sources is None:
            rows = []
            for group, size in enumerate(map_names.group_sizes()):
                for number in range(size):
                    result = self.map_reader.read_static(group, number)
                    state = result.state if result.usable else None
                    if state is None:
                        continue
                    name = state.identity.name or map_names.map_name(group, number)
                    for obj in state.objects:
                        if obj.script_ptr is None:
                            continue
                        gifts = _gift_branches(self.rom, obj.script_ptr, self.tables)
                        if not gifts:
                            continue
                        rows.extend((group, number, name, obj.local_id, gift) for gift in gifts)
            self._pokemon_sources = tuple(rows)
        flags = snapshot.flags
        out = []
        for group, number, name, object_id, gift in self._pokemon_sources:
            if species is not None and gift.species != species:
                continue
            used = False
            locked = False
            unevaluable = False
            if flags is not None:
                if gift.given_flag is not None and flags.is_set(gift.given_flag):
                    used = True
                locked |= any(not flags.is_set(flag) for flag in gift.gate_flags)
                locked |= any(flags.is_set(flag) for flag in gift.gate_unset_flags)
            else:
                unevaluable |= bool(gift.given_flag or gift.gate_flags or gift.gate_unset_flags)
            if gift.gate_items:
                if snapshot.usable:
                    owned: dict[int, int] = {}
                    for pocket in snapshot.bag:
                        for item in pocket.items:
                            owned[item.item_id] = owned.get(item.item_id, 0) + item.quantity
                    locked |= any(owned.get(item_id, 0) < 1 for item_id in gift.gate_items)
                else:
                    unevaluable = True
            if gift.gate_money is not None:
                if snapshot.player is None:
                    unevaluable = True
                else:
                    locked |= snapshot.player.money < gift.gate_money
            if gift.gate_coins is not None:
                if snapshot.player is None:
                    unevaluable = True
                else:
                    locked |= snapshot.player.coins < gift.gate_coins
            status = (
                "used"
                if used
                else "locked"
                if locked
                else "unknown"
                if gift.gate_unknown or unevaluable
                else "available"
            )
            if used and not include_used:
                continue
            out.append(
                PokemonGiftSource(
                    (group, number),
                    name,
                    object_id,
                    gift.species,
                    self.tables.species_name(gift.species),
                    gift.level,
                    gift.given_flag,
                    gift.gate_flags,
                    gift.gate_unset_flags,
                    gift.gate_items,
                    gift.gate_money,
                    gift.gate_coins,
                    gift.gate_unknown,
                    status,
                )
            )
        return tuple(out)

    def _collect_sources_for(self, item_id: int):
        cached = self._collect_sources.get(item_id)
        if cached is not None:
            return cached
        maps = []
        clearers: dict[int, list[tuple[tuple[int, int], str, MapObject, frozenset[int]]]] = {}
        for group, size in enumerate(map_names.group_sizes()):
            for number in range(size):
                result = self.map_reader.read_static(group, number)
                state = result.state if result.usable else None
                if state is None:
                    continue
                object_commands = {
                    obj.local_id: _commands(self.rom, obj.script_ptr)
                    for obj in state.objects
                    if obj.script_ptr is not None
                }
                name = state.identity.name or map_names.map_name(group, number)
                maps.append((group, number, name, state, object_commands))
                for obj in state.objects:
                    cmds = object_commands.get(obj.local_id, ())
                    added = frozenset(
                        command.u16() for command in cmds if command.name == "addobject"
                    )
                    for flag in _flags(cmds, "clearflag"):
                        clearers.setdefault(flag, []).append(((group, number), name, obj, added))

        rows = []
        for group, number, name, state, object_commands in maps:
            for obj in state.objects:
                if obj.script_ptr is None:
                    continue
                cmds = _commands(self.rom, obj.script_ptr)
                gift = _gift(cmds, self.tables)
                if gift is None or gift.kind != "item" or gift.id != item_id:
                    continue
                hide_flag = obj.hidden_flag_id or None
                candidates = clearers.get(hide_flag, []) if hide_flag is not None else []
                exact = [
                    candidate
                    for candidate in candidates
                    if candidate[0] == (group, number) and obj.local_id in candidate[3]
                ]
                spawner = (
                    exact[0]
                    if len(exact) == 1
                    else (candidates[0] if not exact and len(candidates) == 1 else None)
                )
                if obj.role == "item_ball":
                    kind = "item_ball"
                    set_flags = _flags(cmds, "setflag")
                    taken_flag = set_flags[0] if set_flags else None
                    if (
                        taken_flag is None
                        and spawner is None
                        and any(
                            command.name == "callstd" and command.u16() == 1 for command in cmds
                        )
                    ):
                        taken_flag = hide_flag
                else:
                    kind = "npc"
                    checked, set_flags = _flags(cmds, "checkflag"), _flags(cmds, "setflag")
                    taken_flag = next((flag for flag in set_flags if flag in checked), None)
                rows.append(
                    (
                        group,
                        number,
                        name,
                        obj.local_id,
                        kind,
                        obj.x,
                        obj.y,
                        hide_flag,
                        taken_flag,
                        spawner[2].local_id if spawner is not None else None,
                        spawner[0] if spawner is not None else None,
                        spawner[1] if spawner is not None else None,
                    )
                )
        result = tuple(rows)
        self._collect_sources[item_id] = result
        return result

    def bg_event_is_sign_text(self, script_ptr: int | None) -> bool | None:
        """Return the ROM-proven kind of a sign-facing background event script."""
        if script_ptr is None:
            return None
        commands = _commands(self.rom, script_ptr)
        if not commands:
            return None
        return any(command.name == "signmsg" for command in commands)

    def _items(self, snapshot: StateSnapshot, state: MapState):
        for obj in state.objects:
            if obj.role != "item_ball" or obj.script_ptr is None:
                continue
            cmds = _commands(self.rom, obj.script_ptr)
            gift = _gift(cmds, self.tables)
            if gift is None or gift.kind != "item":
                continue
            collected = obj.hidden_flag_set if obj.hidden_flag_id else None
            yield self._item(
                snapshot,
                "object",
                obj.local_id,
                obj.x,
                obj.y,
                gift.id,
                gift.quantity,
                obj.hidden_flag_id,
                collected,
                obj.knowledge,
            )
        for bg in state.bg_events:
            hidden = bg.hidden_item
            if bg.kind != "hidden_item" or hidden is None:
                continue
            yield self._item(
                snapshot,
                "hidden",
                bg.index,
                bg.x,
                bg.y,
                hidden.item_id,
                hidden.quantity,
                hidden.flag_id,
                hidden.flag_set,
                bg.knowledge,
            )

    def _item(self, snapshot, source, ref, x, y, item_id, quantity, flag_id, collected, knowledge):
        item = self.tables.item(item_id)
        owned = _owned(snapshot, item_id)
        return ItemOpportunity(
            source,
            ref,
            x,
            y,
            item_id,
            item.name,
            quantity,
            flag_id,
            collected,
            knowledge,
            owned,
            owned == 0,
            self._item_use(item, snapshot),
        )

    def _npcs(self, snapshot: StateSnapshot, state: MapState):
        for obj in state.objects:
            if obj.script_ptr is None:
                continue
            cmds = _commands(self.rom, obj.script_ptr)
            gift = _gift(cmds, self.tables)
            # Most item-ball scripts are represented by ``items`` above. A few important gifts
            # (notably the Celadon Eevee) use the same sprite, so retain Pokemon gifts as NPCs.
            if obj.role == "item_ball" and (gift is None or gift.kind == "item"):
                continue
            checked, set_flags = _flags(cmds, "checkflag"), _flags(cmds, "setflag")
            gift_flag = next((flag for flag in set_flags if flag in checked), None)
            if gift is not None and gift_flag is None and set_flags:
                gift_flag = set_flags[-1]
            if gift is not None and gift_flag is None and obj.hidden_flag_id:
                gift_flag = obj.hidden_flag_id
            trainer_id = self._trainer_id(obj, cmds)
            trainer_class = self._trainer_class(trainer_id)
            trainer_kind = self._trainer_kind(trainer_class)
            leader_badge = self._leader_badge(trainer_kind, cmds)
            trainer_flag = TRAINER_FLAGS_START + trainer_id if trainer_id is not None else None
            defeated = (
                snapshot.flags.is_set(trainer_flag)
                if trainer_flag is not None and snapshot.flags is not None and trainer_flag < 0x900
                else None
            )
            marts = tuple(self._mart_items(cmds))
            yield NpcOpportunity(
                local_id=obj.local_id,
                x=obj.x,
                y=obj.y,
                role=(
                    "nurse"
                    if obj.graphics_id == OBJ_EVENT_GFX_NURSE
                    else "clerk"
                    if marts or obj.graphics_id == OBJ_EVENT_GFX_CLERK
                    else obj.role
                ),
                gift=gift,
                gift_flag=gift_flag,
                gift_received=(
                    snapshot.flags.is_set(gift_flag)
                    if gift_flag is not None and snapshot.flags is not None
                    else None
                ),
                trainer_id=trainer_id,
                trainer_class=trainer_class,
                trainer_kind=trainer_kind,
                leader_badge=leader_badge,
                trainer_defeated=defeated,
                trainer_levels=self.trainer_levels(trainer_id) if trainer_id is not None else (),
                line_of_sight=self._line_of_sight(state, obj, defeated),
                story_gate=bool((checked or set_flags) and gift is None and trainer_id is None),
                checked_flags=checked,
                set_flags=set_flags,
                mart=marts,
            )

    def _trainer_id(self, obj: MapObject, cmds: tuple[Cmd, ...]) -> int | None:
        command = next((cmd for cmd in cmds if cmd.name == "trainerbattle"), None)
        if command is not None and len(command.operands) >= 3:
            return command.u16(1)
        offset = _rom_offset(obj.script_ptr or 0, 4, self.rom)
        if offset is not None and self.rom[offset] == 0x5C:
            return int.from_bytes(self.rom[offset + 2 : offset + 4], "little")
        return None

    def _trainer_class(self, trainer_id: int | None) -> int | None:
        if trainer_id is None:
            return None
        table = self.profile.rom_table("gTrainers")
        offset = table.address - ROM_BASE + trainer_id * TRAINER_ENTRY_SIZE
        if not 0 <= offset < len(self.rom) or offset + TRAINER_ENTRY_SIZE > len(self.rom):
            return None
        return self.rom[offset + 1]

    @staticmethod
    def _trainer_kind(trainer_class: int | None) -> str | None:
        return {
            C.TRAINER_CLASS_LEADER: "gym_leader",
            C.TRAINER_CLASS_ELITE_FOUR: "elite_four",
        }.get(trainer_class)

    def _leader_badge(self, trainer_kind: str | None, cmds: tuple[Cmd, ...]) -> int | None:
        if trainer_kind != "gym_leader":
            return None
        badge_flags = {
            C.FLAG_BADGE01_GET + badge - 1: badge for badge in range(1, C.NUM_BADGES + 1)
        }
        return next(
            (badge_flags[flag] for flag in _flags(cmds, "setflag") if flag in badge_flags),
            None,
        )

    def trainer_levels(self, trainer_id: int) -> tuple[int, ...]:
        table = self.profile.rom_table("gTrainers")
        offset = table.address - ROM_BASE + trainer_id * TRAINER_ENTRY_SIZE
        if not 0 <= offset <= len(self.rom) - TRAINER_ENTRY_SIZE:
            return ()
        row = self.rom[offset : offset + TRAINER_ENTRY_SIZE]
        flags, count = row[0], row[32]
        pointer = struct.unpack_from("<I", row, 36)[0]
        party = _rom_offset(pointer, 1, self.rom)
        if party is None or not 0 < count <= 6:
            return ()
        stride = 16 if flags & 1 else 8
        if party + count * stride > len(self.rom):
            return ()
        return tuple(
            struct.unpack_from("<H", self.rom, party + index * stride + 2)[0]
            for index in range(count)
        )

    def _mart_items(self, cmds: tuple[Cmd, ...]):
        for cmd in cmds:
            if cmd.name != "pokemart" or len(cmd.operands) != 4:
                continue
            pointer = cmd.u32()
            offset = _rom_offset(pointer, 2, self.rom)
            if offset is None:
                continue
            items: list[int] = []
            for index in range(128):
                item_id = struct.unpack_from("<H", self.rom, offset + index * 2)[0]
                if item_id == 0:
                    break
                if not 0 < item_id < len(self.tables.items):
                    items = []
                    break
                items.append(item_id)
            if items:
                yield from items

    def _shop_offers(self, snapshot: StateSnapshot, ids: tuple[int, ...]):
        for item_id in ids:
            item = self.tables.item(item_id)
            yield ShopOffer(
                item_id,
                item.name,
                item.price,
                _owned(snapshot, item_id),
                self._shop_need(item, snapshot),
            )

    def shop_inventory(self, snapshot: StateSnapshot) -> tuple[ShopOffer, ...] | None:
        """The active list, resolved by its RAM row count when a map has multiple clerks."""
        if snapshot.map is None and snapshot.player is not None:
            static = self.map_reader.read_static(snapshot.player.map_group, snapshot.player.map_num)
            if static.usable:
                snapshot = snapshot.with_map(static.state)
        view = self.view(snapshot)
        candidates = [offers for _local_id, offers in view.marts]
        if not candidates:
            return None
        ui = snapshot.ui
        total = ui.menu.list_menu.total_items if ui and ui.menu and ui.menu.list_menu else None
        if total is not None:
            matched = [offers for offers in candidates if len(offers) + 1 == total]
            if len(matched) == 1:
                return matched[0]
        return candidates[0] if len(candidates) == 1 else None

    def _read_wild(self):
        table = self.profile.rom_table("gWildMonHeaders")
        at = table.address - ROM_BASE
        result: dict[tuple[int, int], tuple[tuple[str, int, float, int, int], ...]] = {}
        for _ in range(table.size // WILD_HEADER_SIZE):
            group, number, _pad, *pointers = struct.unpack_from("<BBHIIII", self.rom, at)
            at += WILD_HEADER_SIZE
            if (group, number) == (0xFF, 0xFF):
                break
            rows = []
            for method, pointer in zip(WILD_SLOTS, pointers, strict=True):
                if not pointer:
                    continue
                info = _rom_offset(pointer, 8, self.rom)
                if info is None:
                    continue
                slots_ptr = struct.unpack_from("<I", self.rom, info + 4)[0]
                slots = _rom_offset(slots_ptr, WILD_SLOTS[method] * 4, self.rom)
                if slots is None:
                    continue
                for index in range(WILD_SLOTS[method]):
                    lo, hi, species = struct.unpack_from("<BBH", self.rom, slots + index * 4)
                    rows.append((method, species, WILD_WEIGHTS[method][index] / 100, lo, hi))
            result[(group, number)] = tuple(rows)
        return result

    def wild_encounters(self, map_key: tuple[int, int]):
        """The immutable ROM wild table for one map, for objective adapters."""
        return self._wild.get(tuple(map_key), ())

    def wild_encounter_maps(self, species: int) -> frozenset[tuple[int, int]]:
        """Maps whose ROM wild tables contain ``species`` in any decoded method."""
        return frozenset(
            map_key
            for map_key, rows in self._wild.items()
            if any(row[1] == species for row in rows)
        )

    def wild_encounter_sources(
        self, species: int
    ) -> tuple[tuple[tuple[int, int], frozenset[str]], ...]:
        """ROM wild methods containing ``species``, grouped by map."""
        return tuple(
            (map_key, frozenset(row[0] for row in rows if row[1] == species))
            for map_key, rows in self._wild.items()
            if any(row[1] == species for row in rows)
        )

    def _dex(self, snapshot: StateSnapshot, state: MapState) -> DexFacts:
        dex = snapshot.pokedex
        seen, caught = (set(dex.seen), set(dex.owned)) if dex is not None else (None, None)
        combined: dict[tuple[str, int], list[float | int]] = {}
        for method, species, chance, lo, hi in self._wild.get(
            (state.identity.group, state.identity.number), ()
        ):
            key = (method, species)
            row = combined.setdefault(key, [0.0, lo, hi])
            row[0] = min(1.0, float(row[0]) + chance)
            row[1], row[2] = min(int(row[1]), lo), max(int(row[2]), hi)
        encounters = tuple(
            EncounterOpportunity(
                method,
                species,
                self.tables.species_name(species),
                float(row[0]),
                int(row[1]),
                int(row[2]),
                species in seen if seen is not None else None,
                species in caught if caught is not None else None,
            )
            for (method, species), row in sorted(combined.items())
        )
        thresholds = (
            (10, "HM05 Flash"),
            (20, "Everstone"),
            (30, "Itemfinder"),
            (40, "Amulet Coin"),
            (50, "Exp. Share"),
        )
        next_reward = next(
            (
                (count, reward)
                for count, reward in thresholds
                if caught is not None and len(caught) < count
            ),
            None,
        )
        return DexFacts(
            len(seen) if seen is not None else None,
            len(caught) if caught is not None else None,
            encounters,
            next_reward[0] if next_reward else None,
            next_reward[1] if next_reward else None,
        )

    def party_need(self, snapshot: StateSnapshot) -> tuple[str, ...]:
        """Return direct party-state facts without deriving an upkeep objective."""
        party = tuple(mon for mon in snapshot.party if mon.species)
        needs: list[str] = []
        if any(mon.hp < mon.max_hp for mon in party):
            needs.append("party HP is below max")
        if any(mon.hp == 0 for mon in party):
            needs.append("a party member is fainted")
        if any(mon.status for mon in party):
            needs.append("a party member has a status condition")
        for mon in party:
            pp_low = False
            damaging = [
                move
                for move in mon.moves
                if move.move_id and not self.tables.move(move.move_id).is_status
            ]
            for slot, move in enumerate(mon.moves):
                if not move.move_id:
                    continue
                base = int(self.tables.move(move.move_id).pp)
                ups = (int(mon.pp_bonuses) >> (slot * 2)) & 3
                if move.pp < base + base * 20 * ups // 100:
                    pp_low = True
                    break
            if pp_low and "move PP is below max" not in needs:
                needs.append("move PP is below max")
            if damaging and all(move.pp == 0 for move in damaging):
                needs.append("a damaging move set is out of PP")
                break
        return tuple(needs)

    def _item_use(self, item: ItemData, snapshot: StateSnapshot) -> str:
        if item.importance:
            return "key item or HM"
        if item.is_ball:
            return "catching wild Pokemon"
        if item.effect is not None:
            if item.effect.revive:
                return "revives a fainted party member"
            if item.effect.heals_hp:
                return "restores HP"
            if item.effect.cures:
                return "cures status"
            if item.effect.heal_pp is not None:
                return "restores PP"
        if item.price >= 1000:
            return "valuable item"
        return "general inventory item"

    def _shop_need(self, item: ItemData, snapshot: StateSnapshot) -> str | None:
        owned = _owned(snapshot, item.item_id)
        if item.is_ball and owned == 0:
            return "no Poke Balls left"
        if item.effect is not None:
            if item.effect.revive and any(mon.hp == 0 for mon in snapshot.party):
                return "party has a fainted member"
            if item.effect.heals_hp and owned == 0:
                return "no HP medicine left"
            if item.effect.cures and any(mon.status for mon in snapshot.party):
                return "party has a status condition"
        return None

    @staticmethod
    def _line_of_sight(state: MapState, obj: MapObject, defeated: bool | None) -> bool:
        player = state.player
        live = obj.live
        if defeated or player is None or live is None or obj.trainer_type not in (1, 2):
            return False
        dx, dy = player.x - obj.x, player.y - obj.y
        direction = live.facing
        ahead = (
            (direction == "N" and dx == 0 and -obj.trainer_range <= dy < 0)
            or (direction == "S" and dx == 0 and 0 < dy <= obj.trainer_range)
            or (direction == "W" and dy == 0 and -obj.trainer_range <= dx < 0)
            or (direction == "E" and dy == 0 and 0 < dx <= obj.trainer_range)
        )
        return bool(ahead)


@lru_cache(maxsize=8)
def load_world_adapter() -> WorldAdapter | None:
    path = os.environ.get("FIRERED_ROM_PATH")
    if not path or not Path(path).is_file():
        return None
    try:
        return WorldAdapter.from_rom_file(path)
    except Exception as exc:
        # Optional enrichment must never make the normal loop unusable, but failures should be
        # diagnosable rather than silently masking a malformed ROM or profile.
        logger.warning("world adapter unavailable for %s: %s", path, exc)
        return None
