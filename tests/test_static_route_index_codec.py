"""Focused synthetic tests for the unused route static-index codec."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import fields, replace
from pathlib import Path

import pytest

from pokegent.games.gen3.firered.rom import static_route_index_codec as codec
from pokegent.games.gen3.firered.rom.static_symbolic_codec import (
    MAX_CONTAINER_LENGTH,
    MAX_STRING_BYTES,
    CodecError,
)

FIELDS = (
    "gates",
    "gate_requirements",
    "script_toggles",
    "trainers_by_flag",
    "script_door_gates",
    "on_frame_landings",
    "seagallop_warps",
)


def facts() -> codec.StaticRouteFacts:
    return codec.StaticRouteFacts(
        gates=(
            ((3, 4), ((1, 2, 0x120, 7), (1, 2, 0x120, 7), (5, 6, -3, 0))),
            ((3, 4), ()),
            ((-1, 9), ((-2, 0, 0x121, -4),)),
        ),
        gate_requirements=(
            (((3, 4), 7), "flag:288"),
            (((3, 4), 7), "dup:same-key"),
            (((-1, 9), -4), ""),
        ),
        script_toggles=(
            (
                (3, 4),
                (
                    (5, 6, 0x120, "up", 7, 2),
                    (5, 6, 0x120, None, 7, None),
                    (-1, -2, 0x121, "left", 0, 0),
                ),
            ),
            ((3, 4), ()),
        ),
        trainers_by_flag=(
            (0x500, ((0x500, (3, 4), 2, 5, 6), (0x500, (3, 4), 2, 5, 6))),
            (0x501, ((0x501, (-1, 9), 0, -2, 0),)),
        ),
        script_door_gates=(
            (((3, 4), 1), ("flag:288", "CARD KEY (flag 288)", ("item:13",))),
            (((3, 4), 2), ("flag:289", "", ())),
            (((-1, 9), -4), ("x&y", 'quote" back\\slash\nnul\x00é', ("a", "b"))),
        ),
        on_frame_landings=(
            (
                (3, 4),
                (
                    (0x403A, -7, ((0, -1), (1, 0), (0, 1))),
                    (0x403A, 0, ()),
                ),
            ),
            ((3, 4), ()),
        ),
        seagallop_warps=(
            (0, (3, 4, -1, 5, 6)),
            (1, (-1, 9, -1, -2, 0)),
            (7, (3, 4, -1, 0, 0)),
        ),
    )


def exact(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, tuple):
        return len(left) == len(right) and all(
            exact(a, b) for a, b in zip(left, right)  # type: ignore[arg-type]
        )
    return left == right


def wire() -> dict:
    return json.loads(json.dumps(codec.encode_route_facts(facts())))


def test_exact_roundtrip_preserves_all_seven_families() -> None:
    original = facts()
    encoded = codec.encode_route_facts(original)
    assert type(encoded) is dict and set(encoded) == {"version", "type", *FIELDS}
    decoded = codec.decode_route_facts(json.loads(json.dumps(encoded)))
    assert type(decoded) is codec.StaticRouteFacts
    for field in FIELDS:
        assert exact(getattr(decoded, field), getattr(original, field)), field
        assert type(getattr(decoded, field)) is tuple
    assert decoded.gates[0][1][0] == (1, 2, 0x120, 7)
    assert decoded.gates[0][1][0] is not decoded.gates[0][1][1]
    assert decoded.gates[2][0] == (-1, 9)
    assert decoded.gates[2][1][0] == (-2, 0, 0x121, -4)
    assert decoded.gate_requirements[1][0] == ((3, 4), 7)
    assert decoded.script_toggles[0][1][1][3] is None
    assert decoded.script_toggles[0][1][1][5] is None
    assert decoded.trainers_by_flag[0][1][0][1] == (3, 4)
    assert decoded.script_door_gates[1][1][2] == ()
    assert decoded.on_frame_landings[0][1][0][2] == ((0, -1), (1, 0), (0, 1))
    assert decoded.on_frame_landings[0][1][1][2] == ()
    assert decoded.seagallop_warps[0][1][2] == -1
    assert decoded.seagallop_warps[1] == (1, (-1, 9, -1, -2, 0))


def test_empty_and_duplicate_containers_are_preserved() -> None:
    empty = codec.StaticRouteFacts((), (), (), (), (), (), ())
    encoded = codec.encode_route_facts(empty)
    for field in FIELDS:
        assert encoded[field] == []
    assert codec.decode_route_facts(json.loads(json.dumps(encoded))) == empty
    duplicate = replace(
        facts(), seagallop_warps=((0, (1, 2, -1, 3, 4)), (0, (1, 2, -1, 3, 4))))
    decoded = codec.decode_route_facts(
        json.loads(json.dumps(codec.encode_route_facts(duplicate)))
    )
    assert decoded.seagallop_warps == duplicate.seagallop_warps


def test_framing_refuses_missing_extra_subclass_and_wrong_tags() -> None:
    good = wire()

    class Mapping(dict):
        pass

    with pytest.raises(CodecError):
        codec.decode_route_facts(Mapping(good))
    for field in FIELDS:
        with pytest.raises(CodecError):
            codec.decode_route_facts({k: v for k, v in good.items() if k != field})
    with pytest.raises(CodecError):
        codec.decode_route_facts({**good, "extra": []})
    for tag in ({"version": True}, {"version": 2}, {"type": "static_map"}, {"type": "other"}):
        with pytest.raises(CodecError):
            codec.decode_route_facts({**good, **tag})
    wrong_key = dict(good)
    del wrong_key["gates"]
    wrong_key[0] = []
    with pytest.raises(CodecError):
        codec.decode_route_facts(wrong_key)


def test_encoder_refuses_non_facts_mutable_and_wrong_top_levels() -> None:
    with pytest.raises(CodecError):
        codec.encode_route_facts(object())
    with pytest.raises(CodecError):
        codec.StaticRouteFacts(  # type: ignore[arg-type]
            gates=[], gate_requirements=(), script_toggles=(),
            trainers_by_flag=(), script_door_gates=(), on_frame_landings=(),
            seagallop_warps=(),
        )  # fmt: skip


@pytest.mark.parametrize(
    "update",
    [
        {"gates": (((3, 4), ((True, 2, 3, 4),)),)},
        {"gates": (((3, 4), ((1, 2, 3.0, 4),)),)},
        {"gates": (((3, 4), ((1, 2, 3),)),)},
        {"gate_requirements": ((((3, 4), 7), 9),)},
        {"script_toggles": (((3, 4), ((5, 6, 7, 8, 9, 10),)),)},
        {"script_toggles": (((3, 4), ((5, 6, 7, "up", 9, "x"),)),)},
        {"trainers_by_flag": ((0x500, ((0x500, [3, 4], 2, 5, 6),)),)},
        {"script_door_gates": ((((3, 4), 1), ("k", "l", ["item:1"])),)},
        {"on_frame_landings": (((3, 4), ((1, 2, ((0, True),)))),)},
        {"seagallop_warps": ((0, (3, 4, -1, 5)),)},
        {"seagallop_warps": ((0, [3, 4, -1, 5, 6]),)},
    ],
)
def test_encoder_refuses_bool_float_list_wrong_nested_types(update: dict) -> None:
    with pytest.raises(CodecError):
        codec.encode_route_facts(replace(facts(), **update))


@pytest.mark.parametrize(
    "field, value",
    [
        ("gates", [[[3, 4], [[True, 2, 3, 4]]]]),
        ("gates", [[[3, 4], [[1, 2, 3]]]]),
        ("gate_requirements", [[[3, 4], 7, 9]]),
        ("script_toggles", [[[3, 4], [[5, 6, 7, 8, 9, 10]]]]),
        ("trainers_by_flag", [[0x500, [[0x500, [3, 4], "2", 5, 6]]]]),
        ("script_door_gates", [[[3, 4], 1, ["k", "l"]]]),
        ("on_frame_landings", [[[3, 4], [[1, 2, [["dx", 0]]]]]]),
        ("seagallop_warps", [[0, [3, 4, -1, True, 6]]]),
        ("seagallop_warps", [[0, [3, 4, -1, 5, 6, 7]]]),
    ],
)
def test_decoder_refuses_bool_int_wrong_nested_types(field: str, value: list) -> None:
    with pytest.raises(CodecError):
        codec.decode_route_facts({**wire(), field: value})


def test_bounds_escapes_cycles_and_surrogates_refuse() -> None:
    too_many = (((3, 4), ()),) * (MAX_CONTAINER_LENGTH + 1)
    with pytest.raises(CodecError):
        codec.encode_route_facts(replace(facts(), gates=too_many))
    with pytest.raises(CodecError):
        codec.decode_route_facts({**wire(), "gates": [[[3, 4], []]] * (MAX_CONTAINER_LENGTH + 1)})
    long_string = replace(
        facts(),
        gate_requirements=(((((3, 4), 7)), "x" * (MAX_STRING_BYTES + 1)),),
    )
    with pytest.raises(CodecError):
        codec.encode_route_facts(long_string)
    with pytest.raises(CodecError):
        codec.encode_route_facts(
            replace(facts(), script_door_gates=((((3, 4), 1), ("\ud800", "x", ())),))
        )
    cycle = wire()
    cycle["gates"].append(cycle)
    with pytest.raises(CodecError):
        codec.decode_route_facts(cycle)


def test_decoded_instances_are_fresh_and_rows_not_shared() -> None:
    transported = wire()
    first = codec.decode_route_facts(transported)
    second = codec.decode_route_facts(transported)
    assert first is not second
    assert first.gates is not second.gates
    assert first.gates[0][1] is not second.gates[0][1]
    assert first.gates[0][1][0] is not second.gates[0][1][0]
    assert first.on_frame_landings[0][1][0][2] is not second.on_frame_landings[0][1][0][2]


def test_actual_toggle_and_trainer_projections_roundtrip_with_field_seam() -> None:
    from pokegent.navigation.graph import CountedTrainer, ScriptToggle

    toggle_names = ("x", "y", "flag", "facing", "event_index", "local_id")
    trainer_names = ("flag", "map_key", "local_id", "x", "y")
    assert tuple(f.name for f in fields(ScriptToggle)) == toggle_names
    assert tuple(f.name for f in fields(CountedTrainer)) == trainer_names

    toggle_values = (
        ScriptToggle(5, 6, 0x120, "up", 7, 2),
        ScriptToggle(-1, -2, 0x121, None, 0, None),
    )
    trainer_values = (
        CountedTrainer(0x500, (3, 4), 2, 5, 6),
        CountedTrainer(0x500, (-1, 9), 0, -2, 0),
    )
    toggle_projection = tuple(
        tuple(getattr(row, name) for name in toggle_names) for row in toggle_values
    )
    trainer_projection = tuple(
        tuple(getattr(row, name) for name in trainer_names) for row in trainer_values
    )
    pairs = ((toggle_projection, toggle_names), (trainer_projection, trainer_names))
    for projection, names in pairs:
        for row in projection:
            assert len(row) == len(names)

    original = replace(
        facts(),
        script_toggles=(((3, 4), toggle_projection),),
        trainers_by_flag=((0x500, trainer_projection),),
    )
    decoded = codec.decode_route_facts(
        json.loads(json.dumps(codec.encode_route_facts(original)))
    )
    assert decoded.script_toggles == (((3, 4), toggle_projection),)
    assert decoded.trainers_by_flag == ((0x500, trainer_projection),)
    assert exact(decoded.script_toggles[0][1], toggle_projection)
    assert exact(decoded.trainers_by_flag[0][1], trainer_projection)


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
from pokegent.games.gen3.firered.rom import static_route_index_codec as codec
assert "pokegent.navigation" not in sys.modules
empty = codec.StaticRouteFacts((), (), (), (), (), (), ())
assert codec.decode_route_facts(codec.encode_route_facts(empty)) == empty
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
