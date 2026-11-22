"""Unused offline builder for one complete static-graph v1 document.

The builder derives the eleven-key identity from explicit caller inputs, reads
the provider's static maps exactly as ``NavigationGraph.from_rom`` does (without
its process cache), constructs a fresh real :class:`NavigationGraph` with the
default-off materialization collectors enabled, and encodes only the fields that
constructor actually authored.

The script corpus is the constructor's own cache-miss capture: its keys equal
the fresh graph's ``_effect_cache`` key set and its results are those owning
interpretations.  Audit rows are the constructor's actual incomplete
``_complete_effects`` calls in call order.  Both collectors are detached before
the result escapes, so neither success nor an ordinary build failure retains a
capturing graph.

This module is not imported by the runtime and provides no CLI, install path,
``from_rom`` switch, cache, or seal wrapper.  Publication stays the existing
caller-driven ``build_static_artifact``.  Synthetic tests do not prove real-ROM
corpus coverage, capacity or correct-analysis restoration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pokegent.games.gen3.firered.rom import static_artifact_loader as loader
from pokegent.games.gen3.firered.rom import static_artifact_writer as writer
from pokegent.games.gen3.firered.rom.rom_tables import GameTables
from pokegent.games.gen3.firered.rom.static_audit_codec import encode_audit_calls
from pokegent.games.gen3.firered.rom.static_effect_index_codec import (
    StaticEffectFacts,
    encode_effect_facts,
)
from pokegent.games.gen3.firered.rom.static_graph_admission import (
    AdmissionError,
    admit_static_graph,
)
from pokegent.games.gen3.firered.rom.static_graph_identity import derive_identity
from pokegent.games.gen3.firered.rom.static_index_codec import (
    StaticIndexFacts,
    encode_index_facts,
)
from pokegent.games.gen3.firered.rom.static_map_codec import (
    encode_interpretation_key,
    encode_static_map,
)
from pokegent.games.gen3.firered.rom.static_route_index_codec import (
    StaticRouteFacts,
    encode_route_facts,
)
from pokegent.games.gen3.firered.rom.static_symbolic_codec import (
    CodecError,
    encode_interpretation,
)
from pokegent.games.gen3.firered.rom.static_transport_codec import (
    StaticTransportFacts,
    encode_transport_facts,
)
from pokegent.navigation.graph import NavigationGraph

__all__ = ("BuildError", "BuiltStaticGraph", "build_static_graph_document")

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


class BuildError(ValueError):
    """The explicit inputs cannot produce one complete static-graph document."""


@dataclass(frozen=True)
class BuiltStaticGraph:
    """Caller-prepared identity, payload and independently recounted metadata."""

    identity: dict[str, Any]
    identity_sha256: str
    payload: dict[str, Any]
    counts: dict[str, int]


def _node_row(node: Any) -> tuple[int, int, int, int, int]:
    return (node.map_group, node.map_number, node.x, node.y, node.elevation)


def _toggle_row(toggle: Any) -> tuple[int, int, int, str | None, int, int | None]:
    return (
        toggle.x,
        toggle.y,
        toggle.flag,
        toggle.facing,
        toggle.event_index,
        toggle.local_id,
    )


def _trainer_row(trainer: Any) -> tuple[int, tuple[int, int], int, int, int]:
    return (
        trainer.flag,
        trainer.map_key,
        trainer.local_id,
        trainer.x,
        trainer.y,
    )


def _ferry_row(route: Any) -> tuple[Any, ...]:
    return (
        _node_row(route.destination),
        route.guards,
        route.selector,
        route.local_id,
        route.coord,
        route.direction,
        route.menu_index,
        route.menu_id,
        route.menu_pc,
        route.menu_pcs,
    )


def _index_facts(graph: NavigationGraph) -> StaticIndexFacts:
    return StaticIndexFacts(
        flag_items=tuple(graph._flag_items.items()),
        on_load_warp_flags=tuple(graph._on_load_warp_flags.items()),
        on_load_barriers=tuple(
            (
                key,
                tuple((xy, tuple(requirements)) for xy, requirements in rows.items()),
            )
            for key, rows in graph._on_load_barriers.items()
        ),
        on_load_barrier_labels=tuple(graph._on_load_barrier_labels.items()),
        script_warp_gates=tuple(graph._script_warp_gates.items()),
    )


def _route_facts(graph: NavigationGraph) -> StaticRouteFacts:
    return StaticRouteFacts(
        gates=tuple((key, tuple(rows)) for key, rows in graph._gates.items()),
        gate_requirements=tuple(graph._gate_requirements.items()),
        script_toggles=tuple(
            (key, tuple(_toggle_row(row) for row in rows))
            for key, rows in graph._script_toggles.items()
        ),
        trainers_by_flag=tuple(
            (flag, tuple(_trainer_row(row) for row in rows))
            for flag, rows in graph._trainers_by_flag.items()
        ),
        script_door_gates=tuple(graph._script_door_gates.items()),
        on_frame_landings=tuple(graph._on_frame_landings.items()),
        seagallop_warps=tuple(graph._seagallop_warps),
    )


def _effect_facts(graph: NavigationGraph) -> StaticEffectFacts:
    return StaticEffectFacts(
        script_obstacles=tuple(
            (key, tuple(rows)) for key, rows in graph._script_obstacles.items()
        ),
        on_load_warp_openings=tuple(
            (key, tuple(rows)) for key, rows in graph._on_load_warp_openings.items()
        ),
        on_load_metatiles=tuple(
            (key, tuple(rows)) for key, rows in graph._on_load_metatiles.items()
        ),
    )


def _transport_facts(graph: NavigationGraph) -> StaticTransportFacts:
    return StaticTransportFacts(
        elevators=tuple((key, tuple(rows)) for key, rows in graph._elevators.items()),
        ferries=tuple(
            (
                _node_row(source),
                tuple(_ferry_row(route) for route in routes),
            )
            for source, routes in graph._ferries.items()
        ),
    )


def _corpus_wire(graph: NavigationGraph) -> dict[str, Any]:
    capture = graph._materialized
    if capture is None:
        raise BuildError("materialization collectors are not installed")
    if set(capture) != set(graph._effect_cache):
        raise BuildError("captured corpus keys do not equal the actual effect cache keys")
    rows = []
    for key, (result, reads) in capture.items():
        if graph._effect_cache.get(key) is not result:
            raise BuildError("captured corpus result is not the owning cache result")
        rows.append(
            {
                "key": encode_interpretation_key(key),
                "result": encode_interpretation(result),
                "behavior_reads": [[metatile, behavior] for metatile, behavior in reads],
            }
        )
    return {"version": 1, "type": "script_corpus", "rows": rows}


def _counts(graph: NavigationGraph, audit: tuple[Any, ...], corpus_rows: int) -> dict[str, int]:
    return {
        "maps": len(graph.maps),
        "warp_rows": sum(len(state.warps) for state in graph.maps.values()),
        "gate_rows": sum(len(rows) for rows in graph._gates.values()),
        "obstacle_rows": sum(len(rows) for rows in graph._script_obstacles.values()),
        "audit_calls": len(audit),
        "corpus_calls": corpus_rows,
        "elevator_rows": sum(len(rows) for rows in graph._elevators.values()),
        "ferry_rows": sum(len(rows) for rows in graph._ferries.values()),
    }


def _prepare_payload(
    graph: NavigationGraph, identity: dict[str, Any], digest: str
) -> tuple[dict[str, Any], dict[str, int]]:
    capture = graph._materialized
    audit_source = graph._materialized_audit
    if capture is None or audit_source is None:
        raise BuildError("materialization collectors are not installed")
    audit = tuple(audit_source)
    try:
        corpus = _corpus_wire(graph)
        payload = {
            "version": 1,
            "type": "static_graph",
            "identity_sha256": digest,
            "counts": _counts(graph, audit, len(corpus["rows"])),
            "maps": [encode_static_map(state) for state in graph.maps.values()],
            "index": encode_index_facts(_index_facts(graph)),
            "route": encode_route_facts(_route_facts(graph)),
            "effect": encode_effect_facts(_effect_facts(graph)),
            "transport": encode_transport_facts(_transport_facts(graph)),
            "audit": encode_audit_calls(audit),
            "corpus": corpus,
        }
    except CodecError as exc:
        raise BuildError(f"static graph encoding refused: {exc}") from None

    counts = payload["counts"]
    if type(counts) is not dict or set(counts) != set(_COUNT_FIELDS):
        raise BuildError("independent counts do not cover the exact eight fields")
    try:
        raw = writer._payload_bytes(payload)
        loader._parse_json(raw, "payload")
    except (writer.ArtifactWriteError, loader.ArtifactLoadError) as exc:
        raise BuildError(f"bounded document preparation refused: {exc}") from None
    try:
        admitted = admit_static_graph(payload, identity=identity)
    except AdmissionError as exc:
        raise BuildError(f"self-admission refused the document: {exc}") from None
    if dict(admitted.counts) != counts:
        raise BuildError("self-admission recounted different counts")
    return payload, counts


def build_static_graph_document(
    *,
    rom: bytes,
    profile: Any,
    provider: Any,
    index_path: Any,
    puzzle_path: Any,
) -> BuiltStaticGraph:
    """Build, independently recount and self-admit one complete v1 document.

    ``rom``, ``profile``, ``provider``, ``index_path`` and ``puzzle_path`` are
    explicit existing inputs; no resolver or process graph cache is consulted.
    Unusable static maps are not invented: the payload covers the usable subset
    the provider returns and its counts state that exact coverage.  The caller
    still owns identity trust, publication and real-corpus evidence.
    """
    portable, digest = derive_identity(
        rom,
        profile,
        provider,
        index_path=index_path,
        puzzle_path=puzzle_path,
    )
    group_sizes = tuple(provider.group_sizes())
    maps: dict[tuple[int, int], Any] = {}
    for group, size in enumerate(group_sizes):
        for number in range(size):
            result = provider.read_static(rom, profile, group, number)
            if result.usable and result.state is not None:
                maps[(group, number)] = result.state
    tables = GameTables.from_rom_image(rom, profile)
    item_names = {item.item_id: item.name.title() for item in tables.items}
    graph = NavigationGraph(
        maps,
        rom=rom,
        provider=provider,
        item_names=item_names,
        profile=profile,
        _materialize=True,
    )
    try:
        payload, counts = _prepare_payload(graph, portable, digest)
    finally:
        graph._materialized = None
        graph._materialized_audit = None
    return BuiltStaticGraph(
        identity=portable,
        identity_sha256=digest,
        payload=payload,
        counts=counts,
    )
