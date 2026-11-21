"""Invented sign/NPC switches must keep their ROM event namespaces separate."""
from types import SimpleNamespace

import pytest
from test_planner_fossil_step import _FakeGraph, _snapshot

from pokegent.actions.field.builder import FieldBuilder
from pokegent.actions.field.macros import FieldMacros
from pokegent.actions.field.targets import Approach
from pokegent.actions.field.targets import Target as FieldTarget
from pokegent.goals.models import Objective
from pokegent.goals.validate import ResolvedObjective, ResolvedTarget
from pokegent.navigation.adapter import NavigationAdapter
from pokegent.navigation.models import (
    NavigationEdge,
    NavigationNode,
    NavigationPlan,
    NavigationTarget,
)
from pokegent.options.models import Legality, Option


def fixture(local_id):
    start = NavigationNode(0, 0, 1, 3)
    stand = NavigationNode(0, 0, 2, 2)
    end = NavigationNode(0, 0, 4, 1)
    toggle = NavigationEdge('toggle:0:0:1:77', stand, stand, 'toggle', 6,
        direction='N', executor_target=(('kind', 'toggle'), ('event', 1),
        ('local_id', local_id), ('flag', 77), ('toggle_to', True)))
    route = (NavigationEdge('walk:first', start, stand, 'walk', 2), toggle,
        NavigationEdge('walk:last', stand, end, 'walk', 3))
    plan = NavigationPlan(start, NavigationTarget(0, 0, x=4, y=1), 11, route)
    objective = Objective(objective_id='invented_switch_route', revision=1,
        kind='reach', target={'map': 'InventedSwitchRoom', 'x': 4, 'y': 1})
    resolved = ResolvedObjective(objective, ResolvedTarget(group=0, number=0, x=4, y=1), ())
    options = [Option(id='sign:1', label='Read sign', legality=Legality.LEGAL,
        executor_ref='field.interact_with', facts={'target': {'kind': 'sign',
            'map': [0, 0], 'bg': 1}}),
        Option(id='talk:1', label='Talk to person', legality=Legality.LEGAL,
        executor_ref='field.talk_to', facts={'target': {'kind': 'npc',
            'map': [0, 0], 'local_id': 1}})]
    adapter = NavigationAdapter(_FakeGraph(names={(0, 0): 'InventedSwitchRoom'}))
    return adapter, plan, resolved, options


@pytest.mark.parametrize('local_id,expected', [(0, 'sign:1'), (1, 'talk:1')])
def test_toggle_marks_only_matching_event_namespace(local_id, expected):
    adapter, plan, resolved, options = fixture(local_id)
    annotated, _ = adapter.annotate(options, _snapshot(group=0, number=0, x=1, y=3),
        resolved, plan=plan)
    next_steps = [o for o in annotated if o.facts.get('next_route_step')]
    assert [o.id for o in next_steps] == [expected]
    assert next_steps[0].facts['planned_approach'] == {'stand': [2, 2], 'face': 'N'}
    # Same-map geometry mutation stays in the plan; it is not a map transition.
    assert plan.route[1].source == plan.route[1].destination
    target = FieldTarget(expected, 'sign' if local_id == 0 else 'npc', 'talk', 'Interact',
        approaches=(Approach((1, 2), 'E'), Approach((2, 2), 'N')))
    ctx = SimpleNamespace(option=next_steps[0])
    bound, error = FieldMacros._bind_planned_approach(ctx, target)
    assert error is None and bound.approaches == (Approach((2, 2), 'N'),)
    unavailable = FieldTarget(expected, target.kind, 'talk', 'Interact',
        approaches=(Approach((1, 2), 'E'),))
    assert FieldMacros._bind_planned_approach(ctx, unavailable)[1] == 'planned_approach_unavailable'


@pytest.mark.parametrize('local_id,word', [(0, 'sign'), (1, 'NPC')])
def test_toggle_route_label_names_interaction(local_id, word):
    adapter, plan, _, _ = fixture(local_id)
    builder = FieldBuilder(navigation=adapter)
    assert word in builder._navigation_step_label(plan.route[1])
    assert 'travel to' not in builder._navigation_step_label(plan.route[1])


@pytest.mark.parametrize('local_id', [0, 1])
def test_explicit_switch_option_keeps_exact_stand_and_flag_matching(local_id):
    adapter, plan, _, _ = fixture(local_id)
    option = Option(id='switch:invented:2:2', label='Press switch',
        legality=Legality.LEGAL, executor_ref='special.press_switch',
        facts={'stand_tile': [2, 2], 'state_bit': 77})
    assert adapter._matches(option, plan.route[1], plan)
    wrong_flag = option.model_copy(update={'facts': {'stand_tile': [2, 2], 'state_bit': 78}})
    assert not adapter._matches(wrong_flag, plan.route[1], plan)
