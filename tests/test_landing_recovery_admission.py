"""An unmet ON_FRAME landing prediction must not become an available route.

A landing-recovery edge models the script that moves the player off an inbound warp
tile when a scene variable holds the scripted value.  While the variable holds another
value the movement is only a prediction: the default edge set must omit it, and the
planner must not claim a reachable, requirement-free route through it.  The maps are
invented and the script-derived landing row is seeded directly; the graph, edge
builder, requirement helper and planner are real.
"""

from pokegent.games.gen3.firered.maps import map_constants as K
from pokegent.games.gen3.firered.maps.map_provider import FireRedMapProvider
from pokegent.games.gen3.firered.maps.map_schema import (
    MapGrid,
    MapIdentity,
    MapLayoutInfo,
    MapState,
    WarpInfo,
)
from pokegent.navigation.capabilities import CapabilityState
from pokegent.navigation.graph import NavigationGraph
from pokegent.navigation.models import NavigationNode, NavigationTarget
from pokegent.navigation.planner import NavigationPlanner

SOURCE = (3, 8)
LANDING_MAP = (3, 9)
VAR = 0x4001
LANDING = (2, 2)
TARGET = (3, 3)
PATH = ((0, 1), (1, 0))
WIDTH = HEIGHT = 7
COLUMNS = WIDTH + 2 * K.MAP_OFFSET
AROUND_TARGET = ((3, 2), (2, 3), (4, 3), (3, 4))
WRONG = CapabilityState(vars=((VAR, 1),))
MET = CapabilityState(vars=((VAR, 0),))


def _warp(x, y, destination):
    return WarpInfo(
        index=1, x=x, y=y, elevation=0,
        dest_group=destination[0], dest_number=destination[1],
        dest_name="InventedLandingMap", dest_warp_id=1, dynamic=False,
        tile_behavior=0, tile_kind="normal", trigger=None, trigger_direction=None,
    )


def _state(key, name, warps=(), blocked=()):
    blocks = [0] * (COLUMNS * COLUMNS)
    for x, y in blocked:
        blocks[(y + K.MAP_OFFSET) * COLUMNS + x + K.MAP_OFFSET] = K.MAPGRID_COLLISION_MASK
    identity = MapIdentity(
        group=key[0], number=key[1], name=name, layout_id=1, region_section_id=0,
        map_type=0, map_type_name="indoor", weather=0, cave=0, floor_num=0,
        battle_type=0, music=0, biking_allowed=False, allow_escaping=False,
        allow_running=True, identity_verified=True,
    )
    layout = MapLayoutInfo(
        width=WIDTH, height=HEIGHT, border_width=1, border_height=1, border=(0,),
        primary_tileset=0, secondary_tileset=0, layout_ptr=0,
    )
    grid = MapGrid(
        columns=COLUMNS, rows=COLUMNS, blocks=tuple(blocks), attributes=((0, 0),)
    )
    return MapState(identity=identity, layout=layout, grid=grid, warps=warps)


def _graph():
    landing_map = _state(
        LANDING_MAP, "InventedLandingMap",
        warps=(_warp(*LANDING, destination=SOURCE),), blocked=AROUND_TARGET,
    )
    source_map = _state(
        SOURCE, "InventedLandingSource",
        warps=(_warp(*LANDING, destination=LANDING_MAP),),
    )
    graph = NavigationGraph(
        {LANDING_MAP: landing_map, SOURCE: source_map}, provider=FireRedMapProvider()
    )
    # The script-derived row: VAR == 0 moves the player along PATH after the warp.
    graph._on_frame_landings = {LANDING_MAP: ((VAR, 0, PATH),)}
    return graph


def _start():
    return NavigationNode(*LANDING_MAP, *LANDING)


def _edges(graph, capabilities=CapabilityState(), *, include_unmet=False):
    return graph.edges_from(
        _start(), capabilities, include_unmet=include_unmet
    )


def _landings(edges):
    return [edge for edge in edges if edge.kind == "forced"]


def test_unmet_landing_is_default_excluded_and_kept_for_diagnostics():
    graph = _graph()

    assert _landings(_edges(graph, WRONG)) == []
    diagnostic = _landings(_edges(graph, WRONG, include_unmet=True))
    assert [(edge.available, edge.requirements[0].key) for edge in diagnostic] == [
        (False, f"var:{VAR}:0")
    ]


def test_planner_does_not_route_through_the_unmet_landing():
    plan = NavigationPlanner(_graph()).plan(
        _start(), NavigationTarget(*LANDING_MAP, x=TARGET[0], y=TARGET[1]), WRONG
    )

    assert not plan.reachable
    assert plan.unmet == (f"var {VAR} 0",)
    assert plan.blocked_choice_step is None
    assert [edge.kind for edge in plan.route] == ["forced"]


def test_met_landing_and_ordinary_walk_stay_available():
    graph = _graph()

    landings = _landings(_edges(graph, MET))
    assert [(edge.available, edge.destination) for edge in landings] == [
        (True, graph.node(LANDING_MAP, *TARGET))
    ]
    walks = [edge for edge in _edges(graph) if edge.kind == "walk"]
    assert walks and all(edge.available for edge in walks)
    plan = NavigationPlanner(graph).plan(
        _start(), NavigationTarget(*LANDING_MAP, x=TARGET[0], y=TARGET[1]), MET
    )
    assert plan.reachable and plan.unmet == ()
