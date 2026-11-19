"""Invented source/cache inputs only; actual game data stays outside Git."""

import hashlib
import io
import json
import re
import struct
import subprocess
import tarfile

import pytest

from pokegent import cli
from pokegent.games.gen3.firered.rom import local_setup as setup
from pokegent.games.gen3.firered.rom import rom_cache
from pokegent.games.gen3.firered.rom import setup_source as source
from pokegent.games.gen3.firered.rom.rom_data import ProfileSet, load_profiles
from pokegent.games.gen3.firered.rom.setup_facts import Facts, message


@pytest.mark.parametrize('badge_number,dismount', [(2, 7), (8, 12)])
@pytest.mark.parametrize('section', ['badge', 'water_behaviors', 'dismount_rule'])
def test_surf_diagnostics_follow_extracted_facts(badge_number, dismount, section):
    from pokegent.games.gen3.firered.rom.setup_extract import surf

    badge = f'FLAG_BADGE{badge_number:02}_GET'
    files = {
        'data/maps/map_groups.json': b'{"group_order":["Demo"],"Demo":["Room"]}',
        'src/field_control_avatar.c': (
            f'if (FlagGet({badge}) == TRUE && PartyHasMonWithSurf()'
            ' == TRUE && IsPlayerFacingSurfableFishableWater() == TRUE)').encode(),
        'src/field_player_avatar.c': (
            'PlayerGetElevation() == 9\n && MetatileAtCoordsIsWaterTile'
            f'\nMapGridGetElevationAt(x, y) == {dismount}\n'
            ' && GetObjectEventIdByPosition(x, y, 0)').encode(),
        'data/text/surf.inc': b'Text_WantToSurf::\n .string "Invented wave prompt$"\n',
        'include/constants/flags.h': f'#define {badge} 42\n'.encode(),
        'include/constants/moves.h': b'#define MOVE_SURF 17\n',
        **{f'include/constants/{name}.h': b''
           for name in ('opponents', 'menu', 'metatile_behaviors')},
    }
    result = surf(Facts(source.Source(files, 'invented')))
    assert result['capability']['move']['move_id'] == 17
    assert result['start_rule']['player_elevation'] == 9
    if section == 'badge':
        assert result['capability']['badge'] == {
            'number': badge_number, 'name': f'Badge {badge_number}', 'flag': 42}
    elif section == 'water_behaviors':
        assert result['start_rule']['water_behaviors'] == {}
    else:
        assert result['dismount_rule'] == {
            'land_elevation': dismount,
            'rule': f'Step onto land at elevation {dismount} without a blocking object or collision',
        }


def test_cache_rejects_changed_named_index(tmp_path, monkeypatch):
    index = tmp_path / 'index.json'
    index.write_text('{"group_order":["Demo"],"groups":{"Demo":["Room"]}}')
    monkeypatch.setenv('POKEGENT_MAP_GROUPS', str(index))
    monkeypatch.setattr(rom_cache, '_generator_fingerprint', lambda: 'invented-generator')
    target = tmp_path / 'cache'
    target.mkdir(mode=0o700)
    for name in rom_cache.CACHE_FILES:
        (target / name).write_text('invented')
    manifest = {
        'sha1': 'invented-rom', 'format_version': rom_cache.CACHE_FORMAT_VERSION,
        'generator_sha256': 'invented-generator', 'artifacts': list(rom_cache.CACHE_FILES),
        'sha256': rom_cache._artifact_digests(target),
        'map_index_sha256': hashlib.sha256(index.read_bytes()).hexdigest(),
    }
    (target / 'manifest.json').write_text(json.dumps(manifest))
    assert rom_cache._valid_cache(target, 'invented-rom')
    index.write_text('{"group_order":["Demo"],"groups":{"Demo":["OtherRoom"]}}')
    assert not rom_cache._valid_cache(target, 'invented-rom')


def test_setup_cli_registered():
    with pytest.raises(SystemExit) as result:
        cli.main(['rom-data', 'setup', '--help'])
    assert result.value.code == 0


def archive(tmp_path, files, *, extra=()):
    path = tmp_path / 'source.tar.gz'
    with tarfile.open(path, 'w:gz') as output:
        root = tarfile.TarInfo('pokefirered-fixture')
        root.type = tarfile.DIRTYPE
        output.addfile(root)
        for name, content in files.items():
            info = tarfile.TarInfo('pokefirered-fixture/' + name)
            info.size = len(content)
            output.addfile(info, io.BytesIO(content))
        for info, content in extra:
            output.addfile(info, io.BytesIO(content))
    return path


def digest(files):
    return source.tree_digest({name: hashlib.sha256(content).digest()
                               for name, content in files.items()})


def test_tree_digest_tar_directory_and_exact_encoding(tmp_path):
    files = {'data/maps/map_groups.json': b'{"invented":true}', 'invented.txt': b'demo'}
    expected = digest(files)
    encoded = b'pokegent-decomp-tree-v1\0' + struct.pack('>Q', 2)
    for name in sorted(files):
        encoded += struct.pack('>I', len(name)) + name.encode()
        encoded += hashlib.sha256(files[name]).digest()
    assert expected == hashlib.sha256(encoded).hexdigest()
    tree = tmp_path / 'tree'
    for name, content in files.items():
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (tree / '.git').mkdir()
    (tree / '.git' / 'ignored').write_text('metadata')
    a = source.read_source(archive(tmp_path, files), expected, 'fixture')
    b = source.read_source(tree, expected, 'fixture')
    assert a == b
    assert a.json('data/maps/map_groups.json') == {'invented': True}
    (tree / 'invented.txt').write_text('changed')
    with pytest.raises(source.SetupError, match='pinned tree'):
        source.read_source(tree, expected, 'fixture')


@pytest.mark.parametrize('kind', ['symlink', 'duplicate', 'traversal', 'wrong-root'])
def test_tar_refuses_unsafe_or_ambiguous_members(tmp_path, kind):
    files = {'data/maps/map_groups.json': b'{}'}
    name = {'traversal': 'pokefirered-fixture/../escaped', 'wrong-root': 'other/file'}.get(
        kind, 'pokefirered-fixture/data/maps/map_groups.json')
    info = tarfile.TarInfo(name)
    if kind == 'symlink':
        info.type, info.linkname = tarfile.SYMTYPE, '/etc/passwd'
    with pytest.raises(source.SetupError):
        source.read_source(archive(tmp_path, files, extra=[(info, b'')]), digest(files), 'fixture')
    assert not (tmp_path / 'escaped').exists()


def test_directory_refuses_extra_file_and_symlinks(tmp_path):
    (tmp_path / 'file').write_text('demo')
    expected = digest({'file': b'demo'})
    assert source.read_source(tmp_path, expected).digest == expected
    (tmp_path / 'extra').write_text('unexpected')
    with pytest.raises(source.SetupError):
        source.read_source(tmp_path, expected)
    (tmp_path / 'extra').unlink()
    (tmp_path / 'link').symlink_to(tmp_path / 'file')
    with pytest.raises(source.SetupError, match='nonregular'):
        source.read_source(tmp_path, expected)


def test_source_limits_and_duplicate_json(tmp_path, monkeypatch):
    files = {'data/maps/map_groups.json': b'{"same":1,"same":2}'}
    parsed = source.read_source(archive(tmp_path, files), digest(files), 'fixture')
    with pytest.raises(source.SetupError):
        parsed.json('data/maps/map_groups.json')
    monkeypatch.setattr(source, 'MAX_TEXT', 2)
    with pytest.raises(source.SetupError, match='text size'):
        source.read_source(tmp_path / 'source.tar.gz', digest(files), 'fixture')


def test_bounded_constant_and_message_parsers():
    facts = Facts.__new__(Facts)
    facts.defines = {'BASE': '0x10', 'FIRST': '(BASE + 1)', 'END': '(FIRST + COUNT - 1)',
                     'COUNT': '3', 'FLAG': '(END + 2)', 'CYCLE': 'CYCLE', 'BAD': 'f()'}
    assert facts.number('FLAG') == 21
    for value in ['CYCLE', 'BAD', 'MISSING']:
        with pytest.raises(source.SetupError):
            facts.number(value)
    text = b'Demo::\n .string "Does the lamp glow?\\n"\n .string "Choose now.$"\n'
    s = source.Source({'data/maps/Demo/text.inc': text}, 'invented')
    assert message(s, 'data/maps/Demo/text.inc', 'Demo') == 'Does the lamp glow? Choose now.'
    with pytest.raises(source.SetupError):
        message(s, 'data/maps/Demo/text.inc', 'Absent')


@pytest.fixture
def setup_inputs(tmp_path, monkeypatch):
    monkeypatch.delenv('POKEGENT_MAP_GROUPS', raising=False)
    # Two groups of invented empty map headers; shared structural parser is real.
    image = bytearray(0x400)
    image[0xAC:0xB0] = b'TEST'
    base = 0x08000000
    struct.pack_into('<3I', image, 0x40, base+0x100, base+0x120, base+0x140)
    struct.pack_into('<2I', image, 0x4C, base+0x40, base+0x48)
    p = load_profiles().get('firered-us-rev0')
    symbols = {**p.symbols, 'gMapGroups': p.symbols['gMapGroups'].model_copy(
        update={'address': base+0x4C, 'size': 8})}
    identity = p.rom.model_copy(update={'sha1': hashlib.sha1(image).hexdigest(),
        'sha256': hashlib.sha256(image).hexdigest(), 'size': len(image), 'game_code': 'TEST'})
    p = p.model_copy(update={'symbols': symbols, 'rom': identity, 'pret_commit': 'fixture'})
    rom = tmp_path / 'own-input.bin'
    rom.write_bytes(image)
    files = {'data/maps/map_groups.json': b'{"invented":true}'}
    decomp = archive(tmp_path, files)
    docs = {
        'pret_map_groups.json': {'group_order': ['A', 'B'],
                                'groups': {'A': ['Demo', 'Exit'], 'B': ['Other']}},
        'fly_destinations.json': {'destinations': [{'mapsec': 1, 'name': 'Exit', 'map_key': [0, 1],
            'landing': [1, 1], 'visited_flag': 2, 'cell': [0, 0]}]},
        'cinnabar_quiz.yaml': {'questions': [{'number': 1, 'prompt': 'Does the lamp glow?',
                                           'answer_yes': True, 'door_flag': 3}]},
        'prompt_rules.yaml': {'rules': [{'kind': 'quiz', 'patterns': ['Does the lamp glow?'],
                                       'default_yes': True}]},
        'elevators.yaml': {'elevators': {'demo': {'scene': 'dialog.elevator', 'map': [0, 0],
            'menu': 'multichoice', 'panels': [[0, 0]], 'exit_mat': [1, 1], 'exit_push': 'down',
            'floors': [{'code': 'exit', 'label': 'EXIT', 'map': [0, 1], 'place': 'Demo exit'}],
            'cancel': 127}}},
        'puzzles.yaml': {'mansion': {'switch_flag': 4, 'maps': {'Demo': [0, 0]},
            'secret_key': {'map': [0, 0], 'tile': [1, 1], 'hidden_item_flag': 5},
            'statues': [{'id': 'demo', 'floor': 'Demo', 'map': [0, 0], 'statue': [1, 1],
                        'stand': [1, 2], 'facing': 'N', 'opens': [[[2, 1]]],
                        'closes': [[[3, 1]]]}]}},
        'field_surf.yaml': {'capability': {'move': {'name': 'Wave', 'move_id': 1},
            'badge': {'number': 1, 'name': 'Lamp', 'flag': 6}, 'prompt': {'text': 'Use the wave?'}},
            'start_rule': {'player_elevation': 1, 'front_tile': 'Water', 'result': 'Swim',
                           'water_behaviors': {}},
            'dismount_rule': {'land_elevation': 1, 'water_elevation': 0, 'rule': 'Step onto land'}},
    }
    profile = setup.SetupProfile(ProfileSet([p]), 'fixture', digest(files), lambda _: docs)
    return rom, tmp_path / 'generated', decomp, tmp_path / 'cache', profile, docs


def test_complete_setup_rerun_and_cache_repair(setup_inputs):
    rom, out, decomp, cache, profile, docs = setup_inputs
    calls = []
    def ensure(*args):
        calls.append(args)
        assert (out / setup.MANIFEST).is_file()  # publication precedes cache
    setup.setup(rom, out, decomp=decomp, cache_root=cache, profile=profile, ensure_cache=ensure)
    mtimes = {p.name: p.stat().st_mtime_ns for p in out.iterdir()}
    assert set(mtimes) == {*docs, setup.MANIFEST}
    assert out.stat().st_mode & 0o777 == cache.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in out.iterdir())
    decomp.unlink()  # rerun must not read/fetch/regenerate source
    setup.setup(rom, out, cache_root=cache, profile=profile, ensure_cache=ensure)
    assert len(calls) == 2
    assert {p.name: p.stat().st_mtime_ns for p in out.iterdir()} == mtimes


def test_cache_failure_preserves_complete_local_files(setup_inputs):
    rom, out, decomp, cache, profile, docs = setup_inputs
    def fail(*args):
        raise source.SetupError('invented cache failure')
    with pytest.raises(source.SetupError, match='cache failure'):
        setup.setup(rom, out, decomp=decomp, cache_root=cache, profile=profile, ensure_cache=fail)
    assert set(p.name for p in out.iterdir()) == {*docs, setup.MANIFEST}
    setup.setup(rom, out, cache_root=cache, profile=profile, ensure_cache=lambda *a: None)


def test_manual_data_and_bad_source_preserved(setup_inputs):
    rom, out, decomp, cache, profile, _ = setup_inputs
    out.mkdir()
    marker = out / 'manual.txt'
    marker.write_text('Keep me')
    with pytest.raises(source.SetupError):
        setup.setup(rom, out, decomp=decomp, cache_root=cache, profile=profile)
    assert marker.read_text() == 'Keep me'
    marker.unlink()
    out.rmdir()
    decomp.write_bytes(b'invented invalid gzip')
    with pytest.raises(source.SetupError):
        setup.setup(rom, out, decomp=decomp, cache_root=cache, profile=profile)
    assert not out.exists()


def test_old_or_missing_index_manifest_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv('POKEGENT_MAP_GROUPS', raising=False)
    monkeypatch.delenv('POKEGENT_LOCAL_DATA_DIR', raising=False)
    monkeypatch.setattr(rom_cache, '_generator_fingerprint', lambda: 'demo')
    for name in rom_cache.CACHE_FILES:
        (tmp_path / name).write_text('demo')
    manifest = {'sha1': 'demo', 'format_version': 2, 'generator_sha256': 'demo',
                'artifacts': list(rom_cache.CACHE_FILES),
                'sha256': rom_cache._artifact_digests(tmp_path), 'map_index_sha256': None}
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    assert not rom_cache._valid_cache(tmp_path, 'demo')
    manifest['format_version'] = rom_cache.CACHE_FORMAT_VERSION
    del manifest['map_index_sha256']
    path.write_text(json.dumps(manifest))
    assert not rom_cache._valid_cache(tmp_path, 'demo')


def test_failed_publication_preserves_competing_winner(setup_inputs, monkeypatch):
    rom, out, decomp, cache, profile, _ = setup_inputs
    original = setup.os.rename
    def race(start, target):
        out.mkdir()
        (out / 'manual').write_text('winner')
        original(start, target)  # actual POSIX refusal against a populated directory
    monkeypatch.setattr(setup.os, 'rename', race)
    with pytest.raises(OSError):
        setup.setup(rom, out, decomp=decomp, cache_root=cache, profile=profile)
    assert (out / 'manual').read_text() == 'winner'
    assert not list(out.parent.glob('.setup-tmp-*'))


def test_prepublication_io_error_leaves_no_output(setup_inputs, monkeypatch):
    rom, out, decomp, cache, profile, _ = setup_inputs
    def failed_sync(fd):
        raise OSError('invented disk failure')
    monkeypatch.setattr(setup.os, 'fsync', failed_sync)
    with pytest.raises(OSError):
        setup.setup(rom, out, decomp=decomp, cache_root=cache, profile=profile)
    assert not out.exists()
    assert not list(out.parent.glob('.setup-tmp-*'))


def test_changed_local_output_is_never_repaired_silently(setup_inputs):
    rom, out, decomp, cache, profile, _ = setup_inputs
    setup.setup(rom, out, decomp=decomp, cache_root=cache, profile=profile,
                ensure_cache=lambda *a: None)
    path = out / 'prompt_rules.yaml'
    path.write_text('{"rules":[]}')
    before = path.read_bytes()
    with pytest.raises(source.SetupError, match='new --out-dir'):
        setup.setup(rom, out, cache_root=cache, profile=profile, ensure_cache=lambda *a: None)
    assert path.read_bytes() == before


def test_download_total_deadline_and_owned_cleanup(tmp_path, monkeypatch):
    def timeout(command, **kwargs):
        assert command[1:4] == ['-m', source.__name__, '--download']
        assert kwargs['timeout'] == 60 and kwargs['check']
        raise subprocess.TimeoutExpired(command, 60)
    monkeypatch.setattr(source.subprocess, 'run', timeout)
    with pytest.raises(source.SetupError, match='offline'):
        source.cached_source(tmp_path)
    assert not list((tmp_path / 'sources').iterdir())


def test_source_redirect_refuses_other_host():
    handler = source._FixedRedirect()
    for url in ['http://codeload.github.com/elsewhere', 'https://other.example/archive',
                'https://user@codeload.github.com/archive']:
        with pytest.raises(source.SetupError):
            handler.redirect_request(None, None, 302, '', {}, url)


def test_selected_index_changes_invalidate_memoized_cache(tmp_path, monkeypatch):
    test_cache_rejects_changed_named_index(tmp_path, monkeypatch)
    index = tmp_path / 'index.json'
    target = tmp_path / 'cache'
    manifest_path = target / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['map_index_sha256'] = hashlib.sha256(index.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(rom_cache, 'rom_cache_dir', lambda rom: target)
    manifest['sha1'] = target.name
    manifest_path.write_text(json.dumps(manifest))
    assert rom_cache.existing_cache_path('demo') == target
    index.write_text('changed again')
    assert rom_cache.existing_cache_path('demo') is None


def test_quiz_parser_extracts_branches_and_rejects_ambiguity():
    from pokegent.games.gen3.firered.rom.setup_extract import quiz

    scripts, texts = [], []
    facts = Facts.__new__(Facts)
    facts.defines = {}
    for n in range(1, 7):
        prefix = f'CinnabarIsland_Gym_EventScript_Quiz{n}'
        scripts.append(f'{prefix}::\n msgbox Demo{n}, MSGBOX_YESNO\n'
            f' goto_if_eq VAR_RESULT, YES, {prefix}Correct\n'
            f' goto_if_eq VAR_RESULT, NO, {prefix}Incorrect\n'
            f'{prefix}Complete::\n setflag FLAG_DEMO{n}\n return\n')
        texts.append(f'Demo{n}::\n .string "Does invented lamp {n} glow?$"\n')
        facts.defines[f'FLAG_DEMO{n}'] = str(n+10)
    path = 'data/maps/CinnabarIsland_Gym/'
    facts.source = source.Source({path+'scripts.inc': ''.join(scripts).encode(),
                                 path+'text.inc': ''.join(texts).encode()}, 'invented')
    rows = quiz(facts)['questions']
    assert [q['door_flag'] for q in rows] == list(range(11, 17))
    assert all(q['answer_yes'] for q in rows)
    facts.source.files[path+'scripts.inc'] += scripts[0].encode()
    with pytest.raises(source.SetupError):
        quiz(facts)


def test_finite_prompt_mapping_and_safety_precedence(monkeypatch):
    from types import SimpleNamespace as NS

    from pokegent.actions.field.surf.rules import surf_capability
    from pokegent.actions.menu.dialog import prompts as dialog
    from pokegent.games.gen3.firered.rom.dialog_text import DialogText
    from pokegent.games.gen3.firered.rom.setup_extract import PROMPT_LABELS, prompts

    files = {}
    for i, (file, label, _, _) in enumerate(PROMPT_LABELS):
        files[file] = files.get(file, b'') + (
            f'{label}::\n .string "Use invented lamp {i}?$"\n').encode()
    questions = {'questions': [{'number': 1, 'prompt': 'Does the lamp glow?',
                               'answer_yes': True, 'door_flag': 3}]}
    rows = prompts(source.Source(files, 'invented'), questions)['rules']
    monkeypatch.setattr(dialog, '_RULES', [])
    for row in rows:
        dialog.register_rule(row['kind'], [re.escape(p) for p in row['patterns']],
                             {k: v for k, v in row.items() if k not in {'kind', 'patterns'}})
    for row in rows:
        info = dialog.classify_prompt(DialogText(tuple(row['patterns']), 'printing'))
        assert info.kind.value == row['kind']
        assert info.irreversible == row.get('irreversible', False)
    assert dialog.classify_prompt(None).kind.value == 'unreadable'
    # A registered permissive pattern still cannot outrank destructive handling.
    dialog.register_rule('field_move', ['Release invented lamp?'], {'default_yes': True})
    assert dialog.classify_prompt(DialogText(('Release invented lamp?',), 'printing')
                                  ).kind.value == 'destructive'
    assert not surf_capability(NS(usable=True, player=NS(badges=()), party=())).available
    assert not surf_capability(NS(usable=True, player=NS(badges=(5,)), party=())).available


@pytest.mark.parametrize('switch_set', [False, True])
def test_gate_order_preserves_actual_builder_verification_tile(monkeypatch, switch_set):
    from types import SimpleNamespace as NS

    from pokegent.actions.special import mansion, switch_puzzle
    from pokegent.games.gen3.firered.rom.setup_extract import _gate_groups

    # First-by-y differs from first-by-x: the consumer deliberately picks [0][0].
    groups = _gate_groups({(1, 9), (2, 9), (7, 1), (7, 2)})
    gates = tuple(tuple(map(tuple, group)) for group in groups)
    statue = mansion.MansionStatue('demo', 'Demo', (0, 0), (3, 3), (3, 4), 'N', gates, gates)
    monkeypatch.setattr(mansion, 'statues_on_map', lambda state: (statue,))
    monkeypatch.setattr(mansion, 'switch_state', lambda snapshot: switch_set)
    monkeypatch.setattr(mansion, 'SECRET_KEY_TILE', (1, 1))
    monkeypatch.setattr(switch_puzzle.N, 'Navigator', lambda state: NS(search=lambda: ()))
    monkeypatch.setattr(switch_puzzle.T, 'plan_target', lambda *args: NS(length=0))
    state = NS(player=NS(avatar_x=3, avatar_y=4), identity=NS(group=0, number=0, name='Demo'))
    draft = switch_puzzle.MansionBuilder().build(NS(snapshot=NS(map=state)))
    args = draft.options[0].executor_args
    assert (args['verify_x'], args['verify_y']) == (1, 9)
