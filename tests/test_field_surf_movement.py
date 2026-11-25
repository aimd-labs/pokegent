"""Invented water geometry must expose movement while already surfing."""
import pytest
from test_field_targets_hidden_flags import _state
from test_probe_primitives import snapshot

from pokegent.actions.field.builder import FieldBuilder
from pokegent.actions.field.nav import Navigator
from pokegent.actions.field.surf.targets import movement_tiles
from pokegent.actions.field.targets import plan_target, tile_target
from pokegent.goals.models import Objective
from pokegent.goals.validate import ResolvedObjective, ResolvedTarget
from pokegent.options.registry import BuildContext
from pokegent.state.scenes import SceneResult


def water_state():
    state = _state(())
    blocks = list(state.grid.blocks)
    for y in range(5):
        for x in range(5):
            blocks[(y + 7) * 19 + x + 7] = 0x1001 if y else 0x3000
    return state.model_copy(update={
        'grid': state.grid.model_copy(update={'blocks': tuple(blocks),
            'attributes': ((0, 0), (1, 0x15))}),
        'player': state.player.model_copy(update={'surfing': True, 'elevation': 1}),
    })


def frame(state):
    return FieldBuilder(field_menu=False).build(BuildContext(
        snapshot=snapshot().model_copy(update={'map': state}),
        scene=SceneResult(scene_id='field.free', confidence=1, detector='synthetic'),
        goal_id=None,
    ))


def test_surfing_frame_offers_water_movement_alongside_dismount():
    state = water_state()
    nav = Navigator(state)
    assert plan_target(nav, nav.search(), tile_target(state, 3, 2)) is not None
    draft = frame(state)
    options = [o for o in draft.options if o.chooseable]
    assert any(o.id.startswith('dismount:') for o in options)
    moves = [o for o in options if o.id.startswith('tile:')]
    assert {o.id for o in moves} == {'tile:2:1', 'tile:2:4', 'tile:0:2', 'tile:3:2'}
    assert all(o.executor_ref == 'field.walk_to' for o in moves)


def test_on_foot_frame_does_not_advertise_water_movement():
    state = water_state()
    state = state.model_copy(update={'player': state.player.model_copy(update={
        'surfing': False, 'elevation': 3, 'avatar_y': 0, 'y': 0, 'save_y': 0,
        'prev_y': 0})})
    tiles = [o for o in frame(state).options if o.chooseable and o.id.startswith('tile:')]
    # Goal-less local walks may use the ordinary land strip, never the surrounding water.
    assert all(o.id.endswith(':0') for o in tiles)
    assert not {'tile:2:1', 'tile:2:4', 'tile:0:2', 'tile:3:2'} & {o.id for o in tiles}


@pytest.mark.parametrize('block', [0x1401, 0x1002, 0x3000])
def test_free_swim_targets_stop_before_collision_waterfall_or_land(block):
    state = water_state()
    blocks = list(state.grid.blocks)
    blocks[9 * 19 + 10] = block  # east of the swimmer
    state = state.model_copy(update={'grid': state.grid.model_copy(update={
        'blocks': tuple(blocks), 'attributes': ((0, 0), (1, 0x15), (2, 0x13))})})
    assert (3, 2) not in movement_tiles(state, Navigator(state))
    assert not any(o.chooseable and o.id == 'tile:3:2' for o in frame(state).options)


def test_water_coordinate_objective_keeps_its_executable_target():
    state = water_state()
    objective = Objective(objective_id='invented_water', revision=1, kind='reach',
        target={'map': 'DemoMap', 'x': 1, 'y': 3})
    resolved = ResolvedObjective(objective, ResolvedTarget(group=0, number=0, x=1, y=3), ())
    builder = FieldBuilder(field_menu=False, objectives=lambda _: resolved)
    draft = builder.build(BuildContext(snapshot=snapshot().model_copy(update={'map': state}),
        scene=SceneResult(scene_id='field.free', confidence=1, detector='synthetic'),
        goal_id=objective.objective_id))
    option = next(o for o in draft.options if o.id == 'tile:1:3')
    assert option.chooseable and option.executor_ref == 'field.walk_to'
