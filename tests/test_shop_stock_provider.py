"""Invented ROM bytes only: bounded merchant-stock evidence, never real game data."""

import json
import struct
from types import SimpleNamespace

import pytest

from pokegent.games.gen3.firered.maps.map_data import MapReader
from pokegent.games.gen3.firered.rom.rom_data import load_profiles
from pokegent.games.gen3.firered.rom.rom_tables import RomImagePort
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
QUESTLOG_SHA1 = "41cb23d8dccc8ebd7c649cd8fbb58eeace6e2fdc"


def build(scripts, *, std=(), std_table=True, sha1="0" * 40, size=ROM_SIZE):
    rom = bytearray(size)
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
            "rom": profile.rom.model_copy(update={"sha1": sha1}),
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


def special(index):
    return b"\x25" + struct.pack("<H", index)


def specialvar(index):
    return b"\x26" + struct.pack("<HH", 0x800D, index)


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


def test_plain_questlog_special_read_keeps_both_guarded_arms_closed():
    rom, profile = build(
        {
            FIRST_SCRIPT: script(
                special(391),
                b"\x21" + struct.pack("<HH", 0x800D, 2),  # compare VAR_RESULT, 2
                goto_if(0x180, 1),
                pokemart(0x300),
                END,
            ),
            0x180: script(pokemart(0x310), END),
            0x300: items(1),
            0x310: items(2),
        },
        sha1=QUESTLOG_SHA1,
    )
    result = facts(rom, profile, objects=(obj(3, BASE + FIRST_SCRIPT),))
    assert result.coverage is StockCoverage.POSSIBLE_CLOSED
    assert result.possible_item_ids == frozenset({1, 2})
    assert len(result.vendors) == 2
    assert result.effective_inventory is False


@pytest.mark.parametrize("index", [390, 392])
def test_other_plain_specials_stay_unknown(index):
    rom, profile = build(
        {FIRST_SCRIPT: script(special(index), pokemart(0x300), END), 0x300: items(1)},
        sha1=QUESTLOG_SHA1,
    )
    result = facts(rom, profile, objects=(obj(1, BASE + FIRST_SCRIPT),))
    assert result.coverage is StockCoverage.UNKNOWN
    assert result.possible_item_ids == frozenset()


def test_specialvar_questlog_stays_unknown():
    rom, profile = build(
        {FIRST_SCRIPT: script(specialvar(391), pokemart(0x300), END), 0x300: items(1)},
        sha1=QUESTLOG_SHA1,
    )
    result = facts(rom, profile, objects=(obj(1, BASE + FIRST_SCRIPT),))
    assert result.coverage is StockCoverage.UNKNOWN


@pytest.mark.parametrize("sha1", ["0" * 40, "f" * 40])
def test_questlog_special_with_wrong_declared_rom_hash_stays_unknown(sha1):
    rom, profile = build(
        {FIRST_SCRIPT: script(special(391), pokemart(0x300), END), 0x300: items(1)},
        sha1=sha1,
    )
    result = facts(rom, profile, objects=(obj(1, BASE + FIRST_SCRIPT),))
    assert result.coverage is StockCoverage.UNKNOWN


def test_questlog_special_with_wrong_profile_stays_unknown():
    rom, profile = build(
        {FIRST_SCRIPT: script(special(391), pokemart(0x300), END), 0x300: items(1)},
        sha1=QUESTLOG_SHA1,
    )
    profile = profile.model_copy(update={"profile_id": "firered-jp-rev0"})
    result = facts(rom, profile, objects=(obj(1, BASE + FIRST_SCRIPT),))
    assert result.coverage is StockCoverage.UNKNOWN


def test_questlog_special_in_repeated_standard_call_is_bounded_and_closed():
    rom, profile = build(
        {
            FIRST_SCRIPT: script(callstd(0), callstd(0), END),
            0x180: script(special(391), pokemart(0x300), END),
            0x300: items(5),
        },
        std=(BASE + 0x180,),
        sha1=QUESTLOG_SHA1,
    )
    result = facts(rom, profile, objects=(obj(1, BASE + FIRST_SCRIPT),))
    assert result.coverage is StockCoverage.POSSIBLE_CLOSED
    assert result.possible_item_ids == frozenset({5})
    assert len(result.vendors) == 1


def test_questlog_special_allowance_does_not_bypass_command_limit():
    rom, profile = build(
        {FIRST_SCRIPT: special(391) * 8193 + END}, sha1=QUESTLOG_SHA1, size=0x8000
    )
    result = facts(rom, profile, objects=(obj(1, BASE + FIRST_SCRIPT),))
    assert result.coverage is StockCoverage.UNKNOWN


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


def _real_reader_map(tmp_path, monkeypatch, *, clone):
    """One invented map through the real MapReader: clone template or normal NPC."""
    image = bytearray(0x2000)
    struct.pack_into("<I", image, 0x4C, BASE + 0x40)  # gMapGroups -> group table
    struct.pack_into("<I", image, 0x40, BASE + 0x100)  # one MapHeader
    struct.pack_into("<IIII", image, 0x100, BASE + 0x200, BASE + 0x400, 0, 0)
    struct.pack_into(
        "<iiIIIIBB", image, 0x200, 1, 1,
        BASE + 0x500, BASE + 0x502, BASE + 0x300, BASE + 0x320, 1, 1,
    )  # fmt: skip
    struct.pack_into("<I", image, 0x314, BASE + 0x800)  # primary attributes
    struct.pack_into("<I", image, 0x334, BASE + 0x800)  # secondary attributes
    image[0x400] = 1  # one object template
    struct.pack_into("<I", image, 0x404, BASE + 0x420)
    image[0x422] = 0xFF if clone else 0  # ObjectEventTemplate.kind
    image[0x428] = 1  # clone target local id; ignored for a normal NPC
    if not clone:
        struct.pack_into("<I", image, 0x430, BASE + 0x600)  # template script
    image[0x600 : 0x600 + 6] = script(pokemart(0x700), END)
    image[0x700 : 0x700 + 4] = items(7)
    profile = load_profiles().get("firered-us-rev0")
    symbols = dict(profile.symbols)
    symbols["gMapGroups"] = symbols["gMapGroups"].model_copy(
        update={"address": BASE + 0x4C, "size": 4}
    )
    profile = profile.model_copy(update={"symbols": symbols})
    index = tmp_path / "invented-map-groups.json"
    index.write_text(
        json.dumps({"group_order": ["Group_000"], "groups": {"Group_000": ["Map_G000_N000"]}})
    )
    monkeypatch.setenv("POKEGENT_MAP_GROUPS", str(index))
    rom = bytes(image)
    result = MapReader(RomImagePort(rom), profile).read_static(0, 0)
    assert result.usable, result.errors
    return rom, profile, result.state


def test_real_reader_clone_without_script_is_unknown(tmp_path, monkeypatch):
    rom, profile, state = _real_reader_map(tmp_path, monkeypatch, clone=True)
    assert state.objects[0].role == "clone"
    assert state.objects[0].clone_of is not None
    assert state.objects[0].script_ptr is None
    result = facts(rom, profile, objects=state.objects)
    assert result.coverage is StockCoverage.UNKNOWN
    assert result.possible_item_ids == frozenset()
    assert result.reasons


def test_real_reader_normal_npc_control_stays_closed(tmp_path, monkeypatch):
    rom, profile, state = _real_reader_map(tmp_path, monkeypatch, clone=False)
    assert state.objects[0].role != "clone"
    assert state.objects[0].clone_of is None
    result = facts(rom, profile, objects=state.objects)
    assert result.coverage is StockCoverage.POSSIBLE_CLOSED
    assert result.possible_item_ids == frozenset({7})


def test_typed_clone_root_is_unknown_not_silently_omitted():
    rom, profile = build({0x100: script(END)})
    clone = SimpleNamespace(
        role="clone", clone_of=(2, 3, 4), script_ptr=None, source="template"
    )
    result = facts(rom, profile, objects=(clone,))
    assert result.coverage is StockCoverage.UNKNOWN
    assert result.reasons


def test_secret_base_background_event_is_unknown_even_with_script():
    rom, profile = build({0x100: script(pokemart(0x300), END), 0x300: items(1)})
    secret = SimpleNamespace(kind="secret_base", script_ptr=BASE + 0x100)
    result = facts(rom, profile, bgs=(secret,))
    assert result.coverage is StockCoverage.UNKNOWN
    assert result.possible_item_ids == frozenset()


def test_ordinary_sign_with_same_script_remains_closed():
    rom, profile = build({0x100: script(pokemart(0x300), END), 0x300: items(1)})
    sign = SimpleNamespace(kind="sign", script_ptr=BASE + 0x100)
    result = facts(rom, profile, bgs=(sign,))
    assert result.coverage is StockCoverage.POSSIBLE_CLOSED
    assert result.possible_item_ids == frozenset({1})


def trainerbattle(battle_type, pointer):
    size = {0: 13, 1: 17, 2: 17, 3: 9, 4: 17, 5: 13, 6: 21, 7: 17, 8: 21, 9: 13}[battle_type]
    return b"\x5c" + bytes([battle_type]) + bytes(size - 5) + struct.pack("<I", pointer)


@pytest.mark.parametrize("pointer", [0, 0x0200_0100])
def test_continuation_bearing_trainerbattle_with_invalid_pointer_is_unknown(pointer):
    rom, profile = build({0x100: script(trainerbattle(1, pointer), END)})
    result = facts(rom, profile, objects=(obj(1, BASE + 0x100),))
    assert result.coverage is StockCoverage.UNKNOWN
    assert result.possible_item_ids == frozenset()


def test_continuation_bearing_trainerbattle_with_rom_pointer_is_followed():
    rom, profile = build(
        {
            0x100: script(trainerbattle(1, BASE + 0x180), END),
            0x180: script(pokemart(0x300), END),
            0x300: items(7),
        }
    )
    result = facts(rom, profile, objects=(obj(1, BASE + 0x100),))
    assert result.coverage is StockCoverage.POSSIBLE_CLOSED
    assert result.possible_item_ids == frozenset({7})


def test_non_continuation_trainerbattle_pointer_is_not_followed():
    rom, profile = build(
        {
            0x100: script(trainerbattle(0, BASE + 0x180), END),
            0x180: script(pokemart(0x300), END),
            0x300: items(7),
        }
    )
    result = facts(rom, profile, objects=(obj(1, BASE + 0x100),))
    assert result.coverage is StockCoverage.POSSIBLE_CLOSED
    assert result.possible_item_ids == frozenset()
