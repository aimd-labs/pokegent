"""Focused synthetic tests for the unused effect static-index codec."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import fields, replace
from pathlib import Path

import pytest

from pokegent.games.gen3.firered.maps.obstacles import (
    KINDS,
    FieldObstacle,
    ObstacleKind,
    script_gate,
    script_tile,
)
from pokegent.games.gen3.firered.rom import static_effect_index_codec as codec
from pokegent.games.gen3.firered.rom.script_effects import Effect, Guard
from pokegent.games.gen3.firered.rom.static_symbolic_codec import (
    MAX_CONTAINER_LENGTH,
    MAX_STRING_BYTES,
    CodecError,
)

FIELDS = ("script_obstacles", "on_load_warp_openings", "on_load_metatiles")
KIND_FIELDS = (
    "role", "name", "option_prefix", "verb", "requirement_key",
    "requirement_kind", "capability_kind", "completion", "label", "prompt_key",
    "irreversible",
)
OBSTACLE_FIELDS = (
    "kind", "source", "local_id", "bg_index", "x", "y", "script_ptr",
    "hide_flag", "script_sets_hide_flag", "script_starts_battle",
    "requirement_label", "equivalent_requirements", "triggerable", "declinable",
)


def exact(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if hasattr(type(left), "__dataclass_fields__"):
        return all(
            exact(getattr(left, name), getattr(right, name))
            for name in type(left).__dataclass_fields__  # type: ignore[arg-type]
        )
    if isinstance(left, tuple):
        return len(left) == len(right) and all(
            exact(a, b) for a, b in zip(left, right)  # type: ignore[arg-type]
        )
    return left == right


def effects() -> tuple[Effect, ...]:
    return (
        Effect(
            kind="setmetatile",
            source=7,
            guards=(
                Guard("flag", 0x120, "eq", True, origin=(1, 2, "setmetatile", 3)),
                Guard("trainer", 0x500, "eq", False, origin=(4, 5, "script", 6, 7)),
            ),
            values=(
                ("x", 4), ("y", -5), ("collision", 1), ("behavior", 0x10),
                ("metatile_id", "unresolved-T"), ("flag", True),
                ("unresolved", (1, True, None, "s", [2, False])),
            ),
            decoded=False,
            reason="script P: A; B",
        ),
        Effect(kind="setmetatile", source=0x800001, guards=(), values=(("x", True),)),
    )


def tile_obstacle() -> FieldObstacle:
    return script_tile(
        x=-2, y=3, bg_index=4, opened_flag=0x120,
        requirement_key="item:13", name="Card Key door",
    )


def gate_obstacle() -> FieldObstacle:
    return script_gate(
        x=5, y=6, requirement_key="item:13", name="Gatekeeper",
        requirement_label="Gate label", equivalent_requirements=("flag:288", "item:13"),
        triggerable=True, declinable=True,
    )


def object_obstacle() -> FieldObstacle:
    return FieldObstacle(
        kind=KINDS["snorlax"], source="object_event", local_id=2, bg_index=None,
        x=5, y=6, script_ptr=0x800000, hide_flag=0x120,
        script_sets_hide_flag=True, script_starts_battle=True,
        requirement_label=None, equivalent_requirements=(), triggerable=False,
        declinable=True,
    )


def facts() -> codec.StaticEffectFacts:
    return codec.StaticEffectFacts(
        script_obstacles=(
            ((3, 4), (tile_obstacle(), object_obstacle())),
            ((3, 4), ()),
            ((-1, 9), (gate_obstacle(),)),
        ),
        on_load_warp_openings=(
            (((3, 4), 1), (effects()[0], effects()[1])),
            (((-1, 9), -4), ()),
            (((3, 4), 1), (effects()[1],)),
        ),
        on_load_metatiles=(
            ((3, 4), (((0x120, 0x121), effects()[0]), ((1,), effects()[1]))),
            ((3, 4), ()),
        ),
    )


def wire() -> dict:
    return json.loads(json.dumps(codec.encode_effect_facts(facts())))


def roundtrip(facts_value: codec.StaticEffectFacts) -> codec.StaticEffectFacts:
    return codec.decode_effect_facts(
        json.loads(json.dumps(codec.encode_effect_facts(facts_value)))
    )


def test_exact_roundtrip_preserves_three_families() -> None:
    original = facts()
    encoded = codec.encode_effect_facts(original)
    assert type(encoded) is dict and set(encoded) == {"version", "type", *FIELDS}
    decoded = codec.decode_effect_facts(json.loads(json.dumps(encoded)))
    assert type(decoded) is codec.StaticEffectFacts
    for field in FIELDS:
        assert exact(getattr(decoded, field), getattr(original, field)), field
        assert type(getattr(decoded, field)) is tuple
    first = decoded.script_obstacles[0][1][0]
    assert type(first) is FieldObstacle and type(first.kind) is ObstacleKind
    assert first.kind.role == "script_gate" and first.source == "tile"
    assert first.kind.label == tile_obstacle().kind.label
    assert decoded.on_load_warp_openings[2][0] == ((3, 4), 1)
    assert len(decoded.on_load_warp_openings) == 3
    assert decoded.on_load_metatiles[0][1][0][0] == (0x120, 0x121)
    assert decoded.on_load_warp_openings[0][1][0].decoded is False
    assert decoded.on_load_warp_openings[0][1][0].reason == "script P: A; B"
    assert decoded.on_load_warp_openings[0][1][0].guards[1].origin == (4, 5, "script", 6, 7)


def test_all_obstacle_kind_and_field_values_drift_exactly() -> None:
    assert tuple(f.name for f in fields(ObstacleKind)) == KIND_FIELDS
    assert tuple(f.name for f in fields(FieldObstacle)) == OBSTACLE_FIELDS
    kind = ObstacleKind(
        role="drift_role", name="drift name", option_prefix="drift_prefix",
        verb="DRIFT_VERB", requirement_key="drift_key",
        requirement_kind="field_move", capability_kind="drift_cap",
        completion="object_gone", label="Drift label", prompt_key="drift prompt",
        irreversible=True,
    )
    obstacle = FieldObstacle(
        kind=kind, source="tile", local_id=None, bg_index=-3, x=-4, y=5,
        script_ptr=None, hide_flag=-6, script_sets_hide_flag=True,
        script_starts_battle=False, requirement_label="drift requirement",
        equivalent_requirements=("a", "b"), triggerable=True, declinable=False,
    )
    decoded = roundtrip(replace(facts(), script_obstacles=(((7, 8), (obstacle,)),)))
    result = decoded.script_obstacles[0][1][0]
    for name in KIND_FIELDS:
        assert exact(getattr(result.kind, name), getattr(kind, name)), name
    for name in OBSTACLE_FIELDS:
        assert exact(getattr(result, name), getattr(obstacle, name)), name
    assert result is not obstacle and result.kind is not kind


def test_dynamic_script_tile_and_gate_kinds_keep_owning_labels() -> None:
    assert "script_gate" not in KINDS
    tile, gate = tile_obstacle(), gate_obstacle()
    assert tile.kind.role == gate.kind.role == "script_gate"
    assert tile.kind.requirement_kind == "script_flag"
    assert tile.kind.completion == "script_flag"
    assert gate.kind.requirement_kind == "key_item"
    assert gate.kind.label == "Pass Gatekeeper"
    decoded = roundtrip(replace(facts(), script_obstacles=(((1, 2), (tile, gate)),)))
    rows = decoded.script_obstacles[0][1]
    assert exact(rows[0], tile) and exact(rows[1], gate)
    assert rows[0].kind.role == rows[1].kind.role == "script_gate"
    assert rows[0].hide_flag == 0x120 and rows[1].hide_flag is None


def test_tagged_effect_values_and_origins_keep_exact_types() -> None:
    decoded = roundtrip(facts()).on_load_warp_openings[0][1][0]
    source, reason = decoded.source, decoded.reason
    assert type(source) is int and source == 7
    assert type(reason) is str and reason == "script P: A; B"
    assert decoded.decoded is False
    assert type(decoded.guards[0].value) is bool and decoded.guards[0].value is True
    assert type(decoded.guards[1].value) is bool and decoded.guards[1].value is False
    assert type(decoded.guards[0].origin) is tuple and len(decoded.guards[0].origin) == 4
    assert len(decoded.guards[1].origin) == 5
    values = dict(decoded.values)
    assert values["x"] == 4 and type(values["x"]) is int
    assert values["metatile_id"] == "unresolved-T" and type(values["metatile_id"]) is str
    assert values["flag"] is True and type(values["flag"]) is bool
    assert type(values["unresolved"]) is tuple
    assert type(values["unresolved"][3]) is str
    assert type(values["unresolved"][4]) is list
    second = decoded = roundtrip(facts()).on_load_warp_openings[0][1][1]
    assert dict(second.values)["x"] is True and type(dict(second.values)["x"]) is bool


def test_empty_duplicate_and_order_preserved() -> None:
    empty = codec.StaticEffectFacts((), (), ())
    encoded = codec.encode_effect_facts(empty)
    for field in FIELDS:
        assert encoded[field] == []
    assert roundtrip(empty) == empty
    duplicate = replace(
        facts(),
        on_load_warp_openings=(
            (((3, 4), 1), (effects()[0],)),
            (((3, 4), 1), (effects()[0],)),
        ),
        script_obstacles=(((3, 4), (tile_obstacle(), tile_obstacle())),),
    )
    decoded = roundtrip(duplicate)
    assert len(decoded.on_load_warp_openings) == 2
    assert decoded.script_obstacles[0][1][0] is not decoded.script_obstacles[0][1][1]


def test_framing_refuses_missing_extra_subclass_and_wrong_tags() -> None:
    good = wire()

    class Mapping(dict):
        pass

    with pytest.raises(CodecError):
        codec.decode_effect_facts(Mapping(good))
    for field in FIELDS:
        with pytest.raises(CodecError):
            codec.decode_effect_facts({k: v for k, v in good.items() if k != field})
    with pytest.raises(CodecError):
        codec.decode_effect_facts({**good, "extra": []})
    for tag in ({"version": True}, {"version": 2}, {"type": "static_map"}, {"type": "other"}):
        with pytest.raises(CodecError):
            codec.decode_effect_facts({**good, **tag})
    wrong_key = dict(good)
    del wrong_key["script_obstacles"]
    wrong_key[0] = []
    with pytest.raises(CodecError):
        codec.decode_effect_facts(wrong_key)


def test_encoder_refuses_non_facts_mutable_and_wrong_top_levels() -> None:
    with pytest.raises(CodecError):
        codec.encode_effect_facts(object())
    with pytest.raises(CodecError):
        codec.StaticEffectFacts([], (), ())  # type: ignore[arg-type]
    with pytest.raises(CodecError):
        codec.encode_effect_facts(
            replace(facts(), script_obstacles=[((3, 4), ())])  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "update",
    [
        {"script_obstacles": (((3, 4), ((tile_obstacle().kind, tile_obstacle().source,
          None, None, 1, 2, None, None, True, False, None, (), False, False),)),)},
        {"script_obstacles": (((3, 4), (object(),)),)},
        {"script_obstacles": (((3, 4), (replace(tile_obstacle(), x=True),)),)},
        {"script_obstacles": (((3, 4), (replace(tile_obstacle(), local_id=True),)),)},
        {"script_obstacles": (((3, 4), (replace(tile_obstacle(), kind=replace(
            tile_obstacle().kind, requirement_kind="guessed")),)),)},
        {"script_obstacles": (((3, 4), (replace(tile_obstacle(), source="npc"),)),)},
        {"script_obstacles": (((3, 4), (replace(
            tile_obstacle(), equivalent_requirements=("a", 1)),)),)},
        {"script_obstacles": (((3, 4), (replace(
            tile_obstacle(), equivalent_requirements=["a"]),)),)},
        {"on_load_metatiles": (((3, 4), (((0x120, True), effects()[0]),)),)},
        {"on_load_metatiles": (((3, 4), (((0x120,), [effects()[0]]),)),)},
    ],
)
def test_encoder_refuses_bool_int_list_and_guessed_enums(update: dict) -> None:
    with pytest.raises(CodecError):
        codec.encode_effect_facts(replace(facts(), **update))


@pytest.mark.parametrize(
    "field, value",
    [
        ("script_obstacles", [[[3, 4], [[[1], "object_event", None, None, 1, 2,
                                          None, None, True, False, None, [], False, False]]]]),
        ("script_obstacles", [[[3, 4], [[["role", "n", "p", "v", "k", "guessed",
                                          "c", "script_flag", "l", "p", True],
                                         "object_event", None, None, 1, 2, None,
                                         None, True, False, None, [], False, False]]]]),
        ("script_obstacles", [[[3, 4], [[["role", "n", "p", "v", "k", "field_move",
                                          "c", "script_flag", "l", "p", 1],
                                         "object_event", None, None, 1, 2, None,
                                         None, True, False, None, [], False, False]]]]),
        ("script_obstacles", [[[3, 4], [[["role", "n", "p", "v", "k", "field_move",
                                          "c", "script_flag", "l", "p", True],
                                         "object_event", True, None, 1, 2, None,
                                         None, True, False, None, [], False, False]]]]),
        ("on_load_warp_openings", [[[[3, 4], 1], [[]]]]),
        ("on_load_metatiles", [[[3, 4], [[[True], "effect"]]]]),
        ("on_load_metatiles", [[[3, 4], [[[1], {"bad": "effect"}]]]]),
    ],
)
def test_decoder_refuses_malformed_nested_types(field: str, value: list) -> None:
    with pytest.raises(CodecError):
        codec.decode_effect_facts({**wire(), field: value})


def test_bounds_cycles_escapes_and_surrogates_refuse() -> None:
    too_many = (((3, 4), ()),) * (MAX_CONTAINER_LENGTH + 1)
    with pytest.raises(CodecError):
        codec.encode_effect_facts(replace(facts(), script_obstacles=too_many))
    with pytest.raises(CodecError):
        codec.decode_effect_facts(
            {**wire(), "script_obstacles": [[[3, 4], []]] * (MAX_CONTAINER_LENGTH + 1)}
        )
    long_string = replace(
        facts(),
        on_load_metatiles=(((3, 4), (((1,), Effect(
            "setmetatile", 1, (), (("m", "x" * (MAX_STRING_BYTES + 1)),))),)),),
    )
    with pytest.raises(CodecError):
        codec.encode_effect_facts(long_string)
    with pytest.raises(CodecError):
        codec.encode_effect_facts(
            replace(facts(), script_obstacles=(((3, 4), (replace(
                tile_obstacle(), equivalent_requirements=("\ud800",)),)),))
        )
    cycle = wire()
    cycle["script_obstacles"].append(cycle)
    with pytest.raises(CodecError):
        codec.decode_effect_facts(cycle)


def test_decoded_instances_are_fresh_and_rows_not_shared() -> None:
    transported = wire()
    first = codec.decode_effect_facts(transported)
    second = codec.decode_effect_facts(transported)
    assert first is not second
    assert first.script_obstacles is not second.script_obstacles
    assert first.script_obstacles[0][1][0] is not second.script_obstacles[0][1][0]
    assert first.script_obstacles[0][1][0].kind is not second.script_obstacles[0][1][0].kind
    assert (first.on_load_warp_openings[0][1][0]
            is not second.on_load_warp_openings[0][1][0])


def test_codec_import_is_graph_free_in_a_fresh_interpreter() -> None:
    repo = Path(__file__).resolve().parents[1]
    code = """
import sys
class Tripwire:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "pokegent.navigation" or fullname.startswith("pokegent.navigation."):
            raise AssertionError(f"codec imported {fullname}")
        return None
sys.meta_path.insert(0, Tripwire())
from pokegent.games.gen3.firered.rom import static_effect_index_codec as codec
assert "pokegent.navigation" not in sys.modules
empty = codec.StaticEffectFacts((), (), ())
assert codec.decode_effect_facts(codec.encode_effect_facts(empty)) == empty
print("graph-free")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(repo)},
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "graph-free"
