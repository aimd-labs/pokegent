"""Synthetic ROM/memory evidence for the strict static map and key codec."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from pokegent.games.gen3.firered.maps import map_constants as K
from pokegent.games.gen3.firered.maps.map_data import MapReader
from pokegent.games.gen3.firered.maps.map_schema import (
    BgEventInfo,
    ConnectionInfo,
    CoordEventInfo,
    HiddenItem,
    MapGrid,
    MapIdentity,
    MapLayoutInfo,
    MapObject,
    MapState,
    PlayerPlacement,
    ViewWindow,
    WarpInfo,
)
from pokegent.games.gen3.firered.rom import static_map_codec as codec
from pokegent.games.gen3.firered.rom.rom_data import load_profiles
from pokegent.games.gen3.firered.rom.rom_tables import RomImagePort
from pokegent.games.gen3.firered.rom.script_effects import Guard
from pokegent.games.gen3.firered.rom.static_symbolic_codec import CodecError

BASE = 0x08000000


def script(*words: int) -> bytes:
    return b"".join(struct.pack("<H", word) for word in words)


def build_static_rom(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[bytes, object]:
    """One invented two-by-two map with every representative static event kind."""
    image = bytearray(0x3000)
    struct.pack_into("<I", image, 0x4C, BASE + 0x40)  # gMapGroups -> group table
    struct.pack_into("<I", image, 0x40, BASE + 0x100)  # one MapHeader
    struct.pack_into("<IIII", image, 0x100, BASE + 0x200, BASE + 0x400, 0, BASE + 0x900)
    struct.pack_into("<H", image, 0x110, 0x1234)  # music
    struct.pack_into("<H", image, 0x112, 7)  # mapLayoutId
    image[0x114] = 2  # regionMapSectionId
    image[0x115] = 1  # cave
    image[0x116] = 3  # weather
    image[0x117] = 1  # mapType
    image[0x118] = 1  # bikingAllowed
    image[0x119] = 3  # allowEscaping + allowRunning
    image[0x11A] = 0xFE  # floorNum -2 (signed)
    image[0x11B] = 2  # battleType
    struct.pack_into(
        "<iiIIIIBB", image, 0x200, 2, 2, BASE + 0x520, BASE + 0x530,
        BASE + 0x300, BASE + 0x320, 2, 2,
    )  # fmt: skip
    struct.pack_into("<I", image, 0x314, BASE + 0xB00)  # primary attributes
    struct.pack_into("<I", image, 0x334, BASE + 0x1600)  # secondary attributes
    for mid, value in ((1, 0x20), (2, 0x21), (3, 0x22), (4, 0x23)):
        if mid < K.NUM_METATILES_IN_PRIMARY:
            struct.pack_into("<I", image, 0xB00 + mid * 4, value)
        else:
            struct.pack_into(
                "<I", image, 0x1600 + (mid - K.NUM_METATILES_IN_PRIMARY) * 4, value
            )
    for index, block in enumerate((0x0001, 0x0002, 0x0003, 0x0004)):
        struct.pack_into("<H", image, 0x520 + index * 2, block)  # border
    for index, block in enumerate((0x0001, 0x2402, 0x0803, 0x0C04)):
        struct.pack_into("<H", image, 0x530 + index * 2, block)  # map blocks
    # MapEvents: four objects, two warps, one coordinate event, two background events.
    image[0x400:0x404] = bytes((4, 2, 1, 2))
    struct.pack_into("<IIII", image, 0x404, BASE + 0x600, BASE + 0x700, BASE + 0x740, BASE + 0x760)
    objects = (
        (1, 10, 0, -1, 1, 1, 1, 0x21, 0, 0, 0, 0),
        (2, 11, 0, 1, 1, 1, 0, 0x12, 1, 3, BASE + 0x860, 0),
        (3, K.OBJ_EVENT_GFX_ITEM_BALL, 0, 1, 0, 0, 0, 0, 0, 0, 0, 10),
        (4, 12, K.OBJ_KIND_CLONE, 0, 0, 7, 0, 0, 9, 8, 0, 0),
    )
    for index, row in enumerate(objects):
        struct.pack_into("<BBBxhhBBBxHHIH2x", image, 0x600 + index * 0x18, *row)
    struct.pack_into("<hhBBBB", image, 0x700, 1, -1, 1, 0, 5, 6)
    struct.pack_into("<hhBBBB", image, 0x708, 20, 20, 0, 1, 127, 127)
    struct.pack_into("<HHBBHHHI", image, 0x740, 1, 0, 1, 0, 0x4001, 2, 0, BASE + 0x800)
    struct.pack_into("<HHBB", image, 0x760, 0, 1, 1, 1)  # sign
    struct.pack_into("<I", image, 0x768, BASE + 0x820)
    struct.pack_into("<HHBB", image, 0x76C, 1, 1, 0, K.BG_EVENT_HIDDEN_ITEM)
    struct.pack_into("<HBB", image, 0x774, 13, 5, 0x83)
    struct.pack_into("<iI", image, 0x900, 1, BASE + 0x920)  # one connection
    struct.pack_into("<B3xiBB2x", image, 0x920, 1, -3, 2, 3)  # S, signed offset
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
    return bytes(image), profile


@pytest.fixture
def static_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MapState:
    rom, profile = build_static_rom(tmp_path, monkeypatch)
    result = MapReader(RomImagePort(rom), profile).read_static(0, 0)
    assert result.usable, result.errors
    return result.state


def wire_of(state: MapState) -> dict:
    return json.loads(json.dumps(codec.encode_static_map(state)))


def exact(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(exact(left[k], right[k]) for k in left)
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(exact(a, b) for a, b in zip(left, right))
    return left == right


def test_real_read_static_roundtrip_preserves_every_fact(static_state: MapState) -> None:
    state = static_state
    assert state.player is None and state.strength_active is None
    assert any(w.dynamic for w in state.warps)
    assert any(o.role == "clone" for o in state.objects)
    assert state.identity.floor_num == -2
    wire = wire_of(state)
    decoded = codec.decode_static_map(wire)
    assert type(decoded) is MapState
    assert decoded == state
    assert decoded.model_dump(mode="json") == wire["map"]
    # Owning container and scalar types are reconstructed exactly, not as JSON lists.
    assert type(decoded.grid.blocks) is tuple and type(decoded.grid.attributes) is tuple
    assert type(decoded.grid.attributes[0]) is tuple
    assert type(decoded.warps) is tuple and type(decoded.objects) is tuple
    assert type(decoded.identity.biking_allowed) is bool
    assert type(decoded.identity.floor_num) is int
    clone = next(o for o in decoded.objects if o.role == "clone")
    assert clone.clone_of == (7, 8, 9) and type(clone.clone_of) is tuple
    dynamic = next(w for w in decoded.warps if w.dynamic)
    assert (dynamic.dest_group, dynamic.dest_number) == (127, 127)
    assert dynamic.dest_name is None
    assert dynamic.tile_behavior is None and dynamic.tile_kind is None
    raw_warp = next(w for w in decoded.warps if not w.dynamic)
    assert (raw_warp.dest_group, raw_warp.dest_number) == (6, 5) and raw_warp.dest_name is None
    assert decoded.objects[0].x == -1
    hidden = next(b.hidden_item for b in decoded.bg_events if b.hidden_item is not None)
    assert (hidden.item_id, hidden.flag_id, hidden.flag_set, hidden.quantity) == (
        13, K.FLAG_HIDDEN_ITEMS_START + 5, None, 3
    )
    assert any(c.active is None for c in decoded.coord_events)
    assert decoded.connections[0].offset == -3
    assert decoded.view == ViewWindow() and decoded.seen_tiles == 0


def test_decoded_instance_is_fresh_and_caches_are_not_persisted(static_state: MapState) -> None:
    state = static_state
    _ = state.grid.attribute_map
    _ = state.raw_block(0, 0)  # populate the derived runtime tile cache
    assert "_tile_cache" in state.__dict__ and "attribute_map" in state.grid.__dict__
    wire = wire_of(state)
    assert "_tile_cache" not in wire["map"] and "attribute_map" not in wire["map"]["grid"]
    first = codec.decode_static_map(wire)
    second = codec.decode_static_map(wire)
    assert first is not second and first is not state
    assert "_tile_cache" not in first.__dict__ and "attribute_map" not in first.grid.__dict__
    first.grid.attribute_map[1] = 0xFFFF
    first.raw_block(1, 1)
    assert second.grid.attribute_map[1] != 0xFFFF
    assert "_tile_cache" not in second.__dict__
    assert state.grid.attribute_map[1] != 0xFFFF


def _player() -> PlayerPlacement:
    return PlayerPlacement(
        save_x=0, save_y=0, avatar_x=0, avatar_y=0, prev_x=0, prev_y=0, x=0, y=0,
        position_consistent=True, facing="N", elevation=3, object_event_id=0,
        avatar_flags=1, surfing=False, underwater=False, biking=False,
        controllable_flag=True, forced=False, moving=False, tile_transition_state=0,
        running_state=0,
    )  # fmt: skip


@pytest.mark.parametrize(
    "update",
    [
        {"player": _player()},
        {"strength_active": False},
        {"flash_level": 0},
        {"seen_tiles": 1},
        {"view": ViewWindow(left=6)},
    ],
)
def test_encoder_refuses_live_state(static_state: MapState, update: dict) -> None:
    with pytest.raises(CodecError):
        codec.encode_static_map(static_state.model_copy(update=update))


@pytest.mark.parametrize("update", [{"source": "live"}, {"hidden_flag_set": False}])
def test_encoder_refuses_laundered_object_facts(static_state: MapState, update: dict) -> None:
    objects = (static_state.objects[0].model_copy(update=update),)
    with pytest.raises(CodecError):
        codec.encode_static_map(static_state.model_copy(update={"objects": objects}))


def test_encoder_refuses_live_coord_and_hidden_flag(static_state: MapState) -> None:
    coords = (static_state.coord_events[0].model_copy(update={"active": True}),)
    with pytest.raises(CodecError):
        codec.encode_static_map(static_state.model_copy(update={"coord_events": coords}))
    bg = static_state.bg_events[1]
    flagged_item = bg.hidden_item.model_copy(update={"flag_set": True})
    flagged = bg.model_copy(update={"hidden_item": flagged_item})
    with pytest.raises(CodecError):
        codec.encode_static_map(static_state.model_copy(update={"bg_events": (flagged,)}))


def test_encoder_refuses_wrong_or_subclassed_model(static_state: MapState) -> None:
    with pytest.raises(CodecError):
        codec.encode_static_map(static_state.model_dump(mode="json"))

    class Derived(MapState):
        pass

    with pytest.raises(CodecError):
        codec.encode_static_map(Derived.model_validate(static_state.model_dump()))


def test_decoder_refuses_missing_defaultable_and_nested_unknown_keys(
    static_state: MapState,
) -> None:
    wire = wire_of(static_state)
    missing = json.loads(json.dumps(wire))
    del missing["map"]["warps"][0]["knowledge"]  # defaultable, must not be silently inserted
    with pytest.raises(CodecError):
        codec.decode_static_map(missing)
    absent_flag = json.loads(json.dumps(wire))
    del absent_flag["map"]["objects"][0]["hidden_flag_set"]  # defaultable None
    with pytest.raises(CodecError):
        codec.decode_static_map(absent_flag)
    nested_missing = json.loads(json.dumps(wire))
    del nested_missing["map"]["identity"]["weather"]  # required owning field
    with pytest.raises(CodecError):
        codec.decode_static_map(nested_missing)
    extra = json.loads(json.dumps(wire))
    extra["map"]["identity"]["invented"] = 1
    with pytest.raises(CodecError):
        codec.decode_static_map(extra)
    extra_top = json.loads(json.dumps(wire))
    extra_top["invented"] = 1
    with pytest.raises(CodecError):
        codec.decode_static_map(extra_top)


@pytest.mark.parametrize(
    "path,value",
    [
        (("map", "identity", "biking_allowed"), 1),
        (("map", "warps", 0, "index"), True),
        (("map", "layout", "width"), 2.0),
        (("map", "identity", "group"), "0"),
        (("map", "warps", 0, "knowledge"), "guessed"),
        (("map", "identity", "identity_verified"), 1),
        (("map", "coord_events", 0, "active"), 1),
        (("map", "strength_active"), 0),
    ],
)
def test_decoder_refuses_wrong_types_and_enums(
    static_state: MapState, path: tuple, value: object
) -> None:
    wire = wire_of(static_state)
    target = wire
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(CodecError):
        codec.decode_static_map(wire)


@pytest.mark.parametrize(
    "wire",
    [
        [],
        {"version": 1, "type": "static_map"},
        {"version": 2, "type": "static_map", "map": {}},
        {"version": 1, "type": "interpretation_key", "map": {}},
    ],
)
def test_decoder_refuses_bad_framing(wire: object) -> None:
    with pytest.raises(CodecError):
        codec.decode_static_map(wire)


class StringSubclass(str):
    pass


@pytest.mark.parametrize("record", ["map", "key"])
def test_framing_refuses_string_subclass_keys(static_state: MapState, record: str) -> None:
    if record == "map":
        wire = codec.encode_static_map(static_state)
        decode = codec.decode_static_map
    else:
        wire = codec.encode_interpretation_key(sample_key())
        decode = codec.decode_interpretation_key
    bad = {StringSubclass(key): value for key, value in wire.items()}
    with pytest.raises(CodecError):
        decode(bad)


@pytest.mark.parametrize(
    "case", ["seen_tiles", "floor_bool", "warp_index_bool", "object_elev_float", "layout_width_str"]
)
def test_encoder_refuses_owning_field_type_coercion(static_state: MapState, case: str) -> None:
    state = static_state
    if case == "seen_tiles":
        bad = state.model_copy(update={"seen_tiles": False})
    elif case == "floor_bool":
        identity = state.identity.model_copy(update={"floor_num": True})
        bad = state.model_copy(update={"identity": identity})
    elif case == "warp_index_bool":
        warps = (state.warps[0].model_copy(update={"index": False}),) + state.warps[1:]
        bad = state.model_copy(update={"warps": warps})
    elif case == "object_elev_float":
        objects = (state.objects[0].model_copy(update={"elevation": 1.0}),) + state.objects[1:]
        bad = state.model_copy(update={"objects": objects})
    else:
        layout = state.layout.model_copy(update={"width": "2"})
        bad = state.model_copy(update={"layout": layout})
    with pytest.raises(CodecError):
        codec.encode_static_map(bad)


def test_encoded_byte_bound_is_enforced_before_dumps(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    real = codec._dumps
    monkeypatch.setattr(codec, "PAYLOAD_MAX_BYTES", 1000)

    def spy(value: object) -> str:
        calls.append(value)
        return real(value)

    monkeypatch.setattr(codec, "_dumps", spy)
    with pytest.raises(CodecError):
        codec._serialize({"v": "\x01" * 200})  # 200 raw bytes, ~1202 encoded bytes
    assert calls == []
    with pytest.raises(CodecError):
        codec._serialize({"v": [10**60] * 50})
    assert calls == []
    assert json.loads(codec._serialize({"v": [1, 2]})) == {"v": [1, 2]}
    assert calls and calls[-1] == {"v": [1, 2]}


def test_decoder_encoded_byte_bound_precedes_json_serialization(
    static_state: MapState, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire = json.loads(json.dumps(codec.encode_static_map(static_state)))
    wire["map"]["identity"]["name"] = "\x01" * 20_000  # 20k raw, 120k encoded
    calls: list[object] = []
    real = codec._dumps
    monkeypatch.setattr(codec, "PAYLOAD_MAX_BYTES", 50_000)

    def spy(value: object) -> str:
        calls.append(value)
        return real(value)

    monkeypatch.setattr(codec, "_dumps", spy)
    with pytest.raises(CodecError):
        codec.decode_static_map(wire)
    assert calls == []


@pytest.mark.parametrize("bad", [1.5, object(), {1: "non-str-key"}, ("tuple",)])
def test_decoder_refuses_values_outside_the_wire_algebra(
    static_state: MapState, bad: object
) -> None:
    wire = wire_of(static_state)
    wire["map"]["identity"]["invented"] = bad
    with pytest.raises(CodecError):
        codec.decode_static_map(wire)


def test_decoder_refuses_cycles_and_excessive_depth(static_state: MapState) -> None:
    wire = wire_of(static_state)
    cycle: list = []
    cycle.append(cycle)
    wire["map"]["identity"]["invented"] = cycle
    with pytest.raises(CodecError):
        codec.decode_static_map(wire)
    deep: object = 0
    for _ in range(300):
        deep = [deep]
    deep_wire = wire_of(static_state)
    deep_wire["map"]["identity"]["invented"] = deep
    with pytest.raises(CodecError):
        codec.decode_static_map(deep_wire)


def test_full_model_field_sets_force_a_codec_decision(static_state: MapState) -> None:
    wire = wire_of(static_state)["map"]
    assert set(wire) == set(MapState.model_fields)
    assert set(wire["identity"]) == set(MapIdentity.model_fields)
    assert set(wire["layout"]) == set(MapLayoutInfo.model_fields)
    assert set(wire["grid"]) == set(MapGrid.model_fields)
    assert set(wire["warps"][0]) == set(WarpInfo.model_fields)
    assert set(wire["coord_events"][0]) == set(CoordEventInfo.model_fields)
    assert set(wire["bg_events"][0]) == set(BgEventInfo.model_fields)
    assert set(wire["bg_events"][1]["hidden_item"]) == set(HiddenItem.model_fields)
    assert set(wire["objects"][0]) == set(MapObject.model_fields)
    assert set(wire["connections"][0]) == set(ConnectionInfo.model_fields)
    assert set(wire["view"]) == set(ViewWindow.model_fields)


def sample_key() -> tuple:
    return (
        0,
        (3, 0),
        ((0x4002, 5), (0x4001, 7), (0x4002, 5)),
        (-2, 3),
        True,
        (
            Guard("flag", 0x820, "eq", True),
            Guard("var", 0x4001, "ge", 3, origin=(1, 2, "x", 4)),
        ),
        False,
    )


def test_interpretation_key_roundtrip_preserves_order_duplicates_and_types() -> None:
    key = sample_key()
    wire = json.loads(json.dumps(codec.encode_interpretation_key(key)))
    assert wire["map"] == [3, 0] and wire["player_xy"] == [-2, 3]
    assert wire["initial_variables"] == [[0x4002, 5], [0x4001, 7], [0x4002, 5]]
    decoded = codec.decode_interpretation_key(wire)
    assert exact(decoded, key)
    assert type(decoded) is tuple and type(decoded[1]) is tuple
    assert type(decoded[2]) is tuple and all(type(pair) is tuple for pair in decoded[2])
    assert type(decoded[4]) is bool and type(decoded[6]) is bool
    assert type(decoded[5]) is tuple and all(type(g) is Guard for g in decoded[5])
    assert type(decoded[5][0].value) is bool and type(decoded[5][1].value) is int


@pytest.mark.parametrize(
    "key",
    [
        [0, (0, 0), (), None, True, (), False],
        (0, (0, 0), (), None, True, ()),
        (True, (0, 0), (), None, True, (), False),
        (0, [0, 0], (), None, True, (), False),
        (0, (0, 0), [[1, 2]], None, True, (), False),
        (0, (0, 0), ((1, True),), None, True, (), False),
        (0, (0, 0), (), [0, 0], True, (), False),
        (0, (0, 0), (), None, 1, (), False),
        (0, (0, 0), (), None, True, [], False),
        (0, (0, 0), (), None, True, ("not-a-guard",), False),
        (0, (0, 0), (), None, True, (), 0),
        (0, (0, 0), (), None, True, (Guard("flag", 1, "invented", True),), False),
    ],
)
def test_encoder_refuses_noncanonical_keys(key: object) -> None:
    with pytest.raises(CodecError):
        codec.encode_interpretation_key(key)


@pytest.mark.parametrize(
    "change",
    ["pointer_bool", "minimize_int", "ferry_int", "var_bool", "extra", "missing", "tuple_map"],
)
def test_key_decoder_is_strict(change: str) -> None:
    wire = json.loads(json.dumps(codec.encode_interpretation_key(sample_key())))
    if change == "pointer_bool":
        wire["pointer"] = True
    elif change == "minimize_int":
        wire["minimize_guards"] = 1
    elif change == "ferry_int":
        wire["ferry_mode"] = 0
    elif change == "var_bool":
        wire["initial_variables"][0][1] = True
    elif change == "extra":
        wire["invented"] = 1
    elif change == "missing":
        del wire["ferry_mode"]
    else:
        wire["map"] = (3, 0)
    with pytest.raises(CodecError):
        codec.decode_interpretation_key(wire)


def test_key_decoder_refuses_unsupported_guard_operator() -> None:
    wire = json.loads(json.dumps(codec.encode_interpretation_key(sample_key())))
    wire["guards"][0]["operator"] = "invented"
    with pytest.raises(CodecError):
        codec.decode_interpretation_key(wire)


def test_key_preserves_five_tuple_guard_origin() -> None:
    key = (
        1,
        (0, 1),
        ((0x4000, -1),),
        None,
        False,
        (Guard("trainer", 7, "ne", False, origin=(2, 3, "op", 4, 5)),),
        True,
    )
    decoded = codec.decode_interpretation_key(
        json.loads(json.dumps(codec.encode_interpretation_key(key)))
    )
    assert exact(decoded, key)
    assert decoded[5][0].origin == (2, 3, "op", 4, 5)


def test_null_player_xy_and_zero_pointer_are_preserved() -> None:
    key = (0, (0, 0), (), None, False, (), True)
    decoded = codec.decode_interpretation_key(
        json.loads(json.dumps(codec.encode_interpretation_key(key)))
    )
    assert exact(decoded, key) and decoded[3] is None and decoded[0] == 0
