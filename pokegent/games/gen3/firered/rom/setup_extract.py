"""Seven private documents from bounded pinned-source facts and explicit UI policy."""

from __future__ import annotations

import re

from pokegent.games.gen3.firered.rom.setup_facts import Facts, Tiles, message, one, script_block
from pokegent.games.gen3.firered.rom.setup_source import SetupError, Source
from pokegent.local_data import SCHEMAS

# Message-label/policy recipes, not copied game strings. Unused source messages do
# not register actions; normal current-RAM capability/legality checks still apply.
PROMPT_LABELS = (
    ('data/scripts/field_moves.inc', 'Text_CutTreeDown', 'field_move', 'cut'),
    ('data/scripts/field_moves.inc', 'Text_UseRockSmash', 'field_move', 'rock smash'),
    ('data/scripts/field_moves.inc', 'Text_UseStrength', 'field_move', 'strength'),
    ('data/text/surf.inc', 'Text_WantToSurf', 'field_move', 'surf'),
    ('data/scripts/field_moves.inc', 'Text_UseWaterfall', 'field_move', 'waterfall'),
    ('data/scripts/field_moves.inc', 'Text_SeaIsDeepUseDive', 'field_move', 'dive'),
    ('data/scripts/field_moves.inc', 'Text_LightFilteringUseDive', 'field_move', 'dive'),
    ('data/maps/Route12/text.inc', 'Text_WantToUsePokeFlute', 'poke_flute', None),
    ('data/maps/MtMoon_B2F/text.inc', 'MtMoon_B2F_Text_YouWantDomeFossil', 'fossil_choice', None),
    ('data/maps/MtMoon_B2F/text.inc', 'MtMoon_B2F_Text_YouWantHelixFossil', 'fossil_choice', None),
    ('data/maps/PokemonMansion_2F/text.inc', 'PokemonMansion_Text_PressSecretSwitch',
     'secret_switch', None),
)


def quiz(facts: Facts) -> dict:
    name = 'CinnabarIsland_Gym'
    scripts = facts.source.text(f'data/maps/{name}/scripts.inc')
    rows = []
    for number in range(1, 7):
        prefix = f'{name}_EventScript_Quiz{number}'
        body = script_block(scripts, prefix)
        label = one(r'\bmsgbox (\w+), MSGBOX_YESNO', body)[0]
        answers = re.findall(r'goto_if_eq VAR_RESULT, (YES|NO), (\w+)', body)
        correct = [a for a, dest in answers if dest == prefix + 'Correct']
        if len(answers) != 2 or len(correct) != 1:
            raise SetupError('quiz answer branches are ambiguous')
        completed = script_block(scripts, prefix + 'Complete')
        flag = one(r'^\s*setflag (\w+)\s*$', completed)[0]
        rows.append({'number': number, 'prompt': message(facts.source,
                     f'data/maps/{name}/text.inc', label), 'answer_yes': correct[0] == 'YES',
                     'door_flag': facts.number(flag)})
    return {'questions': rows}


def prompts(source: Source, questions: dict) -> dict:
    rows = []
    for file, label, kind, move in PROMPT_LABELS:
        row = {'kind': kind, 'patterns': [message(source, file, label)]}
        if kind == 'fossil_choice':
            row['irreversible'] = True
        else:
            row['default_yes'] = True
        if move is not None:
            row['field_move'] = move
        rows.append(row)
    for q in questions['questions']:
        rows.append({'kind': 'quiz', 'patterns': [q['prompt']], 'default_yes': q['answer_yes'],
                     'wrong_answer_cost': 'trainer battle',
                     'metadata': {'door': q['number'], 'door_flag': q['door_flag']}})
    return {'rules': rows}


def fly(facts: Facts) -> dict:
    source = facts.source
    sections = source.json('src/data/region_map/region_map_sections.json')['map_sections']
    section_by = {row['id']: (i, row) for i, row in enumerate(sections)}
    heals = {row['id']: row for row in source.json('src/data/heal_locations.json')
             ['heal_locations']}
    text = source.text('src/region_map.c')
    rows = re.findall(r'\[(MAPSEC_\w+)\s*- KANTO_MAPSEC_START\]\s*=\s*'
                      r'\{MAP\((MAP_\w+)\),\s*(HEAL_LOCATION_\w+)\}', text)
    result = []
    for section, constant, heal_id in rows:
        if heal_id == 'HEAL_LOCATION_NONE':
            # Only the reference-supported contiguous Kanto heal-point prefix.
            # Later route and Sevii rows are outside this setup's runtime contract.
            break
        i, entry = section_by[section]
        name, key = facts.map(constant)
        heal = heals[heal_id]
        if heal['map'] != constant:
            raise SetupError('Fly map and heal location disagree')
        branch = one(r'case ' + re.escape(section)
                     + r':([\s\S]*?)(?=\n    case |\n    default:)', text)[0]
        flag = one(r'return FlagGet\((\w+)\)', branch)[0]
        Tiles(source, facts.map_json(constant)).tile(heal['x'], heal['y'])
        result.append({'mapsec': i, 'name': name, 'map_key': key,
                       'landing': [heal['x'], heal['y']], 'visited_flag': facts.number(flag),
                       'cell': [entry['x'], entry['y']]})
    if len(result) != 13:
        raise SetupError('unsupported Kanto Fly destination table')
    return {'destinations': result}


def _c_array(text: str, name: str) -> str:
    return one(re.escape(name) + r'\[[^\n]*?\]\s*=\s*\{([\s\S]*?)\n\};', text)[0]


def _menu_labels(source: Source, kind: str, constant: str) -> list[str]:
    if kind == 'list':
        text = source.text('src/field_specials.c')
        body = one(r'\[' + re.escape(constant) + r'\]\s*=\s*\{([^}]+)\}',
                   _c_array(text, 'sListMenuLabels'))[0]
    else:
        text = source.text('src/script_menu.c')
        array = one(r'\[' + re.escape(constant) + r'\]\s*=\s*MULTICHOICE\((\w+)\)',
                    text)[0]
        body = _c_array(text, array)
    labels = re.findall(r'\bg(?:Other)?Text_\w+', body)
    if not labels or len(labels) > 12:
        raise SetupError('unsupported elevator menu labels')
    strings = source.text('src/strings.c')
    return [one(r'const u8 ' + re.escape(label) + r'\[\]\s*=\s*_\("([^"\n]+)"\)',
                strings)[0] for label in labels]


def elevators(facts: Facts) -> dict:
    rows = {}
    for key, name in (('celadon', 'CeladonCity_DepartmentStore_Elevator'),
                      ('silph', 'SilphCo_Elevator'), ('rocket', 'RocketHideout_Elevator')):
        constant = next((c for c, (n, _) in facts.maps.items() if n == name), None)
        document = facts.map_json(constant)
        scripts = facts.source.text(f'data/maps/{name}/scripts.inc')
        kind = 'list' if re.search(r'^\s*special ListMenu\s*$', scripts, re.M) else 'multichoice'
        menus = set(re.findall(r'(?:multichoicedefault \d+, \d+, |setvar VAR_0x8004, )'
                               r'((?:MULTICHOICE|LISTMENU)_\w+)', scripts))
        if len(menus) != 1:
            raise SetupError('elevator menu constant missing or ambiguous')
        menu = menus.pop()
        labels = _menu_labels(facts.source, kind, menu)
        choice = name + '_EventScript_' + ('FloorSelect' if kind == 'list' else 'ChooseFloor')
        cases = re.findall(r'^\s*case (\d+), (\w+)\s*$', script_block(scripts, choice), re.M)
        selected = {int(i): target for i, target in cases}
        if len(selected) != len(cases) or set(selected) != {*range(len(labels)), 127}:
            raise SetupError('elevator floor cases disagree with labels')
        floors = []
        for i, label in enumerate(labels):
            body = script_block(scripts, selected[i])
            warps = re.findall(r'^\s*setdynamicwarp (\w+), 255, (\d+), (\d+)\s*$', body, re.M)
            if not warps:
                if i != len(labels) - 1 or selected[i] != selected[127]:
                    raise SetupError('unexpected elevator cancellation case')
                # Historic EXIT map is UI fallback metadata; cancel executes no warp.
                code = 'b2f' if key == 'rocket' else '1f'
                fallback = next((f for f in floors if f['code'] == code), None)
                if fallback is None:
                    raise SetupError('elevator EXIT fallback floor unavailable')
                floors.append({**fallback, 'code': 'exit', 'label': label})
                continue
            if len(warps) != 1:
                raise SetupError('ambiguous elevator floor warp')
            dest, x, y = warps[0]
            place, map_key = facts.map(dest)
            Tiles(facts.source, facts.map_json(dest)).tile(int(x), int(y))
            floor = {'code': label.lower(), 'label': label, 'map': map_key, 'place': place}
            if key != 'rocket':
                floor['arrival'] = [int(x), int(y)]
            floors.append(floor)
        tiles = Tiles(facts.source, document)
        panels = [[r['x'], r['y']] for r in document['bg_events']
                  if r.get('script') == name + '_EventScript_FloorSelect']
        arrows = {facts.number(f'MB_{direction}_ARROW_WARP'): push
                  for direction, push in (('SOUTH', 'down'), ('NORTH', 'up'),
                                          ('WEST', 'left'), ('EAST', 'right'))}
        exits = [(r['x'], r['y'], arrows[tiles.tile(r['x'], r['y'])[2]])
                 for r in document['warp_events'] if r['dest_map'] == 'MAP_DYNAMIC'
                 and r['x'] < tiles.width and r['y'] < tiles.height
                 and tiles.tile(r['x'], r['y'])[2] in arrows]
        if not panels or len(exits) != 1:
            raise SetupError('elevator panels or exit missing')
        x, y, push = exits[0]
        row = {'scene': 'menu.elevator' if kind == 'list' else 'dialog.elevator',
               'map': facts.map(constant)[1], 'menu': kind, 'panels': panels,
               'exit_mat': [x, y], 'exit_push': push, 'floors': floors, 'cancel': 127}
        if key == 'celadon':
            row['menu_id'] = facts.number(menu)
        if kind == 'list':
            body = one(r'case ' + menu + r':([\s\S]*?)break;',
                       facts.source.text('src/field_specials.c'))[0]
            row['shown'] = int(one(r'task->data\[0\] = (\d+);', body)[0])
            row['total'] = int(one(r'task->data\[1\] = (\d+);', body)[0])
        if key == 'rocket':
            one(r'goto_if_unset FLAG_CAN_USE_ROCKET_HIDEOUT_LIFT, (\w+)',
                script_block(scripts, name + '_EventScript_FloorSelect'))
            row['requires'] = 'lift_key'
        rows[key] = row
    return {'elevators': rows}


def _gate_groups(points: set[tuple[int, int]]) -> list[list[list[int]]]:
    groups = []
    while points:
        seed = min(points, key=lambda p: (p[1], p[0]))
        pending, group = [seed], set()
        while pending:
            x, y = pending.pop()
            if (x, y) not in points:
                continue
            points.remove((x, y))
            group.add((x, y))
            pending.extend(((x-1, y), (x+1, y), (x, y-1), (x, y+1)))
        groups.append([list(p) for p in sorted(group, key=lambda p: (p[1], p[0]))])
    # The switch builder verifies [0][0]; preserve left-to-right gate order.
    return sorted(groups, key=lambda g: (min(p[0] for p in g), min(p[1] for p in g)))


def mansion(facts: Facts) -> dict:
    text = facts.source.text('data/scripts/pokemon_mansion.inc')
    flag = one(r'^\s*setflag (\w+)\s*$',
               script_block(text, 'PokemonMansion_EventScript_SecretSwitch'))[0]
    rows, maps, secret = [], {}, None
    for floor in ('1F', '2F', '3F', 'B1F'):
        constant = 'MAP_POKEMON_MANSION_' + floor
        name, key = facts.map(constant)
        document = facts.map_json(constant)
        tiles = Tiles(facts.source, document)
        maps[floor] = key
        states = []
        for state in ('PressSwitch', 'ResetSwitch'):
            body = script_block(text, f'PokemonMansion_EventScript_{state}_{floor}')
            cells = re.findall(r'^\s*setmetatile\s+(\d+),\s*(\d+),\s*\w+,\s*([01])\s*$',
                               body, re.M)
            if not cells or len(cells) != len(re.findall(r'\bsetmetatile\b', body)):
                raise SetupError('unsupported mansion metatile command')
            states.append({(int(x), int(y)): int(c) for x, y, c in cells})
        if states[0].keys() != states[1].keys():
            raise SetupError('mansion switch states disagree')
        opens = _gate_groups({p for p in states[0] if states[0][p] == 0 and states[1][p] == 1})
        closes = _gate_groups({p for p in states[0] if states[0][p] == 1 and states[1][p] == 0})
        for event in document['bg_events']:
            if event.get('script') != name + '_EventScript_Statue':
                continue
            x, y = event['x'], event['y']
            if event['player_facing_dir'] != 'BG_EVENT_PLAYER_FACING_NORTH':
                raise SetupError('unsupported statue interaction facing')
            if tiles.tile(x, y+1)[0] != 0:
                raise SetupError('statue stand is not passable')
            rows.append({'id': f'{floor}:{x}:{y}', 'floor': floor, 'map': key,
                         'statue': [x, y], 'stand': [x, y+1], 'facing': 'N',
                         'opens': opens, 'closes': closes})
        for obj in document['object_events']:
            if obj.get('script') == name + '_EventScript_ItemSecretKey':
                tiles.tile(obj['x'], obj['y'])
                secret = {'map': key, 'tile': [obj['x'], obj['y']],
                          'hidden_item_flag': facts.number(obj['flag'])}
    if secret is None or len(rows) != 5:
        raise SetupError('mansion objects incomplete')
    return {'mansion': {'switch_flag': facts.number(flag), 'maps': maps,
                        'secret_key': secret, 'statues': rows}}


def surf(facts: Facts) -> dict:
    control = facts.source.text('src/field_control_avatar.c')
    player = facts.source.text('src/field_player_avatar.c')
    badge = one(r'if \(FlagGet\((FLAG_BADGE\d+_GET)\) == TRUE && PartyHasMonWithSurf\(\)'
                r' == TRUE && IsPlayerFacingSurfableFishableWater\(\) == TRUE\)', control)[0]
    elevation = int(one(r'PlayerGetElevation\(\) == (\d+)\s*\n'
                        r'\s*&& MetatileAtCoordsIsWaterTile', player)[0])
    dismount = int(one(r'MapGridGetElevationAt\(x, y\) == (\d+)\s*\n'
                       r'\s*&& GetObjectEventIdByPosition\(x, y, \d+\)', player)[0])
    return {
        'capability': {
            'move': {'name': 'Surf', 'move_id': facts.number('MOVE_SURF')},
            'badge': {'number': int(re.search(r'\d+', badge)[0]), 'name': 'Soul',
                      'flag': facts.number(badge)},
            'prompt': {'text': message(facts.source, 'data/text/surf.inc', 'Text_WantToSurf')},
        },
        'start_rule': {
            'player_elevation': elevation,
            'front_tile': 'Water terrain with an elevation mismatch',
            'result': 'Move onto the water tile in front and begin surfing',
            'water_behaviors': {'start_ok': ['MB_OCEAN_WATER', 'MB_POND_WATER'],
                                'too_fast': ['MB_FAST_WATER']},
        },
        'dismount_rule': {
            'land_elevation': dismount, 'water_elevation': 1,
            'rule': 'Step onto land at elevation 3 without a blocking object or collision',
        },
    }


def extract_documents(source: Source) -> dict[str, dict]:
    facts = Facts(source)
    questions = quiz(facts)
    documents = {
        'pret_map_groups.json': facts.groups, 'fly_destinations.json': fly(facts),
        'elevators.yaml': elevators(facts), 'puzzles.yaml': mansion(facts),
        'field_surf.yaml': surf(facts), 'cinnabar_quiz.yaml': questions,
        'prompt_rules.yaml': prompts(source, questions),
    }
    for name, document in documents.items():
        SCHEMAS[name].model_validate(document)
    return documents
