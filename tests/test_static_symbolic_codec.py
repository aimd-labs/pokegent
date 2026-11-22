"""Focused tests for the pure bounded symbolic Guard/Effect/Interpretation codec."""

from __future__ import annotations

import copy
import json
from dataclasses import fields, is_dataclass
from enum import IntEnum

import pytest

from pokegent.games.gen3.firered.rom.script_effects import Effect, Guard, Interpretation
from pokegent.games.gen3.firered.rom.static_symbolic_codec import (
    MAX_CONTAINER_LENGTH,
    MAX_CUMULATIVE_TEXT_BYTES,
    MAX_DEPTH,
    MAX_INT_BITS,
    MAX_NODES,
    MAX_STRING_BYTES,
    CodecError,
    decode_effect,
    decode_guard,
    decode_interpretation,
    encode_effect,
    encode_guard,
    encode_interpretation,
)


def assert_exact(actual, expected):
    """Recursively compare values and exact Python types (True != 1, tuple != list)."""
    assert type(actual) is type(expected), f"{type(actual)!r} != {type(expected)!r}"
    if is_dataclass(expected):
        for field in fields(expected):
            assert_exact(getattr(actual, field.name), getattr(expected, field.name))
    elif type(expected) in (tuple, list):
        assert len(actual) == len(expected)
        for actual_item, expected_item in zip(actual, expected):
            assert_exact(actual_item, expected_item)
    else:
        assert actual == expected, f"{actual!r} != {expected!r}"


def make_guard(**overrides):
    values = {"kind": "flag", "subject": 1, "operator": "==", "value": True, "origin": None}
    values.update(overrides)
    return Guard(**values)


def make_effect(**overrides):
    values = {
        "kind": "set",
        "source": 2,
        "guards": (),
        "values": (),
        "decoded": True,
        "reason": None,
    }
    values.update(overrides)
    return Effect(**values)


def make_interpretation(**overrides):
    values = {"script_ptr": 0, "effects": (), "unknown": (), "commands": 0}
    values.update(overrides)
    return Interpretation(**values)


def nested_lists(levels, leaf=0):
    value = leaf
    for _ in range(levels):
        value = [value]
    return value


def node_budget_interpretation(extra=0):
    # Root/records/fields/containers/pair/name/outer list = 12 nodes, then
    # 29 sublists (first one shorter) + 99956 ints -> exactly 100000.
    sublists = [[0] * 3447 for _ in range(29)]
    sublists[0] = sublists[0][:-7]
    sublists[0].extend([0] * extra)
    value = make_interpretation(
        effects=(make_effect(values=(("v", sublists),)),),
    )
    return value


def test_codec_error_is_value_error():
    assert issubclass(CodecError, ValueError)


def test_owning_dataclass_field_parity():
    assert [field.name for field in fields(Guard)] == [
        "kind",
        "subject",
        "operator",
        "value",
        "origin",
    ]
    assert [field.name for field in fields(Effect)] == [
        "kind",
        "source",
        "guards",
        "values",
        "decoded",
        "reason",
    ]
    assert [field.name for field in fields(Interpretation)] == [
        "script_ptr",
        "effects",
        "unknown",
        "commands",
    ]


def test_guard_round_trip_exact_types_and_four_entry_origin():
    guard = make_guard(kind="var", subject=12, operator=">=", value=False, origin=(1, -2, "x", 3))
    decoded = decode_guard(encode_guard(guard))
    assert_exact(decoded, guard)
    assert type(decoded.value) is bool


def test_guard_round_trip_five_entry_origin_and_int_value():
    guard = make_guard(kind="item", subject=0, operator="==", value=-7, origin=(0, 1, "s", -2, 9))
    decoded = decode_guard(encode_guard(guard))
    assert_exact(decoded, guard)
    assert type(decoded.value) is int


def test_guard_bool_and_int_values_stay_distinct():
    for raw_value in (True, 1, False, 0, -1):
        decoded = decode_guard(encode_guard(make_guard(value=raw_value)))
        assert_exact(decoded.value, raw_value)


def test_effect_round_trip_nested_values_duplicates_and_negative_sentinel():
    effect = make_effect(
        kind="give",
        source=99,
        guards=(
            make_guard(),
            make_guard(kind="item", subject=2, operator="==", value=0),
            make_guard(),
        ),
        values=(
            ("same", (1, [True, False, None], (2, 3))),
            ("same", -2_147_483_648),
        ),
        decoded=False,
        reason="native unknown",
    )
    decoded = decode_effect(encode_effect(effect))
    assert_exact(decoded, effect)
    assert [item[0] for item in decoded.values] == ["same", "same"]


def test_interpretation_complete_round_trip():
    interpretation = make_interpretation(
        script_ptr=0x1234,
        effects=(make_effect(decoded=True), make_effect(decoded=True)),
        unknown=(),
        commands=7,
    )
    decoded = decode_interpretation(encode_interpretation(interpretation))
    assert_exact(decoded, interpretation)
    assert decoded.complete is True


def test_interpretation_incomplete_paths_stay_independent():
    unknown_only = make_interpretation(unknown=("a", "a", "b"))
    unknown_decoded = decode_interpretation(encode_interpretation(unknown_only))
    assert_exact(unknown_decoded, unknown_only)
    assert unknown_decoded.complete is False

    decoded_false_only = make_interpretation(effects=(make_effect(decoded=False),))
    false_decoded = decode_interpretation(encode_interpretation(decoded_false_only))
    assert_exact(false_decoded, decoded_false_only)
    assert false_decoded.complete is False

    both = make_interpretation(
        effects=(make_effect(decoded=False, reason="why"), make_effect(decoded=False)),
        unknown=("x", "y", "x"),
    )
    assert_exact(decode_interpretation(encode_interpretation(both)), both)


def test_interpretation_duplicate_effect_order_is_preserved():
    first = make_effect(kind="a", source=1)
    second = make_effect(kind="b", source=2)
    interpretation = make_interpretation(effects=(first, second, first))
    decoded = decode_interpretation(encode_interpretation(interpretation))
    assert_exact(decoded, interpretation)


def test_complete_field_is_rejected_not_trusted():
    raw = encode_interpretation(make_interpretation())
    raw["complete"] = True
    with pytest.raises(CodecError):
        decode_interpretation(raw)


def test_wire_output_is_json_compatible():
    raw = encode_interpretation(
        make_interpretation(
            effects=(make_effect(guards=(make_guard(origin=(1, 2, "z", 3)),)),),
            unknown=("u",),
            commands=3,
        )
    )
    assert json.loads(json.dumps(raw, allow_nan=False)) == raw


@pytest.mark.parametrize("bad_value", [1.5, b"bytes", {"a": 1}, {1, 2}, object()])
def test_unsupported_encode_values_are_rejected(bad_value):
    with pytest.raises(CodecError):
        encode_effect(make_effect(values=(("n", bad_value),)))


@pytest.mark.parametrize(
    "bad_value",
    [
        {"t": "wat", "v": None},
        {"t": "none", "v": None, "x": 1},
        {"t": "none"},
        {"t": "tuple", "v": ()},
        {"t": "bool", "v": 0},
        {"t": "int", "v": True},
        {"t": "str", "v": b"x"},
        {"t": None, "v": None},
        [1, 2],
    ],
)
def test_decode_tagged_value_controls(bad_value):
    raw = encode_guard(make_guard())
    raw["value"] = bad_value
    with pytest.raises(CodecError):
        decode_guard(raw)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw.update(version=2),
        lambda raw: raw.update(version=True),
        lambda raw: raw.update(type="effect"),
        lambda raw: raw.pop("origin"),
        lambda raw: raw.update(complete=True),
    ],
)
def test_guard_framing_controls(mutate):
    raw = encode_guard(make_guard())
    mutate(raw)
    with pytest.raises(CodecError):
        decode_guard(raw)


@pytest.mark.parametrize(
    "bad_guard",
    [
        make_guard(kind="not-a-kind"),
        make_guard(subject=-1),
        make_guard(subject=True),
        make_guard(value="1"),
        make_guard(origin=[1, 2, "x", 3]),
        make_guard(origin=(1, 2, "x")),
        make_guard(origin=(1, 2, "x", 3, 4, 5)),
        make_guard(origin=(True, 2, "x", 3)),
        make_guard(origin=(1, 2, 3, 4)),
        make_guard(origin=(1, 2, "x", True)),
    ],
)
def test_malformed_guard_fields_refuse(bad_guard):
    with pytest.raises(CodecError):
        encode_guard(bad_guard)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("kind", "not-a-kind"),
        ("subject", -1),
        ("subject", True),
        ("value", {"t": "str", "v": "1"}),
        ("origin", {"t": "list", "v": []}),
        ("origin", {"t": "tuple", "v": [{"t": "int", "v": 1}]}),
    ],
)
def test_malformed_guard_decode_fields_refuse(field, bad_value):
    raw = encode_guard(make_guard())
    raw[field] = bad_value
    with pytest.raises(CodecError):
        decode_guard(raw)


@pytest.mark.parametrize(
    "bad_effect",
    [
        make_effect(guards=[]),
        make_effect(values=[]),
        make_effect(values=([],)),
        make_effect(values=(("a", 1, 2),)),
        make_effect(values=((1, 2),)),
        make_effect(decoded=1),
        make_effect(reason=5),
    ],
)
def test_malformed_effect_fields_refuse(bad_effect):
    with pytest.raises(CodecError):
        encode_effect(bad_effect)


def test_malformed_effect_decode_fields_refuse():
    good = encode_effect(make_effect(values=(("a", 1),)))
    controls = [
        ("guards", [1]),
        ("guards", None),
        ("values", {"t": "list", "v": []}),
        ("values", [{"t": "tuple", "v": [{"t": "int", "v": 1}]}]),
        ("values", [{"t": "tuple", "v": [{"t": "str", "v": "a"}]}]),
        ("decoded", 1),
        ("reason", 5),
        ("source", True),
        ("source", -1),
    ]
    for field, bad_value in controls:
        raw = copy.deepcopy(good)
        raw[field] = bad_value
        with pytest.raises(CodecError):
            decode_effect(raw)


@pytest.mark.parametrize(
    "bad_interpretation",
    [
        make_interpretation(effects=[]),
        make_interpretation(unknown=[]),
        make_interpretation(unknown=("a", 1)),
        make_interpretation(commands=-1),
        make_interpretation(commands=True),
        make_interpretation(script_ptr=-1),
        make_interpretation(script_ptr=True),
        make_interpretation(effects=(make_effect(decoded=False), 1)),
    ],
)
def test_malformed_interpretation_fields_refuse(bad_interpretation):
    with pytest.raises(CodecError):
        encode_interpretation(bad_interpretation)


class Colour(IntEnum):
    RED = 1


class StringSubclass(str):
    pass


class ListSubclass(list):
    pass


class TupleSubclass(tuple):
    pass


class GuardSubclass(Guard):
    pass


class EffectSubclass(Effect):
    pass


class InterpretationSubclass(Interpretation):
    pass


@pytest.mark.parametrize(
    "bad_value",
    [Colour.RED, StringSubclass("x"), ListSubclass([1]), TupleSubclass((1,))],
)
def test_scalar_and_container_subclasses_refuse(bad_value):
    with pytest.raises(CodecError):
        encode_effect(make_effect(values=(("n", bad_value),)))


@pytest.mark.parametrize(
    "bad_guard",
    [
        make_guard(kind=StringSubclass("flag")),
        make_guard(subject=Colour.RED),
        make_guard(operator=StringSubclass("==")),
        make_guard(origin=(1, 2, StringSubclass("x"), 3)),
    ],
)
def test_guard_subclass_fields_refuse(bad_guard):
    with pytest.raises(CodecError):
        encode_guard(bad_guard)


def test_record_subclasses_refuse():
    guard = GuardSubclass(
        **{field.name: getattr(make_guard(), field.name) for field in fields(Guard)}
    )
    effect = EffectSubclass(
        **{field.name: getattr(make_effect(), field.name) for field in fields(Effect)}
    )
    interpretation = InterpretationSubclass(
        **{
            field.name: getattr(make_interpretation(), field.name)
            for field in fields(Interpretation)
        }
    )
    with pytest.raises(CodecError):
        encode_guard(guard)
    with pytest.raises(CodecError):
        encode_effect(effect)
    with pytest.raises(CodecError):
        encode_interpretation(interpretation)


def test_record_mapping_keys_must_be_exact_str():
    raw = encode_guard(make_guard())
    raw = {
        (StringSubclass("version") if key == "version" else key): value
        for key, value in raw.items()
    }
    with pytest.raises(CodecError):
        decode_guard(raw)


@pytest.mark.parametrize("subclass_key", ["t", "v"])
def test_tagged_mapping_keys_must_be_exact_str(subclass_key):
    raw = encode_guard(make_guard())
    wrapper = {"t": "int", "v": 1}
    wrapper = {
        (StringSubclass(key) if key == subclass_key else key): value
        for key, value in wrapper.items()
    }
    raw["value"] = wrapper
    with pytest.raises(CodecError):
        decode_guard(raw)


def test_lone_surrogate_is_rejected_on_encode_and_decode():
    with pytest.raises(CodecError):
        encode_guard(make_guard(operator="\ud800"))
    with pytest.raises(CodecError):
        encode_effect(make_effect(values=(("n", "\ud800"),)))
    raw = encode_guard(make_guard())
    raw["operator"] = "\ud800"
    with pytest.raises(CodecError):
        decode_guard(raw)


def test_cycles_raise_codec_error_not_recursion_error():
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(CodecError):
        encode_effect(make_effect(values=(("n", cyclic),)))

    raw = encode_guard(make_guard())
    cyclic_wire = {"t": "list", "v": []}
    cyclic_wire["v"].append(cyclic_wire)
    raw["value"] = cyclic_wire
    with pytest.raises(CodecError):
        decode_guard(raw)


def test_depth_limit_and_limit_plus_one():
    at_limit = make_effect(values=(("v", nested_lists(MAX_DEPTH - 3)),))
    assert_exact(decode_effect(encode_effect(at_limit)), at_limit)

    over_limit = make_effect(values=(("v", nested_lists(MAX_DEPTH - 2)),))
    with pytest.raises(CodecError):
        encode_effect(over_limit)

    raw = copy.deepcopy(encode_effect(at_limit))
    raw["values"][0]["v"][1] = {"t": "list", "v": [raw["values"][0]["v"][1]]}
    with pytest.raises(CodecError):
        decode_effect(raw)


def test_container_length_limit_and_limit_plus_one():
    at_limit = make_effect(values=(("v", [0] * MAX_CONTAINER_LENGTH),))
    assert_exact(decode_effect(encode_effect(at_limit)), at_limit)

    over_limit = make_effect(values=(("v", [0] * (MAX_CONTAINER_LENGTH + 1)),))
    with pytest.raises(CodecError):
        encode_effect(over_limit)


def test_string_byte_limit_and_limit_plus_one():
    at_limit = make_effect(reason="a" * MAX_STRING_BYTES)
    assert decode_effect(encode_effect(at_limit)).reason == at_limit.reason

    with pytest.raises(CodecError):
        encode_effect(make_effect(reason="a" * (MAX_STRING_BYTES + 1)))

    multibyte_at_limit = make_effect(reason="é" * (MAX_STRING_BYTES // 2))
    assert decode_effect(encode_effect(multibyte_at_limit)).reason == multibyte_at_limit.reason

    with pytest.raises(CodecError):
        encode_effect(make_effect(reason="é" * (MAX_STRING_BYTES // 2 + 1)))


def test_cumulative_text_limit_and_limit_plus_one():
    full = "a" * MAX_STRING_BYTES
    copies = MAX_CUMULATIVE_TEXT_BYTES // MAX_STRING_BYTES
    at_limit = make_interpretation(unknown=(full,) * copies)
    assert_exact(decode_interpretation(encode_interpretation(at_limit)), at_limit)

    over_limit = make_interpretation(unknown=at_limit.unknown + ("x",))
    with pytest.raises(CodecError):
        encode_interpretation(over_limit)


def test_integer_bit_limit_signed_sentinels():
    maximum = 1 << (MAX_INT_BITS - 1)
    minimum = -(1 << (MAX_INT_BITS - 1))
    for value in (maximum, minimum):
        effect = make_effect(values=(("n", value),))
        assert_exact(decode_effect(encode_effect(effect)), effect)

    for value in (1 << MAX_INT_BITS, -(1 << MAX_INT_BITS)):
        with pytest.raises(CodecError):
            encode_effect(make_effect(values=(("n", value),)))


def test_node_count_limit_and_limit_plus_one():
    at_limit = node_budget_interpretation(0)
    assert_exact(decode_interpretation(encode_interpretation(at_limit)), at_limit)

    over_limit = node_budget_interpretation(1)
    with pytest.raises(CodecError):
        encode_interpretation(over_limit)

    raw = copy.deepcopy(encode_interpretation(at_limit))
    raw["effects"][0]["values"][0]["v"][1]["v"][0]["v"].append({"t": "int", "v": 0})
    with pytest.raises(CodecError):
        decode_interpretation(raw)


def test_node_budget_fixture_is_exact():
    # Guard against silently shifting the boundary fixture away from MAX_NODES.
    assert MAX_NODES == 100_000
    with pytest.raises(CodecError):
        encode_interpretation(node_budget_interpretation(1))
