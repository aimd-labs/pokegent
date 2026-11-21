"""Actual field draft never advertises a historical dialog result as a live write."""
import struct

import pytest
from test_probe_primitives import snapshot
from test_script_guard_consumers import code, graph_for
from test_script_guard_lifetimes import BASE, RESULT, compare, interpret

from pokegent.actions.field.builder import FieldBuilder
from pokegent.games.gen3.firered.maps.map_schema import (
    CoordEventInfo,
    MapGrid,
    MapIdentity,
    MapLayoutInfo,
    MapState,
    PlayerPlacement,
)
from pokegent.options.registry import BuildContext
from pokegent.state.scenes import SceneResult


@pytest.mark.skip(reason="re-land in #106")
def test_actual_field_builder_preserves_trigger_but_refuses_unproven_write():
    identity = MapIdentity(group=0, number=0, name='DemoLab', layout_id=1,
        region_section_id=0, map_type=0, map_type_name='indoor', weather=0,
        cave=0, floor_num=0, battle_type=0, music=0, biking_allowed=False,
        allow_escaping=False, allow_running=True, identity_verified=True)
    layout = MapLayoutInfo(width=3, height=3, border_width=1, border_height=1,
        border=(0,), primary_tileset=0, secondary_tileset=0, layout_ptr=0)
    player = PlayerPlacement(save_x=1, save_y=2, avatar_x=1, avatar_y=2,
        prev_x=1, prev_y=2, x=1, y=2, position_consistent=True, facing='N',
        elevation=0, object_event_id=0, avatar_flags=0, surfing=False,
        underwater=False, biking=False, controllable_flag=True, forced=False, moving=False,
        tile_transition_state=0, running_state=0)
    blocks = [0x3ff]*(17*17)
    for y in range(3):
        for x in range(3):
            blocks[(y+7)*17+x+7] = 0
    event = CoordEventInfo(index=0, x=1, y=1, elevation=0, var_id=0x4000, var_value=0,
                          script_ptr=BASE, weather_only=False, active=True)
    state = MapState(identity=identity, layout=layout,
        grid=MapGrid(columns=17, rows=17, blocks=tuple(blocks), attributes=((0, 0),)),
        player=player, coord_events=(event,))
    raw = code(b'\x6e\x00\x00', compare(RESULT, 1), (1, 'write'), b'\x02', 'write',
               b'\x16'+struct.pack('<HH', 0x4001, 3), b'\x02')
    result = interpret(raw, BASE)
    assert result.complete
    graph = graph_for(result)
    graph.regions = None
    graph._script_gate_at = lambda *a: None
    graph.maps = {(0, 0):state}
    ctx = BuildContext(snapshot=snapshot().model_copy(update={'map':state}),
                       scene=SceneResult(scene_id='field.free', confidence=1, detector='invented'))
    options = FieldBuilder(field_menu=False, graph=graph).build(ctx).options
    triggers = [option for option in options if option.id == 'trigger:1:1']
    assert len(triggers) == 1 and triggers[0].chooseable
    assert triggers[0].facts['set_vars'] == ()
    assert graph.script_incomplete_counts['coord_trigger_options'] == 1
    # A deterministic write has no speculative condition and remains advertised.
    graph._script_interpretation = lambda *a, **k: interpret(
        b'\x16'+struct.pack('<HH', 0x4001, 3)+b'\x02', BASE)
    options = FieldBuilder(field_menu=False, graph=graph).build(ctx).options
    trigger = next(option for option in options if option.id == 'trigger:1:1')
    assert trigger.facts['set_vars'] == ((0x4001, 3),)
