"""Live RAM geometry outranks stale static ON_LOAD barrier assumptions.

The barrier gate is a plan-time prediction for maps whose layout is only known from the ROM.
On the current map the snapshot's collision bits are the local fact: a tile the switch has
already opened must stay walkable even while an old puzzle variable/flag requirement is unmet.
A tile that is still blocked, and the same barrier on a map without live geometry, must keep
refusing.  These are invented fixtures: they prove the source transition, not a Victory Road
root cause or a solved puzzle.
"""

from types import SimpleNamespace

import pytest

from pokegent.games.gen3.firered.maps import map_constants as K
from pokegent.games.gen3.firered.maps.map_boulders import with_player
from pokegent.games.gen3.firered.maps.map_provider import FireRedMapProvider
from pokegent.games.gen3.firered.maps.map_schema import (
    MapGrid,
    MapIdentity,
    MapLayoutInfo,
    MapObject,
    MapState,
)
from pokegent.navigation.graph import NavigationGraph
from pokegent.navigation.models import CapabilityState

KEY = (3, 7)
OTHER = (3, 8)
WIDTH = HEIGHT = 7
COLUMNS = WIDTH + 2 * K.MAP_OFFSET
BARRIER = (2, 1)
START = (1, 1)


def _state(key=KEY, *, blocked=(), raw=None, attributes=((0, 0),), player=False):
    identity = MapIdentity(
        group=key[0],
        number=key[1],
        name="InventedBarrierMap",
        layout_id=1,
        region_section_id=0,
        map_type=0,
        map_type_name="indoor",
        weather=0,
        cave=0,
        floor_num=0,
        battle_type=0,
        music=0,
        biking_allowed=False,
        allow_escaping=False,
        allow_running=True,
        identity_verified=True,
    )
    layout = MapLayoutInfo(
        width=WIDTH,
        height=HEIGHT,
        border_width=1,
        border_height=1,
        border=(0,),
        primary_tileset=0,
        secondary_tileset=0,
        layout_ptr=0,
    )
    blocks = [0] * (COLUMNS * COLUMNS)
    for x, y in blocked:
        index = (y + K.MAP_OFFSET) * COLUMNS + x + K.MAP_OFFSET
        blocks[index] = K.MAPGRID_COLLISION_MASK
    for (x, y), block in (raw or {}).items():
        blocks[(y + K.MAP_OFFSET) * COLUMNS + x + K.MAP_OFFSET] = block
    state = MapState(
        identity=identity,
        layout=layout,
        grid=MapGrid(
            columns=COLUMNS, rows=COLUMNS, blocks=tuple(blocks), attributes=attributes
        ),
        objects=(),
    )
    return with_player(state, *START) if player else state


def _graph(requirement):
    state = _state()
    graph = NavigationGraph.__new__(NavigationGraph)
    graph.provider = FireRedMapProvider()
    graph.rom = b""
    graph.maps = {KEY: state, OTHER: _state(OTHER)}
    graph._puzzle_table = None
    graph._blocking_object_indexes = {}
    graph._on_load_metatiles = {}
    graph._on_load_barriers = {KEY: {BARRIER: (requirement,)}}
    graph._on_load_barrier_labels = {KEY: {BARRIER: f"static {requirement}"}}
    graph._on_frame_landings = {}
    graph._ferries = {}
    graph._landing_recovery_edges = lambda *_a, **_k: ()
    graph._boulder_plan_edges = lambda *_a, **_k: ()
    graph.elevator_edges = lambda *_a, **_k: ()
    graph.gate_edges = lambda *_a, **_k: ()
    graph._script_gate_edges = lambda *_a, **_k: ()
    graph._toggle_edges = lambda *_a, **_k: ()
    graph._fly_edges = lambda *_a, **_k: ()
    graph._script_gate_at = lambda *_a: None
    graph._connection_edge = lambda *_a: None
    graph._warp_at = lambda *_a: None
    graph._warp_opening = lambda *_a: None
    return graph


def _east_edges(graph, state, *, live, include_unmet=False, capabilities=CapabilityState()):
    node = graph.node(KEY, *START)
    return tuple(
        edge
        for edge in graph.edges_from(
            node, capabilities, live=live, include_unmet=include_unmet
        )
        if edge.direction == "E"
    )


@pytest.mark.parametrize("requirement", ["var:16548:100", "flag:900"])
def test_live_open_tile_is_not_reclosed_by_stale_static_requirement(requirement):
    state = _state(player=True)
    graph = _graph(requirement)
    assert state.collision(*BARRIER) == 0

    edges = _east_edges(graph, state, live=state, include_unmet=True)

    assert [(edge.kind, edge.available) for edge in edges] == [("walk", True)]
    assert edges[0].destination == graph.node(KEY, *BARRIER)


@pytest.mark.parametrize("requirement", ["var:16548:100", "flag:900"])
def test_live_blocked_barrier_tile_still_refuses(requirement):
    state = _state(blocked=(BARRIER,), player=True)
    graph = _graph(requirement)
    assert state.collision(*BARRIER)

    edges = _east_edges(graph, state, live=state, include_unmet=True)

    assert edges
    assert all(edge.kind == "gate" and not edge.available for edge in edges)
    assert {row.key for edge in edges for row in edge.requirements} == {requirement}


@pytest.mark.parametrize("requirement", ["var:16548:100", "flag:900"])
def test_static_map_without_live_geometry_keeps_the_guard(requirement):
    graph = _graph(requirement)

    edges = _east_edges(graph, None, live=None, include_unmet=True)

    assert edges
    assert all(edge.kind == "gate" and not edge.available for edge in edges)
    assert {row.key for edge in edges for row in edge.requirements} == {requirement}


def test_live_geometry_of_another_map_does_not_open_this_map():
    graph = _graph("var:16548:100")
    elsewhere = _state(OTHER, player=True)

    edges = _east_edges(graph, elsewhere, live=elsewhere, include_unmet=True)

    assert edges
    assert all(edge.kind == "gate" and not edge.available for edge in edges)


def test_modeled_hypothetical_grid_keeps_the_requirement():
    graph = _graph("var:16548:100")
    state = _state(player=True)
    modeled = state.model_copy(update={"identity": state.identity})
    graph._state_for = lambda *_a, **_k: modeled

    edges = _east_edges(graph, modeled, live=state, include_unmet=True)

    assert edges
    assert all(edge.kind == "gate" and not edge.available for edge in edges)


def test_unverified_live_identity_keeps_the_requirement():
    graph = _graph("var:16548:100")
    state = _state(player=True)
    unverified = state.model_copy(
        update={"identity": state.identity.model_copy(update={"identity_verified": False})}
    )

    edges = _east_edges(graph, unverified, live=unverified, include_unmet=True)

    assert edges
    assert all(edge.kind == "gate" and not edge.available for edge in edges)


def test_live_open_tile_with_a_blocking_object_still_refuses():
    graph = _graph("var:16548:100")
    blocker = MapObject(
        local_id=9,
        graphics_id=1,
        template_graphics_id=1,
        role="npc",
        x=BARRIER[0],
        y=BARRIER[1],
        elevation=0,
        movement_type=0,
        movement_type_name="none",
        range_x=0,
        range_y=0,
        trainer_type=0,
        trainer_range=0,
        script_ptr=None,
        hidden_flag_id=0,
        hidden_flag_set=False,
        source="template",
        blocks_tile=True,
    )
    state = _state(player=True).model_copy(update={"objects": (blocker,)})

    edges = _east_edges(graph, state, live=state, include_unmet=True)

    assert not any(edge.kind == "walk" for edge in edges)


def test_undefined_metatile_with_elevation_bits_keeps_the_requirement():
    graph = _graph("var:16548:100")
    block = K.MAPGRID_UNDEFINED | (3 << K.MAPGRID_ELEVATION_SHIFT)
    state = _state(player=True, raw={BARRIER: block})
    assert state.collision(*BARRIER) == 0  # raw collision bits are clear

    edges = _east_edges(graph, state, live=state, include_unmet=True)

    assert edges
    assert all(edge.kind == "gate" and not edge.available for edge in edges)


def test_missing_or_unknown_metatile_attributes_keep_the_requirement():
    graph = _graph("var:16548:100")
    missing = _state(player=True, attributes=((1, 0),))
    unknown = _state(player=True, attributes=((0, 0xFF),))

    for state in (missing, unknown):
        edges = _east_edges(graph, state, live=state, include_unmet=True)
        assert edges
        assert all(edge.kind == "gate" and not edge.available for edge in edges)


def test_unknown_live_facts_are_not_treated_as_solved():
    state = _state(player=True)
    graph = _graph("var:16548:100")
    unknown = CapabilityState(vars_known=False, flags_known=False)

    # The current tile itself is open in RAM, so local movement is proven even though no
    # puzzle fact is known.
    live_edges = _east_edges(graph, state, live=state, capabilities=unknown)
    assert [(edge.kind, edge.available) for edge in live_edges] == [("walk", True)]

    # The same unknown requirement on a map without live geometry stays unresolved.
    static_edges = _east_edges(
        graph, SimpleNamespace(), live=None, include_unmet=True, capabilities=unknown
    )
    assert static_edges
    assert all(edge.kind == "gate" and not edge.available for edge in static_edges)


def test_live_open_proof_runs_only_for_an_unmet_live_barrier(monkeypatch):
    graph = _graph("var:16548:100")
    state = _state(player=True)
    calls = []

    def counting(state_arg, live_arg, map_key, x, y):
        calls.append((map_key, x, y))
        return True

    monkeypatch.setattr(graph, "_live_open_tile", counting)

    # No barrier on the map: an ordinary walk must not probe live geometry.
    graph._on_load_barriers = {KEY: {}}
    _east_edges(graph, state, live=state, include_unmet=True)
    assert calls == []

    # A barrier whose requirement is already met needs no live-open proof either.
    graph._on_load_barriers = {KEY: {BARRIER: ("var:16548:100",)}}
    met = CapabilityState(vars=((16548, 100),))
    _east_edges(graph, state, live=state, include_unmet=True, capabilities=met)
    assert calls == []

    # An unmet live barrier still takes the guarded proof path.
    _east_edges(graph, state, live=state, include_unmet=True)
    assert calls == [(KEY, *BARRIER)]
