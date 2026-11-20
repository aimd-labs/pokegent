"""Invented identities through the real setup/cache validators and admission factory."""
import hashlib
import json

import pytest
from test_local_setup import setup_inputs as setup_inputs
from test_story_source import artifact, load

from pokegent.app import AppError, config_from_mapping, make_objective_session
from pokegent.games.gen3.firered.rom import local_setup, rom_cache, setup_source
from pokegent.goals.source import ObjectiveSession
from pokegent.goals.store import ObjectiveStore


@pytest.fixture
def identities(setup_inputs, monkeypatch):
    from pokegent.goals import story_identity as identity
    rom, out, decomp, cache, profile, docs = setup_inputs
    local_setup.setup(rom, out, decomp=decomp, cache_root=cache, profile=profile,
                      ensure_cache=lambda *a: None)
    monkeypatch.setenv('POKEGENT_LOCAL_DATA_DIR', str(out))
    monkeypatch.setenv('POKEGENT_ROM_DATA_CACHE', str(cache))
    monkeypatch.setattr(identity, 'PRET_COMMIT', profile.commit)
    monkeypatch.setattr(identity, 'TREE_SHA256', profile.tree_sha256)
    monkeypatch.setattr(identity, 'load_profiles', lambda: profile.profiles)
    monkeypatch.setattr(rom_cache, '_generator_fingerprint', lambda: 'invented-generator')
    sha1 = hashlib.sha1(rom.read_bytes()).hexdigest()
    target = cache / sha1
    target.mkdir()
    for name in rom_cache.CACHE_FILES:
        (target / name).write_text('invented cache data')
    index_hash = rom_cache._map_index_digest()
    manifest = dict(sha1=sha1, format_version=rom_cache.CACHE_FORMAT_VERSION,
                    generator_sha256='invented-generator', artifacts=list(rom_cache.CACHE_FILES),
                    sha256=rom_cache._artifact_digests(target), map_index_sha256=index_hash)
    (target / 'manifest.json').write_text(json.dumps(manifest))
    data = artifact()
    data['provenance'].update(rom_sha256=hashlib.sha256(rom.read_bytes()).hexdigest(),
        source_sha256=profile.tree_sha256, source_pin=profile.commit,
        map_index_sha256=index_hash)
    story_dir = out.parent / 'story'
    story_dir.mkdir()
    source = load(story_dir, data)
    return identity, rom, out, target, source, docs


def test_identity_binds_actual_inputs_without_changing_setup_or_cache(identities):
    identity, rom, out, cache, source, docs = identities
    before = {p: p.stat().st_mtime_ns for d in (out, cache) for p in d.iterdir()}
    identity.verify_identity(source.graph, rom)
    assert {p: p.stat().st_mtime_ns for d in (out, cache) for p in d.iterdir()} == before
    assert set(p.name for p in out.iterdir()) == {*docs, local_setup.MANIFEST}


@pytest.mark.parametrize('bad', ['rom', 'declared', 'setup', 'source', 'index', 'cache', 'missing'])
def test_identity_refuses_tampering_and_overrides(identities, monkeypatch, bad):
    from pokegent.goals.dex_fallback import FallbackUnavailable
    identity, rom, out, cache, source, _ = identities
    if bad == 'rom':
        rom.write_bytes(b'other image')
    elif bad == 'declared':
        source.graph.provenance.rom_sha256 = 'd' * 64
    elif bad == 'setup':
        (out / 'prompt_rules.yaml').write_text('{}')
    elif bad == 'source':
        manifest = json.loads((out / local_setup.MANIFEST).read_text())
        manifest['tree_sha256'] = 'e' * 64
        (out / local_setup.MANIFEST).write_text(json.dumps(manifest))
    elif bad == 'index':
        alternate = out.parent / 'alternate.json'
        alternate.write_text('{"group_order":["X"],"groups":{"X":["Other"]}}')
        monkeypatch.setenv('POKEGENT_MAP_GROUPS', str(alternate))
    elif bad == 'cache':
        # Prime the existing memo; admission must still inspect actual cache bytes.
        assert rom_cache.existing_cache_path(rom) == cache
        (cache / rom_cache.CACHE_FILES[0]).write_text('corrupted')
    else:
        rom = None
    with pytest.raises(FallbackUnavailable, match='^story identity unavailable$'):
        identity.verify_identity(source.graph, rom)


@pytest.mark.parametrize('form', ['path', 'store', 'session'])
def test_admission_checks_all_factory_forms_and_preserves_manual_goal(identities, form):
    _, rom, out, _, source, _ = identities
    path = out.parent / 'objective.json'
    store = ObjectiveStore(path)
    assert store.propose(dict(objective_id='manual', kind='hold', revision=1,
                              set_by='operator')).accepted
    original = ObjectiveSession(store)
    arg = dict(path=path, store=store, session=original)[form]
    story_path = out.parent / 'story' / 'progression.json'
    session = make_objective_session(arg, story_fallback=True, story_path=story_path, rom_path=rom)
    if form == 'session':
        assert session is original
    session.refresh()
    assert session.objective.objective_id == 'manual'
    with pytest.raises(AppError):
        make_objective_session(arg, story_fallback=True, story_path=story_path)


def test_config_explicit_story_path(tmp_path):
    path = str(tmp_path / 'story' / 'progression.json')
    cfg = config_from_mapping(dict(objectives=dict(fallback='story', story_path=path),
                                   run=dict(on_objective_done='continue')))
    assert cfg.story_path == path


def test_source_retains_new_constants_without_new_setup_schema(tmp_path):
    from test_local_setup import archive, digest
    files = {f'include/constants/{name}.h': b'#define INVENTED 7\n'
             for name in ('items', 'vars')}
    source = setup_source.read_source(archive(tmp_path, files), digest(files), 'fixture')
    assert set(source.files) == set(files)
    from pokegent.local_data import SCHEMAS
    assert len(SCHEMAS) == 7 and 'progression.json' not in SCHEMAS


def test_prebuilt_story_cannot_bypass_identity_with_flag_off(identities):
    _, rom, out, _, source, _ = identities
    session = ObjectiveSession(ObjectiveStore(out.parent / 'goal.json'), fallback=source)
    assert make_objective_session(session, rom_path=rom) is session
    with pytest.raises(AppError):
        make_objective_session(session)


@pytest.mark.parametrize('change', ['index', 'setup', 'rom'])
def test_mid_admission_mutation_refuses_without_repair(identities, monkeypatch, change):
    identity, rom, out, _, source, _ = identities
    valid = rom_cache._valid_cache
    def mutate(directory, sha1):
        result = valid(directory, sha1)
        if change == 'index':
            (out / 'pret_map_groups.json').write_text('{}')
        elif change == 'setup':
            (out / 'prompt_rules.yaml').write_text('{}')
        else:
            rom.write_bytes(b'changed during admission')
        return result
    monkeypatch.setattr(rom_cache, '_valid_cache', mutate)
    from pokegent.goals.dex_fallback import FallbackUnavailable
    with pytest.raises(FallbackUnavailable, match='^story identity unavailable$'):
        identity.verify_identity(source.graph, rom)
    if change != 'rom':
        assert (out / ('pret_map_groups.json' if change == 'index' else 'prompt_rules.yaml')
                ).read_text() == '{}'
    else:
        assert rom.read_bytes() == b'changed during admission'


@pytest.mark.parametrize('form', ['path', 'store', 'session'])
def test_off_and_dex_do_not_require_rom(tmp_path, monkeypatch, form):
    from pokegent.games.gen3.firered.rom import wild
    from pokegent.goals.dex_fallback import DexFallback
    store = ObjectiveStore(tmp_path / 'goal.json')
    original = ObjectiveSession(store)
    arg = dict(path=store.path, store=store, session=original)[form]
    monkeypatch.setattr(wild, 'load_wild_table', lambda: object())
    assert make_objective_session(arg) is not None
    assert isinstance(make_objective_session(arg, dex_fallback=True).fallback, DexFallback)


@pytest.mark.parametrize('path', ['', 42, False])
def test_invalid_story_paths_rejected_consistently(path, tmp_path):
    from pokegent.app import AppConfig
    from pokegent.user_config import load_user_config
    with pytest.raises(AppError):
        AppConfig(story_path=path)
    with pytest.raises(AppError):
        config_from_mapping(dict(objectives=dict(story_path=path)))
    target = tmp_path / 'config.yaml'
    target.write_text(json.dumps(dict(objectives=dict(story_path=path))))
    with pytest.raises(ValueError):
        load_user_config(target)


def test_build_runner_threads_selected_rom_and_story_path(tmp_path, monkeypatch):
    from pokegent.app import AppConfig, build_runner
    class ReachedFactory(Exception):
        pass
    def admission(objectives, plan, **kwargs):
        assert kwargs['rom_path'] == tmp_path / 'selected.bin'
        assert kwargs['story_path'] == str(tmp_path / 'recipe.json')
        assert kwargs['story_fallback'] is True
        raise ReachedFactory
    monkeypatch.setattr('pokegent.app.make_objective_session', admission)
    with pytest.raises(ReachedFactory):
        build_runner(actor=None, profile=None, events=None, rom_path=tmp_path / 'selected.bin',
                     objectives=tmp_path / 'goal.json',
                     config=AppConfig(story_fallback=True, on_objective_done='continue',
                                      story_path=str(tmp_path / 'recipe.json')))


def test_explicit_dex_replaces_prebuilt_story_without_rom(tmp_path, monkeypatch):
    from pokegent.games.gen3.firered.rom import wild
    from pokegent.goals.dex_fallback import DexFallback
    source = load(tmp_path)
    session = ObjectiveSession(ObjectiveStore(tmp_path / 'goal.json'), fallback=source)
    monkeypatch.setattr(wild, 'load_wild_table', lambda: object())
    assert isinstance(make_objective_session(session, dex_fallback=True).fallback, DexFallback)


@pytest.mark.parametrize('bad', [False, 7, object()])
def test_direct_factory_bad_story_path_is_redacted(tmp_path, bad):
    with pytest.raises(AppError, match='^story fallback requires valid private progression data$'):
        make_objective_session(tmp_path / 'goal.json', story_fallback=True, story_path=bad)


@pytest.mark.parametrize('field,value', [('source_sha256', 'f' * 64),
                                          ('map_index_sha256', 'f' * 64),
                                          ('source_pin', 'other-pin')])
def test_declared_source_pin_and_index_cannot_override_current_inputs(identities, field, value):
    from pokegent.goals.dex_fallback import FallbackUnavailable
    identity, rom, _, _, source, _ = identities
    setattr(source.graph.provenance, field, value)
    with pytest.raises(FallbackUnavailable, match='^story identity unavailable$'):
        identity.verify_identity(source.graph, rom)
