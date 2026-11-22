"""Unused strict codec for the seven route static graph-index families.

Wire format (JSON-compatible; version and record type tags are fixed)::

    {"version": 1, "type": "static_route_index",
     "gates": [[[group, number],
                [[x, y, opened_flag, bg_index]...]]...],
     "gate_requirements": [[[[group, number], bg_index], requirement]...],
     "script_toggles": [[[group, number],
                         [[x, y, flag, facing|null, event_index,
                           local_id|null]...]]...],
     "trainers_by_flag": [[flag,
                           [[flag, [group, number], local_id, x, y]...]]...],
     "script_door_gates": [[[[group, number], warp_index],
                            [requirement_key, label,
                             [equivalent_requirement...]]]...],
     "on_frame_landings": [[[group, number],
                            [[var, value, [[dx, dy]...]]...]]...],
     "seagallop_warps": [[selector, [group, number, warp_id, x, y]]...]}

This is a route-index subset, not a complete graph or snapshot envelope.  It
owns exactly the seven tables built by ``_effect_script_gates``,
``_effect_script_toggles``, ``_read_trainers_by_flag``,
``_effect_script_door_gates``, ``_read_on_frame_landings`` and
``_read_seagallop_warps``.  Facts are ordered tuple-pair rows, so producer
order, duplicate keys and empty containers survive; decoding rebuilds the
exact source tuple shape from transport arrays.  Signed ints survive, exact
bool is never an int, nullable toggle fields stay explicit and Seagallop keeps
its literal ``warp_id=-1``.  Requirement keys/labels stay opaque: this codec
never parses requirement grammar or infers current flags/access.  Map/warp
reference and unique-key admission belong to later graph integration.

No navigation module is imported (``navigation/__init__.py`` eagerly loads the
graph) and this module is not wired into the runtime.  Bounded framing,
serialization and integer admission are reused from the static map/index
codecs; no runtime class is constructed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pokegent.games.gen3.firered.rom.static_index_codec import (
    _array,
    _array_rows,
    _read_map_key,
    _string,
    _tuple,
    _wire_map_key,
)
from pokegent.games.gen3.firered.rom.static_map_codec import _int, _serialize
from pokegent.games.gen3.firered.rom.static_symbolic_codec import CodecError

ROUTE_CODEC_VERSION = 1
ROUTE_RECORD_TYPE = "static_route_index"

_MapKey = tuple[int, int]
_GateRow = tuple[int, int, int, int]
_ToggleRow = tuple[int, int, int, str | None, int, int | None]
_TrainerRow = tuple[int, _MapKey, int, int, int]
_LandingRow = tuple[int, int, tuple[tuple[int, int], ...]]
_DoorValue = tuple[str, str, tuple[str, ...]]

_KEYS = frozenset(
    {
        "version", "type", "gates", "gate_requirements", "script_toggles",
        "trainers_by_flag", "script_door_gates", "on_frame_landings",
        "seagallop_warps",
    }
)


@dataclass(frozen=True)
class StaticRouteFacts:
    """The seven route families as recursively immutable ordered rows."""

    gates: tuple[tuple[_MapKey, tuple[_GateRow, ...]], ...]
    gate_requirements: tuple[tuple[tuple[_MapKey, int], str], ...]
    script_toggles: tuple[tuple[_MapKey, tuple[_ToggleRow, ...]], ...]
    trainers_by_flag: tuple[tuple[int, tuple[_TrainerRow, ...]], ...]
    script_door_gates: tuple[tuple[tuple[_MapKey, int], _DoorValue], ...]
    on_frame_landings: tuple[tuple[_MapKey, tuple[_LandingRow, ...]], ...]
    seagallop_warps: tuple[tuple[int, tuple[int, int, int, int, int]], ...]

    def __post_init__(self) -> None:
        for name in (
            "gates", "gate_requirements", "script_toggles", "trainers_by_flag",
            "script_door_gates", "on_frame_landings", "seagallop_warps",
        ):
            if type(getattr(self, name)) is not tuple:
                raise CodecError(f"{name} must be an exact tuple")


def _rows(value: Any, label: str, length: int) -> list[tuple]:
    """Validate an owning tuple before iteration; every row has a fixed length."""
    return [_tuple(row, f"{label} row", length) for row in _tuple(value, label)]


def _ints(values: Any, labels: tuple[str, ...], *, read: bool = False) -> Any:
    checked = tuple(_int(v, label=label) for v, label in zip(values, labels))
    return checked if read else list(checked)


def _table(rows: Any, label: str, keys: str, entries: str, n: int,
           fn: Any) -> list[list[Any]]:
    return [
        [_wire_map_key(key, keys), [fn(row) for row in _rows(rows_, entries, n)]]
        for key, rows_ in _rows(rows, label, 2)
    ]


def _read_table(raw: Any, label: str, keys: str, entries: str, n: int,
                fn: Any) -> tuple[tuple[_MapKey, tuple[Any, ...]], ...]:
    return tuple(
        (
            _read_map_key(row[0], keys),
            tuple(fn(e) for e in _array_rows(row[1], entries, length=n)),
        )
        for row in _array_rows(raw, label, length=2)
    )


def _framing(wire: Any) -> None:
    if type(wire) is not dict:
        raise CodecError("record must be an exact mapping")
    if len(wire) != len(_KEYS):
        raise CodecError("record keys mismatch")
    for key in wire:
        if type(key) is not str or key not in _KEYS:
            raise CodecError("record keys mismatch")
    if type(wire["version"]) is not int or wire["version"] != ROUTE_CODEC_VERSION:
        raise CodecError("unsupported codec version")
    if type(wire["type"]) is not str or wire["type"] != ROUTE_RECORD_TYPE:
        raise CodecError("record type mismatch")


_GATES = ("gate x", "gate y", "gate opened flag", "gate bg index")


def _encode_gates(rows: Any) -> list[list[Any]]:
    return _table(rows, "gates", "gate map", "gate entries", 4,
                  lambda r: _ints(r, _GATES))


def _decode_gates(raw: Any) -> tuple[tuple[_MapKey, tuple[_GateRow, ...]], ...]:
    return _read_table(raw, "gates", "gate map", "gate entries", 4,
                       lambda e: _ints(e, _GATES, read=True))


def _encode_gate_requirements(rows: Any) -> list[list[Any]]:
    result = []
    for row in _rows(rows, "gate_requirements", 2):
        key = _tuple(row[0], "gate requirement key", length=2)
        result.append([
            [_wire_map_key(key[0], "gate requirement map"),
             _int(key[1], label="gate requirement bg index")],
            _string(row[1], "gate requirement"),
        ])
    return result


def _decode_gate_requirements(raw: Any) -> tuple[tuple[tuple[_MapKey, int], str], ...]:
    result = []
    for row in _array_rows(raw, "gate_requirements", length=2):
        key = _array(row[0], "gate requirement key", length=2)
        result.append((
            (_read_map_key(key[0], "gate requirement map"),
             _int(key[1], label="gate requirement bg index")),
            _string(row[1], "gate requirement"),
        ))
    return tuple(result)


def _encode_toggle_row(row: tuple) -> list[Any]:
    return [
        _int(row[0], label="toggle x"), _int(row[1], label="toggle y"),
        _int(row[2], label="toggle flag"),
        None if row[3] is None else _string(row[3], "toggle facing"),
        _int(row[4], label="toggle event_index"),
        None if row[5] is None else _int(row[5], label="toggle local_id"),
    ]


def _decode_toggle_row(entry: list) -> _ToggleRow:
    return (
        _int(entry[0], label="toggle x"), _int(entry[1], label="toggle y"),
        _int(entry[2], label="toggle flag"),
        None if entry[3] is None else _string(entry[3], "toggle facing"),
        _int(entry[4], label="toggle event_index"),
        None if entry[5] is None else _int(entry[5], label="toggle local_id"),
    )


def _encode_script_toggles(rows: Any) -> list[list[Any]]:
    return _table(rows, "script_toggles", "toggle map", "toggle entries", 6,
                  _encode_toggle_row)


def _decode_script_toggles(
    raw: Any,
) -> tuple[tuple[_MapKey, tuple[_ToggleRow, ...]], ...]:
    return _read_table(raw, "script_toggles", "toggle map", "toggle entries", 6,
                       _decode_toggle_row)


def _encode_trainers(rows: Any) -> list[list[Any]]:
    result = []
    for flag, entries in _rows(rows, "trainers_by_flag", 2):
        result.append([
            _int(flag, label="trainer group flag"),
            [[_int(r[0], label="trainer flag"),
              _wire_map_key(r[1], "trainer map"),
              _int(r[2], label="trainer local_id"),
              _int(r[3], label="trainer x"),
              _int(r[4], label="trainer y")]
             for r in _rows(entries, "trainer entries", 5)],
        ])
    return result


def _decode_trainers(raw: Any) -> tuple[tuple[int, tuple[_TrainerRow, ...]], ...]:
    return tuple(
        (_int(row[0], label="trainer group flag"),
         tuple((_int(e[0], label="trainer flag"),
                _read_map_key(e[1], "trainer map"),
                _int(e[2], label="trainer local_id"),
                _int(e[3], label="trainer x"),
                _int(e[4], label="trainer y"))
               for e in _array_rows(row[1], "trainer entries", length=5)))
        for row in _array_rows(raw, "trainers_by_flag", length=2)
    )


def _door_parts(value: Any) -> _DoorValue:
    row = _tuple(value, "door gate value", length=3)
    return (
        _string(row[0], "door requirement key"),
        _string(row[1], "door label"),
        tuple(_string(i, "door equivalent requirement")
              for i in _tuple(row[2], "door equivalents")),
    )


def _encode_door_gates(rows: Any) -> list[list[Any]]:
    result = []
    for row in _rows(rows, "script_door_gates", 2):
        key = _tuple(row[0], "door gate key", length=2)
        requirement_key, label, equivalents = _door_parts(row[1])
        result.append([
            [_wire_map_key(key[0], "door gate map"),
             _int(key[1], label="door warp index")],
            [requirement_key, label, list(equivalents)],
        ])
    return result


def _decode_door_gates(raw: Any) -> tuple[tuple[tuple[_MapKey, int], _DoorValue], ...]:
    result = []
    for row in _array_rows(raw, "script_door_gates", length=2):
        key = _array(row[0], "door gate key", length=2)
        value = _array(row[1], "door gate value", length=3)
        result.append((
            (_read_map_key(key[0], "door gate map"),
             _int(key[1], label="door warp index")),
            (_string(value[0], "door requirement key"),
             _string(value[1], "door label"),
             tuple(_string(i, "door equivalent requirement")
                   for i in _array(value[2], "door equivalents"))),
        ))
    return tuple(result)


def _encode_landing_row(row: tuple) -> list[Any]:
    return [
        _int(row[0], label="landing var"), _int(row[1], label="landing value"),
        [[_int(s[0], label="landing step dx"), _int(s[1], label="landing step dy")]
         for s in _rows(row[2], "landing path steps", 2)],
    ]


def _decode_landing_row(entry: list) -> _LandingRow:
    return (
        _int(entry[0], label="landing var"),
        _int(entry[1], label="landing value"),
        tuple((_int(s[0], label="landing step dx"),
               _int(s[1], label="landing step dy"))
              for s in _array_rows(entry[2], "landing path steps", length=2)),
    )


def _encode_landings(rows: Any) -> list[list[Any]]:
    return _table(rows, "on_frame_landings", "landing map", "landing entries", 3,
                  _encode_landing_row)


def _decode_landings(raw: Any) -> tuple[tuple[_MapKey, tuple[_LandingRow, ...]], ...]:
    return _read_table(raw, "on_frame_landings", "landing map", "landing entries",
                       3, _decode_landing_row)


_SEAG = ("seagallop group", "seagallop number", "seagallop warp_id",
         "seagallop x", "seagallop y")


def _encode_seagallop(rows: Any) -> list[list[Any]]:
    return [
        [_int(row[0], label="seagallop selector"),
         _ints(_tuple(row[1], "seagallop destination", length=5), _SEAG)]
        for row in _rows(rows, "seagallop_warps", 2)
    ]


def _decode_seagallop(raw: Any) -> tuple[tuple[int, tuple[int, int, int, int, int]], ...]:
    return tuple(
        (_int(row[0], label="seagallop selector"),
         _ints(_array(row[1], "seagallop destination", length=5), _SEAG, read=True))
        for row in _array_rows(raw, "seagallop_warps", length=2)
    )


def encode_route_facts(facts: StaticRouteFacts) -> dict[str, Any]:
    """Encode exactly the seven route families in producer order."""
    if type(facts) is not StaticRouteFacts:
        raise CodecError("expected exact StaticRouteFacts")
    wire = {
        "version": ROUTE_CODEC_VERSION,
        "type": ROUTE_RECORD_TYPE,
        "gates": _encode_gates(facts.gates),
        "gate_requirements": _encode_gate_requirements(facts.gate_requirements),
        "script_toggles": _encode_script_toggles(facts.script_toggles),
        "trainers_by_flag": _encode_trainers(facts.trainers_by_flag),
        "script_door_gates": _encode_door_gates(facts.script_door_gates),
        "on_frame_landings": _encode_landings(facts.on_frame_landings),
        "seagallop_warps": _encode_seagallop(facts.seagallop_warps),
    }
    _serialize(wire)
    return wire


def decode_route_facts(wire: Any) -> StaticRouteFacts:
    """Strictly decode one route-index subset into fresh tuple facts."""
    _framing(wire)
    _serialize(wire)
    return StaticRouteFacts(
        gates=_decode_gates(wire["gates"]),
        gate_requirements=_decode_gate_requirements(wire["gate_requirements"]),
        script_toggles=_decode_script_toggles(wire["script_toggles"]),
        trainers_by_flag=_decode_trainers(wire["trainers_by_flag"]),
        script_door_gates=_decode_door_gates(wire["script_door_gates"]),
        on_frame_landings=_decode_landings(wire["on_frame_landings"]),
        seagallop_warps=_decode_seagallop(wire["seagallop_warps"]),
    )
