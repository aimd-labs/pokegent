"""Explicit private seven-file setup, followed by the existing verified ROM cache."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pokegent.games.gen3.firered.rom.map_index import _external, extract_map_index
from pokegent.games.gen3.firered.rom.rom_data import (
    ProfileSet,
    identify_rom_image,
    load_profiles,
    require_supported,
)
from pokegent.games.gen3.firered.rom.setup_extract import extract_documents
from pokegent.games.gen3.firered.rom.setup_source import (
    PRET_COMMIT,
    TREE_SHA256,
    SetupError,
    Source,
    cached_source,
    read_source,
)
from pokegent.local_data import SCHEMAS

GENERATOR_VERSION = 1
MANIFEST = 'setup-manifest.json'


@dataclass(frozen=True)
class SetupProfile:
    """Internal synthetic-test seam; production CLI never admits custom profiles."""

    profiles: ProfileSet
    commit: str = PRET_COMMIT
    tree_sha256: str = TREE_SHA256
    extract: Callable[[Source], dict[str, dict]] = extract_documents


def _cache(rom: Path, directory: Path, cache: Path) -> None:
    env = {**os.environ, 'POKEGENT_LOCAL_DATA_DIR': str(directory),
           'POKEGENT_ROM_DATA_CACHE': str(cache), 'FIRERED_ROM_PATH': str(rom)}
    env.pop('POKEGENT_MAP_GROUPS', None)
    result = subprocess.run([sys.executable, '-m', 'pokegent.cli', 'rom-data', 'build',
                             '--if-missing', '--rom', str(rom)], env=env, timeout=300,
                            stdout=subprocess.DEVNULL)
    if result.returncode:
        raise SetupError('local files are complete; cache failed, rerun setup to repair')


def _encode(document: dict) -> bytes:
    # JSON is a strict YAML subset; one deterministic serializer serves both readers.
    return (json.dumps(document, sort_keys=True, ensure_ascii=False, indent=2) + '\n').encode()


def _write(directory: Path, name: str, content: bytes) -> None:
    fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _validate_documents(documents: dict[str, dict]) -> None:
    if set(documents) != set(SCHEMAS):
        raise SetupError('setup must produce exactly seven documents')
    for name, document in documents.items():
        SCHEMAS[name].model_validate(document)
    index = documents['pret_map_groups.json']
    groups = [index['groups'][name] for name in index['group_order']]
    def label(key):
        try:
            return groups[key[0]][key[1]]
        except IndexError:
            raise SetupError('local sidecar map key is outside the named index') from None
    for row in documents['fly_destinations.json']['destinations']:
        if label(row['map_key']).replace('_', '').lower() != row['name'].replace('_', '').lower():
            raise SetupError('local Fly label disagrees with named index')
    for row in documents['elevators.yaml']['elevators'].values():
        label(row['map'])
        for floor in row['floors']:
            label(floor['map'])
    mansion = documents['puzzles.yaml']['mansion']
    for key in mansion['maps'].values():
        label(key)
    label(mansion['secret_key']['map'])


def _rerun(directory: Path, identity: dict) -> None:
    from pokegent.local_data import read_data

    if set(p.name for p in directory.iterdir()) != {*SCHEMAS, MANIFEST}:
        raise SetupError('existing output differs; choose a new --out-dir')
    hashes = {}
    for name in SCHEMAS:
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise SetupError('existing output differs; choose a new --out-dir')
        read_data(name, path=path)
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path = directory / MANIFEST
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 65536:
        raise SetupError('existing setup manifest invalid')
    manifest = json.loads(manifest_path.read_text())
    if manifest != {**identity, 'sha256': hashes}:
        raise SetupError('existing output differs; choose a new --out-dir')


def setup(rom: Path, out_dir: Path, *, decomp: Path | None = None,
          cache_root: Path | None = None, profile: SetupProfile | None = None,
          ensure_cache: Callable[[Path, Path, Path], None] = _cache) -> Path:
    """Preserve manual output; reruns validate local files and repair only the cache."""
    from pokegent.games.gen3.firered.rom.rom_cache import cache_root as default_cache_root

    profile = profile or SetupProfile(load_profiles())
    if not out_dir.is_absolute() or out_dir.is_symlink():
        raise SetupError('output must be an absolute external directory without a final symlink')
    rom, out_dir = _external(rom), _external(out_dir)
    cache = _external(cache_root or default_cache_root())
    if cache == out_dir or cache.is_relative_to(out_dir) or out_dir.is_relative_to(cache):
        raise SetupError('local data and cache directories must be separate')
    override = os.environ.get('POKEGENT_MAP_GROUPS')
    if override and Path(override).expanduser().resolve() != out_dir / 'pret_map_groups.json':
        raise SetupError('unset conflicting POKEGENT_MAP_GROUPS before setup')
    with os.fdopen(os.open(rom, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 0x2000000:
            raise SetupError('ROM must be a bounded regular file')
        image = handle.read(info.st_size + 1)
    identification = identify_rom_image(image, profile.profiles)
    supported = require_supported(identification, profile.profiles)
    if supported.profile_id != 'firered-us-rev0' or supported.pret_commit != profile.commit:
        raise SetupError('setup requires the supported FireRed profile and pinned source')
    identity = {'generator_version': GENERATOR_VERSION, 'pret_commit': profile.commit,
                'tree_sha256': profile.tree_sha256, 'rom_sha1': identification.sha1}
    if out_dir.exists():
        _rerun(out_dir, identity)
    else:
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        source = (read_source(_external(decomp), profile.tree_sha256, profile.commit)
                  if decomp else cached_source(cache))
        documents = profile.extract(source)
        _validate_documents(documents)
        structural = extract_map_index(image, supported)
        named = documents['pret_map_groups.json']
        if ([len(named['groups'][g]) for g in named['group_order']]
                != [len(v) for v in structural['groups'].values()]):
            raise SetupError('source map groups disagree with verified ROM')
        out_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix='.setup-tmp-', dir=out_dir.parent))
        published = False
        try:
            hashes = {}
            for name, document in documents.items():
                content = _encode(document)
                _write(temporary, name, content)
                hashes[name] = hashlib.sha256(content).hexdigest()
            _write(temporary, MANIFEST, _encode({**identity, 'sha256': hashes}))
            directory_fd = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            # POSIX directory rename is atomic; a populated competing winner fails.
            # User-controlled paths: this does not promise RENAME_NOREPLACE semantics.
            if out_dir.exists():
                raise SetupError('output appeared during setup; choose a new --out-dir')
            os.rename(temporary, out_dir)
            published = True
            try:
                parent_fd = os.open(out_dir.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            except OSError:
                print('warning: complete local files published; directory durability uncertain',
                      file=sys.stderr)
        finally:
            if not published:
                shutil.rmtree(temporary)
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        ensure_cache(rom, out_dir, cache)
    except subprocess.TimeoutExpired:
        raise SetupError('local files complete; cache timed out; rerun setup to repair') from None
    return out_dir
