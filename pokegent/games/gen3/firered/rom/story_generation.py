"""One conditional, partial private recipe; no traversal or live-state assertions."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from pokegent.games.gen3.firered.maps.map_data import MapReader
from pokegent.games.gen3.firered.rom import rom_cache
from pokegent.games.gen3.firered.rom.local_setup import _encode, _write
from pokegent.games.gen3.firered.rom.map_index import _external
from pokegent.games.gen3.firered.rom.rom_tables import RomImagePort
from pokegent.games.gen3.firered.rom.script_effects import Guard, ScriptContext, interpret
from pokegent.games.gen3.firered.rom.script_scan import decode_command
from pokegent.games.gen3.firered.rom.setup_facts import Facts, one, script_block
from pokegent.games.gen3.firered.rom.setup_source import (
    PRET_COMMIT,
    SetupError,
    _unique_pairs,
    read_source,
)
from pokegent.goals.story import Policy, Text, _Strict, load_progression
from pokegent.goals.story_identity import current_identity, verify_identity
from pokegent.local_data import read_data


class Continuation(_Strict):
    map: Text
    object: Text
    script: Text
    item: Text
    gift_flag: Text
    scene_var: Text
    boarding_map: Text
    boarding_scripts: Annotated[list[Text], Field(min_length=2, max_length=2)]
    ticket_flag: Text
    ticket_trigger_var: Text
    rival_map: Text
    rival_scripts: Annotated[list[Text], Field(min_length=3, max_length=3)]
    rival_scene_var: Text
    departure_map: Text
    departure_frame: Text


def continuation_map(image, profile, source, selector):
    # Script-local aliases never modify another map's symbols or the prefix facts.
    facts = Facts(source, extra_headers=('items', 'vars', 'event_objects'))
    name, key = facts.map(selector)
    metadata = facts.map_json(selector)
    text = source.text(f'data/maps/{name}/scripts.inc')
    aliases(facts, text)
    result = MapReader(RomImagePort(image), profile).read_static(*key)
    require(result.usable)
    return facts, name, key, metadata, text, result.state


def continuation_coords(metadata, state, scripts, facts):
    require(len(set(scripts)) == len(scripts)
            and len(metadata['coord_events']) == len(state.coord_events))
    events = []
    for label in scripts:
        matches = [(i, row) for i, row in enumerate(metadata['coord_events'])
                   if row.get('script') == label]
        require(len(matches) == 1)
        i, row = matches[0]
        event = state.coord_events[i]
        require(event.script_ptr is not None and row['type'] == 'trigger'
                and (event.x, event.y, event.elevation, event.var_id, event.var_value)
                == (int(row['x']), int(row['y']), int(row['elevation']),
                    facts.number(row['var']), facts.number(row['var_value'])))
        events.append(event)
    return events


def consumer_proof(image, profile, source, spec, ticket):
    facts, name, _, metadata, text, state = continuation_map(
        image, profile, source, spec.boarding_map)
    events = continuation_coords(metadata, state, spec.boarding_scripts, facts)
    targets = []
    for label, event in zip(spec.boarding_scripts, events):
        common, = one(r'^\s*lockall\s+goto (\w+)\s+end\s*$', script_block(text, label))
        lock = decode_command(image, event.script_ptr - 0x08000000)
        jump = decode_command(image, lock.addr + 1 + len(lock.operands))
        require(lock.name == 'lockall' and jump.name == 'goto')
        targets.append((common, jump.target))
    require(targets[0] == targets[1])
    common, pc = targets[0]
    scene_symbol, ferry = one(r'^\s*goto_if_eq (\w+), 3, (\w+)\s*$',
                              script_block(text, common))
    scene = facts.number(scene_symbol)
    require(scene == facts.number(spec.scene_var) and 0x4000 <= scene <= 0x40FF)
    commands = []
    for _ in range(5):
        command = decode_command(image, pc)
        commands.append(command)
        pc = command.addr + 1 + len(command.operands)
    require([c.name for c in commands] == ['textcolor', 'applymovement', 'applymovement',
                                          'waitmovement', 'compare_var_to_value'])
    require(list(commands[0].operands) == [facts.number('NPC_TEXT_COLOR_MALE')] == [0]
            and scene not in {facts.number('VAR_TEXT_COLOR'), facts.number('VAR_PREV_TEXT_COLOR')}
            and commands[-1].u16() == scene and commands[-1].u16(2) == 3)
    branch = decode_command(image, pc)
    require(branch.name == 'goto_if' and branch.operands[0] == 1)
    require(re.search(r'\.macro textcolor color:req\s+\.byte 0xc7\s+\.byte \\color\s+\.endm',
                      source.text('asm/macros/event.inc')) is not None)
    native = source.text('src/scrcmd.c')
    require('gSpecialVar_PrevTextColor = gSpecialVar_TextColor;' in native
            and 'gSpecialVar_TextColor = ScriptReadByte(ctx);' in native)
    first = decode_command(image, branch.target)
    second = decode_command(image, first.addr + 1 + len(first.operands))
    ferry_scene, = one(r'^\s*goto_if_ge (\w+), 5, \w+\s*$', script_block(text, ferry))
    require(first.name == 'setvar' and first.u16() == 0x8004
            and second.name == 'compare_var_to_value'
            and second.u16() == facts.number(ferry_scene) and second.u16(2) == 5)
    require(facts.number(spec.ticket_flag) == ticket)
    premise = Guard('var', scene, 'ne', 3)
    results = [interpret(image, e.script_ptr, std_scripts_ptr=profile.symbol('gStdScripts').address,
                         context=ScriptContext(initial_guards=(premise,))) for e in events]
    trigger = facts.number(spec.ticket_trigger_var)
    for result in results:
        require(result.complete and all(premise in e.guards for e in result.effects))
        writes = [e for e in result.effects if e.kind == 'setvar'
                  and e.value('var') == trigger and e.value('value') == 1]
        require(len(writes) == 1 and Guard('flag', ticket, 'eq', True) in writes[0].guards
                and all(g.origin is None for e in result.effects for g in e.guards
                        if g.kind == 'var' and g.subject == scene))
    return dict(map=name, premise=asdict(premise), events=[e.model_dump() for e in events],
                entry_commands=[dict(pc=c.addr, name=c.name, operands=list(c.operands))
                                for c in commands],
                ferry_branch=dict(pc=branch.addr, operands=list(branch.operands),
                                  target=branch.target), no_precompare_scene_writes=True,
                results=[asdict(r) for r in results], complete=True,
                excluded_domain='scene == 3 ferry domain remains unrestricted/incomplete',
                claim='conditional ticket consumer only; no boarding availability proof')


def gift_attempt(result, image, item, flag, scene):
    require(result.complete)
    items = [e for e in result.effects if e.kind == 'additem']
    flags = [e for e in result.effects if e.kind == 'setflag' and e.value('flag') == flag]
    scenes = [e for e in result.effects if e.kind == 'setvar' and e.value('var') == scene]
    require(len(items) == 1 and items[0].value('item') == item
            and items[0].value('count') == 1 and flags and scenes
            and items[0].guards == (Guard('flag', flag, 'eq', False),)
            and all(e.value('value') == 1 for e in scenes)
            and all(set(items[0].guards) <= set(e.guards) for e in flags + scenes))
    # This proves an attempt followed by one-shot writes, never successful acquisition.
    tail = decode_command(image, scenes[0].source)
    release = decode_command(image, tail.addr + 1 + len(tail.operands))
    end = decode_command(image, release.addr + 1 + len(release.operands))
    require(release.name == 'release' and end.name == 'end'
            and all(items[0].source < e.source < tail.addr for e in flags))
    return dict(item=asdict(items[0]), flags=[asdict(e) for e in flags],
                scenes=[asdict(e) for e in scenes])


def acquisition_proof(image, profile, source, spec, rationale, ticket):
    facts, name, _, metadata, text, state = continuation_map(image, profile, source, spec.map)
    rows = [(i, row) for i, row in enumerate(metadata['object_events'], 1)
            if row.get('local_id') == spec.object]
    require(len(rows) == 1 and len(state.objects) == len(metadata['object_events']))
    local, row = rows[0]
    found = [o for o in state.objects if o.local_id == local]
    require(len(found) == 1 and row['script'] == spec.script)
    obj = found[0]
    require((obj.x, obj.y, obj.elevation, obj.template_graphics_id, obj.hidden_flag_id)
            == (int(row['x']), int(row['y']), int(row['elevation']),
                facts.number(row['graphics_id']), facts.number(row['flag'])))
    item, flag, scene = [facts.number(s) for s in (spec.item, spec.gift_flag, spec.scene_var)]
    require(0x20 <= flag <= 0x8FF and flag != ticket and 0x4000 <= scene <= 0x40FF)
    body = '\n'.join(line.split('@', 1)[0]
                     for line in script_block(text, spec.script).splitlines())
    require(re.search(r'goto_if_set '+re.escape(spec.gift_flag)+r', \w+', body) is not None)
    require(re.search(r'giveitem_msg \w+, '+re.escape(spec.item)+r', 1, \w+\s+'
                      r'msgbox \w+\s+setflag '+re.escape(spec.gift_flag)+r'\s+'
                      r'setvar '+re.escape(spec.scene_var)+r', 1\s+release\s+end\s*$', body))
    result = interpret(image, obj.script_ptr, std_scripts_ptr=profile.symbol('gStdScripts').address)
    attempt = gift_attempt(result, image, item, flag, scene)
    require(re.search(r'\.macro giveitem_msg[^\n]*\n\s*additem \\item, \\amount\s+'
                      r'msgreceiveditem ', source.text('asm/macros/event.inc')) is not None)
    require('gSpecialVar_Result = AddBagItem(itemId, (u8)quantity);'
            in source.text('src/scrcmd.c'))
    boarding = consumer_proof(image, profile, source, spec, ticket)
    rf, rn, _, rm, rt, rs = continuation_map(image, profile, source, spec.rival_map)
    events = continuation_coords(rm, rs, spec.rival_scripts, rf)
    rival_roots = []
    for label, event in zip(spec.rival_scripts, events):
        variable, value, common = one(
            r'^\s*lockall\s+setvar (\w+), (\d+)\s+goto (\w+)\s+end\s*$',
            script_block(rt, label))
        lock = decode_command(image, event.script_ptr - 0x08000000)
        setter = decode_command(image, lock.addr + 1 + len(lock.operands))
        jump = decode_command(image, setter.addr + 1 + len(setter.operands))
        require(lock.name == 'lockall' and setter.name == 'setvar' and jump.name == 'goto'
                and setter.u16() == rf.number(variable) and setter.u16(2) == int(value))
        rival_roots.append((common, jump.target))
    require(len(set(rival_roots)) == 1)
    require(re.search(r'\bsetvar '+re.escape(spec.rival_scene_var)+r', 1\s*$',
                      script_block(rt, rival_roots[0][0]), re.M) is not None)
    rival = [interpret(image, e.script_ptr, std_scripts_ptr=profile.symbol('gStdScripts').address)
             for e in events]
    require(all(r.complete for r in rival))
    require(all(e.var_id == rf.number(spec.rival_scene_var) and e.var_value == 0 for e in events))
    # The source frame-table condition is separate from, and never run by, acquisition.
    from pokegent.games.gen3.firered.maps.boulder_scan import (
        header_scripts_ptr,
        on_frame_roots,
        script_table,
    )
    df, dn, dk, _, dt, _ = continuation_map(image, profile, source, spec.departure_map)
    dep_scene, dep_root = one(r'^\s*map_script_2 (\w+), 1, (\w+)\s*\n\s*\.2byte 0\s*$',
                              script_block(dt, spec.departure_frame))
    roots = on_frame_roots(image, script_table(image, header_scripts_ptr(image, profile, *dk))[2])
    require(len(roots) == 1 and roots[0][:2] == (scene, 1) and df.number(dep_scene) == scene)
    departure = dict(map=dn, trigger=roots[0], source_root=dep_root,
                     status='excluded/incomplete; native departure unclassified',
                     claim='outside acquisition root; no exit/warp/availability proof')
    node = dict(id='acquisition', requires=['gift'], objective=dict(
        objective_id='partial_acquisition', revision=1, kind='talk',
        target=dict(map=name, npc=local), rationale=rationale,
        success_probes=[dict(kind='item', item=item, min_quantity=1)]))
    members = {f'data/maps/{m}/{suffix}' for m in (name, boarding['map'], rn, dn)
               for suffix in ('scripts.inc', 'map.json')}
    members.update({'asm/macros/event.inc', 'src/scrcmd.c', 'src/item.c',
                    'data/scripts/std_msgbox.inc', 'src/data/items.json'})
    return node, dict(boarding=boarding, acquisition=asdict(result), attempt=attempt,
        object=obj.model_dump(),
        source_members={m: hashlib.sha256(source.files[m]).hexdigest() for m in sorted(members)},
        source_scripts={m: source.text(m) for m in sorted(members) if m.endswith('scripts.inc')},
        one_shot_flag=flag, one_shot_is_not_completion=True,
        completion='current usable BAG item quantity >= 1 only; PC excluded',
        no_retry=('gift attempt may fail yet set flag/scene; '
                  'flag set + item absent is unrecoverable'),
        rival=dict(map=rn, events=[e.model_dump() for e in events],
                   results=[asdict(r) for r in rival], roots=rival_roots, node=False,
                   limitation='durable proof not admitted; use current legal trigger options'),
        departure=departure, premise_scope='boarding only; never prefix or current RAM')


class Recipe(_Strict):
    schema_version: Annotated[int, Field(ge=1, le=1)]
    coverage: Literal['partial']
    readiness: Literal['prerequisites-only']
    map: Text
    objects: Annotated[list[Text], Field(min_length=2, max_length=2)]
    npc_script: Text
    computer_script: Text
    return_alias: Text
    phase_alias: Text
    helped_flag: Text
    gift_flag: Text
    gift_item: Text
    continuation: Continuation | None = None
    policy: Policy
    rationales: dict[Text, Text]


def require(condition):
    if not condition:
        raise SetupError('conditional recipe proof unavailable')


def aliases(facts, text):
    """Only direct aliases; header resolution owns cycles and bounded arithmetic."""
    seen = set()
    for line in text.splitlines():
        line = line.split('@')[0].strip()
        if not line.startswith('.equ'):
            continue
        match = re.fullmatch(r'\.equ\s+(\w+),\s*(\w+)', line)
        require(match is not None)
        name, value = match.groups()
        require(name not in seen and name not in facts.defines)
        seen.add(name)
        facts.defines[name] = value


def joined_events(image, profile, source, facts, recipe):
    name, key = facts.map(recipe.map)
    metadata = facts.map_json(recipe.map)
    require(read_data('pret_map_groups.json') == facts.groups)
    result = MapReader(RomImagePort(image), profile).read_static(*key)
    require(result.usable)
    state = result.state
    require(len(state.objects) == len(metadata['object_events']))
    objects = []
    for symbol in recipe.objects:
        matches = [(i, row) for i, row in enumerate(metadata['object_events'], 1)
                   if row.get('local_id') == symbol]
        require(len(matches) == 1)
        local_id, row = matches[0]  # pinned mapjson emits object LOCALID as i+1
        found = [o for o in state.objects if o.local_id == local_id]
        require(len(found) == 1 and row['script'] == recipe.npc_script)
        obj = found[0]
        require((obj.x, obj.y, obj.elevation, obj.template_graphics_id, obj.hidden_flag_id)
                == (int(row['x']), int(row['y']), int(row['elevation']),
                    facts.number(row['graphics_id']), facts.number(row['flag'])))
        objects.append(obj)
    require(len(set(recipe.objects)) == 2 and objects[0].script_ptr == objects[1].script_ptr)
    backgrounds = [(i, row) for i, row in enumerate(metadata['bg_events'])
                   if row.get('script') == recipe.computer_script]
    require(len(backgrounds) == 1 and len(state.bg_events) == len(metadata['bg_events']))
    index, row = backgrounds[0]
    event = state.bg_events[index]
    require(row['type'] == event.kind == 'sign' and event.script_ptr is not None)
    require((event.x, event.y, event.elevation, event.sign_facing)
            == (int(row['x']), int(row['y']), int(row['elevation']), 'N'))
    require(row['player_facing_dir'] == 'BG_EVENT_PLAYER_FACING_NORTH')
    return name, objects, event


def entry_premise(image, text, script, returned, phase, recipe):
    body = script_block(text, recipe.computer_script)
    branches = re.findall(r'^\s*goto_if_set\s+(\w+),\s*(\w+)\s*$', body, re.M)
    require(len(branches) == 2 and [v[0] for v in branches]
            == [recipe.return_alias, recipe.phase_alias])
    at, commands = script - 0x08000000, []
    for _ in range(5):
        cmd = decode_command(image, at)
        commands.append(cmd)
        at += 1 + len(cmd.operands)
    require([c.name for c in commands]
            == ['lockall', 'checkflag', 'goto_if', 'checkflag', 'goto_if'])
    require(commands[1].u16() == returned and commands[3].u16() == phase
            and commands[2].operands[0] == commands[4].operands[0] == 1
            and commands[2].target != commands[4].target)
    # Pinned source msgbox/goto and fadeoutbgm entry signatures bind the two targets.
    require(re.match(r'\s*msgbox\s+\w+\s*\n\s*goto\s+\w+',
                     script_block(text, branches[0][1])) is not None)
    require(decode_command(image, commands[2].target).name == 'loadword')
    require(script_block(text, branches[1][1]).lstrip().startswith('fadeoutbgm 0'))
    require(decode_command(image, commands[4].target).name == 'fadeoutbgm')
    return Guard('flag', returned, 'eq', False), [dict(
        pc=c.addr, name=c.name, operands=list(c.operands), target=c.target) for c in commands]


def project(result, premise, phase, image):
    """Retain the original result; classify exactly three unresolved spatial occurrences."""
    require(not result.unknown and all(premise in e.guards for e in result.effects))
    unresolved = [e for e in result.effects if not e.decoded]
    expected = [(437, 0, 'AnimateTeleporterHousing'), (437, 1, 'AnimateTeleporterHousing'),
                (439, None, 'AnimateTeleporterCable')]
    require(len(unresolved) == 3 and len({e.source for e in unresolved}) == 3)
    for effect, (special, selector, name) in zip(unresolved, expected):
        cmd = decode_command(image, effect.source)
        require(effect.kind == 'set_metatiles_relative_to_player'
                and effect.value('special') == special and effect.value('selector') == selector
                and effect.value('name') == name and cmd.name == 'special' and cmd.u16() == special
                and effect.value('reference')
                == f'pret/pokefirered@{PRET_COMMIT[:7]}:src/special_field_anim.c#{name}'
                and Guard('flag', phase, 'eq', True) in effect.guards)
        if selector is not None:
            setter = decode_command(image, effect.source - 5)
            require(setter.name == 'setvar' and setter.u16() == 0x8004
                    and setter.u16(2) == selector)
    return [asdict(e) for e in unresolved]


def branch_proofs(npc, computer, premise, phase, helped, gift, item):
    require(npc.complete)
    def flags(result, flag):
        return [e for e in result.effects if e.kind == 'setflag'
                and e.value('flag') == flag and e.value('set') is True]
    helped_effects = flags(computer, helped)
    phase_effects, gifts = flags(npc, phase), flags(npc, gift)
    require(len(helped_effects) == 1 and phase_effects and gifts)
    require(all(premise in e.guards and Guard('flag', phase, 'eq', True) in e.guards
                for e in helped_effects))
    capacity = Guard('item_space', item, 'ge', 1)
    items = [e for e in npc.effects if e.kind == 'additem' and e.value('item') == item
             and e.value('count') == 1]
    require(items and all(capacity in e.guards for e in items))
    require(all(premise in e.guards and capacity in e.guards
                and Guard('flag', helped, 'eq', True) in e.guards
                and any(set(a.guards) <= set(e.guards) for a in items) for e in gifts))
    scenarios = []
    for gender in (0, 1):
        for answer in (0, 1):
            matches = []
            for effect in phase_effects:
                seen, compatible = set(), True
                for guard in effect.guards:
                    if guard.kind == 'var' and guard.origin is not None:
                        writer = guard.origin[2]
                        if writer in {'checkplayergender', 'yesnobox'}:
                            seen.add(writer)
                            value = gender if writer == 'checkplayergender' else answer
                            require(guard.operator in {'eq', 'ne'})
                            compatible &= (value == guard.value) == (guard.operator == 'eq')
                if compatible and seen == {'checkplayergender', 'yesnobox'}:
                    matches.append(asdict(effect))
            require(matches and all(premise in e.guards for e in phase_effects))
            scenarios.append(dict(gender=gender, answer=answer, guarded_phase_effects=matches))
    for gender in (0, 1):
        require(any(any(g.origin and g.origin[2] == 'checkplayergender' for g in e.guards)
                    and all((gender == g.value) == (g.operator == 'eq') for g in e.guards
                            if g.origin and g.origin[2] == 'checkplayergender') for e in gifts))
    return dict(helped=[asdict(e) for e in helped_effects], gift=[asdict(e) for e in gifts],
                items=[asdict(e) for e in items], gender_response_scenarios=scenarios,
                gift_genders=[0, 1], no_capacity_cannot_set_gift=True)


def generate_story(rom: Path, decomp: Path, policy: Path, out_dir: Path):
    """Publish a new private candidate and raw evidence; acceptance remains with review."""
    require(out_dir.is_absolute() and not out_dir.exists() and not out_dir.is_symlink())
    out_dir = _external(out_dir)
    setup = Path(os.environ['POKEGENT_LOCAL_DATA_DIR']).expanduser().resolve()
    require(not out_dir.is_relative_to(setup)
            and not out_dir.is_relative_to(rom_cache.cache_root().expanduser().resolve()))
    identity, image, profile = current_identity(rom)
    source = read_source(_external(decomp))
    policy = _external(policy)
    require(policy.is_file())
    with policy.open('rb') as handle:
        raw_policy = handle.read(65537)
    require(len(raw_policy) <= 65536)
    recipe = Recipe.model_validate(json.loads(raw_policy, object_pairs_hook=_unique_pairs))
    order = ['helped', 'gift'] + (['acquisition'] if recipe.continuation else [])
    require(recipe.policy.order == order and set(recipe.rationales) == set(order))
    require(recipe.policy.required == ['acquisition'] if recipe.continuation
            else set(recipe.policy.required) <= {'gift', 'helped'})
    facts = Facts(source, extra_headers=('items', 'vars', 'event_objects'))
    name, objects, event = joined_events(image, profile, source, facts, recipe)
    text = source.text(f'data/maps/{name}/scripts.inc')
    aliases(facts, text)
    returned, phase, helped, gift, item = [facts.number(getattr(recipe, key)) for key in
        ('return_alias', 'phase_alias', 'helped_flag', 'gift_flag', 'gift_item')]
    require(len({returned, phase, helped, gift}) == 4 and returned < 0x20 and phase < 0x20
            and helped >= 0x20 and gift >= 0x20)
    premise, entry = entry_premise(image, text, event.script_ptr, returned, phase, recipe)
    kwargs = dict(std_scripts_ptr=profile.symbol('gStdScripts').address)
    npc = interpret(image, objects[0].script_ptr, **kwargs)
    unrestricted = interpret(image, event.script_ptr, **kwargs)
    require(not unrestricted.complete)
    conditional = interpret(image, event.script_ptr,
                            context=ScriptContext(initial_guards=(premise,)), **kwargs)
    unresolved = project(conditional, premise, phase, image)
    proofs = branch_proofs(npc, conditional, premise, phase, helped, gift, item)
    require(any(e.kind == 'addobject' and e.value('local_id') == objects[0].local_id
                for e in conditional.effects)
            and any(e.kind == 'clearflag' and e.value('flag') == objects[0].hidden_flag_id
                    for e in conditional.effects)
            and any(e.kind == 'removeobject' and e.value('local_id') == objects[1].local_id
                    for e in npc.effects))
    nodes = [dict(id=key, requires=[] if key == 'helped' else ['helped'], objective=dict(
        objective_id=f'partial_{key}', revision=1, kind='reach' if key == 'helped' else 'talk',
        target=dict(map=name, **({} if key == 'helped' else {'npc': objects[0].local_id})),
        rationale=recipe.rationales[key], success_probes=[dict(kind='flag', flag=flag)]))
        for key, flag in [('helped', helped), ('gift', gift)]]
    continuation = None
    if recipe.continuation:
        node, continuation = acquisition_proof(image, profile, source, recipe.continuation,
                                              recipe.rationales['acquisition'], gift)
        nodes.append(node)
    effective = recipe.policy.model_dump()
    graph = dict(schema_version=1, provenance={**identity, 'generator': 'conditional-recipe/1'},
                 policy=effective, policy_sha256=hashlib.sha256(json.dumps(
                     effective, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                 nodes=nodes)
    content = _encode(graph)
    receipt = dict(accepted_recipe=False,
        artifact_acceptance='pending-independent-source-data-review',
        conditional_derivation=True, coverage=('partial-two-persistent-milestones'
            if continuation is None else f'partial-{len(nodes)}-milestones-item-endpoint'),
        premise=asdict(premise), candidates=[dict(node=n,
            premise=asdict(premise) if n['id'] != 'acquisition' else None) for n in nodes],
        identity=identity, rom_sha1=hashlib.sha1(image).hexdigest(), profile=profile.profile_id,
        setup_manifest=json.loads((setup / 'setup-manifest.json').read_text()),
        source_members={k: hashlib.sha256(v).hexdigest() for k, v in source.files.items()
                        if k.endswith(('scripts.inc', 'map.json')) and f'/{name}/' in k
                        or k in {'tools/mapjson/mapjson.cpp', 'asm/macros/map.inc',
                                 'src/special_field_anim.c', 'include/constants/flags.h',
                                 'include/constants/items.h', 'include/constants/vars.h',
                                 'include/constants/event_objects.h'}},
        source_archive_sha256=(hashlib.sha256(decomp.read_bytes()).hexdigest()
                               if decomp.is_file() else None),
        authored_input_sha256=hashlib.sha256(raw_policy).hexdigest(),
        policy_sha256=graph['policy_sha256'],
        authored_fields=['recipe selection', 'selectors', 'order', 'required', 'rationales',
                         'prerequisites-only readiness'],
        derived_fields=['compiled events', 'guards', 'effects', 'helped prerequisite',
                        'persistent predicates',
                        'conditional premise'],
        events=dict(objects=[o.model_dump() for o in objects],
                    sign=event.model_dump(), entry=entry),
        unrestricted=asdict(unrestricted), unrestricted_complete=unrestricted.complete,
        conditional=asdict(conditional), conditional_complete=conditional.complete, npc=asdict(npc),
        unresolved_spatial=unresolved, proofs=proofs,
        excluded_return_branch='unsupported/incomplete', continuation=continuation,
        cache=json.loads((rom_cache.cache_root() / hashlib.sha1(image).hexdigest()
                          / 'manifest.json').read_text()),
        artifact_sha256=hashlib.sha256(content).hexdigest(),
        nodes=len(nodes), utf8_bytes=len(content),
        limitations=['conditional possibilities only; never current RAM/readiness/input legality',
                     'task capacity, collision, timing, rendering and animation success unproven',
                     'native gameplay/reentry untested',
                     'full coverage and complete artifact fit unmeasured'])
    out_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.story-', dir=out_dir.parent))
    try:
        _write(stage, 'progression.json', content)
        candidate = load_progression(path=stage / 'progression.json')
        verify_identity(candidate.graph, rom)
        _write(stage, 'receipt.json', _encode(receipt))
        require(not out_dir.exists())
        os.rename(stage, out_dir)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return out_dir
