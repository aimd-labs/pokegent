"""Unused strict codec for the three effect-derived static graph-index families.

Wire format (JSON-compatible; version and record type tags are fixed)::

    {"version": 1, "type": "static_effect_index",
     "script_obstacles": [[[group, number], [OBSTACLE...]]...],
     "on_load_warp_openings": [[[[group, number], warp_index],
                                 [effect...]]...],
     "on_load_metatiles": [[[group, number], [[[flag...], effect]...]]...]}

OBSTACLE is one complete owning :class:`FieldObstacle`; its KIND is one complete
owning :class:`ObstacleKind` with all eleven fields::

    [role, name, option_prefix, verb, requirement_key, requirement_kind,
     capability_kind, completion, label, prompt_key, irreversible]
    [KIND, source, local_id|null, bg_index|null, x, y, script_ptr|null,
     hide_flag|null, script_sets_hide_flag, script_starts_battle,
     requirement_label|null, [equivalent_requirement...], triggerable,
     declinable]

This is an effect-index subset, not a complete graph or snapshot envelope.  It
owns exactly the three tables built by ``_effect_script_obstacles``,
``_effect_on_load_warp_openings`` and ``_effect_on_load_metatiles``.  Facts are
ordered rows, so producer order, duplicate keys/rows and empty groups survive.
Obstacles are encoded field by field with the owning constructors instead of a
``KINDS`` registry name: script-derived kinds with ``role="script_gate"`` keep
their exact labels, requirement kind and all nullable fields.  Closed enum
values come from the owning ``Literal`` aliases.  ``Effect`` and nested
``Guard`` values reuse the existing tagged symbolic codec, so source, guards,
origin, decoded, reason and unresolved tagged values are never dropped or
normalised.  No completeness property is written.

No navigation module is imported (``navigation/__init__.py`` eagerly loads the
graph) and this module is not wired into the runtime.  Bounded framing,
serialization, map keys and scalar admission are reused from the static map,
index and route-index codecs; per-derived eligibility and references stay out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, get_args

from pokegent.games.gen3.firered.maps.obstacles import (
    Completion,
    FieldObstacle,
    ObstacleKind,
    RequirementKind,
    SourceKind,
)
from pokegent.games.gen3.firered.rom.script_effects import Effect
from pokegent.games.gen3.firered.rom.static_index_codec import (
    _array,
    _read_map_key,
    _string,
    _tuple,
    _wire_map_key,
)
from pokegent.games.gen3.firered.rom.static_map_codec import (
    _bool,
    _framing,
    _int,
    _serialize,
)
from pokegent.games.gen3.firered.rom.static_route_index_codec import (
    _read_table,
    _rows,
    _table,
)
from pokegent.games.gen3.firered.rom.static_symbolic_codec import (
    CodecError,
    decode_effect,
    encode_effect,
)

EFFECT_CODEC_VERSION = 1
EFFECT_RECORD_TYPE = "static_effect_index"

_MapKey = tuple[int, int]
_MetatileRow = tuple[tuple[int, ...], Effect]
_ObstacleRows = tuple[tuple[_MapKey, tuple[FieldObstacle, ...]], ...]
_WarpOpenings = tuple[tuple[tuple[_MapKey, int], tuple[Effect, ...]], ...]
_Metatiles = tuple[tuple[_MapKey, tuple[_MetatileRow, ...]], ...]

_KIND_FIELDS = (
    "role",
    "name",
    "option_prefix",
    "verb",
    "requirement_key",
    "requirement_kind",
    "capability_kind",
    "completion",
    "label",
    "prompt_key",
    "irreversible",
)
_OBSTACLE_FIELDS = (
    "kind",
    "source",
    "local_id",
    "bg_index",
    "x",
    "y",
    "script_ptr",
    "hide_flag",
    "script_sets_hide_flag",
    "script_starts_battle",
    "requirement_label",
    "equivalent_requirements",
    "triggerable",
    "declinable",
)
_KIND_LENGTH = len(_KIND_FIELDS)
_OBSTACLE_LENGTH = len(_OBSTACLE_FIELDS)
_METATILE_LENGTH = 2
_WARP_KEY_LENGTH = 2

_KEYS = frozenset(
    {
        "version",
        "type",
        "script_obstacles",
        "on_load_warp_openings",
        "on_load_metatiles",
    }
)
_REQUIREMENT_KINDS = frozenset(get_args(RequirementKind))
_COMPLETIONS = frozenset(get_args(Completion))
_SOURCE_KINDS = frozenset(get_args(SourceKind))


@dataclass(frozen=True)
class StaticEffectFacts:
    """The three effect families as recursively immutable ordered rows."""

    script_obstacles: _ObstacleRows
    on_load_warp_openings: _WarpOpenings
    on_load_metatiles: _Metatiles

    def __post_init__(self) -> None:
        for name in (
            "script_obstacles",
            "on_load_warp_openings",
            "on_load_metatiles",
        ):
            if type(getattr(self, name)) is not tuple:
                raise CodecError(f"{name} must be an exact tuple")


def _closed(value: Any, allowed: frozenset[str], label: str) -> str:
    if type(value) is not str or value not in allowed:
        raise CodecError(f"{label} is not a supported value")
    return value


def _optional_int(value: Any, label: str) -> int | None:
    return None if value is None else _int(value, label=label)


def _optional_string(value: Any, label: str) -> str | None:
    return None if value is None else _string(value, label)


def _encode_kind(kind: Any) -> list[Any]:
    if type(kind) is not ObstacleKind:
        raise CodecError("obstacle kind must be an exact ObstacleKind")
    return [
        _string(kind.role, "obstacle kind role"),
        _string(kind.name, "obstacle kind name"),
        _string(kind.option_prefix, "obstacle kind option_prefix"),
        _string(kind.verb, "obstacle kind verb"),
        _string(kind.requirement_key, "obstacle kind requirement_key"),
        _closed(kind.requirement_kind, _REQUIREMENT_KINDS, "obstacle kind requirement_kind"),
        _string(kind.capability_kind, "obstacle kind capability_kind"),
        _closed(kind.completion, _COMPLETIONS, "obstacle kind completion"),
        _string(kind.label, "obstacle kind label"),
        _string(kind.prompt_key, "obstacle kind prompt_key"),
        _bool(kind.irreversible, label="obstacle kind irreversible"),
    ]


def _decode_kind(raw: Any) -> ObstacleKind:
    row = _array(raw, "obstacle kind", length=_KIND_LENGTH)
    return ObstacleKind(
        role=_string(row[0], "obstacle kind role"),
        name=_string(row[1], "obstacle kind name"),
        option_prefix=_string(row[2], "obstacle kind option_prefix"),
        verb=_string(row[3], "obstacle kind verb"),
        requirement_key=_string(row[4], "obstacle kind requirement_key"),
        requirement_kind=_closed(
            row[5], _REQUIREMENT_KINDS, "obstacle kind requirement_kind"
        ),
        capability_kind=_string(row[6], "obstacle kind capability_kind"),
        completion=_closed(row[7], _COMPLETIONS, "obstacle kind completion"),
        label=_string(row[8], "obstacle kind label"),
        prompt_key=_string(row[9], "obstacle kind prompt_key"),
        irreversible=_bool(row[10], label="obstacle kind irreversible"),
    )


def _encode_obstacle(obstacle: Any) -> list[Any]:
    if type(obstacle) is not FieldObstacle:
        raise CodecError("obstacle must be an exact FieldObstacle")
    equivalent = _tuple(obstacle.equivalent_requirements, "obstacle equivalent_requirements")
    return [
        _encode_kind(obstacle.kind),
        _closed(obstacle.source, _SOURCE_KINDS, "obstacle source"),
        _optional_int(obstacle.local_id, "obstacle local_id"),
        _optional_int(obstacle.bg_index, "obstacle bg_index"),
        _int(obstacle.x, label="obstacle x"),
        _int(obstacle.y, label="obstacle y"),
        _optional_int(obstacle.script_ptr, "obstacle script_ptr"),
        _optional_int(obstacle.hide_flag, "obstacle hide_flag"),
        _bool(obstacle.script_sets_hide_flag, label="obstacle script_sets_hide_flag"),
        _bool(obstacle.script_starts_battle, label="obstacle script_starts_battle"),
        _optional_string(obstacle.requirement_label, "obstacle requirement_label"),
        [_string(item, "obstacle equivalent requirement") for item in equivalent],
        _bool(obstacle.triggerable, label="obstacle triggerable"),
        _bool(obstacle.declinable, label="obstacle declinable"),
    ]


def _decode_obstacle(raw: Any) -> FieldObstacle:
    row = _array(raw, "obstacle", length=_OBSTACLE_LENGTH)
    return FieldObstacle(
        kind=_decode_kind(row[0]),
        source=_closed(row[1], _SOURCE_KINDS, "obstacle source"),
        local_id=_optional_int(row[2], "obstacle local_id"),
        bg_index=_optional_int(row[3], "obstacle bg_index"),
        x=_int(row[4], label="obstacle x"),
        y=_int(row[5], label="obstacle y"),
        script_ptr=_optional_int(row[6], "obstacle script_ptr"),
        hide_flag=_optional_int(row[7], "obstacle hide_flag"),
        script_sets_hide_flag=_bool(row[8], label="obstacle script_sets_hide_flag"),
        script_starts_battle=_bool(row[9], label="obstacle script_starts_battle"),
        requirement_label=_optional_string(row[10], "obstacle requirement_label"),
        equivalent_requirements=tuple(
            _string(item, "obstacle equivalent requirement")
            for item in _array(row[11], "obstacle equivalent_requirements")
        ),
        triggerable=_bool(row[12], label="obstacle triggerable"),
        declinable=_bool(row[13], label="obstacle declinable"),
    )


def _encode_script_obstacles(rows: Any) -> list[list[Any]]:
    result = []
    for key, entries in _rows(rows, "script_obstacles", 2):
        result.append(
            [
                _wire_map_key(key, "obstacle map"),
                [
                    _encode_obstacle(obstacle)
                    for obstacle in _tuple(entries, "obstacle entries")
                ],
            ]
        )
    return result


def _decode_script_obstacles(raw: Any) -> _ObstacleRows:
    result = []
    for row in _array(raw, "script_obstacles"):
        row = _array(row, "obstacle table row", length=2)
        result.append(
            (
                _read_map_key(row[0], "obstacle map"),
                tuple(
                    _decode_obstacle(obstacle)
                    for obstacle in _array(row[1], "obstacle entries")
                ),
            )
        )
    return tuple(result)


def _encode_warp_openings(rows: Any) -> list[list[Any]]:
    result = []
    for row in _rows(rows, "on_load_warp_openings", 2):
        key = _tuple(row[0], "warp opening key", length=_WARP_KEY_LENGTH)
        effects = _tuple(row[1], "warp opening effects")
        result.append(
            [
                [
                    _wire_map_key(key[0], "warp opening map"),
                    _int(key[1], label="warp opening index"),
                ],
                [encode_effect(effect) for effect in effects],
            ]
        )
    return result


def _decode_warp_openings(raw: Any) -> _WarpOpenings:
    result = []
    for row in _array(raw, "on_load_warp_openings"):
        row = _array(row, "warp opening row", length=2)
        key = _array(row[0], "warp opening key", length=_WARP_KEY_LENGTH)
        effects = _array(row[1], "warp opening effects")
        result.append(
            (
                (
                    _read_map_key(key[0], "warp opening map"),
                    _int(key[1], label="warp opening index"),
                ),
                tuple(decode_effect(effect) for effect in effects),
            )
        )
    return tuple(result)


def _encode_metatile_row(row: Any) -> list[Any]:
    pair = _tuple(row, "metatile row", length=_METATILE_LENGTH)
    flags = _tuple(pair[0], "metatile flags")
    return [
        [_int(flag, label="metatile flag") for flag in flags],
        encode_effect(pair[1]),
    ]


def _decode_metatile_row(raw: Any) -> _MetatileRow:
    row = _array(raw, "metatile row", length=_METATILE_LENGTH)
    return (
        tuple(
            _int(flag, label="metatile flag")
            for flag in _array(row[0], "metatile flags")
        ),
        decode_effect(row[1]),
    )


def _encode_metatiles(rows: Any) -> list[list[Any]]:
    return _table(rows, "on_load_metatiles", "metatile map", "metatile entries",
                  _METATILE_LENGTH, _encode_metatile_row)


def _decode_metatiles(raw: Any) -> _Metatiles:
    return _read_table(raw, "on_load_metatiles", "metatile map", "metatile entries",
                       _METATILE_LENGTH, _decode_metatile_row)


def encode_effect_facts(facts: StaticEffectFacts) -> dict[str, Any]:
    """Encode exactly the three effect families in producer order."""
    if type(facts) is not StaticEffectFacts:
        raise CodecError("expected exact StaticEffectFacts")
    wire = {
        "version": EFFECT_CODEC_VERSION,
        "type": EFFECT_RECORD_TYPE,
        "script_obstacles": _encode_script_obstacles(facts.script_obstacles),
        "on_load_warp_openings": _encode_warp_openings(facts.on_load_warp_openings),
        "on_load_metatiles": _encode_metatiles(facts.on_load_metatiles),
    }
    _serialize(wire)
    return wire


def decode_effect_facts(wire: Any) -> StaticEffectFacts:
    """Strictly decode one effect-index subset into fresh owning rows."""
    _framing(wire, EFFECT_RECORD_TYPE, _KEYS)
    _serialize(wire)
    return StaticEffectFacts(
        script_obstacles=_decode_script_obstacles(wire["script_obstacles"]),
        on_load_warp_openings=_decode_warp_openings(wire["on_load_warp_openings"]),
        on_load_metatiles=_decode_metatiles(wire["on_load_metatiles"]),
    )
