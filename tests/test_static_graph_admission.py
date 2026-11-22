"""Focused synthetic tests for the unused private graph-admission section helpers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from pokegent.games.gen3.firered.maps.map_schema import (
    MapGrid,
    MapIdentity,
    MapLayoutInfo,
    MapState,
    WarpInfo,
)
from pokegent.games.gen3.firered.maps.obstacles import script_gate
from pokegent.games.gen3.firered.rom import static_artifact_loader as loader
from pokegent.games.gen3.firered.rom import static_graph_admission as admission
from pokegent.games.gen3.firered.rom.script_effects import Effect, Interpretation
from pokegent.games.gen3.firered.rom.static_artifact_loader import (
    MAX_INTERPRETATIONS,
    MAX_MAPS,
)
from pokegent.games.gen3.firered.rom.static_audit_codec import AuditCall, encode_audit_calls
from pokegent.games.gen3.firered.rom.static_effect_index_codec import (
    StaticEffectFacts,
    encode_effect_facts,
)
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
    MAX_CONTAINER_LENGTH,
    encode_interpretation,
)
from pokegent.games.gen3.firered.rom.static_transport_codec import (
    StaticTransportFacts,
    encode_transport_facts,
)

ROOT = Path(__file__).resolve().parents[1]


def _map(
    group: int,
    number: int,
    indexes: tuple[int, ...] = (0, 1),
    attributes: tuple[tuple[int, int], ...] = ((1, 0),),
) -> MapState:
    warps = tuple(
        WarpInfo(
            index=index,
            x=index,
            y=0,
            elevation=3,
            dest_group=99,
            dest_number=99,
            dest_name=None,
            dest_warp_id=0,
            dynamic=False,
            tile_behavior=0,
            tile_kind=None,
            trigger=None,
            trigger_direction=None,
        )
        for index in indexes
    )
    return MapState(
        identity=MapIdentity(
            group=group,
            number=number,
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
        grid=MapGrid(columns=16, rows=16, blocks=(1,) * 256, attributes=attributes),
        warps=warps,
    )


def _effect(source: int = 1) -> Effect:
    return Effect(kind="setmetatile", source=source, guards=(), values=(("x", 1), ("y", 2)))


def _obstacle():
    return script_gate(x=3, y=4, requirement_key="item:13", name="Gatekeeper")


def _sections() -> dict:
    maps = [_map(0, 0), _map(1, 1, (0,))]
    index = StaticIndexFacts(
        flag_items=((0x120, 13),),
        on_load_warp_flags=((((0, 0), 1), 0x121),),
        on_load_barriers=((((0, 0), (((1, 2), ("flag:1", "flag:2")),)),)),
        on_load_barrier_labels=((((0, 0), (1, 2)), "label"),),
        script_warp_gates=((((0, 0), 0), ("flag:3", 4)),),
    )
    gate_row = (1, 2, 3, 4)
    gate_row_2 = (5, 6, 7, 8)
    toggle_row = (1, 2, 0x111, "N", 3, None)
    trainer_row = (0x500, (1, 1), 2, 3, 4)
    landing_row = (2, 3, ((0, 1), (0, 1)))
    elevator_row = (1, 2, 3, 4, (1, 1), 5, 6, None, None, 0, 0)
    metatile_row = ((0x100,), _effect())
    route = StaticRouteFacts(
        gates=(((0, 0), (gate_row, gate_row_2)),),
        gate_requirements=((((0, 0), 4), "flag:3"),),
        script_toggles=(((0, 0), (toggle_row,)),),
        trainers_by_flag=((0x500, (trainer_row, trainer_row)),),
        script_door_gates=((((0, 0), 1), ("flag:4", "label", ("flag:4",))),),
        on_frame_landings=(((1, 1), (landing_row, landing_row)),),
        seagallop_warps=((0, (1, 1, -1, 4, 5)), (1, (2, 2, -1, 6, 7))),
    )
    effect = StaticEffectFacts(
        script_obstacles=(((0, 0), (_obstacle(),)),),
        on_load_warp_openings=((((0, 0), 1), (_effect(),)),),
        on_load_metatiles=(((0, 0), (metatile_row,)),),
    )
    transport = StaticTransportFacts(
        elevators=(((0, 0), (elevator_row,)),),
        ferries=((_ferry_source(), (_ferry_row(),)),),
    )
    audit = (
        AuditCall("elevators:bg", (0, 0), (1, 2), 0x800000, ("A",)),
        AuditCall("elevators:bg", (0, 0), (1, 2), 0x800000, ("A",)),
    )
    return {
        "maps": [encode_static_map(state) for state in maps],
        "index": encode_index_facts(index),
        "route": encode_route_facts(route),
        "effect": encode_effect_facts(effect),
        "transport": encode_transport_facts(transport),
        "audit": encode_audit_calls(audit),
    }


def _ferry_source() -> tuple[int, int, int, int, int]:
    return (0, 0, 1, 2, 3)


def _ferry_row() -> tuple:
    return (
        (99, 99, 4, 5, 6),
        (),
        7,
        None,
        None,
        None,
        None,
        None,
        None,
        ((0, 1),),
    )


def _facts() -> admission._SectionFacts:
    return admission._decode_sections(_sections())


def _clone(wire: dict) -> dict:
    return json.loads(json.dumps(wire))


def test_public_surface_exports_only_complete_admission() -> None:
    assert set(admission.__all__) == {
        "AdmissionError",
        "AdmittedStaticGraph",
        "admit_static_graph",
    }
    assert not hasattr(admission, "admit_sections")


def test_decode_sections_roundtrips_every_merged_family() -> None:
    facts = _facts()
    assert [state.identity.number for state in facts.maps] == [0, 1]
    assert facts.index.flag_items == ((0x120, 13),)
    assert facts.route.seagallop_warps[0][1] == (1, 1, -1, 4, 5)
    assert facts.effect.on_load_metatiles[0][1][0][1] == _effect()
    assert facts.transport.ferries[0][1][0][0] == (99, 99, 4, 5, 6)
    assert [call.reasons for call in facts.audit] == [("A",), ("A",)]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda wire: wire.pop("audit"),
        lambda wire: wire.update({"corpus": []}),
        lambda wire: wire.update({"maps": tuple(wire["maps"])}),
        lambda wire: wire.update({"version": 1}),
    ],
)
def test_section_frame_requires_exact_keys(mutate) -> None:
    wire = _clone(_sections())
    mutate(wire)
    with pytest.raises(admission.AdmissionError):
        admission._decode_sections(wire)


def test_decoded_facts_are_isolated_from_later_wire_mutation() -> None:
    wire = _sections()
    facts = admission._decode_sections(wire)
    wire["maps"][0]["map"]["identity"]["group"] = 77
    wire["audit"]["calls"][0][0] = "ferries:npc"
    assert facts.maps[0].identity.group == 0
    assert facts.audit[0].reader == "elevators:bg"


def _change_index(facts: admission._SectionFacts, **changes) -> admission._SectionFacts:
    return replace(facts, index=replace(facts.index, **changes))


def _change_route(facts: admission._SectionFacts, **changes) -> admission._SectionFacts:
    return replace(facts, route=replace(facts.route, **changes))


def _change_effect(facts: admission._SectionFacts, **changes) -> admission._SectionFacts:
    return replace(facts, effect=replace(facts.effect, **changes))


def _change_transport(facts: admission._SectionFacts, **changes) -> admission._SectionFacts:
    return replace(facts, transport=replace(facts.transport, **changes))


def _duplicate_cases():
    base = _facts()
    idx = base.index
    route = base.route
    effect = base.effect
    transport = base.transport
    return {
        "maps": replace(base, maps=base.maps + (base.maps[0],)),
        "flag_items": _change_index(base, flag_items=idx.flag_items + (idx.flag_items[0],)),
        "warp_flags": _change_index(
            base, on_load_warp_flags=idx.on_load_warp_flags + (idx.on_load_warp_flags[0],)
        ),
        "barriers_outer": _change_index(
            base, on_load_barriers=idx.on_load_barriers + (((0, 0), ()),)
        ),
        "barriers_inner": _change_index(
            base,
            on_load_barriers=(((0, 0), (((1, 2), ()), ((1, 2), ()))),),
        ),
        "barrier_labels": _change_index(
            base, on_load_barrier_labels=idx.on_load_barrier_labels * 2
        ),
        "warp_gates": _change_index(base, script_warp_gates=idx.script_warp_gates * 2),
        "gates_outer": _change_route(base, gates=route.gates + (((0, 0), ()),)),
        "gates_rows": _change_route(
            base, gates=(((0, 0), route.gates[0][1] + (route.gates[0][1][0],)),)
        ),
        "gate_requirements": _change_route(base, gate_requirements=route.gate_requirements * 2),
        "toggles_outer": _change_route(base, script_toggles=route.script_toggles + (((0, 0), ()),)),
        "toggles_rows": _change_route(
            base, script_toggles=(((0, 0), (route.script_toggles[0][1][0],) * 2),)
        ),
        "trainers_outer": _change_route(base, trainers_by_flag=route.trainers_by_flag * 2),
        "door_gates": _change_route(base, script_door_gates=route.script_door_gates * 2),
        "landings_outer": _change_route(
            base, on_frame_landings=route.on_frame_landings + (((1, 1), ()),)
        ),
        "seagallop": _change_route(
            base, seagallop_warps=route.seagallop_warps + ((0, (1, 1, -1, 4, 5)),)
        ),
        "obstacles_outer": _change_effect(
            base, script_obstacles=effect.script_obstacles + (((0, 0), ()),)
        ),
        "warp_openings": _change_effect(
            base, on_load_warp_openings=effect.on_load_warp_openings * 2
        ),
        "metatiles_outer": _change_effect(
            base, on_load_metatiles=effect.on_load_metatiles + (((0, 0), ()),)
        ),
        "metatiles_rows": _change_effect(
            base,
            on_load_metatiles=(((0, 0), effect.on_load_metatiles[0][1] * 2),),
        ),
        "elevators_outer": _change_transport(base, elevators=transport.elevators + (((0, 0), ()),)),
        "elevators_rows": _change_transport(
            base, elevators=(((0, 0), transport.elevators[0][1] * 2),)
        ),
        "ferries_outer": _change_transport(
            base, ferries=transport.ferries + (((0, 0, 1, 2, 3), ()),)
        ),
        "ferries_rows": _change_transport(
            base, ferries=(((0, 0, 1, 2, 3), transport.ferries[0][1] * 2),)
        ),
    }


def test_outer_duplicates_refuse_and_only_producer_deduped_rows_refuse() -> None:
    for label, facts in _duplicate_cases().items():
        with pytest.raises(admission.AdmissionError, match="duplicate"):
            admission._check_unique_keys(facts)
        assert label


def test_ordered_subrows_and_audit_duplicates_are_not_rejected() -> None:
    facts = _facts()
    admission._check_unique_keys(facts)
    assert len(facts.route.trainers_by_flag[0][1]) == 2
    assert len(facts.route.on_frame_landings[0][1]) == 2
    assert len(facts.audit) == 2


_REFERENCE_MUTATORS = {
    "warp_flags": lambda f: _change_index(f, on_load_warp_flags=((((9, 9), 0), 1),)),
    "barriers": lambda f: _change_index(f, on_load_barriers=(((9, 9), ()),)),
    "barrier_labels": lambda f: _change_index(f, on_load_barrier_labels=((((9, 9), (1, 2)), "x"),)),
    "warp_gates": lambda f: _change_index(f, script_warp_gates=((((9, 9), 0), ("x", 1)),)),
    "gates": lambda f: _change_route(f, gates=(((9, 9), ()),)),
    "gate_requirements": lambda f: _change_route(f, gate_requirements=((((9, 9), 0), "x"),)),
    "toggles": lambda f: _change_route(f, script_toggles=(((9, 9), ()),)),
    "door_gates": lambda f: _change_route(f, script_door_gates=((((9, 9), 0), ("x", "y", ())),)),
    "landings": lambda f: _change_route(f, on_frame_landings=(((9, 9), ()),)),
    "trainers": lambda f: _change_route(
        f, trainers_by_flag=((0x500, ((0x500, (9, 9), 1, 2, 3),)),)
    ),
    "obstacles": lambda f: _change_effect(f, script_obstacles=(((9, 9), ()),)),
    "warp_openings": lambda f: _change_effect(f, on_load_warp_openings=((((9, 9), 0), ()),)),
    "metatiles": lambda f: _change_effect(f, on_load_metatiles=(((9, 9), ()),)),
    "elevators": lambda f: _change_transport(f, elevators=(((9, 9), ()),)),
    "ferries": lambda f: _change_transport(f, ferries=(((9, 9, 1, 2, 3), ()),)),
    "audit": lambda f: replace(f, audit=(AuditCall("elevators:bg", (9, 9), None, 1, ()),)),
}


@pytest.mark.parametrize("label", sorted(_REFERENCE_MUTATORS))
def test_every_source_map_reference_must_exist(label: str) -> None:
    with pytest.raises(admission.AdmissionError, match="map"):
        admission._check_references(_REFERENCE_MUTATORS[label](_facts()))


_WARP_MUTATORS = {
    "warp_flags": lambda f: _change_index(f, on_load_warp_flags=((((0, 0), 9), 1),)),
    "warp_gates": lambda f: _change_index(f, script_warp_gates=((((0, 0), 9), ("x", 1)),)),
    "door_gates": lambda f: _change_route(f, script_door_gates=((((0, 0), 9), ("x", "y", ())),)),
    "warp_openings": lambda f: _change_effect(f, on_load_warp_openings=((((0, 0), 9), ()),)),
}


@pytest.mark.parametrize("label", sorted(_WARP_MUTATORS))
def test_warp_index_families_require_an_actual_source_warp(label: str) -> None:
    with pytest.raises(admission.AdmissionError, match="warp"):
        admission._check_references(_WARP_MUTATORS[label](_facts()))


def test_elevator_floor_key_must_exist_but_other_destinations_stay_rows() -> None:
    base = _facts()
    row = base.transport.elevators[0][1][0]
    unknown_floor = (row[0], row[1], row[2], row[3], (9, 9), *row[5:])
    with pytest.raises(admission.AdmissionError, match="floor"):
        admission._check_references(
            _change_transport(base, elevators=((((0, 0), (unknown_floor,)),)))
        )
    # Seagallop, ferry and warp destinations are not source references here.
    absent = _change_transport(base, ferries=((((0, 0, 1, 2, 3), (_ferry_row(),)),)))
    admission._check_references(absent)


def _counts_wire() -> dict:
    return {
        "maps": 2,
        "warp_rows": 3,
        "gate_rows": 2,
        "obstacle_rows": 1,
        "audit_calls": 2,
        "corpus_calls": 7,
        "elevator_rows": 1,
        "ferry_rows": 1,
    }


def test_recount_uses_eight_exact_formulas_and_explicit_corpus_calls() -> None:
    facts = _facts()
    expected = _counts_wire()
    counts = admission._recount(facts, corpus_calls=7)
    assert isinstance(counts, admission._AdmissionCounts)
    assert admission._decode_counts(expected) == counts
    admission._compare_counts(counts, admission._decode_counts(expected))


@pytest.mark.parametrize(
    "field",
    [
        "maps",
        "warp_rows",
        "gate_rows",
        "obstacle_rows",
        "audit_calls",
        "corpus_calls",
        "elevator_rows",
        "ferry_rows",
    ],
)
def test_each_count_is_recomputed_and_one_off_refuses(field: str) -> None:
    facts = _facts()
    supplied = _counts_wire()
    supplied[field] += 1
    with pytest.raises(admission.AdmissionError, match="count"):
        admission._compare_counts(
            admission._recount(facts, corpus_calls=7),
            admission._decode_counts(supplied),
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update({"maps": True}),
        lambda c: c.update({"maps": -1}),
        lambda c: c.update({"maps": 4_000_001}),
        lambda c: c.update({"maps": "2"}),
        lambda c: c.pop("maps"),
        lambda c: c.update({"extra": 0}),
    ],
)
def test_count_field_set_types_and_bounds_refuse(mutate) -> None:
    wire = _counts_wire()
    mutate(wire)
    with pytest.raises(admission.AdmissionError):
        admission._decode_counts(wire)


@pytest.mark.parametrize("bad", [True, -1, 1_000_001])
def test_corpus_calls_are_explicit_bounded_exact_ints(bad) -> None:
    with pytest.raises(admission.AdmissionError):
        admission._recount(_facts(), corpus_calls=bad)


def test_section_map_ceiling_is_enforced_before_materializing() -> None:
    wire = _sections()
    wire["maps"] = [wire["maps"][0]] * (MAX_MAPS + 1)
    with pytest.raises(admission.AdmissionError, match="map"):
        admission._decode_sections(wire)


def test_admission_error_is_the_owning_error_for_bad_input() -> None:
    with pytest.raises(admission.AdmissionError):
        admission._decode_sections(None)
    assert issubclass(admission.AdmissionError, ValueError)


@pytest.mark.parametrize("error", [MemoryError, SystemExit])
def test_memory_error_and_system_exit_propagate(monkeypatch, error) -> None:
    def raise_error(_wire):
        raise error

    monkeypatch.setattr(admission, "decode_static_map", raise_error)
    with pytest.raises(error):
        admission._decode_sections(_sections())


def test_importing_admission_does_not_import_navigation() -> None:
    code = (
        "import sys; "
        "import pokegent.games.gen3.firered.rom.static_graph_admission; "
        "import pokegent.games.gen3.firered.rom.static_corpus_codec; "
        "print(any(name == 'pokegent.navigation' or name.startswith('pokegent.navigation.') "
        "for name in sys.modules))"
    )
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False"


# -- C4b corpus and complete mandatory-v1 admission -------------------------------------------


def _identity(**changes) -> dict:
    identity = {
        "schema_version": 1,
        "rom_sha1": "0" * 40,
        "rom_sha256": "1" * 64,
        "profile_sha256": "2" * 64,
        "profile_id": "firered",
        "generator_sha256": "3" * 64,
        "map_index_sha256": "4" * 64,
        "group_order": ["group"],
        "group_sizes": [2],
        "grouping_sha256": "5" * 64,
        "puzzle_sha256": None,
    }
    identity.update(changes)
    return identity


def _key(
    pointer: int = 0x800000,
    map_key: tuple[int, int] = (0, 0),
    *,
    initial: tuple[tuple[int, int], ...] = (),
    player_xy: tuple[int, int] | None = None,
    minimize: bool = True,
    guards: tuple = (),
    ferry: bool = False,
) -> tuple:
    return (pointer, map_key, initial, player_xy, minimize, guards, ferry)


def _setmetatile(metatile: int = 1, behavior: int | None = 0) -> Effect:
    return Effect(
        kind="setmetatile",
        source=1,
        guards=(),
        values=(("x", 1), ("y", 2), ("metatile", metatile), ("behavior", behavior)),
    )


def _interpretation(
    pointer: int = 0x800000,
    *,
    effects: tuple[Effect, ...] = (),
    unknown: tuple[str, ...] = (),
    commands: int = 0,
) -> Interpretation:
    return Interpretation(script_ptr=pointer, effects=effects, unknown=unknown, commands=commands)


def _row(key: tuple, result: Interpretation, reads: list) -> dict:
    return {
        "key": encode_interpretation_key(key),
        "result": encode_interpretation(result),
        "behavior_reads": reads,
    }


def _valid_rows() -> list[dict]:
    key = _key()
    return [_row(key, _interpretation(effects=(_setmetatile(1, 0),)), [[1, 0]])]


def _corpus(rows: list[dict]) -> dict:
    return {"version": 1, "type": "script_corpus", "rows": rows}


def _payload(
    rows: list[dict] | None = None,
    *,
    corpus: dict | None = None,
    maps: list[MapState] | None = None,
    counts: dict | None = None,
    identity_digest: str | None = None,
) -> dict:
    sections = _sections()
    if maps is not None:
        sections["maps"] = [encode_static_map(state) for state in maps]
    if corpus is None:
        corpus = _corpus([] if rows is None else rows)
    payload = {
        "version": 1,
        "type": "static_graph",
        **sections,
        "corpus": corpus,
        "identity_sha256": (
            loader._identity_digest(_identity()) if identity_digest is None else identity_digest
        ),
    }
    if counts is None:
        counts = dict(_counts_wire())
        raw_rows = corpus.get("rows")
        counts["corpus_calls"] = len(raw_rows) if type(raw_rows) is list else 0
    payload["counts"] = counts
    return payload


def _admit(payload: dict, **changes) -> admission.AdmittedStaticGraph:
    return admission.admit_static_graph(payload, identity=_identity(**changes))


def test_complete_empty_corpus_envelope_is_admitted() -> None:
    graph = _admit(_payload())
    assert type(graph) is admission.AdmittedStaticGraph
    assert graph.corpus == ()
    assert graph.identity_sha256 == loader._identity_digest(_identity())
    assert dict(graph.counts)["corpus_calls"] == 0
    assert graph.map_for((1, 1)).identity.number == 1
    with pytest.raises(KeyError):
        graph.map_for((9, 9))
    with pytest.raises(TypeError):
        graph.counts["maps"] = 9


def test_one_valid_and_one_incomplete_corpus_row_are_admitted() -> None:
    rows = _valid_rows()
    rows.append(
        _row(_key(0x800004), _interpretation(0x800004, unknown=("opaque",), commands=3), [])
    )
    graph = _admit(_payload(rows=rows))
    assert graph.corpus[0].key[0] == 0x800000
    assert graph.corpus[0].result.effects[0].value("behavior") == 0
    assert graph.corpus[1].result.unknown == ("opaque",)
    assert graph.corpus[1].result.complete is False
    assert graph.counts["corpus_calls"] == 2


def test_corpus_rows_preserve_read_order_and_duplicates() -> None:
    rows = [_row(_key(), _interpretation(effects=(_setmetatile(1, 0),)), [[1, 0], [1, 0]])]
    graph = _admit(_payload(rows=rows))
    assert graph.corpus[0].behavior_reads == ((1, 0), (1, 0))


_CORPUS_FRAME_MUTATORS = {
    "top-missing-corpus": lambda p: p.pop("corpus"),
    "top-extra-key": lambda p: p.update({"schema": 2}),
    "top-version": lambda p: p.update({"version": 2}),
    "top-bool-version": lambda p: p.update({"version": True}),
    "top-type": lambda p: p.update({"type": "Static_Graph"}),
    "corpus-version": lambda p: p["corpus"].update({"version": 2}),
    "corpus-bool-version": lambda p: p["corpus"].update({"version": True}),
    "corpus-type": lambda p: p["corpus"].update({"type": "interpretations"}),
    "corpus-extra": lambda p: p["corpus"].update({"complete": True}),
    "rows-not-list": lambda p: p["corpus"].update({"rows": tuple(p["corpus"]["rows"])}),
    "row-extra": lambda p: p["corpus"]["rows"][0].update({"complete": True}),
    "row-missing-reads": lambda p: p["corpus"]["rows"][0].pop("behavior_reads"),
    "missing-corpus-key": lambda p: p["corpus"]["rows"][0].pop("key"),
}


@pytest.mark.parametrize("label", sorted(_CORPUS_FRAME_MUTATORS))
def test_corpus_frames_refuse_missing_extra_and_wrong_tags(label: str) -> None:
    payload = _payload(rows=_valid_rows())
    _CORPUS_FRAME_MUTATORS[label](payload)
    with pytest.raises(admission.AdmissionError):
        _admit(payload)


@pytest.mark.parametrize(
    "identity",
    [
        "0" * 64,
        _identity(schema_version=True),
        _identity(rom_sha256="A" * 64),
        {key: value for key, value in _identity().items() if key != "puzzle_sha256"},
        _identity(group_order=["a", "a"], group_sizes=[1, 1]),
    ],
)
def test_identity_is_eleven_keys_not_a_bare_digest_or_self_confirmation(identity) -> None:
    with pytest.raises(admission.AdmissionError, match="identity"):
        admission.admit_static_graph(_payload(), identity=identity)


def test_a_different_valid_identity_or_payload_digest_refuses() -> None:
    with pytest.raises(admission.AdmissionError, match="identity"):
        _admit(_payload(), rom_sha256="9" * 64)
    with pytest.raises(admission.AdmissionError, match="identity"):
        _admit(_payload(identity_digest="0" * 64))


def test_pointer_must_equal_the_exact_key_pointer() -> None:
    rows = [_row(_key(0x800004), _interpretation(0x800000), [])]
    with pytest.raises(admission.AdmissionError, match="pointer"):
        _admit(_payload(rows=rows))


def test_corpus_key_map_must_exist_and_keys_must_be_unique() -> None:
    rows = [_row(_key(map_key=(9, 9)), _interpretation(0x800000), [])]
    with pytest.raises(admission.AdmissionError, match="map"):
        _admit(_payload(rows=rows))
    duplicate = _valid_rows() * 2
    with pytest.raises(admission.AdmissionError, match="duplicate"):
        _admit(_payload(rows=duplicate))


@pytest.mark.parametrize("reads", [[[True, 0]], [[1, True]], [[1, 0, 0]], [1, 0]])
def test_read_pairs_are_exact_two_element_int_arrays(reads) -> None:
    rows = [_row(_key(), _interpretation(effects=(_setmetatile(1, 0),)), reads)]
    with pytest.raises(admission.AdmissionError):
        _admit(_payload(rows=rows))


def test_reads_must_match_raw_attributes_and_absent_metatiles_are_null() -> None:
    wrong = [_row(_key(), _interpretation(effects=(_setmetatile(1, 0),)), [[1, 1]])]
    with pytest.raises(admission.AdmissionError, match="attributes"):
        _admit(_payload(rows=wrong))
    absent = [_row(_key(), _interpretation(effects=(_setmetatile(2, None),)), [[2, None]])]
    assert _admit(_payload(rows=absent)).corpus[0].behavior_reads == ((2, None),)


def test_behavior_reads_use_raw_attributes_not_the_cached_dict(monkeypatch) -> None:
    states = {
        (0, 0): _map(0, 0, attributes=((1, 0),)),
        (1, 1): _map(1, 1, (0,)),
    }
    states[(0, 0)].grid.__dict__["attribute_map"] = {1: 0xFF}

    def decode(wire):
        identity = wire["map"]["identity"]
        return states[(identity["group"], identity["number"])]

    monkeypatch.setattr(admission, "decode_static_map", decode)
    graph = _admit(_payload(maps=list(states.values()), rows=_valid_rows()))
    assert graph.corpus[0].behavior_reads == ((1, 0),)


def test_non_null_setmetatile_behavior_requires_its_exact_tile_read() -> None:
    no_read = [_row(_key(), _interpretation(effects=(_setmetatile(1, 0),)), [])]
    with pytest.raises(admission.AdmissionError, match="read"):
        _admit(_payload(rows=no_read))
    maps = [_map(0, 0, attributes=((1, 0), (2, 0))), _map(1, 1, (0,))]
    other_tile = [_row(_key(), _interpretation(effects=(_setmetatile(1, 0),)), [[2, 0]])]
    with pytest.raises(admission.AdmissionError, match="read"):
        _admit(_payload(rows=other_tile, maps=maps))
    wrong_behavior = [_row(_key(), _interpretation(effects=(_setmetatile(1, 7),)), [[1, 0]])]
    with pytest.raises(admission.AdmissionError, match="read"):
        _admit(_payload(rows=wrong_behavior))


def test_counts_come_from_the_verified_corpus_length() -> None:
    rows = _valid_rows()
    payload = _payload(rows=rows)
    payload["counts"]["corpus_calls"] = 0
    with pytest.raises(admission.AdmissionError, match="corpus_calls"):
        _admit(payload)
    payload = _payload(rows=rows)
    payload["counts"]["corpus_calls"] = 2
    with pytest.raises(admission.AdmissionError, match="corpus_calls"):
        _admit(payload)


def test_corpus_row_and_read_ceilings_refuse_without_truncation() -> None:
    row = _valid_rows()[0]
    oversized_rows = _payload(corpus=_corpus([row] * (MAX_INTERPRETATIONS + 1)))
    with pytest.raises(admission.AdmissionError, match="corpus"):
        _admit(oversized_rows)
    reads = [[1, 0]] * (MAX_CONTAINER_LENGTH + 1)
    oversized_reads = _payload(
        rows=[_row(_key(), _interpretation(effects=(_setmetatile(1, 0),)), reads)]
    )
    with pytest.raises(admission.AdmissionError, match="corpus"):
        _admit(oversized_reads)


def test_admission_never_calls_the_interpreter_reader_or_rom_tables(monkeypatch) -> None:
    import pokegent.games.gen3.firered.maps.map_data as map_data
    import pokegent.games.gen3.firered.rom.rom_tables as rom_tables
    import pokegent.games.gen3.firered.rom.script_effects as script_effects

    def boom(*_args, **_kwargs):
        raise AssertionError("admission must not read, interpret or build ROM tables")

    monkeypatch.setattr(script_effects, "interpret", boom)
    monkeypatch.setattr(map_data.MapReader, "read_static", boom)
    monkeypatch.setattr(rom_tables.GameTables, "from_rom_image", classmethod(boom))
    graph = _admit(_payload(rows=_valid_rows()))
    assert len(graph.corpus) == 1


def test_public_docstrings_state_the_bounded_loader_precondition() -> None:
    assert "load_verified_document" in admission.__doc__
    assert "load_verified_document" in admission.admit_static_graph.__doc__
