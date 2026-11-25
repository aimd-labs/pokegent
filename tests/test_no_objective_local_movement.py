"""A goal-less field frame offers bounded ordinary adjacent land walks (issue #162).

The choices reuse the existing tile target, option facts and ``field.walk_to`` executor.
Nothing here is an exploration policy: only the four immediate neighbours are considered,
and a withdrawn objective id is never silently replaced by local movement.
"""

import pytest
from test_field_targets_hidden_flags import _object, _state
from test_probe_primitives import snapshot

from pokegent.actions.field.builder import FieldBuilder
from pokegent.games.gen3.firered.maps.map_schema import (
    ConnectionInfo,
    CoordEventInfo,
    WarpInfo,
)
from pokegent.goals.models import Objective
from pokegent.goals.validate import ResolvedObjective, ResolvedTarget
from pokegent.options.registry import BuildContext
from pokegent.state.scenes import SceneResult


def _context(state, *, goal_id=None, busy=False, usable=True):
    return BuildContext(
        snapshot=snapshot(usable=usable).model_copy(update={"map": state}),
        scene=SceneResult(
            scene_id="field.free",
            confidence=1,
            detector="synthetic",
            busy=busy,
        ),
        goal_id=goal_id,
    )


def _frame(state, *, goal_id=None, busy=False, usable=True, **builder_kwargs):
    return FieldBuilder(field_menu=False, **builder_kwargs).build(
        _context(state, goal_id=goal_id, busy=busy, usable=usable)
    )


def _tile_options(draft):
    return [
        option
        for option in draft.options
        if option.chooseable and option.id.startswith("tile:")
    ]


def _with_block(state, x, y, block, *, attributes=((0, 0),)):
    blocks = list(state.grid.blocks)
    blocks[(y + 7) * state.grid.columns + x + 7] = block
    return state.model_copy(
        update={
            "grid": state.grid.model_copy(
                update={"blocks": tuple(blocks), "attributes": attributes}
            )
        }
    )


def test_goal_less_open_land_offers_the_four_adjacent_ordinary_walks():
    draft = _frame(_state(()))
    tiles = _tile_options(draft)
    assert {option.id for option in tiles} == {
        "tile:2:1",
        "tile:2:3",
        "tile:1:2",
        "tile:3:2",
    }
    assert all(option.executor_ref == "field.walk_to" for option in tiles)
    assert all(option.postcondition_ref == "field.at_tile" for option in tiles)
    assert all(
        (option.executor_args["x"], option.executor_args["y"])
        == tuple(int(part) for part in option.id.split(":")[1:])
        for option in tiles
    )
    assert all(option.facts["steps"] == 1 for option in tiles)
    assert all(option.facts.get("serves_goal") is False for option in tiles)
    assert all(option.facts.get("route_step") is not True for option in tiles)
    assert all(option.facts.get("on_route") is not True for option in tiles)
    assert draft.context["objective"].startswith("none set")


def test_goal_less_choices_are_deterministic_and_at_most_four():
    state = _state(())
    first = tuple(option.id for option in _tile_options(_frame(state)))
    for _ in range(3):
        assert tuple(option.id for option in _tile_options(_frame(state))) == first
    assert len(first) == 4


def test_collision_npc_water_unknown_warp_connection_script_ledge_and_forced_are_excluded():
    base = _state(())

    collision = _with_block(base, 2, 1, 0x3FF)
    assert "tile:2:1" not in {o.id for o in _tile_options(_frame(collision))}

    npc = _state((_object(7, role="npc", x=2, y=1),))
    assert "tile:2:1" not in {o.id for o in _tile_options(_frame(npc))}

    water = _with_block(base, 2, 1, 0x1001, attributes=((0, 0), (1, 0x10)))
    assert "tile:2:1" not in {o.id for o in _tile_options(_frame(water))}

    unknown = _with_block(base, 2, 1, 0x1001, attributes=((0, 0), (1, 0x3F)))
    assert "tile:2:1" not in {o.id for o in _tile_options(_frame(unknown))}
    assert "tile:2:0" not in {o.id for o in _tile_options(_frame(unknown))}

    warp = base.model_copy(
        update={
            "warps": (
                WarpInfo(
                    index=0,
                    x=2,
                    y=1,
                    elevation=0,
                    dest_group=1,
                    dest_number=2,
                    dest_name="DemoDest",
                    dest_warp_id=0,
                    dynamic=False,
                    tile_behavior=0,
                    tile_kind="normal",
                    trigger="step",
                    trigger_direction=None,
                ),
            )
        }
    )
    assert "tile:2:1" not in {o.id for o in _tile_options(_frame(warp))}

    script = base.model_copy(
        update={
            "coord_events": (
                CoordEventInfo(
                    index=0,
                    x=2,
                    y=1,
                    elevation=0,
                    var_id=0x4000,
                    var_value=0,
                    script_ptr=0x100,
                    weather_only=False,
                    active=True,
                ),
            )
        }
    )
    assert "tile:2:1" not in {o.id for o in _tile_options(_frame(script))}

    ledge = _with_block(base, 2, 1, 0x1001, attributes=((0, 0), (1, 0x3A)))
    assert not {
        "tile:2:1",
        "tile:2:0",
    } & {o.id for o in _tile_options(_frame(ledge))}

    forced = _with_block(base, 2, 1, 0x1001, attributes=((0, 0), (1, 0x23)))
    assert not {
        "tile:2:1",
        "tile:2:0",
    } & {o.id for o in _tile_options(_frame(forced))}


def test_connection_ring_tile_is_not_an_ordinary_walk():
    base = _state(())
    blocks = list(base.grid.blocks)
    blocks[(-1 + 7) * base.grid.columns + 2 + 7] = 0  # the N connection ring tile
    player = base.player.model_copy(
        update={"save_y": 0, "avatar_y": 0, "prev_y": 0, "y": 0}
    )
    state = base.model_copy(
        update={
            "grid": base.grid.model_copy(update={"blocks": tuple(blocks)}),
            "player": player,
            "connections": (
                ConnectionInfo(
                    direction="N", offset=0, dest_group=1, dest_number=2, dest_name="DemoNorth"
                ),
            ),
        }
    )
    assert "tile:2:-1" not in {o.id for o in _tile_options(_frame(state))}


@pytest.mark.parametrize(
    "state",
    [
        _state(()).model_copy(
            update={"identity": _state(()).identity.model_copy(update={"identity_verified": False})}
        ),
        _state(()),
    ],
)
def test_busy_invalid_or_unverified_frames_offer_no_goal_less_walks(state):
    kwargs = {}
    if state.identity.identity_verified:
        kwargs = {"busy": True}
    assert not _tile_options(_frame(state, **kwargs))


def test_invalid_snapshot_offers_no_goal_less_walks():
    draft = FieldBuilder(field_menu=False).build(_context(_state(()), usable=False))
    assert not _tile_options(draft)


def test_withdrawn_or_unresolvable_goal_id_never_becomes_local_movement():
    draft = _frame(_state(()), goal_id="withdrawn")
    assert not _tile_options(draft)


def test_active_objective_keeps_only_its_own_target_tile():
    objective = Objective(
        objective_id="invented_reach",
        revision=1,
        kind="reach",
        target={"map": "DemoMap", "x": 3, "y": 3},
    )
    resolved = ResolvedObjective(
        objective, ResolvedTarget(group=0, number=0, x=3, y=3), ()
    )
    builder = FieldBuilder(
        field_menu=False, objectives=lambda _: resolved
    )
    draft = builder.build(
        _context(_state(()), goal_id=objective.objective_id)
    )
    assert {option.id for option in _tile_options(draft)} == {"tile:3:3"}
