"""Per-ROM cache for data decoded from a user's FireRed image.

The cache is keyed by the complete ROM SHA-1.  It is intentionally outside the
checkout so no ROM dump (or a derivative of one) is part of a release.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from pokegent.games.gen3.firered.rom.rom_data import ROM_PATH_ENV, require_supported

CACHE_ENV = "POKEGENT_ROM_DATA_CACHE"
CACHE_FORMAT_VERSION = 3
CACHE_FILES = (
    "regions.json",
    "regions.PROVENANCE.txt",
    "boulders.yaml",
    "gen3_tables.json",
    "runtime_tables.json",
    "wild_encounters.json",
)

_FIXTURE_CONSTANTS = {
    "FLAG_KINGS_ROCK_AFFECTED": 32,
    "FLAG_MAGIC_COAT_AFFECTED": 4,
    "FLAG_MAKES_CONTACT": 1,
    "FLAG_MIRROR_MOVE_AFFECTED": 16,
    "FLAG_PROTECT_AFFECTED": 2,
    "FLAG_SNATCH_AFFECTED": 8,
    "MOVE_TARGET_BOTH": 8,
    "MOVE_TARGET_DEPENDS": 1,
    "MOVE_TARGET_FOES_AND_ALLY": 32,
    "MOVE_TARGET_OPPONENTS_FIELD": 64,
    "MOVE_TARGET_RANDOM": 4,
    "MOVE_TARGET_SELECTED": 0,
    "MOVE_TARGET_USER": 16,
    "MOVE_TARGET_USER_OR_SELECTED": 2,
    "STATUS1_ANY": 255,
    "STATUS1_BURN": 16,
    "STATUS1_FREEZE": 32,
    "STATUS1_NONE": 0,
    "STATUS1_PARALYSIS": 64,
    "STATUS1_POISON": 8,
    "STATUS1_PSN_ANY": 136,
    "STATUS1_SLEEP": 7,
    "STATUS1_TOXIC_COUNTER": 3840,
    "STATUS1_TOXIC_POISON": 128,
    "TYPE_BUG": 6,
    "TYPE_DARK": 17,
    "TYPE_DRAGON": 16,
    "TYPE_ELECTRIC": 13,
    "TYPE_FIGHTING": 1,
    "TYPE_FIRE": 10,
    "TYPE_FLYING": 2,
    "TYPE_GHOST": 7,
    "TYPE_GRASS": 12,
    "TYPE_GROUND": 4,
    "TYPE_ICE": 15,
    "TYPE_MYSTERY": 9,
    "TYPE_NONE": 255,
    "TYPE_NORMAL": 0,
    "TYPE_POISON": 3,
    "TYPE_PSYCHIC": 14,
    "TYPE_ROCK": 5,
    "TYPE_STEEL": 8,
    "TYPE_WATER": 11,
}


class RomDataError(RuntimeError):
    """The requested ROM-derived cache cannot be loaded or built."""


def _progress(message: str) -> None:
    """Report cold-cache progress without contaminating a path written to stdout."""
    print(message, file=sys.stderr, flush=True)


def cache_root() -> Path:
    configured = os.environ.get(CACHE_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser() / "pokegent"


_ROM_SHA1_CACHE: dict[tuple[str, int, int, int], str] = {}
_CACHE_PATH_VALIDATION: dict[tuple[str, str, int, int, str | None], bool] = {}


def _map_index_digest() -> str | None:
    from pokegent.local_data import local_path

    path = local_path('pret_map_groups.json')
    return hashlib.sha256(path.read_bytes()).hexdigest() if path is not None else None


def _rom_sha1(path: str) -> str:
    digest = hashlib.sha1(Path(path).read_bytes()).hexdigest()
    return digest


def rom_cache_dir(rom: str | os.PathLike[str]) -> Path:
    path = Path(rom).expanduser().resolve()
    stat = path.stat()
    key = (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns)
    digest = _ROM_SHA1_CACHE.get(key)
    if digest is None:
        digest = _rom_sha1(str(path))
        _ROM_SHA1_CACHE[key] = digest
    return cache_root() / digest


def existing_cache_path(rom: str | os.PathLike[str] | None = None) -> Path | None:
    selected = rom or os.environ.get(ROM_PATH_ENV)
    if not selected:
        return None
    try:
        target = rom_cache_dir(selected)
    except OSError:
        return None
    sha1 = target.name
    if not target.is_dir():
        return None
    try:
        manifest = target / "manifest.json"
        stat = manifest.stat()
    except OSError:
        return None
    try:
        index_digest = _map_index_digest()
    except OSError:
        return None
    key = (str(target), sha1, stat.st_mtime_ns, stat.st_size, index_digest)
    valid = _CACHE_PATH_VALIDATION.get(key)
    if valid is None:
        valid = _valid_cache(target, sha1)
        _CACHE_PATH_VALIDATION[key] = valid
    return target if valid else None


def _artifact_digests(directory: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in CACHE_FILES
    }


@lru_cache(maxsize=1)
def _generator_fingerprint() -> str:
    """Hash shipped Python sources so decoder changes cannot reuse an old cache."""
    # Preserve the pre-move fingerprint scope: every shipped pokegent source file,
    # including non-Gen-3 modules, invalidates ROM-derived data.
    package_root = Path(__file__).resolve().parents[4]
    digest = hashlib.sha256()
    for source in sorted(package_root.rglob("*.py")):
        relative = source.relative_to(package_root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        content = source.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _valid_cache(directory: Path, sha1: str) -> bool:
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("sha1") != sha1:
            return False
        if manifest.get("format_version") != CACHE_FORMAT_VERSION:
            return False
        if manifest.get("generator_sha256") != _generator_fingerprint():
            return False
        if ('map_index_sha256' not in manifest
                or manifest['map_index_sha256'] != _map_index_digest()):
            return False
        if manifest.get("artifacts") != list(CACHE_FILES):
            return False
        recorded = manifest.get("sha256")
        if not isinstance(recorded, dict) or set(recorded) != set(CACHE_FILES):
            return False
        return recorded == _artifact_digests(directory)
    except (OSError, ValueError, TypeError):
        return False


def _validated_image(rom: str | os.PathLike[str]) -> tuple[Path, bytes, object, str]:
    rom_path = Path(rom).expanduser().resolve()
    if not rom_path.is_file():
        raise RomDataError(f"ROM file is missing: {rom_path}")
    image = rom_path.read_bytes()
    from pokegent.games.gen3.firered.rom.rom_data import UnsupportedRomError, identify_rom_image

    identification = identify_rom_image(image)
    try:
        profile = require_supported(identification)
    except UnsupportedRomError as exc:
        raise RomDataError(
            "unsupported FireRed ROM: checksum and header must match a known supported revision; "
            f"{exc}"
        ) from exc
    return rom_path, image, profile, identification.sha1


@contextmanager
def _cache_lock(root: Path, sha1: str):
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / f"{sha1}.lock"
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _recover_stale(target: Path, sha1: str) -> bool:
    """Remove stale build debris and restore a valid backup when publication was interrupted."""
    parent = target.parent
    temporary = sorted(parent.glob(f".{sha1}.tmp-*"))
    for path in temporary:
        shutil.rmtree(path, ignore_errors=True)
    backups = sorted(parent.glob(f".{sha1}.old-*"))
    if not target.exists():
        for backup in reversed(backups):
            if _valid_cache(backup, sha1):
                os.replace(backup, target)
                backups.remove(backup)
                break
    for backup in backups:
        shutil.rmtree(backup, ignore_errors=True)
    return _valid_cache(target, sha1)


def _game_tables_json(image: bytes, profile) -> dict:
    from pokegent.games.gen3.firered.rom.rom_tables import GameTables, RomImagePort

    tables = GameTables.from_port(RomImagePort(image), profile)

    def clean_name(name: str) -> str:
        return name.replace("’", "'")

    moves = {
        str(m.move_id): {
            "accuracy": m.accuracy,
            "effect": m.effect,
            "flags": m.flags,
            "name": "-$$$$$$" if m.move_id == 0 else clean_name(m.name),
            "power": m.power,
            "pp": m.pp,
            "priority": m.priority,
            "secondary": m.secondary_chance,
            "target": m.target,
            "type": m.type,
        }
        for m in tables.moves
    }
    species = {
        str(s.species_id): {
            "abilities": list(s.abilities),
            "atk": s.base_atk,
            "catch": s.catch_rate,
            "def": s.base_def,
            "hp": s.base_hp,
            "item_common": s.item_common,
            "item_rare": s.item_rare,
            "name": clean_name(s.name),
            "spa": s.base_spa,
            "spd": s.base_spd,
            "spe": s.base_spe,
            "types": list(s.types),
        }
        for s in tables.all_species
        if s.species_id not in {0, *range(252, 277)}
    }
    addresses = profile.rom_table("gItemEffectTable").address
    pointers = [
        int.from_bytes(
            image[addresses - 0x08000000 + i * 4 : addresses - 0x08000000 + i * 4 + 4],
            "little",
        )
        for i in range(163)
    ]
    next_pointer = {
        ptr: min((other for other in pointers if other > ptr), default=ptr + 10)
        for ptr in pointers
        if ptr
    }
    items = {}
    for item in tables.items:
        effect = list(item.effect.raw) if item.effect is not None else None
        if effect is not None:
            ptr = pointers[item.item_id - 13]
            start = ptr - 0x08000000
            effect = list(image[start : start + min(next_pointer[ptr] - ptr, 10)])
            if item.item_id == 142:
                effect = effect[:7]
            elif 63 <= item.item_id <= 71:
                if item.item_id in (69, 71):
                    effect = effect[:7] + [0, 0]
                    effect[6] = 0
                else:
                    effect = effect[:7] + [0, 0, 0]
            elif 73 <= item.item_id <= 79:
                effect = effect[:6] + [0, 0]
        items[str(item.item_id)] = {
            "battle_usage": item.battle_usage,
            "effect": effect,
            "hold_effect": item.hold_effect,
            "hold_param": item.hold_effect_param,
            "name": ("{POKEBLOCK} CASE" if item.item_id == 273 else clean_name(item.name)),
            "pocket": item.pocket,
            "price": item.price,
        }
    return {
        "ability_names": {str(i): n for i, n in enumerate(tables._ability_names)},
        "constants": _FIXTURE_CONSTANTS,
        "items": items,
        "moves": moves,
        "source": (
            "User-supplied ROM, decoded by pokegent.games.gen3.firered.rom.rom_cache"
        ),
        "species": species,
        "type_chart": list(tables.type_chart.raw),
    }


def _pret_macro_name(name: str) -> str:
    """Convert an allow-listed pret map label to its historic ``MAP_*`` spelling."""
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    words = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", words)
    words = re.sub(r"_(B?\d+)_F(?=_|$)", r"_\1F", words)
    return "MAP_" + words.upper()


def _wild_encounters_json(image: bytes, profile) -> list[dict]:
    """Decode the land rows consumed by the opt-in dex fallback.

    The previous pre-fork dump exposed other methods, but its loader only materialised land rows.
    Keeping that projection preserves gameplay while removing the checked-in extraction.
    """
    from pokegent.games.gen3.firered.maps.map_names import map_name

    base = profile.rom_table("gWildMonHeaders")
    at = base.address - 0x0800_0000
    weights = (20, 20, 10, 10, 10, 10, 5, 5, 4, 4, 1, 1)
    by_map: dict[tuple[int, int], list[dict]] = {}
    for _ in range(base.size // 20):
        group, number, _pad, land, _water, _rock, _fishing = struct.unpack_from(
            "<BBHIIII", image, at
        )
        at += 20
        if (group, number) == (0xFF, 0xFF):
            break
        label = map_name(group, number)
        if not land or label is None:
            continue
        info = land - 0x0800_0000
        slots_pointer = struct.unpack_from("<I", image, info + 4)[0]
        slots = slots_pointer - 0x0800_0000
        rows: list[dict] = []
        for index, probability in enumerate(weights):
            lo, hi, species = struct.unpack_from("<BBH", image, slots + index * 4)
            # The removed pre-fork loader resolved only its Kanto SPECIES_* list.  Preserve that
            # exact gameplay projection: post-game non-Kanto slots were never dex candidates.
            if not 1 <= species <= 151:
                continue
            rows.append(
                {
                    "map": _pret_macro_name(label).removeprefix("MAP_"),
                    "method": "land",
                    "species": species,
                    "probability": probability / 100.0,
                    "min_level": lo,
                    "max_level": hi,
                }
            )
        # FireRed's table contains version/event alternates for a few map ids. The pre-fork JSON
        # object and the runtime world decoder both keep the final header for each map.
        by_map[(group, number)] = rows
    ordered = sorted((rows for rows in by_map.values() if rows), key=lambda rows: rows[0]["map"])
    return [row for rows in ordered for row in rows]


def _build_unlocked(
    rom_path: Path,
    image: bytes,
    profile,
    sha1: str,
    target: Path,
) -> Path:
    """Build into a sibling temporary directory and publish it atomically."""
    total_started = time.perf_counter()
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    was_missing = not target.exists()
    if _recover_stale(target, sha1) and was_missing:
        return target
    temporary = Path(tempfile.mkdtemp(prefix=f".{sha1}.tmp-", dir=parent))

    try:
        # These scanners consume the verified profile and the user's bytes.
        from pokegent.games.gen3.firered.maps.boulder_scan import render, scan
        from pokegent.games.gen3.firered.rom.regiongen import generate

        started = time.perf_counter()
        (temporary / "boulders.yaml").write_text(render(scan(image)), encoding="utf-8")
        _progress(f"ROM data: boulders.yaml {time.perf_counter() - started:.2f}s")
        started = time.perf_counter()
        regions = generate(image, puzzles_path=temporary / "boulders.yaml")
        (temporary / "regions.json").write_text(
            json.dumps(regions, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (temporary / "regions.PROVENANCE.txt").write_text(
            f"generated_at_source: {profile.profile_id}\n"
            f"rom_sha256: {hashlib.sha256(image).hexdigest()}\n"
            "producer: pokegent.games.gen3.firered.rom.rom_cache\n",
            encoding="utf-8",
        )
        _progress(f"ROM data: regions.json + provenance {time.perf_counter() - started:.2f}s")

        started = time.perf_counter()
        (temporary / "gen3_tables.json").write_text(
            json.dumps(_game_tables_json(image, profile), separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        from pokegent.games.gen3.firered.rom.rom_tables import GameTables, RomImagePort

        runtime_tables = GameTables.from_port(RomImagePort(image), profile)
        (temporary / "runtime_tables.json").write_text(
            json.dumps(runtime_tables.runtime_document(), separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        (temporary / "wild_encounters.json").write_text(
            json.dumps(
                _wild_encounters_json(image, profile), separators=(",", ":"), sort_keys=True
            ),
            encoding="utf-8",
        )
        _progress(f"ROM data: tables + wild encounters {time.perf_counter() - started:.2f}s")

        started = time.perf_counter()
        for name in CACHE_FILES:
            with (temporary / name).open("rb") as handle:
                os.fsync(handle.fileno())
        manifest = {
            "format_version": CACHE_FORMAT_VERSION,
            "generator_sha256": _generator_fingerprint(),
            "map_index_sha256": _map_index_digest(),
            "sha1": sha1,
            "profile": profile.profile_id,
            "artifacts": list(CACHE_FILES),
            "sha256": _artifact_digests(temporary),
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        with (temporary / "manifest.json").open("rb") as handle:
            os.fsync(handle.fileno())
        _progress(f"ROM data: bootstrap + manifest {time.perf_counter() - started:.2f}s")
        directory_fd = os.open(temporary, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        backup = None
        if target.exists():
            backup = parent / f".{target.name}.old-{os.getpid()}"
            os.replace(target, backup)
        os.replace(temporary, target)
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
        _progress(f"ROM data: total {time.perf_counter() - total_started:.2f}s")
        return target
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def build(
    rom: str | os.PathLike[str] | None,
    destination: str | os.PathLike[str] | None = None,
) -> Path:
    """Build all runtime ROM data for *rom* and return its cache directory."""
    selected = rom or os.environ.get(ROM_PATH_ENV)
    if not selected:
        raise RomDataError(f"ROM data cache is missing and {ROM_PATH_ENV} is not set")
    rom_path, image, profile, sha1 = _validated_image(selected)
    target = Path(destination).expanduser() if destination else cache_root() / sha1
    with _cache_lock(target.parent, sha1):
        return _build_unlocked(rom_path, image, profile, sha1, target)


def ensure(rom: str | os.PathLike[str] | None = None) -> Path:
    """Return an existing cache or build it, with an actionable failure."""
    selected = rom or os.environ.get(ROM_PATH_ENV)
    if not selected:
        raise RomDataError(f"ROM data cache is missing and {ROM_PATH_ENV} is not set")
    rom_path, image, profile, sha1 = _validated_image(selected)
    target = cache_root() / sha1
    with _cache_lock(target.parent, sha1):
        _recover_stale(target, sha1)
        if _valid_cache(target, sha1):
            return target
        return _build_unlocked(rom_path, image, profile, sha1, target)
