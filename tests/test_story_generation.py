"""Invented source, bytecode and guarded facts; no game-derived inputs."""
import hashlib
import json
import struct
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pokegent import cli
from pokegent.games.gen3.firered.rom import story_generation as gen
from pokegent.games.gen3.firered.rom.script_effects import Effect, Guard, Interpretation
from pokegent.games.gen3.firered.rom.setup_facts import Facts
from pokegent.games.gen3.firered.rom.setup_source import PRET_COMMIT, SetupError, Source

BASE = 0x08000000
PREMISE = Guard('flag', 3, 'eq', False)
PHASE = Guard('flag', 2, 'eq', True)


def policy():
    return dict(schema_version=1, coverage='partial', readiness='prerequisites-only',
                map='MAP_DEMO', objects=['LOCALID_GUIDE', 'LOCALID_CREATURE'],
                npc_script='Demo_Guide', computer_script='Demo_Console',
                return_alias='RETURNED', phase_alias='IN_MACHINE', helped_flag='FLAG_HELPED',
                gift_flag='FLAG_GIFT', gift_item='ITEM_TOKEN',
                policy=dict(order=['helped', 'gift'], required=['gift']),
                rationales=dict(helped='Partial: ask the guide, then use the readable console.',
                                gift='Partial: collect the actual gift.'))


def source_text():
    return ('.equ RETURNED, FLAG_TEMP_B\n.equ IN_MACHINE, FLAG_TEMP_A\n'
            'Demo_Console::\n lockall\n goto_if_set RETURNED, Demo_Loop\n'
            ' goto_if_set IN_MACHINE, Demo_Work\n end\n'
            'Demo_Loop::\n msgbox Demo_Text\n goto Demo_Menu\n'
            'Demo_Work::\n fadeoutbgm 0\n end\n')


def entry_image():
    image = bytearray(256)
    entry = b'\x69\x2b\x03\x00\x06\x01' + struct.pack('<I', BASE+64)
    entry += b'\x2b\x02\x00\x06\x01'+struct.pack('<I', BASE+80)
    image[16:16+len(entry)] = entry
    image[64:70] = b'\x0f'+bytes(5)
    image[80:82] = b'\x37\x00'
    return image


def projection():
    image = bytearray(256)
    effects = []
    for at, special, selector, name in [(32, 437, 0, 'AnimateTeleporterHousing'),
                                      (64, 437, 1, 'AnimateTeleporterHousing'),
                                      (96, 439, None, 'AnimateTeleporterCable')]:
        if selector is not None:
            image[at-5:at] = b'\x16'+struct.pack('<HH', 0x8004, selector)
        image[at:at+3] = b'\x25'+struct.pack('<H', special)
        values = dict(special=special, selector=selector, name=name,
            reference=f'pret/pokefirered@{PRET_COMMIT[:7]}:src/special_field_anim.c#{name}')
        effects.append(Effect('set_metatiles_relative_to_player', at, (PREMISE, PHASE),
                              tuple(values.items()), False, 'unresolved spatial destination'))
    return bytes(image), Interpretation(BASE+16, tuple(effects))


def flag(flag_id, guards):
    return Effect('setflag', 10, guards, (('flag', flag_id), ('set', True)))


def branch_results():
    effects = []
    for gender in (0, 1):
        for answer in (0, 1):
            guards = (PREMISE, Guard('var', 0x800D, 'eq', gender,
                                    (1, 10, 'checkplayergender', 0x800D)),
                      Guard('var', 0x800D, 'eq', answer, (2, 20, 'yesnobox', 0x800D)))
            effects.append(flag(2, guards))
    for gender in (0, 1):
        guards = (PREMISE, Guard('flag', 40, 'eq', True), Guard('item_space', 17, 'ge', 1),
                  Guard('var', 0x800D, 'eq', gender, (3, 25, 'checkplayergender', 0x800D)))
        effects += [Effect('additem', 30, guards, (('item', 17), ('count', 1))), flag(41, guards)]
    return Interpretation(BASE, tuple(effects)), Interpretation(BASE, (flag(40, (PREMISE, PHASE)),))


def test_conditional_entry_uses_compiled_polarity_and_source_aliases():
    facts = Facts.__new__(Facts)
    facts.defines = {'FLAG_TEMP_A': '2', 'FLAG_TEMP_B': '3'}
    gen.aliases(facts, source_text())
    assert facts.number('RETURNED') == 3 and facts.number('IN_MACHINE') == 2
    premise, entry = gen.entry_premise(entry_image(), source_text(), BASE+16, 3, 2,
                                      gen.Recipe.model_validate(policy()))
    assert premise == PREMISE and entry[2]['target'] == 64


@pytest.mark.parametrize('bad', ['polarity', 'operand', 'target', 'alias', 'label'])
def test_entry_rejects_wrong_polarity_source_and_target(bad):
    image, text = entry_image(), source_text()
    if bad == 'polarity':
        image[21] = 0
    elif bad == 'operand':
        image[18] = 9
    elif bad == 'target':
        image[64] = 2
    elif bad == 'alias':
        text = text.replace('goto_if_set RETURNED', 'goto_if_set OTHER')
    else:
        text = text.replace('Demo_Loop::', 'Other::')
    with pytest.raises(SetupError):
        gen.entry_premise(image, text, BASE+16, 3, 2, gen.Recipe.model_validate(policy()))


@pytest.mark.parametrize('text', ['.equ A, B\n.equ A, C', '.equ A, B + 1', '.equ A, f()'])
def test_direct_alias_unsupported_or_duplicate_refused(text):
    facts = Facts.__new__(Facts)
    facts.defines = {}
    with pytest.raises(SetupError):
        gen.aliases(facts, text)


def test_alias_cycle_remains_unresolved():
    facts = Facts.__new__(Facts)
    facts.defines = {}
    gen.aliases(facts, '.equ A, B\n.equ B, A')
    with pytest.raises(SetupError):
        facts.number('A')


def test_projection_preserves_original_incomplete_interpretation():
    image, result = projection()
    before = repr(result)
    exceptions = gen.project(result, PREMISE, 2, image)
    assert len(exceptions) == 3 and not result.complete and repr(result) == before


@pytest.mark.parametrize('bad', ['unknown', 'premise', 'phase', 'selector', 'reference', 'event',
                                 'other_effect', 'duplicate'])
def test_projection_discriminates_every_exception_and_keeps_unknowns(bad):
    image, result = projection()
    effects = list(result.effects)
    if bad == 'unknown':
        result = replace(result, unknown=('state limit',))
    elif bad in {'premise', 'phase'}:
        effects[0] = replace(effects[0], guards=(PHASE,) if bad == 'premise' else (PREMISE,))
    elif bad in {'selector', 'reference'}:
        values = dict(effects[0].values)
        values[bad] = 9 if bad == 'selector' else 'other-source'
        effects[0] = replace(effects[0], values=tuple(values.items()))
    elif bad == 'event':
        effects[0] = replace(effects[0], source=120)
    elif bad == 'other_effect':
        effects.append(Effect('unknown_native', 120, (PREMISE,), decoded=False))
    else:
        effects[1] = effects[0]
    if bad != 'unknown':
        result = replace(result, effects=tuple(effects))
    with pytest.raises(SetupError):
        gen.project(result, PREMISE, 2, image)


def test_branch_correlations_include_decline_and_capacity_not_reachable_union():
    npc, computer = branch_results()
    proof = gen.branch_proofs(npc, computer, PREMISE, 2, 40, 41, 17)
    assert [(p['gender'], p['answer']) for p in proof['gender_response_scenarios']
            ] == [(0, 0), (0, 1), (1, 0), (1, 1)]
    assert proof['no_capacity_cannot_set_gift']

def test_branch_correlations_refuse_unknown_gender_and_yesno_origins():
    npc, computer = branch_results()
    effects = tuple(replace(effect, guards=tuple(replace(guard, origin=None)
        if guard.kind == 'var' else guard for guard in effect.guards)) for effect in npc.effects)
    with pytest.raises(SetupError):
        gen.branch_proofs(replace(npc, effects=effects), computer, PREMISE, 2, 40, 41, 17)

def test_branch_correlations_refuse_unknown_gift_origin():
    npc, computer = branch_results()
    effects = tuple(replace(effect, guards=tuple(replace(guard, origin=None)
        if guard.kind == 'var' else guard for guard in effect.guards))
        if effect.kind == 'setflag' and effect.value('flag') == 41 else effect
        for effect in npc.effects)
    with pytest.raises(SetupError):
        gen.branch_proofs(replace(npc, effects=effects), computer, PREMISE, 2, 40, 41, 17)


@pytest.mark.parametrize('bad', ['decline', 'capacity', 'additem', 'helped_phase', 'premise'])
def test_branch_correlations_refuse_missing_or_wrong_phase_evidence(bad):
    npc, computer = branch_results()
    effects = list(npc.effects)
    if bad == 'decline':
        effects.pop(0)
    elif bad == 'capacity':
        effects[-1] = replace(effects[-1], guards=(PREMISE, Guard('flag', 40, 'eq', True)))
    elif bad == 'additem':
        effects[:] = [e for e in effects if e.kind != 'additem']
    elif bad == 'helped_phase':
        computer = replace(computer, effects=(flag(40, (PREMISE,)),))
    else:
        effects[0] = replace(effects[0], guards=effects[0].guards[1:])
    with pytest.raises(SetupError):
        gen.branch_proofs(replace(npc, effects=tuple(effects)), computer, PREMISE, 2, 40, 41, 17)


def test_policy_explicit_partial_and_no_extended_readiness():
    for field, value in [('coverage', 'complete'), ('readiness', 'stronger'),
                         ('schema_version', True), ('unrequested', 'extra')]:
        raw = policy()
        raw[field] = value
        with pytest.raises(ValueError):
            gen.Recipe.model_validate(raw)


def test_cli_registers_and_redacts_generation_errors(monkeypatch, tmp_path, capsys):
    with pytest.raises(SystemExit) as help_exit:
        cli.main(['rom-data', 'story', '--help'])
    assert help_exit.value.code == 0
    def fail(*args):
        raise ValueError('private-marker')
    monkeypatch.setattr(gen, 'generate_story', fail)
    monkeypatch.setenv('POKEGENT_LOCAL_DATA_DIR', 'invented-prior')
    monkeypatch.setenv('POKEGENT_ROM_DATA_CACHE', 'invented-prior')
    args = ['rom-data', 'story']
    for option in ('rom', 'decomp', 'policy', 'local-data', 'cache', 'out-dir'):
        args += ['--'+option, str(tmp_path / option)]
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2 and 'private-marker' not in capsys.readouterr().err


@pytest.fixture
def joined(monkeypatch):
    rows = [dict(local_id=symbol, script='Demo_Guide', x=x, y=5, elevation=3,
                 graphics_id=f'GFX_{i}', flag=f'HIDE_{i}')
            for i, (symbol, x) in enumerate([('LOCALID_GUIDE', 7), ('LOCALID_CREATURE', 10)], 1)]
    metadata = dict(id='MAP_DEMO', object_events=rows,
                    bg_events=[dict(type='sign', script='Demo_Console', x=4, y=5,
                                    elevation=0, player_facing_dir='BG_EVENT_PLAYER_FACING_NORTH')])
    facts = Facts.__new__(Facts)
    facts.groups = dict(group_order=['Group'], groups={'Group': ['Demo']})
    facts.maps = {'MAP_DEMO': ('Demo', [0, 0])}
    facts.source = Source({'data/maps/Demo/map.json': json.dumps(metadata).encode()}, 'fixture')
    facts.defines = {'GFX_1': '21', 'GFX_2': '22', 'HIDE_1': '51', 'HIDE_2': '52'}
    class Record(SimpleNamespace):
        def model_dump(self):
            return vars(self)
    objects = [Record(local_id=i, x=row['x'], y=5, elevation=3,
                              template_graphics_id=20+i, hidden_flag_id=50+i,
                              script_ptr=BASE+128) for i, row in enumerate(rows, 1)]
    event = Record(x=4, y=5, elevation=0, kind='sign', sign_facing='N', script_ptr=BASE+16)
    state = SimpleNamespace(objects=list(reversed(objects)), bg_events=[event])
    monkeypatch.setattr(gen, 'read_data', lambda _: facts.groups)
    monkeypatch.setattr(gen, 'MapReader', lambda *_: SimpleNamespace(
        read_static=lambda *_: SimpleNamespace(usable=True, state=state)))
    return facts, objects, event, state


def test_object_join_uses_compiled_localid_not_array_position(joined):
    facts, objects, event, _ = joined
    name, actual, sign = gen.joined_events(b'', None, facts.source, facts,
                                         gen.Recipe.model_validate(policy()))
    assert name == 'Demo' and actual == objects and sign is event


@pytest.mark.parametrize('bad', ['local_id', 'graphics', 'hide', 'coords', 'script', 'bg', 'index'])
def test_join_rejects_ambiguous_or_wrong_event_identity(joined, monkeypatch, bad):
    facts, objects, event, state = joined
    if bad == 'local_id':
        objects[1].local_id = 1
    elif bad == 'graphics':
        objects[0].template_graphics_id = 99
    elif bad == 'hide':
        objects[0].hidden_flag_id = 99
    elif bad == 'coords':
        objects[0].x = 99
    elif bad == 'script':
        objects[0].script_ptr += 1
    elif bad == 'bg':
        event.sign_facing = 'S'
    else:
        monkeypatch.setattr(gen, 'read_data', lambda _: {})
    with pytest.raises(SetupError):
        gen.joined_events(b'', None, facts.source, facts, gen.Recipe.model_validate(policy()))


def test_real_loader_exact_bytes_atomic_private_candidate_and_no_overwrite(tmp_path,
                                                                          monkeypatch, joined):
    import importlib
    from dataclasses import asdict
    facts, objects, event, _ = joined
    image, conditional = projection()
    npc, computer = branch_results()
    conditional = replace(conditional, effects=conditional.effects+computer.effects+(
        Effect('addobject', 100, (PREMISE, PHASE), (('local_id', 1),)),
        Effect('clearflag', 103, (PREMISE, PHASE), (('flag', 51),))))
    npc = replace(npc, effects=npc.effects+(
        Effect('removeobject', 110, (PREMISE,), (('local_id', 2),)),))
    raw = policy()
    policy_path = tmp_path / 'policy.json'
    policy_path.write_text(json.dumps(raw))
    decomp = tmp_path / 'source'
    decomp.mkdir()
    setup, cache = tmp_path / 'setup', tmp_path / 'cache'
    setup.mkdir()
    (setup / 'setup-manifest.json').write_text('{}')
    monkeypatch.setenv('POKEGENT_LOCAL_DATA_DIR', str(setup))
    monkeypatch.setenv('POKEGENT_ROM_DATA_CACHE', str(cache))
    digest = hashlib.sha1(image).hexdigest()
    (cache / digest).mkdir(parents=True)
    (cache / digest / 'manifest.json').write_text('{}')
    facts.defines.update(FLAG_TEMP_A='2', FLAG_TEMP_B='3', FLAG_HELPED='40', FLAG_GIFT='41',
                         ITEM_TOKEN='17')
    source = Source({**facts.source.files, 'data/maps/Demo/scripts.inc': source_text().encode()},
                    'b'*64)
    profile = SimpleNamespace(profile_id='invented', symbol=lambda _: SimpleNamespace(address=0))
    identity = dict(rom_sha256='a'*64, source_sha256='b'*64, map_index_sha256='c'*64,
                    source_pin='invented')
    monkeypatch.setattr(gen, 'current_identity', lambda _: (identity, image, profile))
    monkeypatch.setattr(gen, 'read_source', lambda _: source)
    monkeypatch.setattr(gen, 'Facts', lambda *a, **k: facts)
    monkeypatch.setattr(gen, 'entry_premise', lambda *a: (PREMISE, []))
    def interpret(image, ptr, **kwargs):
        if ptr == objects[0].script_ptr:
            return npc
        return conditional if 'context' in kwargs else Interpretation(
            event.script_ptr, (), ('state limit (50000)',))
    monkeypatch.setattr(gen, 'interpret', interpret)
    admitted = []
    monkeypatch.setattr(gen, 'verify_identity', lambda graph, rom: admitted.append(graph))
    validator = importlib.import_module('pokegent.goals.validate')
    monkeypatch.setattr(validator, 'resolve_map', lambda _: (0, 0))
    monkeypatch.setattr(validator.map_names, 'map_name', lambda *_: 'Demo')
    output = tmp_path / 'candidate'
    gen.generate_story(tmp_path / 'rom', decomp, policy_path, output)
    content = (output / 'progression.json').read_bytes()
    receipt = json.loads((output / 'receipt.json').read_text())
    assert len(admitted) == 1
    assert receipt['artifact_sha256'] == hashlib.sha256(content).hexdigest()
    assert receipt['utf8_bytes'] == len(content) and receipt['nodes'] == 2
    assert receipt['accepted_recipe'] is False and receipt['conditional_derivation'] is True
    assert receipt['unrestricted']['unknown'] == ['state limit (50000)']
    assert all(c['premise'] == asdict(PREMISE) for c in receipt['candidates'])
    assert receipt['authored_input_sha256'] == hashlib.sha256(
        policy_path.read_bytes()).hexdigest()
    assert output.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in output.iterdir())
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    with pytest.raises(SetupError):
        gen.generate_story(tmp_path / 'rom', decomp, policy_path, output)
    assert before == {p.name: p.read_bytes() for p in output.iterdir()}
