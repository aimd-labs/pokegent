"""Bounded ROM-only evidence about the merchant scripts of one map.

The result is deliberately weaker than a live shop inventory. ``POSSIBLE_CLOSED``
means every script root that can run on the map was decoded under the small
supported control subset and every merchant list it can reach was read in full.
The item union then proves *possible* absence for a query, never a currently
effective inventory or price; that distinction is kept in the type.

Anything the subset cannot account for (unknown opcode, native/indirect
control, invalid pointer, malformed or unterminated table/list, unsupported map
script or background root, duplicate map script type, exhausted bounds) yields
``UNKNOWN`` with no union. It never yields a complete-empty result.

Only the requested map's header, event scripts and script tables are read. No
other map is scanned and no navigation graph or interpreter is built.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from pokegent.games.gen3.firered.rom.script_scan import (
    _TRAINER_SCRIPT_TYPES,
    Cmd,
    UnknownOpcode,
    decode_stream,
)

ROM_BASE = 0x0800_0000
ROM_SIZE = 0x0200_0000
MAX_ROOTS = 256
MAX_STREAMS = 256
MAX_COMMANDS = 8192
MAX_ITEM_IDS = 128
MAX_MAP_SCRIPT_ROWS = 32
MAX_FRAME_ROWS = 128
STD_SCRIPT_COUNT = 10  # pret/pokefirered data/event_scripts.s: gStdScripts..gStdScriptsEnd


class StockCoverage(str, Enum):
    UNKNOWN = "unknown"
    #: Closed union of every inventory this map's scripts can possibly open.
    POSSIBLE_CLOSED = "possible_closed"


@dataclass(frozen=True)
class ScriptRoot:
    source: str  # object / coord / bg / map_load / map_frame / map_transition / map_resume
    local_id: int | None
    script_ptr: int


@dataclass(frozen=True)
class VendorStock:
    """One reachable ``pokemart`` command; duplicate local ids stay separate."""

    source: str
    local_id: int | None
    item_ids: tuple[int, ...]


@dataclass(frozen=True)
class ShopStockFacts:
    map_key: tuple[int, int]
    profile_id: str
    rom_sha1: str
    coverage: StockCoverage
    vendors: tuple[VendorStock, ...] = ()
    possible_item_ids: frozenset[int] = frozenset()
    #: The union is a possibility set, never a currently effective inventory/price.
    effective_inventory: bool = False
    reasons: tuple[str, ...] = ()


# Map script table types verified in-repo (boulder_scan.py): 1/3 direct, 2 is a table.
_ON_LOAD, _ON_FRAME_TABLE, _ON_TRANSITION = 1, 2, 3
_MAP_SCRIPT_SOURCES = {
    _ON_LOAD: "map_load",
    _ON_FRAME_TABLE: "map_frame",
    _ON_TRANSITION: "map_transition",
}
# Commands the subset cannot account for. Native/indirect transfer or effects are
# never assumed inert for merchant coverage.
_UNSUPPORTED = frozenset(
    {
        "callnative",
        "gotonative",
        "special",
        "specialvar",
        "setvaddress",
        "vgoto",
        "vcall",
        "vgoto_if",
        "vcall_if",
        "returnram",
        "endram",
        "trywondercardscript",
        "dotrainerbattle",
        "gotopostbattlescript",
        "gotobeatenscript",
        "pokemartdecoration",
        "pokemartdecoration2",
    }
)
_BRANCH = frozenset({"goto", "call", "goto_if", "call_if"})
_STD_CALL = frozenset({"callstd", "gotostd", "callstd_if", "gotostd_if"})

# Stock-coverage classification, not execution permission: pret/pokefirered
# 037335f4c725d7c9aecdac87066f2002b4bd7e14 data/specials.inc binds special 391 to
# GetQuestLogState, whose src/quest_log.c body only writes gSpecialVar_Result from
# gQuestLogState (no callees, shop, inventory or callback).  Its unknown RESULT value
# therefore cannot hide a merchant list; both caller arms are still walked.
_QUESTLOG_STATE_SPECIAL = 391
_QUESTLOG_PROFILE_ID = "firered-us-rev0"
_QUESTLOG_ROM_SHA1 = "41cb23d8dccc8ebd7c649cd8fbb58eeace6e2fdc"


def _questlog_state_read(profile, cmd: Cmd) -> bool:
    return (
        cmd.name == "special"
        and cmd.u16() == _QUESTLOG_STATE_SPECIAL
        and profile.profile_id == _QUESTLOG_PROFILE_ID
        and profile.rom.sha1 == _QUESTLOG_ROM_SHA1
    )


class _Unsupported(Exception):
    pass


def _rom_offset(pointer: int) -> int | None:
    if not ROM_BASE <= pointer < ROM_BASE + ROM_SIZE:
        return None
    offset = pointer - ROM_BASE
    return offset


def _u16(rom: bytes, offset: int) -> int:
    if not 0 <= offset <= len(rom) - 2:
        raise _Unsupported(f"read past ROM at {offset:#x}")
    return struct.unpack_from("<H", rom, offset)[0]


def _u32(rom: bytes, offset: int) -> int:
    if not 0 <= offset <= len(rom) - 4:
        raise _Unsupported(f"read past ROM at {offset:#x}")
    return struct.unpack_from("<I", rom, offset)[0]


def _std_target(rom: bytes, profile, index: int) -> int:
    if not 0 <= index < STD_SCRIPT_COUNT:
        raise _Unsupported(f"standard script {index} is out of bounds")
    if not profile.has_symbol("gStdScripts"):
        raise _Unsupported("gStdScripts is unavailable")
    table = _rom_offset(profile.symbol("gStdScripts").address)
    if table is None or table + 4 * STD_SCRIPT_COUNT > len(rom):
        raise _Unsupported("gStdScripts table is invalid")
    pointer = _u32(rom, table + 4 * index)
    target = _rom_offset(pointer)
    if target is None:
        raise _Unsupported(f"standard script {index} has an invalid pointer")
    return target


def _walk(rom: bytes, profile, pointer: int) -> tuple[Cmd, ...]:
    start = _rom_offset(pointer)
    if start is None:
        raise _Unsupported(f"script pointer 0x{pointer:08X} is not ROM")
    seen: set[int] = set()
    todo = [start]
    out: list[Cmd] = []
    while todo:
        offset = todo.pop(0)
        if offset in seen:
            continue
        seen.add(offset)
        if len(seen) > MAX_STREAMS:
            raise _Unsupported(f"more than {MAX_STREAMS} streams")
        try:
            stream = decode_stream(rom, offset, limit=MAX_COMMANDS)
        except UnknownOpcode as exc:
            raise _Unsupported(str(exc)) from exc
        for cmd in stream:
            out.append(cmd)
            if len(out) > MAX_COMMANDS:
                raise _Unsupported(f"more than {MAX_COMMANDS} commands")
            if cmd.name in _UNSUPPORTED and not _questlog_state_read(profile, cmd):
                raise _Unsupported(f"unsupported command {cmd.name}")
            if cmd.name in _STD_CALL:
                index = cmd.operands[0] if cmd.name in ("callstd", "gotostd") else cmd.operands[1]
                todo.append(_std_target(rom, profile, index))
            elif cmd.name in _BRANCH:
                target = cmd.target
                if target is None:
                    raise _Unsupported(f"invalid {cmd.name} target")
                todo.append(target)
            if cmd.name == "trainerbattle" and cmd.operands and (
                cmd.operands[0] in _TRAINER_SCRIPT_TYPES
            ):
                # The helper returns None for both no continuation and an invalid pointer.
                target = _rom_offset(cmd.u32(len(cmd.operands) - 4))
                if target is None:
                    raise _Unsupported("trainerbattle continuation is not ROM")
                todo.append(target)
            else:
                target = cmd.trainer_script_target
                if target is not None:
                    todo.append(target)
    return tuple(out)


def _item_ids(rom: bytes, pointer: int, tables) -> tuple[int, ...]:
    offset = _rom_offset(pointer)
    if offset is None:
        raise _Unsupported(f"pokemart list 0x{pointer:08X} is not ROM")
    count = len(tables.items)
    items: list[int] = []
    for index in range(MAX_ITEM_IDS):
        item_id = _u16(rom, offset + index * 2)
        if item_id == 0:
            return tuple(items)
        if not 0 < item_id < count:
            raise _Unsupported(f"pokemart item id {item_id} is out of range")
        items.append(item_id)
    raise _Unsupported("pokemart item list is unterminated")


def _map_script_roots(rom: bytes, profile, scripts_ptr: int) -> list[ScriptRoot]:
    if scripts_ptr == 0:
        return []
    offset = _rom_offset(scripts_ptr)
    if offset is None:
        raise _Unsupported("map scripts pointer is not ROM")
    roots: list[ScriptRoot] = []
    seen_types: set[int] = set()
    for _ in range(MAX_MAP_SCRIPT_ROWS):
        if offset >= len(rom):
            raise _Unsupported("map scripts table is unterminated")
        kind = rom[offset]
        if kind == 0:
            return roots
        source = _MAP_SCRIPT_SOURCES.get(kind)
        if source is None:
            raise _Unsupported(f"unsupported map script type {kind}")
        if kind in seen_types:
            raise _Unsupported(f"duplicate map script type {kind}")
        seen_types.add(kind)
        pointer = _u32(rom, offset + 1)
        if kind == _ON_FRAME_TABLE:
            roots.extend(_frame_table_roots(rom, pointer))
        else:
            if _rom_offset(pointer) is None:
                raise _Unsupported(f"map script type {kind} has an invalid pointer")
            roots.append(ScriptRoot(source, None, pointer))
        offset += 5
    raise _Unsupported("map scripts table is too long")


def _frame_table_roots(rom: bytes, table_ptr: int) -> list[ScriptRoot]:
    offset = _rom_offset(table_ptr)
    if offset is None:
        raise _Unsupported("ON_FRAME table pointer is not ROM")
    roots: list[ScriptRoot] = []
    for _ in range(MAX_FRAME_ROWS):
        if offset > len(rom) - 8:
            raise _Unsupported("ON_FRAME table is unterminated")
        var = _u16(rom, offset)
        if var == 0:
            return roots
        pointer = _u32(rom, offset + 4)
        if _rom_offset(pointer) is None:
            raise _Unsupported("ON_FRAME row has an invalid script pointer")
        roots.append(ScriptRoot("map_frame", None, pointer))
        offset += 8
    raise _Unsupported("ON_FRAME table is too long")


def _event_roots(objects, coord_events, bg_events) -> list[ScriptRoot]:
    roots: list[ScriptRoot] = []
    for obj in objects:
        if getattr(obj, "role", None) == "clone" or getattr(obj, "clone_of", None):
            # The live engine inherits the referenced template's script, which may be on
            # another map; this bounded reader never resolves clones.
            raise _Unsupported("object clone root is unresolved")
        pointer = getattr(obj, "script_ptr", None)
        if pointer:
            roots.append(ScriptRoot("object", getattr(obj, "local_id", None), pointer))
    for event in coord_events:
        pointer = event.script_ptr
        if pointer:
            roots.append(ScriptRoot("coord", None, pointer))
    for event in bg_events:
        if event.kind == "hidden_item":
            continue  # item payload, not a script root
        if event.kind == "secret_base":
            # MapReader drops the raw payload pointer for kind 8, which may be a script.
            raise _Unsupported("secret_base background event is unsupported")
        if event.kind != "sign":
            raise _Unsupported(f"unsupported background event kind {event.kind!r}")
        if event.script_ptr:
            roots.append(ScriptRoot("bg", None, event.script_ptr))
    return roots


def stock_from_sources(
    rom: bytes,
    profile,
    *,
    objects: Iterable = (),
    coord_events: Iterable = (),
    bg_events: Iterable = (),
    scripts_ptr: int = 0,
    tables,
    map_key: tuple[int, int],
) -> ShopStockFacts:
    """Decode this map's script roots and return bounded stock evidence."""
    identity = (profile.profile_id, profile.rom.sha1)
    try:
        if tables is None:
            raise _Unsupported("item table is unavailable")
        roots = _event_roots(objects, coord_events, bg_events)
        roots.extend(_map_script_roots(rom, profile, scripts_ptr))
        if len(roots) > MAX_ROOTS:
            raise _Unsupported(f"more than {MAX_ROOTS} script roots")
        vendors: list[VendorStock] = []
        for root in roots:
            for cmd in _walk(rom, profile, root.script_ptr):
                if cmd.name == "pokemart":
                    if len(cmd.operands) != 4:
                        raise _Unsupported("pokemart has malformed operands")
                    vendors.append(
                        VendorStock(root.source, root.local_id, _item_ids(rom, cmd.u32(), tables))
                    )
    except _Unsupported as exc:
        return ShopStockFacts(
            map_key, identity[0], identity[1], StockCoverage.UNKNOWN, reasons=(str(exc),)
        )
    union = frozenset(item for vendor in vendors for item in vendor.item_ids)
    return ShopStockFacts(
        map_key, identity[0], identity[1], StockCoverage.POSSIBLE_CLOSED, tuple(vendors), union
    )
