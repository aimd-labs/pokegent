"""A warp event only fires on a metatile behavior that starts a warp.

FireRed's ``TryStartWarpEventScript``/``TryArrowWarp`` require
``IsWarpMetatileBehavior``/``IsArrowWarpMetatileBehavior`` for the tile under the
player (src/field_control_avatar.c).  A warp row on a readable MB_CAVE/MB_NORMAL
tile therefore cannot transition by walking onto it.  It must stay a diagnostic
instead of becoming a field ``step`` option or an automatic navigation edge, or
the executor spends its bounded wait on a transition that never starts.

Invented map data only: no ROM tables, names or captured snapshots.
"""

from pokegent.actions.field import nav as N
from pokegent.actions.field.targets import enumerate_targets, warp_target
from pokegent.games.gen3.firered.maps.map_behaviors import MB, behavior_info, blocks_side
from pokegent.games.gen3.firered.maps.map_schema import (
    MapGrid,
    MapIdentity,
    MapLayoutInfo,
    MapState,
    PlayerPlacement,
    WarpInfo,
)
from pokegent.navigation.capabilities import CapabilityState
from pokegent.navigation.graph import NavigationGraph, NavigationNode

SOURCE = (90, 1)
DEST = (90, 2)
CAVE_DEST = (92, 1)
NORMAL_DEST = (93, 1)
UNKNOWN_DEST = (91, 1)
CAVE_DOOR_DEST = (94, 1)
FALL_DEST = (95, 1)
ARROW_DEST = (96, 1)
DOOR_DEST = (97, 1)


class _Provider:
    """The real behavior table; only the ROM-backed readers are stubbed."""

    def behavior_info(self, behavior):
        return behavior_info(behavior)

    def blocks_side(self, behavior, direction):
        return blocks_side(behavior, direction)


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


# invented metatile ids -> real behavior ids
_TILES = {
    1: MB["MB_NORMAL"],
    2: MB["MB_LADDER"],
    3: MB["MB_CAVE"],
    4: MB["MB_CAVE_DOOR"],
    5: MB["MB_FALL_WARP"],
    6: MB["MB_EAST_ARROW_WARP"],
    7: MB["MB_WARP_DOOR"],
}


def _state(warp_rows, extra=(), identity=SOURCE, name="InventedB2F"):
    blocks = [0x3FF] * (19 * 19)
    for y in range(5):
        for x in range(5):
            blocks[(y + 7) * 19 + x + 7] = 1
    for (x, y), metatile in extra:
        blocks[(y + 7) * 19 + x + 7] = metatile
    return MapState(
        identity=_identity(*identity, name),
        layout=_layout(),
        grid=MapGrid(
            columns=19,
            rows=19,
            blocks=tuple(blocks),
            attributes=tuple(_TILES.items()),
        ),
        player=_player(),
        warps=tuple(warp_rows),
    )


def _warp(
    index, x, y, dest_warp, *, behavior, kind, trigger, direction=None, dest=DEST, elevation=0
):
    return WarpInfo(
        index=index,
        x=x,
        y=y,
        elevation=elevation,
        dest_group=dest[0],
        dest_number=dest[1],
        dest_name="InventedB1F",
        dest_warp_id=dest_warp,
        dynamic=False,
        tile_behavior=behavior,
        tile_kind=kind,
        trigger=trigger,
        trigger_direction=direction,
    )


def _source_state():
    rows = (
        _warp(1, 2, 1, 1, behavior=MB["MB_LADDER"], kind="ladder", trigger="step"),
        _warp(2, 1, 2, 2, behavior=MB["MB_CAVE"], kind="cave", trigger=None, dest=CAVE_DEST),
        _warp(
            3,
            3,
            2,
            3,
            behavior=MB["MB_NORMAL"],
            kind="normal",
            trigger=None,
            dest=NORMAL_DEST,
        ),
        _warp(4, 0, 4, 4, behavior=None, kind=None, trigger=None, dest=UNKNOWN_DEST),
        _warp(
            5,
            2,
            3,
            5,
            behavior=MB["MB_CAVE_DOOR"],
            kind="cave_door",
            trigger="step",
            dest=CAVE_DOOR_DEST,
        ),
        _warp(
            6,
            4,
            2,
            6,
            behavior=MB["MB_FALL_WARP"],
            kind="hole",
            trigger="step",
            dest=FALL_DEST,
        ),
        _warp(
            7,
            3,
            1,
            7,
            behavior=MB["MB_EAST_ARROW_WARP"],
            kind="arrow_warp",
            trigger="arrow",
            direction="E",
            dest=ARROW_DEST,
        ),
        _warp(
            8,
            1,
            3,
            8,
            behavior=MB["MB_WARP_DOOR"],
            kind="door",
            trigger="door",
            dest=DOOR_DEST,
        ),
    )
    extra = (
        ((2, 1), 2),
        ((1, 2), 3),
        ((3, 2), 1),
        ((1, 1), 1),
        ((2, 3), 4),
        ((4, 2), 5),
        ((3, 1), 6),
        ((1, 3), 7),
    )
    return _state(rows, extra)


def _dest_state(identity=DEST):
    rows = tuple(
        _warp(
            index,
            2,
            1,
            0,
            behavior=MB["MB_NORMAL"],
            kind="normal",
            trigger=None,
        )
        for index in range(1, 9)
    )
    return _state(rows, identity=identity, name="InventedLanding")


def _targets():
    state = _source_state()
    return {warp.index: warp_target(state, N.Navigator(state), warp) for warp in state.warps}


def test_readable_non_warp_rows_stay_diagnostics_and_do_not_offer_a_step():
    targets = _targets()
    for index in (2, 3):
        target = targets[index]
        assert target.unavailable == "not_warp_metatile"
        assert target.final == "step"
        assert target.info.get("untested") is not True
    # The reported row is preserved for diagnostics instead of being deleted.
    ids = {
        target.option_id
        for target in enumerate_targets(_source_state(), N.Navigator(_source_state()))
    }
    assert "warp:90:1:2" in ids and "warp:90:1:3" in ids


def test_real_warp_behaviors_keep_their_trigger_and_approach():
    targets = _targets()
    assert targets[1].final == "step" and targets[1].unavailable is None
    assert targets[5].final == "step" and targets[5].unavailable is None
    assert targets[6].final == "step" and targets[6].info["trigger"] == "step"
    assert targets[7].final == "hold" and targets[7].press == "E"
    assert targets[8].final == "door" and targets[8].press == "N"
    assert targets[8].approaches[0].stand == (1, 4)


def test_unreadable_trigger_keeps_the_untested_policy():
    target = _targets()[4]
    assert target.unavailable is None
    assert target.info.get("untested") is True


def _graph():
    return NavigationGraph(
        {
            SOURCE: _source_state(),
            DEST: _dest_state(),
            NORMAL_DEST: _dest_state(NORMAL_DEST),
        },
        provider=_Provider(),
    )


def _step(graph, x, y, direction):
    node = NavigationNode(*SOURCE, x, y, 0)
    return graph._step_edge(_source_state(), node, direction, CapabilityState(), live=None)


def test_readable_ladder_still_routes_as_a_warp_edge():
    edge = _step(_graph(), 2, 2, "N")
    assert edge is not None and edge.kind == "warp"
    assert edge.destination.map_key == DEST
    assert (edge.destination.x, edge.destination.y) == (2, 1)


def test_step_warp_fires_from_a_differing_elevation_neighbor():
    # Seafoam B3F shape: a ladder whose warp row carries elevation while the walkable
    # approach tile sits at another elevation.  The step warp fires on entry regardless
    # of approach elevation; the elevation filter in ``_warp_at`` must only gate the
    # directional/arrow/door presses, not the automatic step branch.
    rows = (
        _warp(
            1,
            2,
            1,
            1,
            behavior=MB["MB_LADDER"],
            kind="ladder",
            trigger="step",
            elevation=3,
        ),
    )
    state = _state(rows, extra=(((2, 1), 2),))
    graph = NavigationGraph({SOURCE: state, DEST: _dest_state()}, provider=_Provider())
    edge = graph._step_edge(
        state, NavigationNode(*SOURCE, 2, 2, 0), "N", CapabilityState(), live=None
    )
    assert edge is not None and edge.kind == "warp"
    assert edge.destination.map_key == DEST
    assert (edge.destination.x, edge.destination.y) == (2, 1)


def test_readable_non_warp_tiles_are_walked_not_warps():
    graph = _graph()
    cave = _step(graph, 2, 2, "W")  # MB_CAVE row at (1, 2)
    normal = _step(graph, 2, 2, "E")  # MB_NORMAL row at (3, 2)
    assert cave is not None and cave.kind == "walk"
    assert normal is not None and normal.kind == "walk"
    assert not cave.stable_key.startswith("warp:")
    assert not normal.stable_key.startswith("warp:")
