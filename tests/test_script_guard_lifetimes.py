"""Invented bytecode and connected graph consumers; no extracted script fixtures."""
import struct
from dataclasses import replace

import pytest

from pokegent.games.gen3.firered.rom.script_effects import (
    Effect,
    Guard,
    ScriptContext,
    interpret,
)
from pokegent.navigation.graph import NavigationGraph
from pokegent.navigation.models import CapabilityState

BASE = 0x08000000
RESULT = 0x800D


def compare(var, value):
    return b'\x21' + struct.pack('<HH', var, value)


def program(writer=b'\x6e\x00\x00', first=RESULT, second=RESULT, value=1):
    code = bytearray(compare(first, 0))
    first_branch = len(code)
    code += b'\x06\x01' + bytes(4) + b'\x02'
    body = len(code)
    code += writer + compare(second, value)
    second_branch = len(code)
    code += b'\x06\x01' + bytes(4) + b'\x02'
    success = len(code)
    code += b'\x29\x07\x00\x02'
    struct.pack_into('<I', code, first_branch+2, BASE+body)
    struct.pack_into('<I', code, second_branch+2, BASE+success)
    return bytes(code)


def flags(result):
    return [e for e in result.effects if e.kind == 'setflag']


def guarded(writer, var=RESULT, value=2, condition=5):
    code = writer + compare(var, value)
    code += b'\x06' + bytes([condition]) + struct.pack('<I', BASE+len(code)+7) + b'\x02'
    return interpret(code+b'\x29\x07\x00\x02', BASE)


@pytest.mark.parametrize('writer,first,second,value,present', [
    (b'\x6e\x00\x00', RESULT, RESULT, 1, True),
    (b'', RESULT, RESULT, 1, False),
    (b'', RESULT, RESULT, 0, True),
    (b'\x6e\x00\x00', 0x4001, 0x4002, 1, True),
])
def test_rewritten_result_no_write_and_distinct_register_controls(writer, first, second,
                                                                 value, present):
    result = interpret(program(writer, first, second, value), BASE)
    assert result.complete, result.unknown
    assert bool(flags(result)) is present
    if present and first == second and writer:
        guards = flags(result)[0].guards
        assert [g.value for g in guards] == [0, 1]
        assert guards[0].origin != guards[1].origin


def test_known_value_writer_invalidates_known_result_but_keeps_entry_condition():
    # Known input is a separate API seam; the later menu must supersede it.
    result = interpret(program(), BASE, context=ScriptContext(initial_variables=((RESULT, 0),)))
    assert result.complete and flags(result)
    result = interpret(program(b'\x16'+struct.pack('<HH', RESULT, 1)), BASE)
    assert result.complete and flags(result)
    assert flags(result)[0].guards == (Guard('var', RESULT, 'eq', 0),)


@pytest.mark.parametrize('writer,changes', [(b'\x2b\x01\x00', False),
    (b'\x60\x01\x00', False), (b'\x46\x01\x00\x01\x00', True),
    (b'\x47\x01\x00\x01\x00', True), (b'\xa0', True),
    (b'\x92'+bytes(5), True), (b'\x92'+bytes(4)+b'\x01', False),
    (b'\xb3'+struct.pack('<H', RESULT), True),
    (b'\xb3'+struct.pack('<H', 0x4001), False)])
def test_source_verified_writer_currentness(writer, changes):
    result = interpret(program(writer), BASE)
    assert result.complete, result.unknown
    assert bool(flags(result)) is changes


def test_copy_keeps_value_identity_and_contradictions():
    copy = b'\x19'+struct.pack('<HH', 0x4002, 0x4001)
    result = interpret(program(copy, first=0x4001, second=0x4002), BASE)
    assert result.complete and not flags(result)


def test_repeated_writer_loop_is_bounded_and_not_false_complete():
    code = b'\x6e\x00\x00\x05'+struct.pack('<I', BASE)
    result = interpret(code, BASE, max_steps=8, max_states=10)
    assert not result.complete and result.unknown


def test_exact_quest_log_origin_and_yesno_negative():
    quest = guarded(b'\x25'+struct.pack('<H', 391))
    menu = guarded(b'\x6e\x00\x00')
    cap = CapabilityState(quest_log_state=0)
    assert NavigationGraph._requirement_from_guard(flags(quest)[0].guards[0], cap).met
    assert NavigationGraph._requirement_from_guard(flags(menu)[0].guards[0], cap) is None


def test_quest_log_capability_currentness_before_and_after_mutation():
    read = b'\x25'+struct.pack('<H', 391)
    cut = b'\x25'+struct.pack('<H', 392)
    before = guarded(read+cut)
    after = guarded(cut+read)
    cap = CapabilityState(quest_log_state=0)
    assert NavigationGraph._requirement_from_guard(flags(before)[0].guards[0], cap).met
    assert NavigationGraph._requirement_from_guard(flags(after)[0].guards[0], cap) is None


def test_distinct_lifetimes_do_not_minimize_or_become_entry_requirements():
    first, second = flags(interpret(program(), BASE))[0].guards
    opposite = replace(second, operator='ne', value=0)
    assert not NavigationGraph._opposite_guards(first, opposite)
    clauses = NavigationGraph._minimize_guard_clauses(((first,), (opposite,)))
    assert frozenset() not in clauses
    assert NavigationGraph._positive_requirement((Guard('flag', 77, 'eq', True), second)) is None
    assert NavigationGraph._clearing_requirement((Guard('flag', 77, 'eq', False), second)) is None


def test_trainer_counter_keeps_distinct_path_identity():
    guards = flags(interpret(program(), BASE))[0].guards
    effects = tuple(Effect('trainerbattle', i, (guard,)) for i, guard in enumerate(guards))
    effects += (Effect('setvar', 3, (guards[0],), (('var', 0x4001), ('value', 2))),
                Effect('removeobject', 4, (guards[0],)))
    assert NavigationGraph._coordinate_completion_requirement(effects, 0x4001, 1) is None


def test_opaque_memory_write_is_unknown():
    result = interpret(b'\x11\x01'+struct.pack('<I', 0x02000000)+b'\x02', BASE)
    assert not result.complete and result.unknown


@pytest.mark.parametrize('name', ['random', 'additem', 'removeitem', 'checkitemtype',
    'addpcitem', 'checkpcitem', 'getpartysize', 'yesnobox', 'multichoice',
    'multichoicedefault', 'multichoicegrid', 'givemon', 'giveegg', 'checkplayergender',
    'addcoins', 'removecoins', 'checkmonmodernfatefulencounter', 'checkpartymove'])
def test_each_result_writer_supersedes_a_known_input_value(name):
    from pokegent.games.gen3.firered.rom.script_scan import OPS
    opcode, (_, size) = next((op, spec) for op, spec in OPS.items() if spec[0] == name)
    result = interpret(program(bytes([opcode])+bytes([1])*size,
                               value=6 if name == 'checkpartymove' else 1), BASE,
                       context=ScriptContext(initial_variables=((RESULT, 0),)))
    assert result.complete, result.unknown
    assert flags(result)


def test_register_assignment_does_not_become_an_entry_ram_guard_on_skip_path():
    from test_script_guard_consumers import code
    raw = code(compare(0x4001, 0), (1, 'write'), compare(0x4001, 1), (1, 'skip'), b'\x02',
        'write', b'\x16'+struct.pack('<HH', 0x4001, 1), compare(0x4001, 1),
        (1, 'changed'), b'\x02', 'changed', b'\x29\x07\x00\x02',
        'skip', b'\x29\x08\x00\x02')
    result = interpret(raw, BASE)
    assert result.complete
    changed = next(e for e in flags(result) if e.value('flag') == 7)
    skipped = next(e for e in flags(result) if e.value('flag') == 8)
    assert changed.guards == (Guard('var', 0x4001, 'eq', 0),)
    assert Guard('var', 0x4001, 'eq', 1) in skipped.guards
    assert all(g.origin is None for g in skipped.guards)


def test_result_predicate_cannot_survive_an_unrelated_explicit_result_destination_write():
    raw = b'\x46\x01\x00\x01\x00\x42'+struct.pack('<HH', RESULT, 0x4001)
    result = guarded(raw, value=1, condition=1)
    assert result.complete and flags(result)
    assert not any(g.kind == 'item_space' for g in flags(result)[0].guards)


def test_subtraction_resolves_variable_operand_in_the_known_value_path():
    raw = b'\x18'+struct.pack('<HH', 0x4001, 0x4002)
    context = ScriptContext(initial_variables=((0x4001, 5), (0x4002, 2)))
    raw += compare(0x4001, 3)+b'\x06\x01'+struct.pack('<I', BASE+17)+b'\x02'
    result = interpret(raw+b'\x29\x07\x00\x02', BASE, context=context)
    assert result.complete and flags(result)
    assert next(e for e in result.effects if e.kind == 'subvar').value('value') == 3


@pytest.mark.parametrize('name,var,value,operands', [
    ('gettime', 0x8000, 0, b''), ('gettime', 0x8001, 0, b''),
    ('gettime', 0x8002, 0, b''), ('textcolor', 0x8012, 1, b'\x01'),
    ('textcolor', 0x8013, 3, b'\x01')])
def test_other_deterministic_source_writers_replace_known_values(name, var, value, operands):
    from test_script_guard_consumers import code

    from pokegent.games.gen3.firered.rom.script_scan import OPS
    opcode = next(op for op, spec in OPS.items() if spec[0] == name)
    raw = code(bytes([opcode])+operands, compare(var, value), (1, 'yes'), b'\x02',
               'yes', b'\x29\x07\x00\x02')
    initial = tuple({var: 9, 0x8012: 3}.items())
    result = interpret(raw, BASE, context=ScriptContext(initial_variables=initial))
    assert result.complete and flags(result)
    assert flags(result)[0].guards == ()


@pytest.mark.parametrize('writer,var', [
    (b'\xd3'+struct.pack('<I', BASE), 0x8004),
    (b'\x42'+struct.pack('<HH', 0x4001, 0x4002), 0x4001)])
def test_other_unknown_writers_replace_known_values(writer, var):
    result = interpret(program(writer, first=var, second=var), BASE,
                       context=ScriptContext(initial_variables=((var, 0),)))
    assert result.complete and flags(result)


def test_state_limit_and_serialized_origin_preserve_historical_identity():
    import json
    from dataclasses import asdict

    loop = b'\x6e\x00\x00\x05'+struct.pack('<I', BASE)
    result = interpret(loop, BASE, max_steps=50, max_states=4)
    assert any('state limit' in reason for reason in result.unknown)
    first, second = flags(interpret(program(), BASE))[0].guards
    assert len({first, second}) == 2
    assert json.loads(json.dumps(asdict(first)))['origin'] is None
    assert json.loads(json.dumps(asdict(second)))['origin'] is not None
    assert repr(Guard('var', RESULT, 'eq', 0)) == repr(first)


def test_quest_log_mutation_currentness_is_local_to_its_executed_branch():
    from test_script_guard_consumers import code
    read = b'\x25'+struct.pack('<H', 391)
    cut = b'\x25'+struct.pack('<H', 392)
    raw = code(compare(0x4001, 0), (1, 'cut'), read, compare(RESULT, 2),
        (5, 'unchanged'), b'\x02', 'unchanged', b'\x29\x07\x00\x02',
        'cut', cut+read, compare(RESULT, 2), (5, 'changed'), b'\x02',
        'changed', b'\x29\x08\x00\x02')
    result = interpret(raw, BASE)
    assert result.complete
    cap = CapabilityState(quest_log_state=0, vars=((0x4001, 1),))
    for effect in flags(result):
        guard = next(g for g in effect.guards if g.subject == RESULT)
        projected = NavigationGraph._requirement_from_guard(guard, cap)
        assert (projected is not None) is (effect.value('flag') == 7)


@pytest.mark.parametrize('between,second,present,entry_equivalent', [
    (b'', 1, False, True), (b'', 0, True, True),
    (b'\x25'+struct.pack('<H', 392), 1, True, False),
    (b'\x28\x01\x00', 1, True, False),
    (b'\x25'+struct.pack('<H', 344), 1, True, False)])
def test_stable_quest_value_identity_and_mutation_yield_uncertainty(between, second,
                                                                 present, entry_equivalent):
    from test_script_guard_consumers import code
    read = b'\x25'+struct.pack('<H', 391)
    raw = code(read, compare(RESULT, 0), (1, 'again'), b'\x02', 'again', between+read,
               compare(RESULT, second), (1, 'yes'), b'\x02', 'yes', b'\x29\x07\x00\x02')
    result = interpret(raw, BASE)
    assert result.complete
    assert bool(flags(result)) is present
    if present:
        first, later = flags(result)[0].guards
        assert first.origin != later.origin  # exact writer metadata remains distinct
        assert first.origin[2:] == ('special', 391)
        assert (first.identity == later.identity) is entry_equivalent
        # Capability projection remains the separately tested exact ne-2 shape.
        cap = CapabilityState(quest_log_state=0)
        supported_first = replace(first, operator='ne', value=2)
        supported_later = replace(later, operator='ne', value=2)
        assert NavigationGraph._requirement_from_guard(supported_first, cap).met
        assert (NavigationGraph._entry_guard(supported_later) is not None) is entry_equivalent


# Native outputs are independent unless the source contract proves a copy alias.
_NATIVE_OUTPUTS = [
    (b'\x25'+struct.pack('<H', 205), 0x8004, 0x8005),
    (b'\x26'+struct.pack('<HH', 0x4009, 205), 0x4009, 0x8005),
    (b'\x26'+struct.pack('<HH', 0x4009, 216), 0x4009, 0x403A),
    (b'\x26'+struct.pack('<HH', 0x4009, 344), 0x4009, RESULT),
    (b'\x26'+struct.pack('<HH', 0x4009, 391), 0x4009, RESULT),
    (b'\x26'+struct.pack('<HH', 0x4009, 423), 0x4009, RESULT),
    # Clear the HM comparison predicate so both observations are variable guards.
    (b'\x7c\x01\x00'+compare(0x4000, 0), 0x8004, RESULT),
    (b'\x42'+struct.pack('<HH', 0x4001, 0x4002), 0x4001, 0x4002),
]


@pytest.mark.parametrize('writer,first,second', _NATIVE_OUTPUTS)
def test_independent_outputs_allow_opposite_value_branches(writer, first, second):
    from test_script_guard_consumers import code
    raw = code(writer, compare(first, 0), (1, 'next'), b'\x02', 'next',
               compare(second, 1), (1, 'yes'), b'\x02', 'yes', b'\x29\x07\x00\x02')
    result = interpret(raw, BASE)
    assert result.complete, result.unknown
    reached = [effect for effect in flags(result) if effect.value('flag') == 7]
    assert len(reached) == 1
    assert len(reached[0].guards) == 2


@pytest.mark.parametrize('writer,first,second', _NATIVE_OUTPUTS)
def test_independent_output_identities_do_not_minimize(writer, first, second):
    left = flags(guarded(writer, var=first, value=0, condition=1))[-1].guards[0]
    right = flags(guarded(writer, var=second, value=0, condition=1))[-1].guards[0]
    assert left.identity != right.identity
    if writer[0] in {0x25, 0x26}:
        assert left.origin[:4] == right.origin[:4]
    opposite = replace(right, operator='ne')
    assert not NavigationGraph._opposite_guards(left, opposite)
    assert frozenset() not in NavigationGraph._minimize_guard_clauses(((left,), (opposite,)))


@pytest.mark.parametrize('writer,source,_second', _NATIVE_OUTPUTS)
def test_output_copies_still_reject_same_value_contradictions(writer, source, _second):
    from test_script_guard_consumers import code
    copy = b'\x19'+struct.pack('<HH', 0x4010, source)
    raw = code(writer, compare(source, 0), (1, 'copy'), b'\x02', 'copy', copy,
               compare(0x4010, 1), (1, 'yes'), b'\x02', 'yes', b'\x29\x07\x00\x02')
    result = interpret(raw, BASE)
    assert result.complete
    assert not any(effect.value('flag') == 7 for effect in flags(result))
    source_guard = flags(guarded(writer, var=source, value=0, condition=1))[-1].guards[0]
    copied_guard = flags(guarded(writer+copy, var=0x4010, value=0, condition=1))[-1].guards[0]
    assert source_guard.origin == copied_guard.origin
    assert source_guard.identity == copied_guard.identity
