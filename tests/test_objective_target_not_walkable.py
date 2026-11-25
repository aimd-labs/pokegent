"""An objective tile that is a positively static wall fails fast, typed.

The planner used to spend the whole objective search proving that a tile with
collision bits set (and no modeled way to land there) cannot be a route node.
These tests drive a real ``NavigationGraph``/``NavigationPlanner`` with invented
maps and count the graph's own ``edges_from`` calls, so the early proof must cost
zero world expansions.  The typed reason travels through the real adapter and
field builder on its existing ``blocked_route_fact`` / ``blocked_route`` carrier.
"""

from __future__ import annotations

from test_probe_primitives import snapshot

from pokegent.actions.field.builder import FieldBuilder
from pokegent.games.gen3.firered.maps.map_behaviors import MB, behavior_info, blocks_side
from pokegent.games.gen3.firered.maps.map_constants import MAPGRID_UNDEFINED
from pokegent.games.gen3.firered.maps.map_schema import (
    MapGrid,
    MapIdentity,
    MapLayoutInfo,
    MapState,
    PlayerPlacement,
    WarpInfo,
)
from pokegent.goals.models import Objective
from pokegent.goals.validate import ResolvedObjective, ResolvedTarget
from pokegent.navigation.adapter import NavigationAdapter
from pokegent.navigation.graph import NavigationGraph, NavigationNode
from pokegent.navigation.models import CapabilityState, NavigationTarget
from pokegent.navigation.planner import (
    OBJECTIVE_FRAME_PLAN_BUDGET,
    TARGET_NOT_WALKABLE_FACT,
    NavigationPlanner,
    objective_frame_budget,
)
from pokegent.options.models import DecisionFrame, to_system_one_payload
from pokegent.options.registry import BuildContext
from pokegent.state.scenes import SceneResult

SOURCE = (0, 0)
TARGET = (0, 1)
START = NavigationNode(*SOURCE, 1, 1)
WALL = (3, 3)
WATER = (2, 3)
WARP_TILE = (3, 2)

_TILES = {1: MB["MB_NORMAL"], 2: MB["MB_POND_WATER"]}


class _Provider:
    """Real behavior classification; ROM-backed readers stay unset."""

    def behavior_info(self, behavior):
        return behavior_info(behavior)

    def blocks_side(self, behavior, direction):
        return blocks_side(behavior, direction)

    def puzzles(self, **_kwargs):
        return {}


class _CountingGraph(NavigationGraph):
    """A real graph that records every world expansion the planner asks for."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.expansions = 0

    def edges_from(self, *args, **kwargs):
        self.expansions += 1
        return super().edges_from(*args, **kwargs)


class _Events:
    def __init__(self):
        self.types = []

    def emit(self, event_type, **kwargs):
        self.types.append(event_type)

    def close(self):
        pass


def _identity(key, name, *, verified=True):
    return MapIdentity(
        group=key[0],
        number=key[1],
        name=name,
        layout_id=1,
        region_section_id=0,
        map_type=0,
        map_type_name="route",
        weather=0,
        cave=0,
        floor_num=0,
        battle_type=0,
        music=0,
        biking_allowed=False,
        allow_escaping=True,
        allow_running=True,
        identity_verified=verified,
    )


def _layout(width=5):
    return MapLayoutInfo(
        width=width,
        height=5,
        border_width=1,
        border_height=1,
        border=(0,),
        primary_tileset=0,
        secondary_tileset=0,
        layout_ptr=0,
    )


def _player(x=1, y=1):
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


def _state(
    key,
    name,
    *,
    blocks=None,
    attributes=None,
    warps=(),
    width=5,
    verified=True,
):
    grid_blocks = [0x3FF] * (19 * 19)
    for y in range(5):
        for x in range(min(width, 5)):
            grid_blocks[(y + 7) * 19 + x + 7] = 1
    for (x, y), block in (blocks or {}).items():
        grid_blocks[(y + 7) * 19 + x + 7] = block
    attrs = dict(_TILES)
    attrs.update(attributes or {})
    return MapState(
        identity=_identity(key, name, verified=verified),
        layout=_layout(width),
        grid=MapGrid(
            columns=19,
            rows=19,
            blocks=tuple(grid_blocks),
            attributes=tuple(sorted(attrs.items())),
        ),
        player=_player(),
        warps=tuple(warps),
    )


def _warp(index, x, y, dest_warp):
    return WarpInfo(
        index=index,
        x=x,
        y=y,
        elevation=0,
        dest_group=SOURCE[0],
        dest_number=SOURCE[1],
        dest_name="InventedRoute",
        dest_warp_id=dest_warp,
        dynamic=False,
        tile_behavior=MB["MB_NORMAL"],
        tile_kind="normal",
        trigger=None,
        trigger_direction=None,
    )


def _wall_state(**kwargs):
    return _state(TARGET, "InventedWall", blocks={WALL: 1 | (1 << 10)}, **kwargs)


def _graph(target_state=None, *, source_state=None, expansions=0):
    return _CountingGraph(
        {
            SOURCE: source_state or _state(SOURCE, "InventedRoute"),
            TARGET: target_state or _wall_state(),
        },
        provider=_Provider(),
    )


def _plan(graph, target, *, live=None, start=START, capabilities=CapabilityState()):
    return NavigationPlanner(graph).plan(start, target, capabilities, live=live)


def test_real_planner_typed_reason_costs_zero_world_expansions(tmp_path):
    graph = _graph()
    events = _Events()
    with objective_frame_budget(events, OBJECTIVE_FRAME_PLAN_BUDGET):
        plan = _plan(graph, NavigationTarget(*TARGET, x=WALL[0], y=WALL[1]))

    assert OBJECTIVE_FRAME_PLAN_BUDGET == 75_000
    assert plan.cost is None and plan.route == ()
    assert plan.blocked_route_fact == TARGET_NOT_WALKABLE_FACT
    assert plan.explored == 0
    assert graph.expansions == 0
    assert events.types == []


def test_small_valid_route_is_not_affected():
    graph = _graph()
    plan = _plan(graph, NavigationTarget(*SOURCE, x=4, y=4))

    assert plan.cost is not None and plan.reachable
    assert plan.blocked_route_fact is None
    assert graph.expansions > 0


def test_warp_tile_and_water_keep_the_existing_search():
    warped = _wall_state(warps=(_warp(1, *WARP_TILE, 1),))
    graph = _graph(warped)
    plan = _plan(graph, NavigationTarget(*TARGET, x=WARP_TILE[0], y=WARP_TILE[1]))
    assert plan.blocked_route_fact != TARGET_NOT_WALKABLE_FACT
    assert graph.expansions > 0

    water = _state(TARGET, "InventedWater", blocks={WATER: 2 | (1 << 10)})
    graph = _graph(water)
    plan = _plan(graph, NavigationTarget(*TARGET, x=WATER[0], y=WATER[1]))
    assert plan.blocked_route_fact != TARGET_NOT_WALKABLE_FACT
    assert graph.expansions > 0


def test_unknown_behavior_is_not_labeled_a_proven_wall():
    unknown = _state(
        TARGET,
        "InventedUnknown",
        blocks={WALL: 1001 | (1 << 10)},
        attributes={1001: 0xFF},
    )
    graph = _graph(unknown)
    plan = _plan(graph, NavigationTarget(*TARGET, x=WALL[0], y=WALL[1]))

    assert plan.blocked_route_fact != TARGET_NOT_WALKABLE_FACT
    assert graph.expansions > 0


def test_missing_attrs_unverified_out_of_grid_and_ledge_forced_stay_unknown():
    cases = (
        (_state(TARGET, "Missing", blocks={WALL: 1001 | (1 << 10)}), WALL, "missing attrs"),
        (
            _state(TARGET, "Unverified", blocks={WALL: 1 | (1 << 10)}, verified=False),
            WALL,
            "unverified identity",
        ),
        (
            _state(TARGET, "OutOfGrid", width=20, blocks={WALL: 1 | (1 << 10)}),
            (15, 3),
            "outside the grid",
        ),
        (
            _state(
                TARGET,
                "Ledge",
                blocks={WALL: 3 | (1 << 10)},
                attributes={3: MB["MB_JUMP_EAST"]},
            ),
            WALL,
            "ledge",
        ),
        (
            _state(
                TARGET,
                "Forced",
                blocks={WALL: 4 | (1 << 10)},
                attributes={4: MB["MB_SLIDE_EAST"]},
            ),
            WALL,
            "forced tile",
        ),
        (
            _state(
                TARGET,
                "MaskedUndefined",
                blocks={WALL: MAPGRID_UNDEFINED | (1 << 10)},
                attributes={MAPGRID_UNDEFINED: 0},
            ),
            WALL,
            "undefined masked metatile",
        ),
    )
    for state, (x, y), label in cases:
        graph = _graph(state)
        plan = _plan(graph, NavigationTarget(*TARGET, x=x, y=y))
        assert plan.blocked_route_fact != TARGET_NOT_WALKABLE_FACT, label
        assert graph.expansions > 0, label


def test_stateful_opening_and_changed_geometry_re_evaluate():
    graph = _graph()
    # A modeled ON_LOAD tile change on the target map keeps the route search in charge.
    graph._on_load_metatiles[TARGET] = (((1,), object()),)
    plan = _plan(graph, NavigationTarget(*TARGET, x=WALL[0], y=WALL[1]))
    assert plan.blocked_route_fact != TARGET_NOT_WALKABLE_FACT
    assert graph.expansions > 0

    # Re-observation: the live target grid proves the same tile open now.
    open_state = _wall_state().model_copy(
        update={
            "grid": _wall_state().grid.model_copy(
                update={"blocks": tuple(_state(TARGET, "InventedWall").grid.blocks)}
            )
        }
    )
    graph = _graph()
    plan = _plan(
        graph,
        NavigationTarget(*TARGET, x=WALL[0], y=WALL[1]),
        live=open_state,
    )
    assert plan.blocked_route_fact != TARGET_NOT_WALKABLE_FACT
    assert graph.expansions > 0


def test_out_of_bounds_and_unreadable_maps_stay_prior_contracts():
    graph = _graph()
    plan = _plan(graph, NavigationTarget(*TARGET, x=99, y=99))
    assert plan.blocked_route_fact != TARGET_NOT_WALKABLE_FACT
    assert graph.expansions > 0

    graph = _graph()
    states = dict(graph.maps)
    states.pop(TARGET)
    graph.maps = states
    plan = _plan(graph, NavigationTarget(*TARGET, x=WALL[0], y=WALL[1]))
    assert plan.blocked_route_fact != TARGET_NOT_WALKABLE_FACT
    assert graph.expansions > 0


def _resolved():
    return ResolvedObjective(
        Objective(
            objective_id="wall_objective",
            revision=1,
            kind="reach",
            target={"map": "InventedWall", "x": WALL[0], "y": WALL[1]},
        ),
        ResolvedTarget(group=TARGET[0], number=TARGET[1], x=WALL[0], y=WALL[1]),
        (),
    )


def test_reason_survives_real_adapter_and_field_builder():
    graph = _graph()
    events = _Events()
    adapter = NavigationAdapter(graph, events=events)
    snap = snapshot().model_copy(update={"map": _state(SOURCE, "InventedRoute")})
    resolved = _resolved()

    result = adapter.plan_result(snap, resolved, objective_budget=OBJECTIVE_FRAME_PLAN_BUDGET)
    assert result.plan is not None
    assert result.plan.cost is None and result.plan.route == ()
    assert result.plan.blocked_route_fact == TARGET_NOT_WALKABLE_FACT
    assert result.unknown_reason is None
    assert graph.expansions == 0
    assert events.types == []

    builder = FieldBuilder(
        field_menu=False,
        objectives=lambda goal_id: resolved if goal_id == "wall_objective" else None,
        navigation=adapter,
    )
    draft = builder.build(
        BuildContext(
            snapshot=snap,
            scene=SceneResult(
                scene_id="field.free", confidence=1, detector="synthetic_field"
            ),
            goal_id="wall_objective",
        )
    )
    assert draft.context["blocked_route"] == TARGET_NOT_WALKABLE_FACT

    frame = DecisionFrame.from_options(
        draft.options,
        decision_id="wall-objective-frame",
        run_id="synthetic",
        snapshot_seq=snap.snapshot_seq,
        scene_id="field.free",
        scene_confidence=1.0,
        state_fingerprint=draft.state_fingerprint,
        goal_id="wall_objective",
        context=draft.context,
    )
    assert to_system_one_payload(frame)["state"]["blocked_route"] == TARGET_NOT_WALKABLE_FACT
