"""Connected invented graph, menu, barrier and field consumers of symbolic guards."""
import struct
from collections import Counter, defaultdict
from types import SimpleNamespace as NS

from test_script_guard_lifetimes import BASE, RESULT, compare, flags, guarded, interpret, program

from pokegent.games.gen3.firered.rom.script_effects import Effect, Guard, ScriptContext
from pokegent.navigation.graph import NavigationGraph
from pokegent.navigation.models import CapabilityState, NavigationNode


def code(*rows):
    raw, labels, patches = bytearray(), {}, []
    for row in rows:
        if isinstance(row, str):
            labels[row] = len(raw)
        elif isinstance(row, tuple):
            condition, label = row
            patches.append((len(raw)+2, label))
            raw += b'\x06'+bytes([condition])+bytes(4)
        else:
            raw += row
    for offset, label in patches:
        struct.pack_into('<I', raw, offset, BASE+labels[label])
    return bytes(raw)


def graph_for(result):
    graph = NavigationGraph.__new__(NavigationGraph)
    graph._script_incomplete_seen = set()
    graph.script_incomplete_counts = Counter()
    graph._script_unknown_sites = defaultdict(set)
    graph._script_interpretation = lambda *a, **k: result
    graph._script_effects = lambda ptr, state, **kw: graph._complete_effects(
        kw['reader'], result, state=state, site=kw.get('site'))
    graph._profile = None
    return graph


def state():
    event = NS(kind='sign', script_ptr=BASE, x=1, y=1, index=0)
    obj = NS(role='npc', script_ptr=BASE, x=1, y=1, local_id=1)
    return NS(identity=NS(group=0, number=0), bg_events=(event,), objects=(obj,),
              coord_events=(), layout=NS(width=3, height=3), collision=lambda x, y: 0,
              elevation=lambda x, y: 0,
              warps=(NS(dynamic=True, index=0, tile_kind='arrow_warp'),))


def test_ordinary_barrier_and_trainer_capabilities_are_preserved():
    cap = CapabilityState(flags=frozenset({77, 0x501}), vars=((0x4001, 3),))
    for guard in (Guard('flag', 77, 'eq', True), Guard('trainer', 1, 'eq', True),
                  Guard('var', 0x4001, 'ge', 2)):
        assert NavigationGraph._requirement_from_guard(guard, cap).met
    assert NavigationGraph._positive_requirement((Guard('flag', 77, 'eq', True),)) == 'flag:77'
    path = (Guard('trainer', 1, 'eq', True),)
    effects = (Effect('trainerbattle', 1, path), Effect('trainerbattle', 2, path),
        Effect('removeobject', 3, path),
        Effect('setvar', 4, path, (('var', 0x4001), ('value', 2))))
    expected = 'var:16385:ge:2'
    assert NavigationGraph._coordinate_completion_requirement(effects, 0x4001, 1) == expected


def test_domain_refusal_does_not_remove_independent_proven_barrier_effects():
    result = guarded(b'\x6e\x00\x00')
    clean = Effect('setflag', 999, (Guard('flag', 77, 'eq', True),), (('flag', 88),))
    result = result.__class__(result.script_ptr, (*result.effects, clean), result.unknown)
    graph = graph_for(result)
    assert graph._complete_effects('on_load_barriers:on_load', result, state=state()) == (clean,)
    assert graph.script_incomplete_counts['on_load_barriers:on_load'] == 1
    assert result.complete  # projection never mutates the original interpretation


def test_elevator_selector_and_floor_setting_classification_survive():
    def recipe(writer):
        return code(b'\x25'+struct.pack('<H', 216), writer,
            b'\x19'+struct.pack('<HH', 0x8000, RESULT), compare(0x8000, 0),
            (1, 'ride'), b'\x02', 'ride',
            b'\x16'+struct.pack('<HH', 0x403A, 1),
            b'\x3f\x00\x01\x00'+struct.pack('<HH', 1, 1), b'\x02')
    for writer, expected in [(b'\x6f\x00\x00\x07\x00', True), (b'\xa0', False)]:
        result = interpret(recipe(writer), BASE)
        assert result.complete
        graph = graph_for(result)
        graph.maps = {(0, 0):state(), (0, 1):NS(warps=(), bg_events=())}
        rows = graph._effect_elevators().get((0, 0), ())
        assert bool(rows) is expected
        if expected:
            assert rows[0][3:5] == (0, (0, 1))
            assert rows[0][8:10] == (0x403A, 1)


def test_ferry_selector_quest_log_and_durable_membership_survive():
    raw = code(b'\x25'+struct.pack('<H', 391), compare(RESULT, 2), (5, 'idle'), b'\x02',
        'idle', compare(0x4076, 5), (0, 'menu'), b'\x02', 'menu',
        b'\x6f\x00\x00\x07\x00', b'\x19'+struct.pack('<HH', 0x8000, RESULT),
        compare(0x8000, 0), (1, 'ride'), b'\x02', 'ride',
        b'\x16'+struct.pack('<HH', 0x8004, 1),
        b'\x16'+struct.pack('<HH', 0x8006, 2), b'\x25'+struct.pack('<H', 379), b'\x02')
    result = interpret(raw, BASE, context=ScriptContext(
        seagallop_warps=((2, (0, 1, 0, 1, 1)),), preserve_seagallop_special_vars=True))
    assert result.complete, result.unknown
    graph = graph_for(result)
    graph.maps = {(0, 0):state(), (0, 1):NS(objects=(), coord_events=())}
    graph._walkable_landing = lambda st, x, y: NavigationNode(0, 1, x, y)
    rows = graph._effect_ferries()
    routes = next(iter(rows.values()))
    assert len(routes) == 1
    route = routes[0]
    assert route.menu_index == 0 and route.menu_id == 7 and route.selector == 2
    assert not any(g.subject == 0x8000 for g in route.guards)
    assert any(g.subject == RESULT and g.origin is not None for g in route.guards)
    edge = graph._ferry_edge(route, CapabilityState(quest_log_state=0, vars=((0x4076, 0),)))
    assert edge is not None and all(r.met for r in edge.requirements)


def test_only_matching_yesno_lifetime_marks_battle_declinable():
    yesno = flags(interpret(program(), BASE))[0].guards[1]
    yesno = yesno.__class__('var', RESULT, 'ne', 0, yesno.origin)
    decline = yesno.__class__('var', RESULT, 'eq', 0, yesno.origin)
    assert NavigationGraph._coordinate_battle_is_declinable((
        Effect('trainerbattle', 1, (yesno,)), Effect('setvar', 2, (decline,))))
    other = flags(guarded(b'\x6e\x00\x00'))[0].guards[0]
    other = other.__class__('var', RESULT, 'eq', 0, other.origin)
    assert not NavigationGraph._coordinate_battle_is_declinable((
        Effect('trainerbattle', 1, (yesno,)), Effect('setvar', 2, (other,))))


def test_native_elevator_cursor_and_actual_list_response_have_distinct_lifetimes():
    for selector, expected in ((1, True), (2, False)):
        raw = code(b'\x25'+struct.pack('<H', 216),
            b'\x26'+struct.pack('<HH', RESULT, 440),
            b'\x19'+struct.pack('<HH', 0x8000, RESULT), compare(0x8000, 0),
            (1, 'menu'), b'\x02', 'menu', b'\x16'+struct.pack('<HH', 0x8004, selector),
            b'\x25'+struct.pack('<H', 344), b'\x19'+struct.pack('<HH', 0x8000, RESULT),
            compare(0x8000, 1), (1, 'ride'), b'\x02', 'ride',
            b'\x3f\x00\x01\x00'+struct.pack('<HH', 1, 1), compare(0x403A, 1),
            (1, 'done'), b'\x16'+struct.pack('<HH', 0x403A, 1), 'done', b'\x02')
        result = interpret(raw, BASE)
        assert result.complete, result.unknown
        graph = graph_for(result)
        graph.maps = {(0, 0):state(), (0, 1):NS(warps=(), bg_events=())}
        rows = graph._effect_elevators().get((0, 0), ())
        assert bool(rows) is expected
        if expected:
            assert rows[0][3] == 1  # actual menu choice, not the earlier default cursor zero
            assert rows[0][8:10] == (0x403A, 1)


def test_native_ferry_converted_response_retains_page_membership():
    raw = code(b'\x25'+struct.pack('<H', 391), compare(RESULT, 2), (5, 'idle'), b'\x02',
        'idle', compare(0x4076, 5), (4, 'menu'), b'\x02', 'menu',
        b'\x16'+struct.pack('<HH', 0x8004, 1), b'\x16'+struct.pack('<HH', 0x8005, 0),
        b'\x25'+struct.pack('<H', 423), b'\x26'+struct.pack('<HH', RESULT, 424),
        b'\x19'+struct.pack('<HH', 0x8000, RESULT), compare(0x8000, 1), (1, 'ride'),
        b'\x02', 'ride', b'\x16'+struct.pack('<HH', 0x8006, 2),
        b'\x25'+struct.pack('<H', 379), b'\x02')
    result = interpret(raw, BASE, context=ScriptContext(
        seagallop_warps=((2, (0, 1, 0, 1, 1)),), preserve_seagallop_special_vars=True))
    assert result.complete
    graph = graph_for(result)
    graph.maps = {(0, 0):state(), (0, 1):NS(objects=(), coord_events=())}
    graph._walkable_landing = lambda st, x, y: NavigationNode(0, 1, x, y)
    route = next(iter(graph._effect_ferries().values()))[0]
    menu = next(effect for effect in result.effects if effect.value('special') == 423)
    assert route.menu_index is None and route.menu_id == 0xFF
    assert route.menu_pcs == ((0, BASE+menu.source+4),)
    edge = graph._ferry_edge(route, CapabilityState(quest_log_state=0, vars=((0x4076, 6),)))
    assert edge is not None and all(row.met for row in edge.requirements)
