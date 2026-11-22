"""Focused synthetic tests for the unused primitive static-index codec."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from pokegent.games.gen3.firered.rom import static_index_codec as codec
from pokegent.games.gen3.firered.rom.script_effects import Effect, Guard
from pokegent.games.gen3.firered.rom.static_symbolic_codec import (
    MAX_CONTAINER_LENGTH,
    MAX_STRING_BYTES,
    CodecError,
)

FIELDS = (
    "flag_items",
    "on_load_warp_flags",
    "on_load_barriers",
    "on_load_barrier_labels",
    "script_warp_gates",
)


def facts() -> codec.StaticIndexFacts:
    return codec.StaticIndexFacts(
        flag_items=((0x120, 13), (0x121, 13), (0x120, 14)),
        on_load_warp_flags=(((((3, 4), 1), 0x120), (((3, 4), 2), -7))),
        on_load_barriers=(
            ((3, 4), (((1, 2), ("flag:288", "var:16386:1")), ((5, 6), ()))),
            ((3, 4), ()),
        ),
        on_load_barrier_labels=(
            (((3, 4), (1, 2)), "Trainer Timmy"),
            (((3, 4), (5, 6)), ""),
        ),
        script_warp_gates=(
            ((((3, 4), 1), ("flag:288", 7))),
            (((3, 4), 2), ("item:13", -1)),
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
    return json.loads(json.dumps(codec.encode_index_facts(facts())))


def test_exact_roundtrip_preserves_all_five_families_and_key_boundaries() -> None:
    original = facts()
    encoded = codec.encode_index_facts(original)
    assert type(encoded) is dict and set(encoded) == {"version", "type", *FIELDS}
    encoded = json.loads(json.dumps(encoded))
    decoded = codec.decode_index_facts(encoded)
    assert type(decoded) is codec.StaticIndexFacts
    for field in FIELDS:
        assert exact(getattr(decoded, field), getattr(original, field)), field
    assert decoded.flag_items == original.flag_items
    assert decoded.on_load_barriers[0][1][0] == (((1, 2), ("flag:288", "var:16386:1")))
    assert type(decoded.on_load_barriers[0][0]) is tuple
    assert type(decoded.on_load_barriers[0][1][0][1]) is tuple
    assert type(decoded.on_load_warp_flags[0][0]) is tuple
    assert type(decoded.on_load_warp_flags[0][0][0]) is tuple
    assert type(decoded.on_load_barrier_labels[0][0]) is tuple
    assert type(decoded.on_load_barrier_labels[0][0][1]) is tuple
    assert type(decoded.script_warp_gates[0][0]) is tuple
    assert type(decoded.script_warp_gates[0][0][0]) is tuple
    assert type(decoded.script_warp_gates[0][1]) is tuple
    assert decoded.on_load_barriers[1] == ((3, 4), ())
    assert decoded.script_warp_gates[1][1] == ("item:13", -1)


def test_empty_containers_are_preserved_as_empty_arrays() -> None:
    empty = codec.StaticIndexFacts((), (), (), (), ())
    encoded = codec.encode_index_facts(empty)
    for field in FIELDS:
        assert encoded[field] == []
    assert codec.decode_index_facts(json.loads(json.dumps(encoded))) == empty


def test_framing_refuses_missing_extra_subclass_and_wrong_tags() -> None:
    good = wire()

    class Mapping(dict):
        pass

    with pytest.raises(CodecError):
        codec.decode_index_facts(Mapping(good))
    with pytest.raises(CodecError):
        codec.decode_index_facts({key: value for key, value in good.items() if key != "flag_items"})
    with pytest.raises(CodecError):
        codec.decode_index_facts({**good, "extra": []})
    for tag in ({"version": True}, {"version": 2}, {"type": "static_map"}, {"type": "other"}):
        with pytest.raises(CodecError):
            codec.decode_index_facts({**good, **tag})
    wrong_key = dict(good)
    del wrong_key["flag_items"]
    wrong_key[0] = []
    with pytest.raises(CodecError):
        codec.decode_index_facts(wrong_key)


def test_encoder_refuses_non_facts_and_mutable_top_level() -> None:
    with pytest.raises(CodecError):
        codec.encode_index_facts(object())
    with pytest.raises(CodecError):
        codec.StaticIndexFacts(  # type: ignore[arg-type]
            flag_items=[], on_load_warp_flags=(), on_load_barriers=(),
            on_load_barrier_labels=(), script_warp_gates=(),
        )  # fmt: skip


@pytest.mark.parametrize(
    "update",
    [
        {"flag_items": ((True, 1),)},
        {"flag_items": ((0x120, 1.0),)},
        {"on_load_barriers": (((3, 4), (((1, 2), ["flag:1"]),)),)},
        {"on_load_barriers": (((3, 4), (([1, 2], ("flag:1",)),)),)},
        {"on_load_barrier_labels": ((((3, 4), (1, 2)), 7),)},
        {"script_warp_gates": ((((((3, 4), 1), ("flag:1", 7, 8)),)) )},
    ],
)
def test_encoder_refuses_bool_float_list_and_nested_wrong_types(update: dict) -> None:
    with pytest.raises(CodecError):
        codec.encode_index_facts(replace(facts(), **update))


@pytest.mark.parametrize(
    "field, value",
    [
        ("flag_items", [[True, 1]]),
        ("flag_items", [{"flag": 1}]),
        ("on_load_warp_flags", [[[3, 4], True, 5]]),
        ("on_load_barriers", [[[3, 4], [[{"x": 1}, ["flag:1"]]]]]),
        ("on_load_barrier_labels", [[[3, 4], [1, 2], 7]]),
        ("script_warp_gates", [[[3, 4], 1, ["flag:1"]]]),
    ],
)
def test_decoder_refuses_bool_int_nested_wrong_types(field: str, value: list) -> None:
    with pytest.raises(CodecError):
        codec.decode_index_facts({**wire(), field: value})


def test_bounds_escapes_cycles_and_surrogates_refuse() -> None:
    too_many = ((0, 0),) * (MAX_CONTAINER_LENGTH + 1)
    with pytest.raises(CodecError):
        codec.encode_index_facts(replace(facts(), flag_items=too_many))
    with pytest.raises(CodecError):
        codec.decode_index_facts({**wire(), "flag_items": [[0, 0]] * (MAX_CONTAINER_LENGTH + 1)})
    cycle = wire()
    cycle["flag_items"].append(cycle)
    with pytest.raises(CodecError):
        codec.decode_index_facts(cycle)
    long_string = replace(
        facts(),
        on_load_barrier_labels=((((3, 4), (1, 2)), "x" * (MAX_STRING_BYTES + 1)),),
    )
    with pytest.raises(CodecError):
        codec.encode_index_facts(long_string)
    with pytest.raises(CodecError):
        codec.encode_index_facts(
            replace(facts(), on_load_barrier_labels=((((3, 4), (1, 2)), "\ud800"),))
        )


def test_string_escaping_roundtrips_exactly() -> None:
    label = 'quote" back\\slash\nnul\x00é'
    original = replace(
        facts(), on_load_barrier_labels=((((3, 4), (1, 2)), label),)
    )
    decoded = codec.decode_index_facts(json.loads(json.dumps(codec.encode_index_facts(original))))
    assert decoded.on_load_barrier_labels == ((((3, 4), (1, 2)), label),)


def test_decoded_instances_are_fresh_and_rows_not_shared() -> None:
    transported = json.loads(json.dumps(codec.encode_index_facts(facts())))
    first = codec.decode_index_facts(transported)
    second = codec.decode_index_facts(transported)
    assert first is not second
    assert first.flag_items is not second.flag_items
    assert first.on_load_barriers[0][1] is not second.on_load_barriers[0][1]
    assert first.on_load_barriers[0][1][0] is not second.on_load_barriers[0][1][0]


def test_graph_flag_items_producer_seam_projects_into_codec_facts() -> None:
    from pokegent.navigation.graph import NavigationGraph

    graph = object.__new__(NavigationGraph)
    first_event = SimpleNamespace(script_ptr=1, x=0, y=0, hidden_flag_id=0x121)
    second_event = SimpleNamespace(script_ptr=2, x=1, y=1)
    graph.maps = {
        (0, 0): SimpleNamespace(
            objects=(first_event, second_event), coord_events=(), bg_events=()
        )
    }
    effects = {
        1: (
            Effect("additem", 1, (), (("item", 13),)),
            Effect("setflag", 2, (), (("flag", 0x120), ("set", True))),
        ),
        2: (
            Effect("additem", 3, (), (("item", 14),)),
            Effect(
                "setflag",
                4,
                (Guard("flag", 0x122, "eq", True),),
                (("flag", 0x122), ("set", True)),
            ),
        ),
    }
    graph._script_effects = lambda script_ptr, *args, **kwargs: effects[script_ptr]
    produced = graph._effect_flag_items()
    assert produced == {0x121: 13, 0x120: 13, 0x122: 14}
    original = replace(facts(), flag_items=tuple(produced.items()))
    decoded = codec.decode_index_facts(json.loads(json.dumps(codec.encode_index_facts(original))))
    assert decoded.flag_items == original.flag_items


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
from pokegent.games.gen3.firered.rom import static_index_codec as codec
assert "pokegent.navigation" not in sys.modules
empty = codec.StaticIndexFacts((), (), (), (), ())
assert codec.decode_index_facts(codec.encode_index_facts(empty)) == empty
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
