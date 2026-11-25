"""An elevator with an unmet Lift Key permission is not an available route.

Invented map data only.  The graph, edge construction, requirement helpers and
planner are the real ones; only the ROM-decoded elevator row is supplied as data.
"""

from pokegent.games.gen3.firered.maps.map_behaviors import MB, behavior_info, blocks_side
from pokegent.games.gen3.firered.maps.map_schema import (
    MapGrid,
    MapIdentity,
    MapLayoutInfo,
    MapState,
    PlayerPlacement,
)
from pokegent.navigation.capabilities import CapabilityState
from pokegent.navigation.graph import NavigationGraph
from pokegent.navigation.models import NavigationNode, NavigationTarget
from pokegent.navigation.planner import NavigationPlanner

SOURCE = (90, 1)
FLOOR = (91, 1)
LIFT_KEY_FLAG = 0x120
PANEL = (2, 2)


class _Provider:
    """The real behavior table; only ROM-backed readers would be stubbed."""

    def behavior_info(self, behavior):
        return behavior_info(behavior)

    def blocks_side(self, behavior, direction):
        return blocks_side(behavior, direction)

    @staticmethod
    def puzzle_for(_state):
        return None


def _identity(group, number, name):
    return MapIdentity(
        group=group,
        number=number,
        name=name,
        layout_id=1,
        region_section_id=0,
        map_type=0,
        map_type_name="underground",
        weather=0,
        cave=0,
        floor_num=0,
        battle_type=0,
        music=0,
        biking_allowed=False,
        allow_escaping=True,
        allow_running=True,
        identity_verified=True,
    )


def _layout():
    return MapLayoutInfo(
        width=5,
        height=5,
        border_width=1,
        border_height=1,
        border=(0,),
        primary_tileset=0,
        secondary_tileset=0,
        layout_ptr=0,
    )


def _player(x=2, y=2):
    return PlayerPlacement(
        save_x=x,
        save_y=y,
        avatar_x=x,
        avatar_y=y,
        prev_x=x,
        prev_y=y,
        x=x,
        y=y,
        position_consistent=True,
        facing="S",
        elevation=0,
        object_event_id=0,
        avatar_flags=0,
        surfing=False,
        underwater=False,
        biking=False,
        controllable_flag=True,
        forced=False,
        moving=False,
        tile_transition_state=0,
        running_state=0,
    )


def _state(identity, name):
    blocks = [0x3FF] * (19 * 19)
    for y in range(5):
        for x in range(5):
            blocks[(y + 7) * 19 + x + 7] = 1
    return MapState(
        identity=_identity(*identity, name),
        layout=_layout(),
        grid=MapGrid(
            columns=19,
            rows=19,
            blocks=tuple(blocks),
            attributes=((1, MB["MB_NORMAL"]),),
        ),
        player=_player(),
        warps=(),
    )


def _graph(*, required_flag=LIFT_KEY_FLAG):
    graph = NavigationGraph(
        {SOURCE: _state(SOURCE, "InventedElevatorLobby"), FLOOR: _state(FLOOR, "InventedB1F")},
        provider=_Provider(),
    )
    # The decoded setdynamicwarp row for the invented panel.
    graph._elevators = {
        SOURCE: (
            (
                PANEL[0],
                PANEL[1],
                0,
                1,
                FLOOR,
                PANEL[0],
                PANEL[1] + 1,
                required_flag,
                0x403A,
                1,
                0,
            ),
        )
    }
    return graph


def _start():
    return NavigationNode(SOURCE[0], SOURCE[1], PANEL[0], PANEL[1] - 1, 0)


def _elevator_edges(graph, capabilities):
    return [
        edge
        for edge in graph.edges_from(_start(), capabilities, include_unmet=True)
        if edge.kind == "elevator"
    ]


def test_default_edges_exclude_unmet_elevator_but_keep_candidate_api():
    graph = _graph()
    capabilities = CapabilityState()

    assert not [
        edge for edge in graph.edges_from(_start(), capabilities) if edge.kind == "elevator"
    ]
    candidates = graph.elevator_edges(SOURCE, capabilities)
    assert len(candidates) == 4 and not any(edge.available for edge in candidates)
    assert candidates[0].cost == 8
    assert candidates[0].unmet == ("Lift Key permission flag 0x120",)


def test_include_unmet_retains_original_label_cost_and_metadata():
    graph = _graph()
    capabilities = CapabilityState()

    edges = _elevator_edges(graph, capabilities)
    assert len(edges) == 1
    edge = edges[0]
    assert not edge.available
    assert edge.unmet == ("Lift Key permission flag 0x120",)
    assert edge.cost == 8
    assert edge.target_dict()["required_flag"] == LIFT_KEY_FLAG
    assert edge.target_dict()["floor_map"] == FLOOR
    assert edge.destination == NavigationNode(*FLOOR, PANEL[0], PANEL[1] + 1, 0)


def test_proven_permission_includes_the_elevator_by_default():
    graph = _graph()
    capabilities = CapabilityState(flags=frozenset({LIFT_KEY_FLAG}))

    edges = [edge for edge in graph.edges_from(_start(), capabilities) if edge.kind == "elevator"]
    assert len(edges) == 1
    assert edges[0].available and edges[0].unmet == ()
    assert edges[0].cost == 8


def test_ordinary_walk_edges_are_unchanged():
    graph = _graph()
    edges = graph.edges_from(_start(), CapabilityState())
    walk_edges = [edge for edge in edges if edge.kind == "walk"]
    assert walk_edges
    assert {edge.direction for edge in walk_edges} >= {"N", "W"}


def test_planner_blocks_on_exact_unmet_label_and_route_cost():
    graph = _graph()
    planner = NavigationPlanner(graph)
    target = NavigationTarget(*FLOOR, PANEL[0], PANEL[1] + 1)

    blocked = planner.plan(_start(), target, CapabilityState())
    assert not blocked.reachable
    assert blocked.unmet == ("Lift Key permission flag 0x120",)
    assert blocked.cost is not None and blocked.cost > 8
    elevator_steps = [edge for edge in blocked.route if edge.kind == "elevator"]
    assert len(elevator_steps) == 1 and elevator_steps[0].cost == 8

    permitted = planner.plan(
        _start(), target, CapabilityState(flags=frozenset({LIFT_KEY_FLAG}))
    )
    assert permitted.reachable and permitted.unmet == ()
    assert permitted.cost == 8
