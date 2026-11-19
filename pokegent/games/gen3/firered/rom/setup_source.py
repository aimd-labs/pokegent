"""Bounded, non-executable input for private first-start generation.

Tree digest v1: domain + uint64be file count, then UTF-8-byte-sorted records
(uint32be path length, relative POSIX path, raw SHA-256 content digest).
Directory metadata and root .git are excluded; all other regular bytes count.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

PRET_COMMIT = '037335f4c725d7c9aecdac87066f2002b4bd7e14'
TREE_SHA256 = 'df2f96d640e48ccf62db35d14519f4efd798773f0e9352965b168a845bea526e'
MAX_COMPRESSED = 64 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
MAX_MEMBERS = 20_000
MAX_TEXT = 2 * 1024 * 1024


class SetupError(ValueError):
    """A bounded input or setup operation failed; never includes game content."""


def tree_digest(records: dict[str, bytes]) -> str:
    digest = hashlib.sha256(b'pokegent-decomp-tree-v1\0' + struct.pack('>Q', len(records)))
    for path, content_hash in sorted(records.items(), key=lambda row: row[0].encode('utf-8')):
        encoded = path.encode('utf-8')
        digest.update(struct.pack('>I', len(encoded)))
        digest.update(encoded)
        digest.update(content_hash)
    return digest.hexdigest()


def _canonical(path: str) -> str:
    parts = PurePosixPath(path).parts
    if (not path or path != '/'.join(parts) or path.startswith('/')
            or any(p in {'.', '..'} for p in parts) or '\\' in path or '\0' in path
            or len(path.encode('utf-8')) > 1024):
        raise SetupError('noncanonical source member')
    return path


def _needed(path: str) -> bool:
    return (
        path.startswith('data/maps/') and path.endswith(('/map.json', '/scripts.inc', '/text.inc'))
        or path.startswith(('data/layouts/', 'data/tilesets/')) and path.endswith('.bin')
        or path in {
            'data/maps/map_groups.json', 'data/text/surf.inc',
            'data/scripts/field_moves.inc', 'data/scripts/surf.inc',
            'data/scripts/pokemon_mansion.inc', 'src/region_map.c',
            'src/script_menu.c', 'src/field_specials.c', 'src/strings.c',
            'src/field_control_avatar.c', 'src/field_player_avatar.c',
            'src/data/heal_locations.json', 'src/data/region_map/region_map_sections.json',
            'include/constants/flags.h', 'include/constants/trainers.h',
            'include/constants/opponents.h',
            'include/constants/menu.h', 'include/constants/moves.h',
            'include/constants/metatile_behaviors.h',
            'data/layouts/layouts.json', 'src/data/tilesets/headers.h',
            'src/data/tilesets/metatiles.h',
        }
    )


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SetupError('duplicate source JSON key')
        result[key] = value
    return result


@dataclass(frozen=True)
class Source:
    files: dict[str, bytes]
    digest: str

    def text(self, path: str) -> str:
        try:
            return self.files[path].decode('utf-8')
        except (KeyError, UnicodeError):
            raise SetupError('required source text unavailable') from None

    def json(self, path: str):
        try:
            return json.loads(self.text(path), object_pairs_hook=_unique_pairs)
        except (ValueError, TypeError):
            raise SetupError('invalid source JSON') from None


class _ExpandedReader:
    def __init__(self, handle):
        self.handle, self.count = handle, 0

    def read(self, size):
        if size < 0 or self.count + size > MAX_EXPANDED:
            raise SetupError('source decompression limit exceeded')
        content = self.handle.read(size)
        self.count += len(content)
        return content


def read_source(path: Path, expected_digest: str = TREE_SHA256,
                commit: str = PRET_COMMIT) -> Source:
    """Hash every regular file, retaining only bounded extractor inputs in memory."""
    records, files, seen = {}, {}, set()
    expanded = 0

    def member(name, size, handle):
        nonlocal expanded
        expanded += size
        if expanded > MAX_EXPANDED:
            raise SetupError('source expanded size limit exceeded')
        if _needed(name) and size > MAX_TEXT:
            raise SetupError('source text size limit exceeded')
        digest, retained, count = hashlib.sha256(), bytearray(), 0
        while chunk := handle.read(min(65536, size - count + 1)):
            count += len(chunk)
            if count > size:
                raise SetupError('source member size changed')
            digest.update(chunk)
            if _needed(name):
                retained.extend(chunk)
        if count != size:
            raise SetupError('truncated source member')
        records[name] = digest.digest()
        if _needed(name):
            files[name] = bytes(retained)

    try:
        if path.is_symlink():
            raise SetupError('source must not be a symlink')
        if path.is_dir():
            for directory, dirs, names in os.walk(path, followlinks=False):
                if Path(directory) == path:
                    dirs[:] = [d for d in dirs if d != '.git']
                    names = [n for n in names if n != '.git']
                for name in dirs + names:
                    item = Path(directory) / name
                    relative = _canonical(item.relative_to(path).as_posix())
                    seen.add(relative)
                    if len(seen) > MAX_MEMBERS:
                        raise SetupError('source member count limit exceeded')
                    info = item.lstat()
                    if stat.S_ISDIR(info.st_mode):
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        raise SetupError('source contains nonregular member')
                    with item.open('rb') as handle:
                        member(relative, info.st_size, handle)
        else:
            if not path.is_file() or path.stat().st_size > MAX_COMPRESSED:
                raise SetupError('source archive size limit exceeded')
            root = f'pokefirered-{commit}'
            with gzip.open(path, 'rb') as compressed, tarfile.open(
                    fileobj=_ExpandedReader(compressed), mode='r|') as archive:
                for item in archive:
                    name = _canonical(item.name.rstrip('/') if item.isdir() else item.name)
                    if name in seen or len(seen) >= MAX_MEMBERS:
                        raise SetupError('duplicate source member or count limit exceeded')
                    seen.add(name)
                    if not (item.isdir() or item.isfile()):
                        raise SetupError('source contains nonregular member')
                    if name == root and item.isdir():
                        continue
                    if not name.startswith(root + '/'):
                        raise SetupError('unexpected source archive root')
                    relative = name[len(root) + 1:]
                    if relative == '.git' or relative.startswith('.git/'):
                        continue
                    if item.isfile():
                        member(relative, item.size, archive.extractfile(item))
        digest = tree_digest(records)
        if digest != expected_digest:
            raise SetupError('source content differs from pinned tree; use a clean checkout')
        return Source(files, digest)
    except (OSError, tarfile.TarError, EOFError, UnicodeError):
        raise SetupError('cannot read bounded source tree or gzip tarball') from None


class _FixedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        from urllib.parse import urlsplit

        parsed = urlsplit(newurl)
        if parsed.scheme != 'https' or parsed.netloc != 'codeload.github.com':
            raise SetupError('source redirect outside fixed HTTPS host')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def cached_source(root: Path) -> Source:
    """Fetch only the fixed public archive; no hooks, extraction or source execution."""
    target = root / 'sources' / f'pokefirered-{PRET_COMMIT}.tar.gz'
    if target.exists():
        return read_source(target)
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.source-', dir=target.parent)
    os.close(fd)
    try:
        # A short-lived child bounds DNS, redirects and reads together, even if
        # the platform resolver ignores socket timeouts. run() kills and reaps it.
        subprocess.run([sys.executable, '-m', __name__, '--download', temporary],
                       timeout=60, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        source = read_source(Path(temporary))
        os.replace(temporary, target)
        return source
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        raise SetupError('fixed public source download failed; use --decomp offline') from None
    finally:
        Path(temporary).unlink(missing_ok=True)


def _download(path: Path) -> None:
    started, total = time.monotonic(), 0
    opener = urllib.request.build_opener(_FixedRedirect())
    url = f'https://codeload.github.com/pret/pokefirered/tar.gz/{PRET_COMMIT}'
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    with os.fdopen(fd, 'wb') as output, opener.open(url, timeout=10) as response:
        while chunk := response.read(65536):
            total += len(chunk)
            if total > MAX_COMPRESSED or time.monotonic() - started > 60:
                raise SetupError('source download exceeded size or time budget')
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--download':
        raise SystemExit(2)
    _download(Path(sys.argv[2]))
