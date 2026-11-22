"""Invented ROM bytes only: bounded merchant-stock evidence, never real game data."""

import struct
from types import SimpleNamespace

import pytest

from pokegent.games.gen3.firered.rom.rom_data import load_profiles
from pokegent.goals.directory import RomMapDirectory
from pokegent.goals.shop_stock import (
    StockCoverage,
    stock_from_sources,
)

BASE = 0x0800_0000
ROM_SIZE = 0x2000
STD_TABLE = 0x40
FIRST_SCRIPT = 0x100
ITEM_COUNT = 300


def build(scripts, *, std=(), std_table=True):
    rom = bytearray(ROM_SIZE)
    profile = load_profiles().get("firered-us-rev0")
    symbols = dict(profile.symbols)
    if std_table:
        symbols["gStdScripts"] = symbols["gStdScripts"].model_copy(
            update={"address": BASE + STD_TABLE, "size": 4 * 10}
        )
        for index, pointer in enumerate(std):
            struct.pack_into("<I", rom, STD_TABLE + 4 * index, pointer or 0)
    else:
        symbols.pop("gStdScripts", None)
    profile = profile.model_copy(
        update={
            "symbols": symbols,
            "rom": profile.rom.model_copy(update={"sha1": "0" * 40}),
        }
    )
    for offset, data in scripts.items():
        rom[offset : offset + len(data)] = data
    return bytes(rom), profile


def mars(ptr):
    return struct.pack("<I", BASE + ptr)


def script(*parts):
    return b"".join(parts)


def pokemart(list_offset):
    return b"\x86" + mars(list_offset)


END = b"\x02"


def goto(target):
    return b"\x05" + mars(target)


def goto_if(target, cond=1):
    return b"\x06" + bytes([cond]) + mars(target)


def callstd(index):
    return b"\x09" + bytes([index])


def items(*ids):
    return b"".join(struct.pack("<H", item) for item in ids) + b"\x00"


def tables():
    return SimpleNamespace(items=[None] + [f"item{index}" for index in range(1, ITEM_COUNT)])


def facts(rom, profile, *, objects=(), coords=(), bgs=(), scripts_ptr=0):
    return stock_from_sources(
        rom,
        profile,
        objects=objects,
        coord_events=coords,
        bg_events=bgs,
        scripts_ptr=scripts_ptr,
        tables=tables(),
        map_key=(3, 7),
    )


def obj(local_id, pointer):
    return SimpleNamespace(local_id=local_id, script_ptr=pointer)


def test_complete_present_and_absent_are_closed_but_not_effective():
    rom, profile = build(
        {
            FIRST_SCRIPT: script(b"\x6a", b"\x5a", pokemart(0x300), b"\x67", mars(0x400), END),
            0x300: items(1, 2),
            0x400: b"\xff\x00",
        }
    )
    result = facts(rom, profile, objects=(obj(4, BASE + FIRST_SCRIPT),))
    assert result.coverage is StockCoverage.POSSIBLE_CLOSED
    assert result.possible_item_ids == frozenset({1, 2})
    assert result.vendors == (result.vendors[0],)
    assert result.vendors[0].local_id == 4
    assert result.effective_inventory is False
    assert result.map_key == (3, 7)
    assert 99 not in result.possible_item_ids  # proven absence is a possibility claim


def test_truly_complete_empty_has_no_vendor_and_does_not_claim_effective_stock():
    rom, profile = build({})
    result = facts(rom, profile)
    assert result.coverage is StockCoverage.POSSIBLE_CLOSED
    assert result.vendors == ()
    assert result.possible_item_ids == frozenset()
    assert result.effective_inventory is False


@pytest.mark.parametrize(
    "list_offset",
    [0x1FFF, 0x300],
)
def test_malformed_or_unterminated_item_lists_are_unknown(list_offset):
    rom, profile = build(
        {
            FIRST_SCRIPT: script(pokemart(list_offset), END),
            # 0x1FFF cannot hold a u16; 0x300 is 128 nonzero ids below.
            0x300: b"\x01\x00" * 128,
        }
    )
    result = facts(rom, profile, objects=(obj(1, BASE + FIRST_SCRIPT),))
    assert result.coverage is StockCoverage.UNKNOWN
    assert result.reasons
    assert result.possible_item_ids == frozenset()


def test_out_of_range_item_id_is_unknown():
    rom, profile = build(
        {FIRST_SCRIPT: script(pokemart(0x300), END), 0x300: items(1, ITEM_COUNT)}
    )
    result = facts(rom, profile, objects=(obj(1, BASE + FIRST_SCRIPT),))
    assert result.coverage is StockCoverage.UNKNOWN


@pytest.mark.parametrize("opcode", [0x41, 0x25, 0x23, 0xB8, 0x87, 0x88])
def test_unknown_or_native_indirect_commands_are_unknown(opcode):
    rom, profile = build({FIRST_SCRIPT: bytes([opcode, 0, 0, 0, 0]) + END})
    result = facts(rom, profile, objects=(obj(1, BASE + FIRST_SCRIPT),))
    assert result.coverage is StockCoverage.UNKNOWN
    assert result.possible_item_ids == frozenset()


def test_invalid_branch_pointer_is_unknown():
    rom, profile = build({FIRST_SCRIPT: b"\x05" + b"\x00\x00\x00\x00"})
    result = facts(rom, profile, objects=(obj(1, BASE + FIRST_SCRIPT),))
    assert result.coverage is StockCoverage.UNKNOWN


def test_standard_call_is_followed_through_gstdscripts():
    rom, profile = build(
        {
            FIRST_SCRIPT: script(callstd(0), END),
            0x180: script(pokemart(0x300), END),
            0x300: items(5),
        },
        std=(BASE + 0x180,),
    )
    result = facts(rom, profile, objects=(obj(1, BASE + FIRST_SCRIPT),))
    assert result.coverage is StockCoverage.POSSIBLE_CLOSED
    assert result.possible_item_ids == frozenset({5})


@pytest.mark.parametrize("index,table", [(10, True), (0, False)])
def test_standard_call_without_valid_bounded_table_is_unknown(index, table):
    rom, profile = build({FIRST_SCRIPT: script(callstd(index), END)}, std_table=table)
    result = facts(rom, profile, objects=(obj(1, BASE + FIRST_SCRIPT),))
    assert result.coverage is StockCoverage.UNKNOWN


def test_hidden_second_merchant_behind_conditional_branch_is_kept_separate():
    rom, profile = build(
        {
            0x100: script(goto_if(0x180), pokemart(0x300), END),
            0x180: script(pokemart(0x310), END),
            0x300: items(1),
            0x310: items(2),
        }
    )
    result = facts(rom, profile, objects=(obj(9, BASE + 0x100),))
    assert result.coverage is StockCoverage.POSSIBLE_CLOSED
    assert result.possible_item_ids == frozenset({1, 2})
    assert len(result.vendors) == 2
    assert {vendor.local_id for vendor in result.vendors} == {9}


def test_frame_only_map_with_zero_npcs_reads_on_frame_table():
    frame_row = struct.pack("<HHI", 0x4001, 1, BASE + 0x180) + b"\x00\x00"
    table = b"\x02" + mars(0x200) + b"\x00"
    rom, profile = build(
        {
            0x100: table,
            0x180: script(pokemart(0x300), END),
            0x200: frame_row,
            0x300: items(7),
        }
    )
    result = facts(rom, profile, scripts_ptr=BASE + 0x100)
    assert result.coverage is StockCoverage.POSSIBLE_CLOSED
    assert result.possible_item_ids == frozenset({7})
    assert result.vendors[0].source == "map_frame"


def test_coordinate_and_background_sign_roots_are_scanned_hidden_item_is_not():
    rom, profile = build(
        {
            0x100: script(pokemart(0x300), END),
            0x180: script(pokemart(0x310), END),
            0x300: items(1),
            0x310: items(2),
        }
    )
    coord = SimpleNamespace(script_ptr=BASE + 0x100)
    sign = SimpleNamespace(kind="sign", script_ptr=BASE + 0x180)
    hidden = SimpleNamespace(kind="hidden_item", script_ptr=None)
    result = facts(rom, profile, coords=(coord,), bgs=(sign, hidden))
    assert result.possible_item_ids == frozenset({1, 2})
    assert {vendor.source for vendor in result.vendors} == {"coord", "bg"}


@pytest.mark.parametrize(
    "table",
    [
        b"\x05" + mars(0x180) + b"\x00",  # unsupported type
        b"\x04" + mars(0x180) + b"\x00",  # type 4 layout unverified
        b"\x01" + mars(0x180) + b"\x01" + mars(0x180) + b"\x00",  # duplicate
        (b"\x01" + mars(0x180)) * 32,  # unterminated
        b"\x02" + mars(0x1FFE) + b"\x00",  # truncated ON_FRAME table
    ],
)
def test_malformed_map_script_tables_are_unknown(table):
    rom, profile = build({0x100: table, 0x180: script(END)})
    result = facts(rom, profile, scripts_ptr=BASE + 0x100)
    assert result.coverage is StockCoverage.UNKNOWN


def test_bounded_cycle_terminates_and_unsupported_bg_kind_is_unknown():
    rom, profile = build({0x100: goto(0x100)})
    assert facts(rom, profile, objects=(obj(1, BASE + 0x100),)).coverage is (
        StockCoverage.POSSIBLE_CLOSED
    )
    unknown_bg = SimpleNamespace(kind="unknown", script_ptr=None)
    assert facts(rom, profile, bgs=(unknown_bg,)).coverage is StockCoverage.UNKNOWN


class _Reader:
    def __init__(self, state, header=BASE + 0x800):
        self.state = state
        self.header = header
        self.calls = []

    def read_static(self, group, number):
        self.calls.append((group, number))
        return SimpleNamespace(usable=True, state=self.state, errors=())

    def rom_header_address(self, group, number):
        return self.header


def test_directory_caches_by_map_and_does_not_read_other_maps():
    rom, profile = build(
        {0x100: script(pokemart(0x300), END), 0x300: items(3)}
    )
    directory = RomMapDirectory.__new__(RomMapDirectory)
    directory._rom = rom
    directory._profile = profile
    directory._tables = tables()
    directory._shop_stock = {}
    state = SimpleNamespace(
        objects=(obj(1, BASE + 0x100),), coord_events=(), bg_events=()
    )
    directory._reader = _Reader(state)
    first = directory.shop_stock(1, 2)
    second = directory.shop_stock(1, 2)
    assert first is second
    assert first.possible_item_ids == frozenset({3})
    assert directory._reader.calls == [(1, 2)]


@pytest.mark.parametrize("header", [None, BASE + ROM_SIZE - 2])
def test_directory_missing_or_truncated_header_is_unknown(header):
    rom, profile = build({0x100: script(END)})
    directory = RomMapDirectory.__new__(RomMapDirectory)
    directory._rom = rom
    directory._profile = profile
    directory._tables = tables()
    directory._shop_stock = {}
    state = SimpleNamespace(objects=(), coord_events=(), bg_events=())
    directory._reader = _Reader(state, header=header)
    result = directory.shop_stock(0, 0)
    assert result.coverage is StockCoverage.UNKNOWN
    assert result.reasons
