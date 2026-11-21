"""Coordinate goals behind script geometry still expose the existing walking prefix."""
from dataclasses import replace

from test_field_targets_hidden_flags import _object, _state
from test_planner_fossil_step import _FakeGraph
from test_probe_primitives import snapshot

from pokegent.actions.field.builder import FieldBuilder
from pokegent.actions.field.nav import Navigator
from pokegent.actions.field.targets import plan_target, tile_target
from pokegent.goals.models import Objective
from pokegent.goals.validate import ResolvedObjective, ResolvedTarget
from pokegent.navigation.adapter import NavigationAdapter
from pokegent.navigation.models import (
    NavigationEdge,
    NavigationNode,
    NavigationPlan,
    NavigationTarget,
)
from pokegent.options.registry import BuildContext
from pokegent.state.scenes import SceneResult


def blocked_point_frame():
    # Invented closed horizontal gate separates the target from the player. Its
    # switch NPC is a ROM template outside the runtime object slots.
    obj = _object(1, role='npc', x=3, y=3)
    state = _state((obj,))
    player = state.player.model_copy(update={'avatar_x': 0, 'avatar_y': 4,
        'x': 0, 'y': 4, 'save_x': 0, 'save_y': 4, 'prev_x': 0, 'prev_y': 4})
    blocks = list(state.grid.blocks)
    for x in range(5):
        blocks[(2 + 7) * 19 + x + 7] = 0xC00
    state = state.model_copy(update={'player': player,
        'grid': state.grid.model_copy(update={'blocks': tuple(blocks)})})
    start = NavigationNode(0, 0, 0, 4)
    stand = NavigationNode(0, 0, 3, 4)
    route = tuple(NavigationEdge(f'walk:{x}:4', NavigationNode(0, 0, x - 1, 4),
        NavigationNode(0, 0, x, 4), 'walk', 1, direction='E') for x in range(1, 4))
    toggle = NavigationEdge('toggle:0:0:1:77', stand, stand, 'toggle', 6,
        direction='N', executor_target=(('kind', 'toggle'), ('local_id', 1),
            ('event', 1), ('flag', 77), ('toggle_to', True)))
    goal = NavigationTarget(0, 0, x=3, y=0)
    plan = NavigationPlan(start, goal, 14, (*route, toggle,
        NavigationEdge('walk:beyond_gate', stand, NavigationNode(0, 0, 3, 0), 'walk', 4)))
    objective = Objective(objective_id='invented_point', revision=1, kind='reach',
        target={'map': 'DemoMap', 'x': 3, 'y': 0})
    resolved = ResolvedObjective(objective, ResolvedTarget(group=0, number=0, x=3, y=0), ())
    graph = _FakeGraph(names={(0, 0): 'DemoMap'})
    adapter = NavigationAdapter(graph)
    adapter.plan = lambda *_args, **_kwargs: plan
    builder = FieldBuilder(field_menu=False, objectives=lambda _: resolved, navigation=adapter)
    snap = snapshot().model_copy(update={'map': state})
    ctx = BuildContext(snapshot=snap,
        scene=SceneResult(scene_id='field.free', confidence=1, detector='invented'),
        goal_id=objective.objective_id)
    return builder, ctx, state, plan


def test_same_map_blocked_point_keeps_executable_planned_prefix():
    builder, ctx, state, plan = blocked_point_frame()
    nav = Navigator(state)
    assert plan_target(nav, nav.search(), tile_target(state, 3, 0)) is None
    assert state.objects[0].live is None and plan.target.local_id is None
    draft = builder.build(ctx)
    assert not any(o.id == 'talk:1' for o in draft.options)
    forward = [o for o in draft.options if o.chooseable and o.id.startswith('tile:')]
    assert forward
    assert all(o.facts['target']['y'] == 4 for o in forward)
    assert all(o.executor_ref == 'field.walk_to' for o in forward)
    assert any(o.facts.get('next_route_step') for o in forward)
    assert next(o for o in forward if o.facts.get('next_route_step')).id == 'tile:3:4'
    assert any(o.id == 'tile:3:0' for o in (*draft.options, *draft.all_options))


def test_changed_live_geometry_refuses_stale_prefix_tiles():
    builder, ctx, state, _ = blocked_point_frame()
    blocks = list(state.grid.blocks)
    for y in (3, 4):
        blocks[(y + 7) * 19 + 1 + 7] = 0xC00
    blocked = state.model_copy(update={'grid': state.grid.model_copy(
        update={'blocks': tuple(blocks)})})
    ctx = replace(ctx, snapshot=ctx.snapshot.model_copy(update={'map': blocked}))
    draft = builder.build(ctx)
    assert not any(o.chooseable and o.id.startswith('tile:') for o in draft.options)


def test_directly_reachable_point_does_not_add_prefix_alternatives():
    builder, ctx, state, _ = blocked_point_frame()
    blocks = list(state.grid.blocks)
    for x in range(5):
        blocks[(2 + 7) * 19 + x + 7] = 0
    open_state = state.model_copy(update={'grid': state.grid.model_copy(
        update={'blocks': tuple(blocks)})})
    ctx = replace(ctx, snapshot=ctx.snapshot.model_copy(update={'map': open_state}))
    draft = builder.build(ctx)
    tiles = [o.id for o in draft.options if o.chooseable and o.id.startswith('tile:')]
    assert tiles == ['tile:3:0']


def test_non_toggle_plan_does_not_add_same_map_prefix():
    builder, ctx, _, plan = blocked_point_frame()
    without_toggle = replace(plan, route=tuple(e for e in plan.route if e.kind != 'toggle'))
    builder.navigation.plan = lambda *_args, **_kwargs: without_toggle
    assert not any(o.chooseable and o.id.startswith('tile:') for o in builder.build(ctx).options)
