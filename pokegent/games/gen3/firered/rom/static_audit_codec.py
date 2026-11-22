"""Unused strict codec for per-call script-effect audit records.

Wire format (JSON-compatible; version and record type tags are fixed)::

    {"version": 1, "type": "static_audit",
     "calls": [[reader, [group, number], [x, y]|null, script_ptr,
                [reason...]]...]}

One ``AuditCall`` is one ``_complete_effects`` call.  Calls stay in call order
and are never deduplicated or unioned: two incomplete calls with the same
consumer, map, site and pointer but reasons A then B remain two records.  That
is required because replay increments the reader counter once for the first
call yet renders two distinct ``script P: A`` / ``script P: B`` site strings.
Mutable counters, seen keys and unknown-site maps are runtime accumulators, not
wire fields.

The closed reader registry repeats the owning ``_SCRIPT_EFFECT_READERS`` names
and a focused drift test compares it with that source.  No navigation module is
imported (``navigation/__init__.py`` eagerly loads the graph), no reducer or
installer is provided, and audit replay remains later runtime work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pokegent.games.gen3.firered.rom.static_index_codec import (
    _array,
    _array_rows,
    _int,
    _read_map_key,
    _string,
    _tuple,
    _wire_map_key,
)
from pokegent.games.gen3.firered.rom.static_map_codec import _framing, _serialize
from pokegent.games.gen3.firered.rom.static_symbolic_codec import CodecError

AUDIT_CODEC_VERSION = 1
AUDIT_RECORD_TYPE = "static_audit"

#: Copied from ``navigation.graph._SCRIPT_EFFECT_READERS``; a test asserts drift.
AUDIT_READERS = (
    "door_gates:on_load",
    "door_gates:on_transition",
    "elevators:bg",
    "flag_items:bg",
    "flag_items:coord",
    "flag_items:object",
    "ferries:coord",
    "ferries:npc",
    "on_load_barrier_labels:on_load",
    "on_load_barriers:on_load",
    "on_load_metatiles:on_load",
    "on_load_warp_flags:on_load",
    "script_gates:bg",
    "script_obstacles:coord",
    "script_obstacles:npc",
    "script_obstacles:on_transition",
    "toggles:bg",
    "toggles:object",
    "warp_gates:bg",
    "warp_gates:on_load",
    "warp_gates:on_transition",
)
_READERS = frozenset(AUDIT_READERS)

_KEYS = frozenset({"version", "type", "calls"})
_CALL_LENGTH = 5


@dataclass(frozen=True)
class AuditCall:
    """One incomplete-effects call with its own reason group."""

    reader: str
    map_key: tuple[int, int]
    site: tuple[int, int] | None
    script_ptr: int
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.reasons) is not tuple:
            raise CodecError("audit reasons must be an exact tuple")


def _reader(value: Any) -> str:
    name = _string(value, "audit reader")
    if name not in _READERS:
        raise CodecError("audit reader is not in the closed registry")
    return name


def _site(value: Any) -> tuple[int, int] | None:
    if value is None:
        return None
    pair = _tuple(value, "audit site", length=2)
    return (_int(pair[0], label="audit site x"), _int(pair[1], label="audit site y"))


def _read_site(value: Any) -> tuple[int, int] | None:
    if value is None:
        return None
    pair = _array(value, "audit site", length=2)
    return (_int(pair[0], label="audit site x"), _int(pair[1], label="audit site y"))


def _encode_call(call: Any) -> list[Any]:
    if type(call) is not AuditCall:
        raise CodecError("expected an exact AuditCall")
    site = _site(call.site)
    return [
        _reader(call.reader),
        _wire_map_key(call.map_key, "audit map"),
        None if site is None else [site[0], site[1]],
        _int(call.script_ptr, label="audit script_ptr"),
        [_string(reason, "audit reason") for reason in call.reasons],
    ]


def _decode_call(raw: Any) -> AuditCall:
    row = _array(raw, "audit call", length=_CALL_LENGTH)
    return AuditCall(
        reader=_reader(row[0]),
        map_key=_read_map_key(row[1], "audit map"),
        site=_read_site(row[2]),
        script_ptr=_int(row[3], label="audit script_ptr"),
        reasons=tuple(
            _string(reason, "audit reason")
            for reason in _array(row[4], "audit reasons")
        ),
    )


def encode_audit_calls(calls: Any) -> dict[str, Any]:
    """Encode one record per call in call order; duplicates are retained."""
    if type(calls) is not tuple:
        raise CodecError("audit calls must be an exact tuple")
    wire = {
        "version": AUDIT_CODEC_VERSION,
        "type": AUDIT_RECORD_TYPE,
        "calls": [_encode_call(call) for call in calls],
    }
    _serialize(wire)
    return wire


def decode_audit_calls(wire: Any) -> tuple[AuditCall, ...]:
    """Strictly decode fresh per-call records without replay or union."""
    _framing(wire, AUDIT_RECORD_TYPE, _KEYS)
    _serialize(wire)
    return tuple(
        _decode_call(row) for row in _array_rows(wire["calls"], "audit calls", length=_CALL_LENGTH)
    )
