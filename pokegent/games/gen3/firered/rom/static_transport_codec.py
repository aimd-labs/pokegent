"""Unused strict codec for the elevator and ferry transport index families.

Wire format (JSON-compatible; version and record type tags are fixed)::

    {"version": 1, "type": "static_transport",
     "elevators": [[[group, number], [[panel_x, panel_y, panel_index, ENTRY,
                                       [floor_group, floor_number], x, y,
                                       required_flag|null, floor_var|null,
                                       FLOOR_VALUE, warp_index]...]]...],
     "ferries": [[[source_group, source_number, source_x, source_y,
                   source_elevation],
                  [[[dest_group, dest_number, dest_x, dest_y, dest_elevation],
                    [guard...], selector, local_id|null, [x, y]|null,
                    direction|null, menu_index|null, menu_id|null,
                    menu_pc|null, [[page, pc]...]]...]]...]}

ENTRY and FLOOR_VALUE are existing tagged VALUE objects.  ``entry`` is the
owning ``Guard.value``, so exact int/bool identity survives; ``floor_value``
comes through ``Effect.value`` after the interpreter resolves an int or a
symbolic str, so it is never coerced.  Floor/warp coordinates come from
``command.u16`` and stay exact ints.  Every one of the eleven elevator fields
and every ``FerryRoute`` field, including both complete ``NavigationNode``
projections, guards with origins, selector, nullable local/coord/direction/menu
fields and ordered ``menu_pcs``, is preserved.

This is a transport subset, not a graph or snapshot envelope.  Facts are
ordered tuple-pair rows, so producer order, duplicate keys/rows and empty
groups survive.  No navigation module is imported (``navigation/__init__.py``
eagerly loads the graph) and no runtime dataclass is constructed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pokegent.games.gen3.firered.rom.script_effects import Guard
from pokegent.games.gen3.firered.rom.static_index_codec import (
    _array,
    _array_rows,
    _int,
    _string,
    _tuple,
    _tuple_rows,
)
from pokegent.games.gen3.firered.rom.static_map_codec import _framing, _serialize
from pokegent.games.gen3.firered.rom.static_route_index_codec import _rows
from pokegent.games.gen3.firered.rom.static_symbolic_codec import (
    CodecError,
    _Budget,
    _decode_value,
    _encode_value,
    decode_guard,
    encode_guard,
)

TRANSPORT_CODEC_VERSION = 1
TRANSPORT_RECORD_TYPE = "static_transport"

_MapKey = tuple[int, int]
_NodeRow = tuple[int, int, int, int, int]
_ElevatorRow = tuple[Any, ...]
_FerryRow = tuple[
    _NodeRow,
    tuple[Guard, ...],
    int,
    int | None,
    tuple[int, int] | None,
    str | None,
    int | None,
    int | None,
    int | None,
    tuple[tuple[int, int], ...],
]

_ELEVATOR_LENGTH = 11
_FERRY_LENGTH = 10
_NODE_LENGTH = 5

_KEYS = frozenset({"version", "type", "elevators", "ferries"})


@dataclass(frozen=True)
class StaticTransportFacts:
    """The two transport families as recursively immutable ordered rows."""

    elevators: tuple[tuple[_MapKey, tuple[_ElevatorRow, ...]], ...]
    ferries: tuple[tuple[_NodeRow, tuple[_FerryRow, ...]], ...]

    def __post_init__(self) -> None:
        if type(self.elevators) is not tuple:
            raise CodecError("elevators must be an exact tuple")
        if type(self.ferries) is not tuple:
            raise CodecError("ferries must be an exact tuple")


def _optional_int(value: Any, label: str) -> int | None:
    return None if value is None else _int(value, label=label)


def _optional_string(value: Any, label: str) -> str | None:
    return None if value is None else _string(value, label)


def _tagged(value: Any, label: str, *, none: bool = False) -> dict[str, Any]:
    if value is None and not none:
        raise CodecError(f"{label} must not be None")
    return _encode_value(value, 0, _Budget())


def _untagged(value: Any, label: str, *, none: bool = False) -> Any:
    result = _decode_value(value, 0, _Budget())
    if result is None and not none:
        raise CodecError(f"{label} must not be None")
    return result


def _entry(value: Any, label: str) -> int | bool:
    result = _untagged(value, label)
    if type(result) not in (int, bool):
        raise CodecError(f"{label} must be an exact int or bool")
    return result


def _floor_value(value: Any) -> int | str | None:
    result = _untagged(value, "elevator floor_value", none=True)
    if result is not None and type(result) not in (int, str):
        raise CodecError("elevator floor_value must be an exact int, str or None")
    return result


def _wire_node(node: Any, label: str) -> list[int]:
    row = _tuple(node, label, length=_NODE_LENGTH)
    names = ("group", "number", "x", "y", "elevation")
    return [_int(v, label=f"{label} {name}") for v, name in zip(row, names)]


def _read_node(value: Any, label: str) -> _NodeRow:
    row = _array(value, label, length=_NODE_LENGTH)
    names = ("group", "number", "x", "y", "elevation")
    return tuple(_int(v, label=f"{label} {name}") for v, name in zip(row, names))


def _encode_elevator(row: Any) -> list[Any]:
    fields = _tuple(row, "elevator row", length=_ELEVATOR_LENGTH)
    floor = _tuple(fields[4], "elevator floor_key", length=2)
    return [
        _int(fields[0], label="elevator panel_x"),
        _int(fields[1], label="elevator panel_y"),
        _int(fields[2], label="elevator panel_index"),
        _tagged(fields[3], "elevator entry"),
        [_int(floor[0], label="elevator floor group"),
         _int(floor[1], label="elevator floor number")],
        _int(fields[5], label="elevator x"),
        _int(fields[6], label="elevator y"),
        _optional_int(fields[7], "elevator required_flag"),
        _optional_int(fields[8], "elevator floor_var"),
        _tagged(fields[9], "elevator floor_value", none=True),
        _int(fields[10], label="elevator warp_index"),
    ]


def _decode_elevator(raw: Any) -> _ElevatorRow:
    row = _array(raw, "elevator row", length=_ELEVATOR_LENGTH)
    floor = _array(row[4], "elevator floor_key", length=2)
    return (
        _int(row[0], label="elevator panel_x"),
        _int(row[1], label="elevator panel_y"),
        _int(row[2], label="elevator panel_index"),
        _entry(row[3], "elevator entry"),
        (_int(floor[0], label="elevator floor group"),
         _int(floor[1], label="elevator floor number")),
        _int(row[5], label="elevator x"),
        _int(row[6], label="elevator y"),
        _optional_int(row[7], "elevator required_flag"),
        _optional_int(row[8], "elevator floor_var"),
        _floor_value(row[9]),
        _int(row[10], label="elevator warp_index"),
    )


def _encode_elevators(rows: Any) -> list[list[Any]]:
    result = []
    for key, entries in _rows(rows, "elevators", 2):
        result.append([
            [_int(key[0], label="elevator map group"),
             _int(key[1], label="elevator map number")],
            [_encode_elevator(row) for row in _tuple(entries, "elevator entries")],
        ])
    return result


def _decode_elevators(raw: Any) -> tuple[tuple[_MapKey, tuple[_ElevatorRow, ...]], ...]:
    result = []
    for row in _array_rows(raw, "elevators", length=2):
        key = _array(row[0], "elevator map", length=2)
        result.append((
            (_int(key[0], label="elevator map group"),
             _int(key[1], label="elevator map number")),
            tuple(_decode_elevator(item)
                  for item in _array(row[1], "elevator entries")),
        ))
    return tuple(result)


def _encode_ferry(row: Any) -> list[Any]:
    fields = _tuple(row, "ferry row", length=_FERRY_LENGTH)
    coord = None if fields[4] is None else _tuple(fields[4], "ferry coord", length=2)
    menu_pcs = _tuple(fields[9], "ferry menu_pcs")
    return [
        _wire_node(fields[0], "ferry destination"),
        [encode_guard(guard) for guard in _tuple(fields[1], "ferry guards")],
        _int(fields[2], label="ferry selector"),
        _optional_int(fields[3], "ferry local_id"),
        None if coord is None else [
            _int(coord[0], label="ferry coord x"),
            _int(coord[1], label="ferry coord y"),
        ],
        _optional_string(fields[5], "ferry direction"),
        _optional_int(fields[6], "ferry menu_index"),
        _optional_int(fields[7], "ferry menu_id"),
        _optional_int(fields[8], "ferry menu_pc"),
        [
            [_int(pair[0], label="ferry menu page"),
             _int(pair[1], label="ferry menu pc")]
            for pair in _tuple_rows(menu_pcs, "ferry menu_pcs")
        ],
    ]


def _decode_ferry(raw: Any) -> _FerryRow:
    row = _array(raw, "ferry row", length=_FERRY_LENGTH)
    coord = None if row[4] is None else _array(row[4], "ferry coord", length=2)
    menu_pcs = []
    for pair in _array_rows(row[9], "ferry menu_pcs", length=2):
        menu_pcs.append((_int(pair[0], label="ferry menu page"),
                         _int(pair[1], label="ferry menu pc")))
    return (
        _read_node(row[0], "ferry destination"),
        tuple(decode_guard(guard) for guard in _array(row[1], "ferry guards")),
        _int(row[2], label="ferry selector"),
        _optional_int(row[3], "ferry local_id"),
        None if coord is None else (
            _int(coord[0], label="ferry coord x"),
            _int(coord[1], label="ferry coord y"),
        ),
        _optional_string(row[5], "ferry direction"),
        _optional_int(row[6], "ferry menu_index"),
        _optional_int(row[7], "ferry menu_id"),
        _optional_int(row[8], "ferry menu_pc"),
        tuple(menu_pcs),
    )


def _encode_ferries(rows: Any) -> list[list[Any]]:
    result = []
    for source, entries in _rows(rows, "ferries", 2):
        result.append([
            _wire_node(source, "ferry source"),
            [_encode_ferry(row) for row in _tuple(entries, "ferry entries")],
        ])
    return result


def _decode_ferries(raw: Any) -> tuple[tuple[_NodeRow, tuple[_FerryRow, ...]], ...]:
    result = []
    for row in _array_rows(raw, "ferries", length=2):
        result.append((
            _read_node(row[0], "ferry source"),
            tuple(_decode_ferry(item) for item in _array(row[1], "ferry entries")),
        ))
    return tuple(result)


def encode_transport_facts(facts: StaticTransportFacts) -> dict[str, Any]:
    """Encode exactly the elevator and ferry families in producer order."""
    if type(facts) is not StaticTransportFacts:
        raise CodecError("expected exact StaticTransportFacts")
    wire = {
        "version": TRANSPORT_CODEC_VERSION,
        "type": TRANSPORT_RECORD_TYPE,
        "elevators": _encode_elevators(facts.elevators),
        "ferries": _encode_ferries(facts.ferries),
    }
    _serialize(wire)
    return wire


def decode_transport_facts(wire: Any) -> StaticTransportFacts:
    """Strictly decode one transport subset into fresh immutable rows."""
    _framing(wire, TRANSPORT_RECORD_TYPE, _KEYS)
    _serialize(wire)
    return StaticTransportFacts(
        elevators=_decode_elevators(wire["elevators"]),
        ferries=_decode_ferries(wire["ferries"]),
    )
