"""Invented pointer arrays and geometry; no ROM data or expected campaign labels."""

import hashlib
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from pokegent import cli
from pokegent.games.gen3.firered.rom import map_index as tool
from pokegent.games.gen3.firered.rom.rom_data import ProfileSet, load_profiles


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    image = bytearray(0x2000)
    image[0xAC:0xB0] = b'TEST'
    base = 0x08000000
    struct.pack_into('<3I', image, 0x40, base + 0x100, base + 0x100, base + 0x120)
    struct.pack_into('<2I', image, 0x4C, base + 0x40, base + 0x48)
    for offset in (0x100, 0x120):
        struct.pack_into('<IIII', image, offset, base + 0x200, base + 0x400, 0, 0)
    struct.pack_into('<iiIIIIBB', image, 0x200, 1, 1, base + 0x500, base + 0x502,
                     base + 0x300, base + 0x320, 1, 1)
    for offset in (0x314, 0x334):
        struct.pack_into('<I', image, offset, base + 0x800)
    profile = load_profiles().get('firered-us-rev0')
    symbols = dict(profile.symbols)
    symbols['gMapGroups'] = symbols['gMapGroups'].model_copy(
        update={'address': base + 0x4C, 'size': 8})
    identity = profile.rom.model_copy(update={
        'sha1': hashlib.sha1(image).hexdigest(), 'sha256': hashlib.sha256(image).hexdigest(),
        'size': len(image), 'game_code': 'TEST',
    })
    profile = profile.model_copy(update={'symbols': symbols, 'rom': identity})
    monkeypatch.setattr(tool, 'load_profiles', lambda path: ProfileSet([profile]))
    rom = tmp_path / 'private-input.bin'
    rom.write_bytes(image)
    output = tmp_path / 'output'
    output.mkdir()
    return bytes(image), profile, rom, output


def run_python(code, directory, *, index, data=None):
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1]),
           'POKEGENT_MAP_GROUPS': str(index), 'PYTHONDONTWRITEBYTECODE': '1'}
    env.pop('POKEGENT_LOCAL_DATA_DIR', None)
    if data is not None:
        env['POKEGENT_LOCAL_DATA_DIR'] = str(data)
    return subprocess.run([sys.executable, '-B', '-c', code], cwd=directory, env=env,
                          capture_output=True, text=True, timeout=20)


def test_build_and_actual_geometry_objective_consumers(inputs, tmp_path):
    from pokegent.games.gen3.firered.maps.map_data import MapReader
    from pokegent.games.gen3.firered.rom.rom_tables import RomImagePort

    image, profile, rom, output = inputs
    target = tool.build_map_index(rom, output)
    doc = json.loads(target.read_text())
    assert doc['group_order'] == ['Group_000', 'Group_001']
    assert doc['groups'] == {'Group_000': ['Map_G000_N000', 'Map_G000_N001'],
                             'Group_001': ['Map_G001_N000']}
    assert doc['label_kind'] == 'numeric-id-v1' and doc['named_compatibility'] is False
    assert doc['provenance']['rom_sha256'] == hashlib.sha256(image).hexdigest()
    assert list(output.iterdir()) == [target]
    assert target.stat().st_mode & 0o777 == 0o600
    assert rom.read_bytes() == image
    code = '''
from pokegent.games.gen3.firered.maps import map_names
from pokegent.goals.validate import validate
assert map_names.group_sizes() == (2, 1)
assert map_names.find_map('map_g001_n000') == (1, 0)
request = dict(objective_id='invented', revision=1, kind='reach',
               target={'map': 'Map_G001_N000'}, success_probes=[], set_by='agent')
assert validate(request).ok
bad = validate({**request, 'target': {'map': 'DemoExit'}})
assert not bad.ok and any(r.code == 'unknown_map' for r in bad.reasons)
from pokegent.actions.field import fly
assert fly.DESTINATIONS == ()
'''
    result = run_python(code, tmp_path, index=target)
    assert result.returncode == 0, result.stderr
    # Select the produced index for the actual map reader, without stubbing its methods.
    from unittest.mock import patch
    with patch.dict(os.environ, {'POKEGENT_MAP_GROUPS': str(target)}):
        reader = MapReader(RomImagePort(image), profile)
        result = reader.read_static(0, 1)
        assert result.usable, result.errors
        assert result.state.identity.name == 'Map_G000_N001'
        assert (result.state.layout.width, result.state.layout.height) == (1, 1)
        assert reader.read_static(0, 2).state is None
    other = tmp_path / 'other'
    other.mkdir()
    assert tool.build_map_index(rom, other).read_bytes() == target.read_bytes()


@pytest.mark.parametrize('offset,value', [
    (0x4C, 0), (0x4C, 0x02000000), (0x4C, 0x07FFFFFC),
    (0x4C, 0x08000041), (0x50, 0x08000040), (0x50, 0x08000050),
    (0x40, 0), (0x40, 0x08001FF0), (0x40, 0x08000101), (0x48, 0x0A000000),
])
def test_bad_pointer_spans_fail_without_partial_result(inputs, offset, value):
    image, profile, _, _ = inputs
    bad = bytearray(image)
    struct.pack_into('<I', bad, offset, value)
    with pytest.raises(tool.MapIndexError):
        tool.extract_map_index(bytes(bad), profile)


@pytest.mark.parametrize('size', [None, 0, 3, 1028])
def test_bad_root_size(inputs, size):
    image, profile, _, _ = inputs
    symbols = dict(profile.symbols)
    symbols['gMapGroups'] = symbols['gMapGroups'].model_copy(update={'size': size})
    with pytest.raises(tool.MapIndexError):
        tool.extract_map_index(image, profile.model_copy(update={'symbols': symbols}))


@pytest.mark.parametrize('end', [0x4D, 0x102, 0x13B])
def test_truncated_root_or_header(inputs, end):
    image, profile, _, _ = inputs
    with pytest.raises(tool.MapIndexError):
        tool.extract_map_index(image[:end], profile)


def test_count_cap_and_root_boundary(inputs, monkeypatch):
    image, profile, _, _ = inputs
    bad = bytearray(image)
    struct.pack_into('<I', bad, 0x4C, 0x08000040)
    symbols = dict(profile.symbols)
    # A single group ending at the root, with 257 slots: all reads would fit.
    struct.pack_into('<I', bad, 0x444, 0x08000040)
    symbols['gMapGroups'] = symbols['gMapGroups'].model_copy(
        update={'address': 0x08000444, 'size': 4})
    read = tool.RomImagePort.read_range
    def bounded_read(port, address, size):
        assert size <= 1024, 'oversized group must fail before its array is read'
        return read(port, address, size)
    monkeypatch.setattr(tool.RomImagePort, 'read_range', bounded_read)
    with pytest.raises(tool.MapIndexError):
        tool.extract_map_index(bytes(bad), profile.model_copy(update={'symbols': symbols}))


@pytest.mark.parametrize('change', ['byte', 'code', 'version', 'size', 'sha256', 'unsupported'])
def test_actual_identity_gate(inputs, monkeypatch, change):
    image, profile, rom, output = inputs
    if change in ('sha256', 'unsupported'):
        profile = profile.model_copy(update={'supported': False}) if change == 'unsupported' else (
            profile.model_copy(update={'rom': profile.rom.model_copy(update={'sha256': '0' * 64})}))
        monkeypatch.setattr(tool, 'load_profiles', lambda path: ProfileSet([profile]))
    else:
        bad = bytearray(image)
        if change == 'size':
            bad.append(0)
        else:
            bad[{'byte': 0x1800, 'code': 0xAC, 'version': 0xBC}[change]] ^= 1
        rom.write_bytes(bad)
        if change in ('code', 'version'):
            identity = profile.rom.model_copy(update={
                'sha1': hashlib.sha1(bad).hexdigest(),
                'sha256': hashlib.sha256(bad).hexdigest(),
            })
            profile = profile.model_copy(update={'rom': identity})
            monkeypatch.setattr(tool, 'load_profiles', lambda path: ProfileSet([profile]))
    with pytest.raises(tool.MapIndexError):
        tool.build_map_index(rom, output)
    assert not list(output.iterdir())


def test_packaged_profile_not_environment_override(inputs, monkeypatch):
    _, _, rom, output = inputs
    monkeypatch.setattr(tool, 'load_profiles', load_profiles)
    monkeypatch.setenv('POKEGENT_ROM_PROFILES', str(rom))
    with pytest.raises(tool.MapIndexError):
        tool.build_map_index(rom, output)
    assert not list(output.iterdir())


@pytest.mark.parametrize('kind', ['existing', 'sidecar', 'target-link', 'out-link', 'input-link',
                                  'directory', 'fifo', 'checkout', 'parent-link', 'git',
                                  'empty', 'oversize', 'relative'])
def test_path_refusals_preserve_existing_files(inputs, tmp_path, monkeypatch, kind):
    _, _, rom, output = inputs
    marker = output / 'pret_map_groups.json'
    if kind in ('existing', 'sidecar'):
        marker = output / ('pret_map_groups.json' if kind == 'existing' else 'other.yaml')
        marker.write_text('invented prior bytes')
    elif kind == 'target-link':
        marker.symlink_to(tmp_path / 'absent')
    elif kind in ('out-link', 'input-link'):
        link = tmp_path / 'link'
        link.symlink_to(output if kind == 'out-link' else rom)
        if kind == 'out-link':
            output = link
        else:
            rom = link
    elif kind == 'directory':
        rom = output
    elif kind == 'fifo':
        rom = tmp_path / 'fifo'
        os.mkfifo(rom)
    elif kind == 'checkout':
        monkeypatch.setattr(tool, '_CHECKOUT', tmp_path)
    elif kind == 'parent-link':
        monkeypatch.setattr(tool, '_CHECKOUT', output)
        link = tmp_path / 'parent'
        link.symlink_to(tmp_path)
        output = link / 'output'
    elif kind == 'git':
        output = tmp_path / '.git'
        output.mkdir()
    elif kind in ('empty', 'oversize'):
        os.truncate(rom, 0 if kind == 'empty' else 0x2000001)
    else:
        output = Path('relative-output')
    with pytest.raises((tool.MapIndexError, OSError)):
        tool.build_map_index(rom, output)
    if kind in ('existing', 'sidecar'):
        assert marker.read_text() == 'invented prior bytes'


def test_no_clobber_race(inputs, monkeypatch):
    _, _, rom, output = inputs
    fsync = os.fsync
    def race(fd):
        fsync(fd)
        (output / 'pret_map_groups.json').write_text('invented concurrent writer')
    monkeypatch.setattr(tool.os, 'fsync', race)
    with pytest.raises(FileExistsError):
        tool.build_map_index(rom, output)
    assert (output / 'pret_map_groups.json').read_text() == 'invented concurrent writer'
    assert len(list(output.iterdir())) == 1


@pytest.mark.parametrize('phase', ['create', 'write', 'flush', 'fsync', 'publish'])
def test_precommit_io_failure_preserves_siblings(inputs, monkeypatch, phase):
    _, _, rom, output = inputs
    sibling = output.parent / 'unrelated'
    sibling.write_text('invented preserved bytes')
    def fail(*args, **kwargs):
        raise OSError('private text must not escape CLI')
    if phase == 'create':
        monkeypatch.setattr(tool.tempfile, 'mkstemp', fail)
    elif phase in ('fsync', 'publish'):
        monkeypatch.setattr(tool.os, 'fsync' if phase == 'fsync' else 'link', fail)
    else:
        fdopen = os.fdopen
        class BrokenFile:
            def __init__(self, *args):
                self.file = fdopen(*args)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.file.close()
            def __getattr__(self, name):
                return fail if name == phase else getattr(self.file, name)
        monkeypatch.setattr(tool.os, 'fdopen', BrokenFile)
    with pytest.raises(OSError):
        tool.build_map_index(rom, output)
    assert not list(output.iterdir())
    assert sibling.read_text() == 'invented preserved bytes'


def test_postcommit_failure_warns_and_retains_output(inputs, monkeypatch, capsys):
    _, _, rom, output = inputs
    fsync = os.fsync
    calls = []
    def fail_directory(fd):
        calls.append(fd)
        if len(calls) == 2:
            raise OSError('private directory detail')
        fsync(fd)
    monkeypatch.setattr(tool.os, 'fsync', fail_directory)
    target = tool.build_map_index(rom, output)
    assert json.loads(target.read_text())['label_kind'] == 'numeric-id-v1'
    assert 'published' in capsys.readouterr().err
    assert list(output.iterdir()) == [target]


def test_postcommit_cleanup_failure_warns(inputs, monkeypatch, capsys):
    _, _, rom, output = inputs
    def fail_unlink(path):
        raise OSError('private cleanup detail')
    with monkeypatch.context() as patch:
        patch.setattr(tool.Path, 'unlink', fail_unlink)
        target = tool.build_map_index(rom, output)
    assert json.loads(target.read_text())['named_compatibility'] is False
    warning = capsys.readouterr().err
    assert 'published' in warning and 'private cleanup detail' not in warning
    assert len(list(output.iterdir())) == 2  # Published artifact and its retained owned temp.


def test_cli_opt_in_success_and_redaction(inputs, monkeypatch, capsys):
    _, _, rom, output = inputs
    args = ['rom-data', 'map-index', '--rom', str(rom), '--out-dir', str(output)]
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2
    capsys.readouterr()
    cli.main([*args, '--numeric-labels'])
    assert capsys.readouterr().out.strip() == 'Numeric map index written.'
    with pytest.raises(SystemExit) as error:
        cli.main([*args, '--numeric-labels'])
    assert error.value.code == 2
    result = capsys.readouterr()
    assert str(rom) not in result.err and str(output) not in result.err
    assert 'Traceback' not in result.err


def test_cli_io_redaction_and_help_outside_cwd(inputs, tmp_path, monkeypatch, capsys):
    _, _, rom, output = inputs
    def fail(*args):
        raise OSError('synthetic secret detail')
    monkeypatch.setattr(tool, 'build_map_index', fail)
    with pytest.raises(SystemExit) as error:
        cli.main(['rom-data', 'map-index', '--numeric-labels', '--rom', str(rom),
                  '--out-dir', str(output)])
    assert error.value.code == 2
    result = capsys.readouterr()
    assert 'synthetic secret' not in result.err and str(rom) not in result.err
    help_result = run_python('from pokegent.cli import main; '
                             'main(["rom-data", "map-index", "--help"])',
                             tmp_path, index=output / 'absent.json')
    assert help_result.returncode == 0 and '--numeric-labels' in help_result.stdout
    bad = run_python('from pokegent.cli import main; main(["rom-data", "map-index", '
                     '"--numeric-labels", "--rom", "absent", "--out-dir", "."])',
                     tmp_path, index=output / 'absent.json')
    assert bad.returncode == 2 and 'Traceback' not in bad.stderr


def test_manual_mixed_fly_and_other_sidecar_contracts(inputs, tmp_path):
    _, _, rom, output = inputs
    numeric = tool.build_map_index(rom, output)
    manual = tmp_path / 'manual'
    manual.mkdir()
    index = manual / 'pret_map_groups.json'
    index.write_text(json.dumps({'group_order': ['Demo'], 'groups': {'Demo': ['DemoExit']}}))
    (manual / 'fly_destinations.json').write_text(json.dumps({'destinations': [dict(
        mapsec=0, name='DemoExit', map_key=[0, 0], landing=[0, 0], visited_flag=0, cell=[0, 0])]}))
    (manual / 'cinnabar_quiz.yaml').write_text(json.dumps({'questions': [dict(
        number=1, prompt='Invented lamp?', answer_yes=True, door_flag=1)]}))
    before = index.read_bytes()
    code = 'from pokegent.actions.field import fly; assert len(fly.DESTINATIONS) == 1'
    assert run_python(code, tmp_path, index=index, data=manual).returncode == 0
    mixed = run_python(code, tmp_path, index=numeric, data=manual)
    assert mixed.returncode != 0 and 'LocalDataError' in mixed.stderr
    assert 'does not match' in mixed.stderr
    other = run_python('from pokegent.local_data import read_data; '
                       'assert read_data("cinnabar_quiz.yaml")["questions"][0]["answer_yes"]',
                       tmp_path, index=numeric, data=manual)
    assert other.returncode == 0, other.stderr
    assert index.read_bytes() == before


def test_padded_names_avoid_current_objective_normalization_collision():
    from pokegent.goals.validate import normalize_map_name
    assert normalize_map_name('Map_1_23') == normalize_map_name('Map_12_3')
    assert normalize_map_name('Map_G001_N023') != normalize_map_name('Map_G012_N003')
