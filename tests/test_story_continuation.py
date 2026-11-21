"""Invented acquisition targets through the real field and snapshot owners."""
from test_probe_primitives import snapshot

from pokegent.actions.field.builder import FieldBuilder
from pokegent.games.gen3.firered.maps.map_schema import (
    CoordEventInfo,
    MapGrid,
    MapIdentity,
    MapLayoutInfo,
    MapObject,
    MapState,
    PlayerPlacement,
)
from pokegent.goals.models import Objective
from pokegent.goals.validate import ResolvedObjective, ResolvedTarget
from pokegent.options.registry import BuildContext
from pokegent.state.scenes import SceneResult


def invented_state():
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
    blocks = [0x3ff] * (17 * 17)
    for y in range(3):
        for x in range(3):
            blocks[(y+7)*17+x+7] = 0
    obj = MapObject(local_id=1, graphics_id=1, role='npc', x=1, y=1,
        elevation=0, movement_type=0, movement_type_name=None, range_x=0, range_y=0,
        trainer_type=0, trainer_range=0, script_ptr=0x08000000, hidden_flag_id=1,
        hidden_flag_set=False, source='template', blocks_tile=True)
    return MapState(identity=identity, layout=layout,
        grid=MapGrid(columns=17, rows=17, blocks=tuple(blocks), attributes=((0, 0),)),
        player=player, objects=(obj,))


def talk_options(state):
    objective = Objective(objective_id='partial_acquisition', revision=1, kind='talk',
        target={'map': 'DemoLab', 'npc': 1}, success_probes=[{'kind':'item','item':17}])
    resolved = ResolvedObjective(objective, ResolvedTarget(group=0, number=0, npc=1), ())
    builder = FieldBuilder(field_menu=False, objectives=lambda _: resolved)
    ctx = BuildContext(snapshot=snapshot().model_copy(update={'map':state}),
        scene=SceneResult(scene_id='field.free', confidence=1, detector='invented'),
        goal_id=objective.objective_id)
    return builder.build(ctx).options


def test_actual_talk_target_offer_and_unknown_hidden_blocked_refusal():
    state = invented_state()
    option = next(o for o in talk_options(state) if o.id == 'talk:1')
    assert option.chooseable and option.executor_ref == 'field.talk_to'
    assert option.facts['serves_goal'] is True
    for hidden in (True, None):
        changed = state.model_copy(update={'objects':(
            state.objects[0].model_copy(update={'hidden_flag_set':hidden}),)})
        assert not any(o.id == 'talk:1' and o.chooseable for o in talk_options(changed))
    blocked = state.model_copy(update={'objects':(
        state.objects[0].model_copy(update={'y':0}),), 'grid':state.grid.model_copy(
        update={'blocks':tuple([0xC00] * (17 * 17))})})
    assert not any(o.id == 'talk:1' and o.chooseable for o in talk_options(blocked))
    assert not any(o.id == 'talk:1' and o.chooseable for o in talk_options(None))


def test_actual_trigger_surface_offer_inactive_and_unknown_refusal():
    from collections import Counter, defaultdict

    from pokegent.games.gen3.firered.rom.script_effects import interpret
    from pokegent.navigation.graph import NavigationGraph
    event = CoordEventInfo(index=0, x=1, y=1, elevation=0, var_id=0x4000,
        var_value=0, script_ptr=0x08000000, weather_only=False, active=True)
    state = invented_state().model_copy(update={'objects':(), 'coord_events':(event,)})
    result = interpret(b'\x16\x01\x40\x01\x00\x02', 0x08000000)
    graph = NavigationGraph.__new__(NavigationGraph)
    graph._script_incomplete_seen = set()
    graph.script_incomplete_counts = Counter()
    graph._script_unknown_sites = defaultdict(set)
    graph._script_interpretation = lambda *a, **k: result
    graph._script_effects = lambda ptr, state, **kw: graph._complete_effects(
        kw['reader'], result, state=state, site=kw.get('site'))
    graph._profile = None
    graph.regions = None
    graph._script_gate_at = lambda *args: None
    graph.maps = {(0, 0):state}
    builder = FieldBuilder(field_menu=False, graph=graph)
    def options(current):
        return builder.build(BuildContext(
            snapshot=snapshot().model_copy(update={'map':current}),
            scene=SceneResult(scene_id='field.free', confidence=1, detector='invented'))).options
    assert any(o.id == 'trigger:1:1' and o.chooseable for o in options(state))
    for active in (False, None):
        changed = state.model_copy(update={'coord_events':(
            event.model_copy(update={'active':active}),)})
        assert not any(o.id == 'trigger:1:1' and o.chooseable for o in options(changed))


def consumer_fixture(monkeypatch):
    import struct
    from types import SimpleNamespace

    from pokegent.games.gen3.firered.rom import story_generation as gen
    from pokegent.games.gen3.firered.rom.setup_source import Source
    base = 0x08000000
    image = bytearray(300)
    for at in (16, 24):
        image[at:at+6] = b'\x69\x05' + struct.pack('<I', base+64)
    common = b'\xc7\x00'
    common += b'\x4f'+struct.pack('<HI', 1, base+256)
    common += b'\x4f'+struct.pack('<HI', 2, base+256)+b'\x51\x00\x00'
    common += b'\x21'+struct.pack('<HH', 0x4001, 3)
    common += b'\x06\x01'+struct.pack('<I', base+200)
    common += b'\x2b\x28\x00\x06\x00'+struct.pack('<I', base+180)
    common += b'\x16'+struct.pack('<HH', 0x4002, 1)+b'\x02'
    image[64:64+len(common)] = common
    image[180] = 2
    image[200:212] = (b'\x16'+struct.pack('<HH', 0x8004, 1)
                      +b'\x21'+struct.pack('<HH', 0x4003, 5)+b'\x02\x02')
    image[256] = 0xfe
    facts = SimpleNamespace(number=lambda s: {'SCENE':0x4001, 'TRIGGER':0x4002,
        'LATER':0x4003, 'TICKET':40, 'NPC_TEXT_COLOR_MALE':0,
        'VAR_TEXT_COLOR':0x8012, 'VAR_PREV_TEXT_COLOR':0x8013, '0':0}[s])
    text = ('Left::\n lockall\n goto Common\n end\n'
            'Right::\n lockall\n goto Common\n end\n'
            'Common::\n goto_if_eq SCENE, 3, Ferry\n end\n'
            'Ferry::\n setvar VAR_0x8004, DEST\n goto_if_ge LATER, 5, Other\n end\n')
    rows = [dict(type='trigger', x=i, y=1, elevation=0, var='TRIGGER', var_value='0',
                 script=label) for i, label in enumerate(('Left','Right'))]
    events = tuple(CoordEventInfo(index=i, x=i, y=1, elevation=0, var_id=0x4002,
        var_value=0, script_ptr=base+at, weather_only=False) for i, at in enumerate((16,24)))
    metadata = {'coord_events':rows}
    state = SimpleNamespace(coord_events=events)
    monkeypatch.setattr(gen, 'continuation_map', lambda *args:
                        (facts, 'Demo', [0,0], metadata, text, state))
    source = Source({'asm/macros/event.inc':b'.macro textcolor color:req\n.byte 0xc7\n'
        b'.byte \\color\n.endm\n', 'src/scrcmd.c':
        b'gSpecialVar_PrevTextColor = gSpecialVar_TextColor;\n'
        b'gSpecialVar_TextColor = ScriptReadByte(ctx);'}, 'invented')
    profile = SimpleNamespace(symbol=lambda _: SimpleNamespace(address=None))
    spec = SimpleNamespace(boarding_map='MAP_DEMO', boarding_scripts=['Left','Right'],
        scene_var='SCENE', ticket_flag='TICKET', ticket_trigger_var='TRIGGER')
    return gen, image, profile, source, spec, metadata


def test_consumer_conditional_complete_and_scoped_entry_origin(monkeypatch):
    gen, image, profile, source, spec, _ = consumer_fixture(monkeypatch)
    proof = gen.consumer_proof(image, profile, source, spec, 40)
    assert proof['complete'] is True and proof['premise']['operator'] == 'ne'
    assert proof['premise']['subject'] == 0x4001
    assert proof['premise']['origin'] is None
    for result in proof['results']:
        assert not result['unknown']
        writes = [e for e in result['effects'] if e['kind'] == 'setvar']
        assert len(writes) == 1
        assert proof['premise'] in writes[0]['guards']


def test_consumer_rejects_polarity_operand_and_ambiguous_event(monkeypatch):
    import pytest

    from pokegent.games.gen3.firered.rom.setup_source import SetupError
    gen, original, profile, source, spec, metadata = consumer_fixture(monkeypatch)
    for at, value in ((89, 0), (84, 2), (65, 1)):
        image = original.copy()
        image[at] = value
        with pytest.raises(SetupError):
            gen.consumer_proof(image, profile, source, spec, 40)
    metadata['coord_events'][1]['script'] = 'Left'
    with pytest.raises(SetupError):
        gen.consumer_proof(original, profile, source, spec, 40)


def test_attempt_guard_and_tail_negatives_do_not_claim_capacity():
    import struct
    from dataclasses import replace

    import pytest

    from pokegent.games.gen3.firered.rom.script_effects import Effect, Guard, Interpretation
    from pokegent.games.gen3.firered.rom.setup_source import SetupError
    from pokegent.games.gen3.firered.rom.story_generation import gift_attempt
    guard = Guard('flag', 60, 'eq', False)
    item = Effect('additem', 16, (guard,), (('item',17),('count',1)))
    flag = Effect('setflag', 24, (guard,), (('flag',60),('set',True)))
    scene = Effect('setvar', 32, (guard,), (('var',0x4001),('value',1)))
    image = bytes(32)+b'\x16'+struct.pack('<HH',0x4001,1)+b'\x6c\x02'
    result = Interpretation(0x08000000,(item,flag,scene))
    # An unconditional attempt can fail capacity; it is never promoted to item possession.
    attempt = gift_attempt(result,image,17,60,0x4001)
    assert attempt['item']['guards'] == (dict(kind='flag',subject=60,operator='eq',
                                             value=False,origin=None),)
    for bad in (replace(result,unknown=('limit',)),
                replace(result,effects=(replace(item,guards=()),flag,scene)),
                replace(result,effects=(item,replace(flag,guards=()),scene)),
                replace(result,effects=(replace(item,values=(('item',18),('count',1))),flag,scene)),
                replace(result,effects=(item,flag,replace(scene,values=(('var',0x4001),('value',2)))))):
        with pytest.raises(SetupError):
            gift_attempt(bad,image,17,60,0x4001)


def test_item_only_endpoint_current_bag_unknown_one_shot_and_or_prefix(tmp_path):
    from types import SimpleNamespace

    import pytest
    from test_story_source import artifact, load, policy_hash
    from test_story_source import snapshot as story_snapshot

    from pokegent.goals.dex_fallback import FallbackUnavailable
    from pokegent.goals.story import StoryComplete
    data = artifact()
    data['nodes'][0]['objective']['success_probes'] = [dict(kind='any_of',children=[
        dict(kind='flag',flag=1),dict(kind='flag',flag=4)])]
    data['nodes'].append(dict(id='r',requires=['q'],objective=dict(objective_id='acquisition',
        revision=1,kind='hold',rationale='Partial item acquisition; no retry guarantee.',
        success_probes=[dict(kind='item',item=17,min_quantity=1)])))
    data['policy'] = dict(order=['p','q','r'],required=['r'])
    data['policy_sha256'] = policy_hash(data['policy'])
    fallback = load(tmp_path,data)
    assert fallback.from_snapshot(story_snapshot())['objective_id'] == 'story_p'
    assert fallback.from_snapshot(story_snapshot(4))['objective_id'] == 'story_q'
    current = story_snapshot(1,2,60)  # one-shot flag is deliberately irrelevant
    assert fallback.from_snapshot(current)['objective_id'] == 'acquisition'
    current.bag = (SimpleNamespace(items=(SimpleNamespace(item_id=17,quantity=1),)),)
    with pytest.raises(StoryComplete):
        fallback.from_snapshot(current)
    current.bag = ()
    current.pc_items = (SimpleNamespace(item_id=17,quantity=1),)
    assert fallback.from_snapshot(current)['objective_id'] == 'acquisition'
    for change in ({'usable':False},):
        bad = SimpleNamespace(**{**current.__dict__,**change})
        with pytest.raises(FallbackUnavailable):
            fallback.from_snapshot(bad)
    # No cached same-map proof: the previously present item is absent again.
    assert fallback.from_snapshot(current)['objective_id'] == 'acquisition'


def test_captain_trailing_comment_retains_exact_command_and_operand_gate(monkeypatch):
    from types import SimpleNamespace

    import pytest

    from pokegent.games.gen3.firered.rom import story_generation as gen
    from pokegent.games.gen3.firered.rom.setup_source import SetupError
    facts = SimpleNamespace(number=lambda s: {'GRAPHICS':1,'VISIBLE':1,'ITEM_PRIZE':17,
        'FLAG_ONE':60,'SCENE':0x4001}[s])
    obj = invented_state().objects[0].model_copy(update={'template_graphics_id':1})
    metadata = {'object_events':[dict(local_id='LOCAL_GUIDE',script='Captain',x='1',y='1',
                                     elevation='0',graphics_id='GRAPHICS',flag='VISIBLE')]}
    state = invented_state().model_copy(update={'objects':(obj,)})
    spec = SimpleNamespace(map='MAP_DEMO',object='LOCAL_GUIDE',script='Captain',
                           item='ITEM_PRIZE',gift_flag='FLAG_ONE',scene_var='SCENE')
    text = ('Captain::\n lock\n goto_if_set FLAG_ONE, Already\n'
            ' giveitem_msg Message, ITEM_PRIZE, 1, MUSIC\n msgbox Explain\n'
            ' setflag FLAG_ONE\n setvar SCENE, 1\n release\n end\n\n @ Unused\n'
            'Already::\n end\n')
    class ReachedInterpretation(Exception):
        pass
    def interpret(*args, **kwargs):
        raise ReachedInterpretation
    monkeypatch.setattr(gen, 'interpret', interpret)
    for altered, expected in ((text,ReachedInterpretation),
                              (text.replace('setvar SCENE, 1','setvar SCENE, 2'),SetupError),
                              (text.replace('release\n end','release\n return'),SetupError)):
        monkeypatch.setattr(gen, 'continuation_map', lambda *args:
                            (facts,'Demo',[0,0],metadata,altered,state))
        with pytest.raises(expected):
            gen.acquisition_proof(b'',SimpleNamespace(
                symbol=lambda _: SimpleNamespace(address=None)),None,spec,'Partial',40)


def test_map_local_aliases_are_isolated_and_conflicts_refused(monkeypatch):
    import json
    from types import SimpleNamespace

    import pytest

    from pokegent.games.gen3.firered.rom import story_generation as gen
    from pokegent.games.gen3.firered.rom.setup_source import SetupError, Source
    files = {'data/maps/map_groups.json':json.dumps(
        {'group_order':['Demo'],'Demo':['First','Second']}).encode()}
    for name, value in (('First','40'),('Second','41')):
        files[f'data/maps/{name}/map.json'] = json.dumps({'id':f'MAP_{name.upper()}'}).encode()
        files[f'data/maps/{name}/scripts.inc'] = f'.equ LOCAL_GATE, {value}\n'.encode()
    files.update({f'include/constants/{name}.h':b'' for name in
        ('flags','opponents','menu','moves','metatile_behaviors','items','vars','event_objects')})
    source = Source(files,'invented')
    monkeypatch.setattr(gen,'MapReader',lambda *args: SimpleNamespace(
        read_static=lambda *key: SimpleNamespace(usable=True,state=object())))
    first = gen.continuation_map(b'',None,source,'MAP_FIRST')[0]
    second = gen.continuation_map(b'',None,source,'MAP_SECOND')[0]
    assert first.number('LOCAL_GATE') == 40 and second.number('LOCAL_GATE') == 41
    files['data/maps/Second/scripts.inc'] += b'.equ LOCAL_GATE, 42\n'
    with pytest.raises(SetupError):
        gen.continuation_map(b'',None,source,'MAP_SECOND')
