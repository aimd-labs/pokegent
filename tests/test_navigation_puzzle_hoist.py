from types import SimpleNamespace

import yaml

from pokegent.games.gen3.firered.maps import map_boulders
from pokegent.games.gen3.firered.maps.map_provider import FireRedMapProvider
from pokegent.games.gen3.firered.maps.map_schema import (
    MapGrid,
    MapIdentity,
    MapLayoutInfo,
    MapObject,
    MapState,
)
from pokegent.navigation.graph import NavigationGraph
from pokegent.navigation.models import CapabilityState, NavigationNode, NavigationTarget
from pokegent.navigation.planner import NavigationPlanner


def test_default_puzzle_path_is_resolved_once_per_graph_for_search_lookups(
    tmp_path, monkeypatch
):
    key = (3, 7)
    puzzle_file = tmp_path / "boulders.yaml"
    puzzle_file.write_text(yaml.safe_dump({"schema": "boulders/1", "maps": {}}))
    resolutions = 0

    def resolve_default():
        nonlocal resolutions
        resolutions += 1
        return puzzle_file

    monkeypatch.setattr(map_boulders, "default_puzzles_path", resolve_default)
    map_boulders._load.cache_clear()

    graph = NavigationGraph.__new__(NavigationGraph)
    graph.provider = FireRedMapProvider()
    state = SimpleNamespace(identity=SimpleNamespace(group=key[0], number=key[1]))

    # Model repeated graph expansions for the same map; filesystem-backed table
    # resolution must remain bounded by one load for the graph lifetime.
    assert [graph._puzzle_for(state) for _ in range(250_000)] == [None] * 250_000
    assert resolutions == 1
    map_boulders._load.cache_clear()


def test_puzzle_lookup_keeps_legacy_provider_fallback():
    puzzle = object()

    class Provider:
        def puzzle_for(self, _state):
            return puzzle

    graph = NavigationGraph.__new__(NavigationGraph)
    graph.provider = Provider()
    state = SimpleNamespace(identity=SimpleNamespace(group=3, number=7))

    assert graph._puzzle_for(state) is puzzle


def test_real_planner_search_resolves_boulder_puzzle_path_once(tmp_path, monkeypatch):
    key = (3, 7)
    puzzle_file = tmp_path / "boulders.yaml"
    puzzle_file.write_text(
        yaml.safe_dump(
            {
                "schema": "boulders/1",
                "maps": {
                    "InventedBoulderMap": {
                        "group": key[0],
                        "number": key[1],
                        "switches": [],
                        "holes": [],
                        "boulders": [
                            {
                                "local_id": 1,
                                "tile": [3, 3],
                                "elevation": 0,
                                "hide_flag": None,
                                "reveal_flag": None,
                            }
                        ],
                        "closed_until_done": [],
                    }
                },
            }
        )
    )
    resolutions = 0

    def resolve_default():
        nonlocal resolutions
        resolutions += 1
        return puzzle_file

    monkeypatch.setattr(map_boulders, "default_puzzles_path", resolve_default)
    map_boulders._load.cache_clear()

    identity = MapIdentity(
        group=key[0], number=key[1], name="InventedBoulderMap", layout_id=1,
        region_section_id=0, map_type=0, map_type_name="indoor", weather=0, cave=0,
        floor_num=0, battle_type=0, music=0, biking_allowed=False,
        allow_escaping=False, allow_running=True, identity_verified=True,
    )
    layout = MapLayoutInfo(
        width=7, height=7, border_width=1, border_height=1, border=(0,),
        primary_tileset=0, secondary_tileset=0, layout_ptr=0,
    )
    state = MapState(
        identity=identity,
        layout=layout,
        grid=MapGrid(columns=21, rows=21, blocks=(0,) * (21 * 21), attributes=((0, 0),)),
        objects=(MapObject(
            local_id=1, graphics_id=97, role="boulder", x=3, y=3, elevation=0,
            movement_type=0, movement_type_name="none", range_x=0, range_y=0,
            trainer_type=0, trainer_range=0, script_ptr=None, hidden_flag_id=0,
            hidden_flag_set=None, source="template", blocks_tile=True,
        ),),
    )
    graph = NavigationGraph.__new__(NavigationGraph)
    graph.provider = FireRedMapProvider()
    graph.maps = {key: state}
    graph.rom = b""
    graph._blocking_object_indexes = {}
    graph._ferries = {}
    graph._on_load_metatiles = {}
    graph._on_load_barriers = {}
    graph.stateful_flags_toward = lambda *_args: frozenset()
    graph._landing_recovery_edges = lambda *_args, **_kwargs: ()
    graph.elevator_edges = lambda *_args, **_kwargs: ()
    graph.gate_edges = lambda *_args, **_kwargs: ()
    graph._script_gate_edges = lambda *_args, **_kwargs: ()
    graph._toggle_edges = lambda *_args, **_kwargs: ()
    graph._fly_edges = lambda *_args, **_kwargs: ()
    graph._script_gate_at = lambda *_args: None
    graph._connection_edge = lambda *_args: None
    expansions = 0
    real_edges_from = graph.edges_from

    def counted_edges_from(*args, **kwargs):
        nonlocal expansions
        expansions += 1
        return real_edges_from(*args, **kwargs)

    graph.edges_from = counted_edges_from
    plan = NavigationPlanner(graph).plan(
        NavigationNode(*key, 1, 1),
        NavigationTarget(*key, x=5, y=5),
        CapabilityState(),
        limit=100,
    )

    assert plan.cost is not None
    assert expansions > 1
    assert resolutions == 1
    map_boulders._load.cache_clear()
