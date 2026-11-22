"""Unused private section, reference and recount helpers for static-graph admission.

This module is the C4a preparation seam only.  It decodes the already-merged
static sections through their public codecs, checks the producer's outer-key and
deduplicated-subrow uniqueness, checks source-map/warp/floor references and
recomputes the exact eight diagnostic counters.  It deliberately exports no
``AdmittedStaticGraph`` and no complete ``admit_static_graph`` API, holds no
corpus section and builds no graph or runtime value.

Uniqueness has two levels.  Outer container keys are unique because the owning
producers assign or ``setdefault`` them.  Sequential subrows stay ordered and
are required duplicate-free only where the producer itself collapses them with
``dict.fromkeys`` (gates, toggles, obstacles, elevators, ferries, warp flags and
the per-map metatile pair set).  Trainer, landing, Seagallop and audit rows are
ordered source rows; duplicates are preserved rather than invented as errors.
Seagallop selectors are unique because the source range generates them.

Reference checks are source-side only: every source map key must exist, the
warp-indexed families must name an actual source warp, and an elevator floor key
must exist because its producer already filters unknown floors.  Seagallop,
ferry and warp destinations (and ``opened_flag``) are deliberately not checked
for map/flag existence; they stay rows and are refused later at traversal.

No navigation module is imported: ``navigation/__init__.py`` eagerly loads the
graph.  Ordinary invalid input becomes :class:`AdmissionError`; ``MemoryError``
and ``SystemExit`` are not intercepted.  Input mutation after decode cannot
change the decoded owning facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from pokegent.games.gen3.firered.maps.map_schema import MapState
from pokegent.games.gen3.firered.rom.static_artifact_loader import (
    MAX_COUNT_VALUE,
    MAX_INTERPRETATIONS,
    MAX_MAPS,
)
from pokegent.games.gen3.firered.rom.static_audit_codec import (
    AuditCall,
    decode_audit_calls,
)
from pokegent.games.gen3.firered.rom.static_effect_index_codec import (
    StaticEffectFacts,
    decode_effect_facts,
)
from pokegent.games.gen3.firered.rom.static_index_codec import (
    StaticIndexFacts,
    decode_index_facts,
)
from pokegent.games.gen3.firered.rom.static_map_codec import decode_static_map
from pokegent.games.gen3.firered.rom.static_route_index_codec import (
    StaticRouteFacts,
    decode_route_facts,
)
from pokegent.games.gen3.firered.rom.static_symbolic_codec import CodecError
from pokegent.games.gen3.firered.rom.static_transport_codec import (
    StaticTransportFacts,
    decode_transport_facts,
)

__all__: tuple[str, ...] = ()

_SECTION_KEYS = frozenset({"maps", "index", "route", "effect", "transport", "audit"})
_COUNT_FIELDS = (
    "maps",
    "warp_rows",
    "gate_rows",
    "obstacle_rows",
    "audit_calls",
    "corpus_calls",
    "elevator_rows",
    "ferry_rows",
)
_MapKey = tuple[int, int]


class AdmissionError(ValueError):
    """Invalid static-graph section facts; no partial admitted value exists."""


@dataclass(frozen=True)
class _SectionFacts:
    """Decoded static sections; private facts, not a complete admitted graph."""

    maps: tuple[MapState, ...]
    index: StaticIndexFacts
    route: StaticRouteFacts
    effect: StaticEffectFacts
    transport: StaticTransportFacts
    audit: tuple[AuditCall, ...]

    def __post_init__(self) -> None:
        if type(self.maps) is not tuple:
            raise AdmissionError("maps must be an exact tuple")
        if len(self.maps) > MAX_MAPS:
            raise AdmissionError("section maps exceed the map ceiling")
        for state in self.maps:
            if type(state) is not MapState:
                raise AdmissionError("maps must hold exact MapState values")
        for name, owner in (
            ("index", StaticIndexFacts),
            ("route", StaticRouteFacts),
            ("effect", StaticEffectFacts),
            ("transport", StaticTransportFacts),
        ):
            if type(getattr(self, name)) is not owner:
                raise AdmissionError(f"{name} must be an exact {owner.__name__}")
        if type(self.audit) is not tuple:
            raise AdmissionError("audit must be an exact tuple")
        for call in self.audit:
            if type(call) is not AuditCall:
                raise AdmissionError("audit must hold exact AuditCall values")


@dataclass(frozen=True)
class _AdmissionCounts:
    """The exact eight diagnostic counters; supplied counts are never authority."""

    maps: int
    warp_rows: int
    gate_rows: int
    obstacle_rows: int
    audit_calls: int
    corpus_calls: int
    elevator_rows: int
    ferry_rows: int

    def __post_init__(self) -> None:
        for name in _COUNT_FIELDS:
            value = getattr(self, name)
            maximum = MAX_INTERPRETATIONS if name == "corpus_calls" else MAX_COUNT_VALUE
            if type(value) is not int or not 0 <= value <= maximum:
                raise AdmissionError(f"count {name} must be an exact bounded int")


def _exact_keys(wire: Any, expected: frozenset[str], label: str) -> None:
    if type(wire) is not dict:
        raise AdmissionError(f"{label} must be an exact mapping")
    if len(wire) != len(expected):
        raise AdmissionError(f"{label} keys mismatch")
    for key in wire:
        if type(key) is not str or key not in expected:
            raise AdmissionError(f"{label} keys mismatch")


def _decode(wire: Any, decoder: Any, label: str) -> Any:
    """Run one owning codec; only its ordinary input error becomes admission's."""
    try:
        return decoder(wire)
    except CodecError as exc:
        raise AdmissionError(f"{label} refused: {exc}") from None


def _decode_sections(wire: Any) -> _SectionFacts:
    """Decode the exact C4a section frame through the merged public codecs."""
    _exact_keys(wire, _SECTION_KEYS, "static graph sections")
    raw_maps = wire["maps"]
    if type(raw_maps) is not list:
        raise AdmissionError("section maps must be an exact array")
    if len(raw_maps) > MAX_MAPS:
        raise AdmissionError("section maps exceed the map ceiling")
    maps = tuple(_decode(raw, decode_static_map, "static map") for raw in raw_maps)
    return _SectionFacts(
        maps=maps,
        index=_decode(wire["index"], decode_index_facts, "primitive index"),
        route=_decode(wire["route"], decode_route_facts, "route index"),
        effect=_decode(wire["effect"], decode_effect_facts, "effect index"),
        transport=_decode(wire["transport"], decode_transport_facts, "transport index"),
        audit=_decode(wire["audit"], decode_audit_calls, "audit calls"),
    )


def _unique(keys: Iterable[Any], label: str) -> None:
    seen = set()
    for key in keys:
        if key in seen:
            raise AdmissionError(f"duplicate {label} key")
        seen.add(key)


def _map_key(state: MapState) -> _MapKey:
    return (state.identity.group, state.identity.number)


def _check_unique_keys(facts: _SectionFacts) -> None:
    """Enforce outer keys and only the subrows the actual producer deduplicates."""
    index = facts.index
    route = facts.route
    effect = facts.effect
    transport = facts.transport

    _unique((_map_key(state) for state in facts.maps), "map")
    _unique((flag for flag, _item in index.flag_items), "flag item")
    _unique((key for key, _flag in index.on_load_warp_flags), "on_load warp flag")
    _unique((key for key, _rows in index.on_load_barriers), "on_load barrier map")
    for key, entries in index.on_load_barriers:
        _unique((xy for xy, _requirements in entries), f"on_load barrier {key}")
    _unique((key for key, _label in index.on_load_barrier_labels), "barrier label")
    _unique((key for key, _value in index.script_warp_gates), "script warp gate")

    _unique((key for key, _rows in route.gates), "gate map")
    for key, rows in route.gates:
        _unique(rows, f"gate row {key}")
    _unique((key for key, _requirement in route.gate_requirements), "gate requirement")
    _unique((key for key, _rows in route.script_toggles), "toggle map")
    for key, rows in route.script_toggles:
        _unique(rows, f"toggle row {key}")
    _unique((flag for flag, _rows in route.trainers_by_flag), "trainer flag")
    _unique((key for key, _value in route.script_door_gates), "script door gate")
    _unique((key for key, _rows in route.on_frame_landings), "landing map")
    _unique((selector for selector, _destination in route.seagallop_warps), "seagallop")

    _unique((key for key, _rows in effect.script_obstacles), "obstacle map")
    for key, rows in effect.script_obstacles:
        _unique(rows, f"obstacle row {key}")
    _unique((key for key, _effects in effect.on_load_warp_openings), "warp opening")
    _unique((key for key, _rows in effect.on_load_metatiles), "metatile map")
    for key, rows in effect.on_load_metatiles:
        _unique(rows, f"metatile row {key}")

    _unique((key for key, _rows in transport.elevators), "elevator map")
    for key, rows in transport.elevators:
        _unique(rows, f"elevator row {key}")
    _unique((source for source, _rows in transport.ferries), "ferry source")
    for source, rows in transport.ferries:
        _unique(rows, f"ferry row {source}")


def _source_map(map_by_key: dict[_MapKey, MapState], key: Any, label: str) -> MapState:
    state = map_by_key.get(key)
    if state is None:
        raise AdmissionError(f"{label} source map is absent from maps")
    return state


def _source_warp(
    map_by_key: dict[_MapKey, MapState], key: _MapKey, warp_index: int, label: str
) -> None:
    state = _source_map(map_by_key, key, label)
    if warp_index not in {warp.index for warp in state.warps}:
        raise AdmissionError(f"{label} source warp index is absent from the map")


def _check_references(facts: _SectionFacts) -> None:
    """Check source-side maps, warp indexes and elevator floors only."""
    map_by_key = {_map_key(state): state for state in facts.maps}
    index = facts.index
    route = facts.route
    effect = facts.effect
    transport = facts.transport

    for key, _flag in index.on_load_warp_flags:
        _source_warp(map_by_key, key[0], key[1], "on_load warp flag")
    for key, _rows in index.on_load_barriers:
        _source_map(map_by_key, key, "on_load barrier")
    for key, _label in index.on_load_barrier_labels:
        _source_map(map_by_key, key[0], "barrier label")
    for key, _value in index.script_warp_gates:
        _source_warp(map_by_key, key[0], key[1], "script warp gate")

    for key, _rows in route.gates:
        _source_map(map_by_key, key, "gate")
    for key, _requirement in route.gate_requirements:
        _source_map(map_by_key, key[0], "gate requirement")
    for key, _rows in route.script_toggles:
        _source_map(map_by_key, key, "toggle")
    for _flag, rows in route.trainers_by_flag:
        for _trainer_flag, map_key, *_rest in rows:
            _source_map(map_by_key, map_key, "trainer")
    for key, _value in route.script_door_gates:
        _source_warp(map_by_key, key[0], key[1], "script door gate")
    for key, _rows in route.on_frame_landings:
        _source_map(map_by_key, key, "landing")

    for key, _rows in effect.script_obstacles:
        _source_map(map_by_key, key, "obstacle")
    for key, _effects in effect.on_load_warp_openings:
        _source_warp(map_by_key, key[0], key[1], "warp opening")
    for key, _rows in effect.on_load_metatiles:
        _source_map(map_by_key, key, "metatile")

    for key, rows in transport.elevators:
        _source_map(map_by_key, key, "elevator")
        for row in rows:
            floor_key = row[4]
            if floor_key not in map_by_key:
                raise AdmissionError("elevator floor key is absent from maps")
    for source, _rows in transport.ferries:
        _source_map(map_by_key, (source[0], source[1]), "ferry")

    for call in facts.audit:
        _source_map(map_by_key, call.map_key, "audit")


def _recount(facts: _SectionFacts, *, corpus_calls: int) -> _AdmissionCounts:
    """Recompute all eight counters from decoded facts; corpus row count is explicit."""
    if type(corpus_calls) is not int or not 0 <= corpus_calls <= MAX_INTERPRETATIONS:
        raise AdmissionError("corpus_calls must be an exact bounded int")
    return _AdmissionCounts(
        maps=len(facts.maps),
        warp_rows=sum(len(state.warps) for state in facts.maps),
        gate_rows=sum(len(rows) for _key, rows in facts.route.gates),
        obstacle_rows=sum(len(rows) for _key, rows in facts.effect.script_obstacles),
        audit_calls=len(facts.audit),
        corpus_calls=corpus_calls,
        elevator_rows=sum(len(rows) for _key, rows in facts.transport.elevators),
        ferry_rows=sum(len(rows) for _source, rows in facts.transport.ferries),
    )


def _decode_counts(wire: Any) -> _AdmissionCounts:
    """Validate exactly the eight payload counters; bool is never an int."""
    _exact_keys(wire, frozenset(_COUNT_FIELDS), "static graph counts")
    return _AdmissionCounts(**{name: wire[name] for name in _COUNT_FIELDS})


def _compare_counts(expected: _AdmissionCounts, supplied: _AdmissionCounts) -> None:
    """Refuse any supplied counter that differs from the recomputation."""
    if type(expected) is not _AdmissionCounts or type(supplied) is not _AdmissionCounts:
        raise AdmissionError("counts must be exact admission counters")
    for name in _COUNT_FIELDS:
        if getattr(expected, name) != getattr(supplied, name):
            raise AdmissionError(f"static graph count {name} mismatch")


def _admit_sections(wire: Any) -> _SectionFacts:
    """Private section admission: decode, unique keys and source references only."""
    facts = _decode_sections(wire)
    _check_unique_keys(facts)
    _check_references(facts)
    return facts
