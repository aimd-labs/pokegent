"""Numeric structural index from one verified local image; never a pret-name dataset.

Layout recipe: pret/pokefirered 037335f4, tools/mapjson/mapjson.cpp:436-456.
The emitter puts consecutive group arrays before the aligned root pointer table.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

from pokegent.games.gen3.firered.reader.memory_port import MemoryReadError
from pokegent.games.gen3.firered.rom.rom_data import (
    RomProfile,
    UnsupportedRomError,
    identify_rom_image,
    load_profiles,
    require_supported,
)
from pokegent.games.gen3.firered.rom.rom_tables import RomImagePort
from pokegent.local_data import MapGroups
from pokegent.resources import reference_path

_CHECKOUT = Path(__file__).resolve().parents[5]
_PROFILE = 'firered-us-rev0'
_PRET = '037335f4c725d7c9aecdac87066f2002b4bd7e14'
_FILENAME = 'pret_map_groups.json'


class MapIndexError(ValueError):
    """Fixed, content-free diagnostic for invalid input or destination."""


def extract_map_index(image: bytes, profile: RomProfile) -> dict:
    """Parse bounded adjacent arrays; callers must verify production image identity first."""
    port = RomImagePort(image)
    root = profile.symbol('gMapGroups')
    if not root.size or root.size % 4 or not 1 <= root.size // 4 <= 256:
        raise MapIndexError('invalid root table size')

    def checked(address: int, size: int) -> bytes:
        if address % 4 or size <= 0 or not 0x08000000 <= address < 0x0A000000:
            raise MapIndexError('invalid map pointer or span')
        try:
            return port.read_range(address, size)
        except MemoryReadError:
            raise MapIndexError('truncated map pointer or header') from None

    raw = checked(root.address, root.size)
    starts = [int.from_bytes(raw[i:i + 4], 'little') for i in range(0, len(raw), 4)]
    groups = {}
    for group, (start, end) in enumerate(zip(starts, starts[1:] + [root.address])):
        size = end - start
        if start >= root.address or size % 4 or not 1 <= size // 4 <= 256:
            raise MapIndexError('invalid group array bounds')
        pointers = checked(start, size)
        for offset in range(0, size, 4):
            header = int.from_bytes(pointers[offset:offset + 4], 'little')
            checked(header, profile.struct_size('MapHeader'))
        groups[f'Group_{group:03d}'] = [f'Map_G{group:03d}_N{n:03d}' for n in range(size // 4)]
    document = {
        'group_order': list(groups), 'groups': groups,
        'label_kind': 'numeric-id-v1', 'named_compatibility': False,
        'provenance': {
            'schema': 1, 'generator': 'map-index-v1', 'rom_profile': profile.profile_id,
            'rom_sha256': hashlib.sha256(image).hexdigest(), 'pret_commit': _PRET,
            'recipe': 'tools/mapjson/mapjson.cpp:436-456',
        },
    }
    MapGroups.model_validate(document)
    return document


def _external(path: Path) -> Path:
    if path.is_symlink():
        raise MapIndexError('final path must not be a symlink')
    resolved = path.resolve()
    if resolved.is_relative_to(_CHECKOUT) or '.git' in resolved.parts:
        raise MapIndexError('input and output must stay outside the checkout')
    return resolved


def build_map_index(rom: Path, out_dir: Path) -> Path:
    """Write a new numeric artifact without replacing any existing file.

    This is for user-controlled local paths, not hostile same-UID ancestor races.
    """
    if not out_dir.is_absolute():
        raise MapIndexError('output directory must be absolute')
    destination = _external(out_dir)
    source = _external(rom)
    if not destination.is_dir() or any(destination.iterdir()):
        raise MapIndexError('output directory must exist and be empty')
    if not source.is_file():
        raise MapIndexError('input must be a regular file')
    profiles = load_profiles(reference_path('rom_profiles.yaml'))
    profile = profiles.get(_PROFILE)
    # Bound before reading, then identify and parse this same buffer. No path re-open.
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 0x2000000:
            raise MapIndexError('input must be a regular bounded file')
        if info.st_size != profile.rom.size:
            raise MapIndexError('unsupported ROM size')
        image = handle.read(info.st_size + 1)
    try:
        verified = require_supported(identify_rom_image(image, profiles), profiles)
    except UnsupportedRomError:
        raise MapIndexError('unsupported ROM identity') from None
    if verified.profile_id != _PROFILE or verified.pret_commit != _PRET:
        raise MapIndexError('unsupported extraction profile')
    document = extract_map_index(image, verified)
    encoded = json.dumps(document, sort_keys=True, separators=(',', ':')) + '\n'
    MapGroups.model_validate(json.loads(encoded))
    target = destination / _FILENAME
    temporary = None
    published = False
    warning = False
    try:
        fd, name = tempfile.mkstemp(prefix='.map-index-', suffix='.tmp', dir=destination)
        temporary = Path(name)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(encoded.encode('utf-8'))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)  # Atomic no-clobber publication, including a target race.
        published = True
        try:
            directory_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            warning = True
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                if not published:
                    raise
                warning = True
    if warning:
        print('warning: numeric map index published; durability or temporary cleanup uncertain.',
              file=sys.stderr)
    return target
