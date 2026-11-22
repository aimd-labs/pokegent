"""Synthetic tests for the unused explicit offline static-graph builder."""

from __future__ import annotations

import copy
import json
import struct
from types import SimpleNamespace

import pytest

from pokegent.games.gen3.firered.maps.map_schema import (
    BgEventInfo,
    MapGrid,
    MapIdentity,
    MapLayoutInfo,
    MapState,
)
from pokegent.games.gen3.firered.rom import static_artifact_loader as loader
from pokegent.games.gen3.firered.rom import static_artifact_writer as writer
from pokegent.games.gen3.firered.rom import static_graph_builder as builder
from pokegent.games.gen3.firered.rom.static_audit_codec import AUDIT_READERS
from pokegent.games.gen3.firered.rom.static_graph_admission import (
    AdmissionError,
    admit_static_graph,
)
from pokegent.games.gen3.firered.rom.static_graph_identity import StaticIdentityError
from pokegent.games.gen3.firered.rom.static_symbolic_codec import CodecError
from pokegent.navigation.graph import NavigationGraph

BASE = 0x08000000
ATTRIBUTES = ((5, 0x1A2),)
UNKNOWN_SCRIPT = b"\xfe\x00\x00"
COMPLETE_SCRIPT = b"\xa2" + struct.pack("<HHHH", 1, 2, 5, 0) + b"\x02"


def _map() -> MapState:
    return MapState(
        identity=MapIdentity(
            group=1,
            number=2,
            layout_id=1,
            region_section_id=1,
            map_type=1,
            map_type_name="town",
            weather=0,
            cave=0,
            floor_num=0,
            battle_type=0,
            music=1,
            biking_allowed=True,
            allow_escaping=True,
            allow_running=True,
            identity_verified=True,
        ),
        layout=MapLayoutInfo(
            width=2,
            height=2,
            border_width=1,
            border_height=1,
            border=(1,),
            primary_tileset=0x08000000,
            secondary_tileset=0x08000001,
            layout_ptr=0x08000002,
        ),
        grid=MapGrid(columns=16, rows=16, blocks=(1,) * 256, attributes=ATTRIBUTES),
        bg_events=(
            BgEventInfo(
                index=0,
                x=1,
                y=2,
                elevation=3,
                kind_id=0,
                kind="sign",
                sign_facing="S",
                hidden_item=None,
                script_ptr=BASE,
            ),
            BgEventInfo(
                index=1,
                x=3,
                y=4,
                elevation=3,
                kind_id=0,
                kind="sign",
                sign_facing=None,
                hidden_item=None,
                script_ptr=BASE + len(COMPLETE_SCRIPT),
            ),
        ),
    )


ROM = COMPLETE_SCRIPT + UNKNOWN_SCRIPT
STATE = _map()


class Provider:
    def __init__(self) -> None:
        self.reads: list[tuple[int, int]] = []

    def group_sizes(self) -> tuple[int, ...]:
        return (2,)

    def read_static(self, rom, profile, group, number):
        self.reads.append((group, number))
        if (group, number) == (0, 0):
            return SimpleNamespace(usable=True, state=STATE)
        return SimpleNamespace(usable=False, state=None)

    def behavior_info(self, value):
        raise AssertionError("no warp-opening path is expected in this fixture")


def _identity() -> dict:
    return {
        "schema_version": 1,
        "rom_sha1": "a" * 40,
        "rom_sha256": "b" * 64,
        "profile_sha256": "c" * 64,
        "profile_id": "synthetic",
        "generator_sha256": "d" * 64,
        "map_index_sha256": "e" * 64,
        "group_order": ["g0"],
        "group_sizes": [2],
        "grouping_sha256": "f" * 64,
        "puzzle_sha256": "1" * 64,
    }


def _install(monkeypatch, identity=None):
    identity = _identity() if identity is None else identity
    digest = loader._identity_digest(identity)
    monkeypatch.setattr(builder, "derive_identity", lambda *a, **k: (identity, digest))
    tables = SimpleNamespace(
        items=(SimpleNamespace(item_id=13, name="potion"),)
    )
    monkeypatch.setattr(
        builder.GameTables, "from_rom_image", classmethod(lambda cls, rom, profile: tables)
    )
    return identity, digest


def _build(monkeypatch):
    identity, digest = _install(monkeypatch)
    provider = Provider()
    return builder.build_static_graph_document(
        rom=ROM,
        profile=None,
        provider=provider,
        index_path="input.mapidx",
        puzzle_path=None,
    ), identity, digest, provider


def test_unused_explicit_builder_api_and_document(monkeypatch) -> None:
    observed: list[dict] = []
    calls = {"total": 0, "misses": 0}
    original_prepare = builder._prepare_payload
    original_interpret = NavigationGraph._script_interpretation

    def spy_interpret(self, *args, **kwargs):
        calls["total"] += 1
        before = len(self._effect_cache)
        result = original_interpret(self, *args, **kwargs)
        if len(self._effect_cache) > before:
            calls["misses"] += 1
        return result

    def spy_prepare(graph, identity, digest):
        observed.append(
            {
                "graph": graph,
                "capture_keys": tuple(graph._materialized),
                "cache_keys": tuple(graph._effect_cache),
                "capture_results": tuple(
                    id(result) for result, _reads in graph._materialized.values()
                ),
                "cache_results": tuple(
                    id(graph._effect_cache[key]) for key in graph._materialized
                ),
                "audit": tuple(graph._materialized_audit),
            }
        )
        return original_prepare(graph, identity, digest)

    monkeypatch.setattr(NavigationGraph, "_script_interpretation", spy_interpret)
    monkeypatch.setattr(builder, "_prepare_payload", spy_prepare)
    monkeypatch.setattr(
        NavigationGraph,
        "from_rom",
        classmethod(lambda cls, *a, **k: (_ for _ in ()).throw(AssertionError("cache path"))),
    )

    result, identity, digest, provider = _build(monkeypatch)

    assert provider.reads == [(0, 0), (0, 1)]
    assert result.identity == identity
    assert result.identity_sha256 == digest
    assert result.payload["identity_sha256"] == digest
    assert result.payload["version"] == 1
    assert result.payload["type"] == "static_graph"
    assert set(result.payload) == {
        "version", "type", "identity_sha256", "counts", "maps", "index",
        "route", "effect", "transport", "audit", "corpus",
    }
    assert result.counts["maps"] == 1
    assert result.counts["corpus_calls"] == 2
    assert result.counts["audit_calls"] > 0

    seen = observed[0]
    assert calls["total"] > calls["misses"] == 2
    assert set(seen["capture_keys"]) == set(seen["cache_keys"])
    assert seen["capture_keys"] == seen["cache_keys"]
    assert seen["capture_results"] == seen["cache_results"]
    assert seen["graph"]._materialized is None
    assert seen["graph"]._materialized_audit is None

    admitted = admit_static_graph(result.payload, identity=identity)
    assert dict(admitted.counts) == result.counts
    assert len(admitted.corpus) == 2
    assert {row.key[1] for row in admitted.corpus} == {(1, 2)}
    assert any(not row.result.complete for row in admitted.corpus)
    assert admitted.audit
    assert {call.reader for call in admitted.audit} <= set(AUDIT_READERS)
    complete_rows = [
        row for row in admitted.corpus if row.result.complete
    ]
    assert complete_rows and complete_rows[0].behavior_reads == ((5, 0x1A2),)
    assert all(
        isinstance(read[0], int) and isinstance(read[1], int)
        for read in complete_rows[0].behavior_reads
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.pop("corpus"),
        lambda payload: payload["counts"].update({"gate_rows": 1}),
        lambda payload: payload["counts"].update({"corpus_calls": 1}),
        lambda payload: payload.update({"audit": []}),
    ],
)
def test_tampered_or_malformed_documents_are_refused(monkeypatch, mutate) -> None:
    result, identity, _digest, _provider = _build(monkeypatch)
    payload = copy.deepcopy(result.payload)
    mutate(payload)
    with pytest.raises(AdmissionError):
        admit_static_graph(payload, identity=identity)


def test_identity_refusal_happens_before_reading_inputs(monkeypatch) -> None:
    _install(monkeypatch)

    def refuse(*args, **kwargs):
        raise StaticIdentityError("synthetic identity refusal")

    monkeypatch.setattr(builder, "derive_identity", refuse)
    provider = Provider()
    with pytest.raises(StaticIdentityError, match="synthetic identity refusal"):
        builder.build_static_graph_document(
            rom=ROM,
            profile=None,
            provider=provider,
            index_path="input.mapidx",
            puzzle_path=None,
        )
    assert provider.reads == []


def test_collectors_are_detached_when_ordinary_build_fails(monkeypatch) -> None:
    _install(monkeypatch)
    graphs: list[NavigationGraph] = []
    original_interpret = NavigationGraph._script_interpretation

    def remember(self, *args, **kwargs):
        graphs.append(self)
        return original_interpret(self, *args, **kwargs)

    def refuse(_calls):
        raise CodecError("synthetic audit refusal")

    monkeypatch.setattr(NavigationGraph, "_script_interpretation", remember)
    monkeypatch.setattr(builder, "encode_audit_calls", refuse)
    with pytest.raises(builder.BuildError, match="encoding refused"):
        builder.build_static_graph_document(
            rom=ROM,
            profile=None,
            provider=Provider(),
            index_path="input.mapidx",
            puzzle_path=None,
        )
    assert graphs
    assert all(graph._materialized is None for graph in graphs)
    assert all(graph._materialized_audit is None for graph in graphs)


def test_one_writer_loader_admission_roundtrip(monkeypatch, tmp_path) -> None:
    result, identity, _digest, _provider = _build(monkeypatch)
    directory = tmp_path / result.identity_sha256
    receipt = writer.build_static_artifact(
        directory,
        identity,
        result.payload,
        counts=result.counts,
        built_utc="2026-11-22T00:00:00Z",
    )
    assert receipt == directory / "receipt.json"

    loaded = loader.load_verified_document(directory, identity)
    assert loaded == result.payload
    assert json.loads((directory / receipt.name).read_text())["payload_sha256"]

    admitted = admit_static_graph(loaded, identity=identity)
    assert dict(admitted.counts) == result.counts
    assert len(admitted.maps) == 1
    assert admitted.maps[0] is not STATE
    assert len(admitted.corpus) == 2
    assert len(admitted.audit) == result.counts["audit_calls"]
    assert dict(admitted.index.__dict__) == dict(
        admit_static_graph(result.payload, identity=identity).index.__dict__
    )
