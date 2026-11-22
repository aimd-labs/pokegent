"""Unused strict codec for the five primitive static graph-index families.

Wire format (JSON-compatible; version and record type tags are fixed)::

    {"version": 1, "type": "static_primitive_index",
     "flag_items": [[flag, item]...],
     "on_load_warp_flags": [[[group, number], warp_index, flag]...],
     "on_load_barriers": [[[group, number],
                           [[[x, y], [requirement...]]...]]...],
     "on_load_barrier_labels": [[[group, number], [x, y], label]...],
     "script_warp_gates": [[[group, number], warp_index,
                            [requirement_key, bg_index]]...]}

This is a primitive-index subset, not a complete graph or snapshot envelope:
it owns exactly the five dicts built by ``NavigationGraph._effect_flag_items``,
``_effect_on_load_warp_flags``, ``_effect_on_load_barriers``,
``_effect_on_load_barrier_labels`` and ``_effect_script_warp_gates``.  Facts
are ordered tuple-pair rows, so producer insertion order, duplicate keys and
empty containers survive.  Decoding reconstructs the exact source tuple shape
from transport arrays.  Signed ints survive, exact bool is never an int, and
requirement strings stay opaque: this codec never parses requirement grammar
or infers current flags/access.  Map/warp reference and unique-key admission
belong to later graph integration.

No navigation module is imported (``navigation/__init__.py`` eagerly loads the
graph) and this module is not wired into the runtime.  Framing, canonical
serialization and integer admission are reused from the static map codec.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pokegent.games.gen3.firered.rom.static_map_codec import _int, _serialize
from pokegent.games.gen3.firered.rom.static_symbolic_codec import (
    MAX_CONTAINER_LENGTH,
    CodecError,
)

INDEX_CODEC_VERSION = 1
INDEX_RECORD_TYPE = "static_primitive_index"

_MapKey = tuple[int, int]
_BarrierRows = tuple[tuple[_MapKey, tuple[tuple[_MapKey, tuple[str, ...]], ...]], ...]

_KEYS = frozenset(
    {
        "version",
        "type",
        "flag_items",
        "on_load_warp_flags",
        "on_load_barriers",
        "on_load_barrier_labels",
        "script_warp_gates",
    }
)


@dataclass(frozen=True)
class StaticIndexFacts:
    """The five primitive index families as recursively immutable ordered rows."""

    flag_items: tuple[tuple[int, int], ...]
    on_load_warp_flags: tuple[tuple[tuple[_MapKey, int], int], ...]
    on_load_barriers: _BarrierRows
    on_load_barrier_labels: tuple[tuple[tuple[_MapKey, _MapKey], str], ...]
    script_warp_gates: tuple[tuple[tuple[_MapKey, int], tuple[str, int]], ...]

    def __post_init__(self) -> None:
        for name in (
            "flag_items",
            "on_load_warp_flags",
            "on_load_barriers",
            "on_load_barrier_labels",
            "script_warp_gates",
        ):
            if type(getattr(self, name)) is not tuple:
                raise CodecError(f"{name} must be an exact tuple")


def _tuple(value: Any, label: str, length: int | None = None) -> tuple:
    if type(value) is not tuple:
        raise CodecError(f"{label} must be an exact tuple")
    if length is not None and len(value) != length:
        raise CodecError(f"{label} must have exactly {length} entries")
    if len(value) > MAX_CONTAINER_LENGTH:
        raise CodecError(f"{label} exceeds its length bound")
    return value


def _array(value: Any, label: str, length: int | None = None) -> list:
    if type(value) is not list:
        raise CodecError(f"{label} must be an exact array")
    if length is not None and len(value) != length:
        raise CodecError(f"{label} must have exactly {length} entries")
    if len(value) > MAX_CONTAINER_LENGTH:
        raise CodecError(f"{label} exceeds its length bound")
    return value


def _tuple_rows(value: Any, label: str) -> Any:
    """Validate an owning tuple before iteration; every row is a two-tuple."""
    return (_tuple(row, f"{label} row", length=2) for row in _tuple(value, label))


def _array_rows(value: Any, label: str, length: int) -> Any:
    """Validate a wire array before iteration; every row has the fixed length."""
    return (
        _array(row, f"{label} row", length=length) for row in _array(value, label)
    )


def _string(value: Any, label: str) -> str:
    if type(value) is not str:
        raise CodecError(f"{label} must be an exact str")
    return value


def _wire_map_key(value: Any, label: str) -> list[int]:
    pair = _tuple(value, label, length=2)
    return [_int(pair[0], label=f"{label} group"), _int(pair[1], label=f"{label} number")]


def _read_map_key(value: Any, label: str) -> _MapKey:
    pair = _array(value, label, length=2)
    return (_int(pair[0], label=f"{label} group"), _int(pair[1], label=f"{label} number"))


def _framing(wire: Any) -> None:
    if type(wire) is not dict:
        raise CodecError("record must be an exact mapping")
    if len(wire) != len(_KEYS):
        raise CodecError("record keys mismatch")
    for key in wire:
        if type(key) is not str or key not in _KEYS:
            raise CodecError("record keys mismatch")
    if type(wire["version"]) is not int or wire["version"] != INDEX_CODEC_VERSION:
        raise CodecError("unsupported codec version")
    if type(wire["type"]) is not str or wire["type"] != INDEX_RECORD_TYPE:
        raise CodecError("record type mismatch")


def _encode_flag_items(rows: Any) -> list[list[int]]:
    return [
        [_int(row[0], label="item flag"), _int(row[1], label="item id")]
        for row in _tuple_rows(rows, "flag_items")
    ]


def _decode_flag_items(raw: Any) -> tuple[tuple[int, int], ...]:
    return tuple(
        (_int(row[0], label="item flag"), _int(row[1], label="item id"))
        for row in _array_rows(raw, "flag_items", length=2)
    )


def _encode_warp_flags(rows: Any) -> list[list[Any]]:
    result = []
    for row in _tuple_rows(rows, "on_load_warp_flags"):
        key = _tuple(row[0], "warp flag key", length=2)
        result.append(
            [
                _wire_map_key(key[0], "warp map"),
                _int(key[1], label="warp index"),
                _int(row[1], label="warp flag"),
            ]
        )
    return result


def _decode_warp_flags(raw: Any) -> tuple[tuple[tuple[_MapKey, int], int], ...]:
    result = []
    for row in _array_rows(raw, "on_load_warp_flags", length=3):
        result.append(
            (
                (_read_map_key(row[0], "warp map"), _int(row[1], label="warp index")),
                _int(row[2], label="warp flag"),
            )
        )
    return tuple(result)


def _encode_barriers(rows: Any) -> list[list[Any]]:
    result = []
    for row in _tuple_rows(rows, "on_load_barriers"):
        inner = []
        for entry in _tuple_rows(row[1], "barrier entries"):
            requirements = [
                _string(item, "barrier requirement")
                for item in _tuple(entry[1], "barrier requirements")
            ]
            inner.append([_wire_map_key(entry[0], "barrier xy"), requirements])
        result.append([_wire_map_key(row[0], "barrier map"), inner])
    return result


def _decode_barriers(raw: Any) -> _BarrierRows:
    result = []
    for row in _array_rows(raw, "on_load_barriers", length=2):
        inner = []
        for entry in _array_rows(row[1], "barrier entries", length=2):
            requirements = tuple(
                _string(item, "barrier requirement")
                for item in _array(entry[1], "barrier requirements")
            )
            inner.append((_read_map_key(entry[0], "barrier xy"), requirements))
        result.append((_read_map_key(row[0], "barrier map"), tuple(inner)))
    return tuple(result)


def _encode_barrier_labels(rows: Any) -> list[list[Any]]:
    result = []
    for row in _tuple_rows(rows, "on_load_barrier_labels"):
        key = _tuple(row[0], "barrier label key", length=2)
        result.append(
            [
                _wire_map_key(key[0], "barrier label map"),
                _wire_map_key(key[1], "barrier label xy"),
                _string(row[1], "barrier label"),
            ]
        )
    return result


def _decode_barrier_labels(raw: Any) -> tuple[tuple[tuple[_MapKey, _MapKey], str], ...]:
    result = []
    for row in _array_rows(raw, "on_load_barrier_labels", length=3):
        result.append(
            (
                (
                    _read_map_key(row[0], "barrier label map"),
                    _read_map_key(row[1], "barrier label xy"),
                ),
                _string(row[2], "barrier label"),
            )
        )
    return tuple(result)


def _encode_warp_gates(rows: Any) -> list[list[Any]]:
    result = []
    for row in _tuple_rows(rows, "script_warp_gates"):
        key = _tuple(row[0], "warp gate key", length=2)
        value = _tuple(row[1], "warp gate value", length=2)
        result.append(
            [
                _wire_map_key(key[0], "warp gate map"),
                _int(key[1], label="warp gate index"),
                [_string(value[0], "warp gate requirement"),
                 _int(value[1], label="warp gate bg index")],
            ]
        )
    return result


def _decode_warp_gates(raw: Any) -> tuple[tuple[tuple[_MapKey, int], tuple[str, int]], ...]:
    result = []
    for row in _array_rows(raw, "script_warp_gates", length=3):
        value = _array(row[2], "warp gate value", length=2)
        result.append(
            (
                (_read_map_key(row[0], "warp gate map"), _int(row[1], label="warp gate index")),
                (
                    _string(value[0], "warp gate requirement"),
                    _int(value[1], label="warp gate bg index"),
                ),
            )
        )
    return tuple(result)


def encode_index_facts(facts: StaticIndexFacts) -> dict[str, Any]:
    """Encode exactly the five primitive index families in producer order."""
    if type(facts) is not StaticIndexFacts:
        raise CodecError("expected exact StaticIndexFacts")
    wire = {
        "version": INDEX_CODEC_VERSION,
        "type": INDEX_RECORD_TYPE,
        "flag_items": _encode_flag_items(facts.flag_items),
        "on_load_warp_flags": _encode_warp_flags(facts.on_load_warp_flags),
        "on_load_barriers": _encode_barriers(facts.on_load_barriers),
        "on_load_barrier_labels": _encode_barrier_labels(facts.on_load_barrier_labels),
        "script_warp_gates": _encode_warp_gates(facts.script_warp_gates),
    }
    _serialize(wire)
    return wire


def decode_index_facts(wire: Any) -> StaticIndexFacts:
    """Strictly decode one primitive-index subset into fresh tuple facts."""
    _framing(wire)
    _serialize(wire)
    return StaticIndexFacts(
        flag_items=_decode_flag_items(wire["flag_items"]),
        on_load_warp_flags=_decode_warp_flags(wire["on_load_warp_flags"]),
        on_load_barriers=_decode_barriers(wire["on_load_barriers"]),
        on_load_barrier_labels=_decode_barrier_labels(wire["on_load_barrier_labels"]),
        script_warp_gates=_decode_warp_gates(wire["script_warp_gates"]),
    )
