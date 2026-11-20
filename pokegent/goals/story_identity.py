"""Fresh admission against selected input bytes; never builds or repairs local data."""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from pokegent import local_data
from pokegent.games.gen3.firered.rom import local_setup, rom_cache
from pokegent.games.gen3.firered.rom.rom_data import (
    ProfileError,
    UnsupportedRomError,
    identify_rom_image,
    load_profiles,
    require_supported,
)
from pokegent.games.gen3.firered.rom.setup_source import PRET_COMMIT, TREE_SHA256
from pokegent.goals.dex_fallback import FallbackUnavailable


def current_identity(rom: str | os.PathLike[str] | None):
    """Return identity and the very image/profile verified here, for offline generation too."""
    try:
        if rom is None:
            raise ValueError('missing ROM')
        selected = Path(rom).expanduser()
        with os.fdopen(os.open(selected, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 0x2000000:
                raise ValueError('invalid ROM')
            image = handle.read(info.st_size + 1)
        profiles = load_profiles()
        identification = identify_rom_image(image, profiles)
        profile = require_supported(identification, profiles)
        if profile.profile_id != 'firered-us-rev0' or profile.pret_commit != PRET_COMMIT:
            raise ValueError('unsupported profile')
        setup = Path(os.environ[local_data.LOCAL_DATA_ENV]).expanduser().resolve()
        if setup.is_relative_to(local_data._CHECKOUT):
            raise ValueError('checkout data')
        expected = dict(generator_version=local_setup.GENERATOR_VERSION, pret_commit=PRET_COMMIT,
                        tree_sha256=TREE_SHA256, rom_sha1=identification.sha1)
        local_setup._rerun(setup, expected)
        index = rom_cache._map_index_digest()
        if index != hashlib.sha256((setup / 'pret_map_groups.json').read_bytes()).hexdigest():
            raise ValueError('index mismatch')
        cache = rom_cache.cache_root() / identification.sha1
        if not rom_cache._valid_cache(cache, identification.sha1):
            raise ValueError('invalid cache')
        # Detect changes during admission, without claiming continuous filesystem authentication.
        local_setup._rerun(setup, expected)
        if index != rom_cache._map_index_digest():
            raise ValueError('index changed')
        after = selected.stat(follow_symlinks=False)
        if (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('ROM changed during admission')
        identity = dict(rom_sha256=hashlib.sha256(image).hexdigest(), source_sha256=TREE_SHA256,
                        source_pin=PRET_COMMIT, map_index_sha256=index)
        return identity, image, profile
    except (OSError, ValueError, TypeError, KeyError, RuntimeError,
            ProfileError, UnsupportedRomError):
        raise FallbackUnavailable('story identity unavailable') from None


def verify_identity(graph, rom):
    identity, _, _ = current_identity(rom)
    if any(getattr(graph.provenance, key) != value for key, value in identity.items()):
        raise FallbackUnavailable('story identity unavailable')
