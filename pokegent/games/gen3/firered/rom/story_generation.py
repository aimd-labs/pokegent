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
from pokegent.games.gen3.firered.rom.setup_facts import Facts, script_block
from pokegent.games.gen3.firered.rom.setup_source import (
    PRET_COMMIT,
    SetupError,
    _unique_pairs,
    read_source,
)
from pokegent.goals.story import Policy, Text, _Strict, load_progression
from pokegent.goals.story_identity import current_identity, verify_identity
from pokegent.local_data import read_data


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
    require(recipe.policy.order == ['helped', 'gift']
            and set(recipe.policy.required) <= {'gift', 'helped'}
            and set(recipe.rationales) == {'helped', 'gift'})
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
    effective = recipe.policy.model_dump()
    graph = dict(schema_version=1, provenance={**identity, 'generator': 'conditional-recipe/1'},
                 policy=effective, policy_sha256=hashlib.sha256(json.dumps(
                     effective, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                 nodes=nodes)
    content = _encode(graph)
    receipt = dict(accepted_recipe=False,
        artifact_acceptance='pending-independent-source-data-review',
        conditional_derivation=True, coverage='partial-two-persistent-milestones',
        premise=asdict(premise), candidates=[dict(node=n, premise=asdict(premise)) for n in nodes],
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
        excluded_return_branch='unsupported/incomplete',
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
