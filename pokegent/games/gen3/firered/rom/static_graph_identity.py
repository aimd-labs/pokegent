"""Derive the portable identity of one static-graph build input.

The eleven keys are exactly the merged portable identity v1.  Every value is
computed from explicit, already-existing inputs: the actual ROM bytes, the
resolved FireRed profile, the shipped-source fingerprint, the captured map
index bytes and (optionally) captured puzzle bytes.  A path or mtime is never a
semantic input, and this helper never builds, creates or refreshes a cache.

``_generator_fingerprint`` is memoized for the process by its owner, so replacing
shipped Python sources takes effect on process restart; callers must not treat a
long-running process as a hot-reload boundary.

The effective map index and puzzle table are compared against the captured
bytes.  A same-size renamed group, a stale ``map_names`` load or a stale
``map_boulders`` LRU table therefore refuses instead of producing an identity
that cannot be admitted later.  ``provider`` must be the standard FireRed
provider; a custom provider is refused rather than fingerprinting its own
behaviour.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import fields, is_dataclass
from typing import Any, Mapping

from pokegent.games.gen3.firered.maps import map_boulders
from pokegent.games.gen3.firered.maps.map_names import loaded_groups
from pokegent.games.gen3.firered.maps.map_provider import FireRedMapProvider
from pokegent.games.gen3.firered.rom import static_artifact_loader
from pokegent.games.gen3.firered.rom.rom_cache import _generator_fingerprint
from pokegent.games.gen3.firered.rom.rom_data import (
    UnsupportedRomError,
    identify_rom_image,
    require_supported,
)
from pokegent.local_data import MapGroups

INDEX_MAX_BYTES = 4 * 1024 * 1024
PUZZLE_MAX_BYTES = 16 * 1024 * 1024
SCHEMA_VERSION = 1

_PUZZLE_TYPES = (
    map_boulders.PuzzleData,
    map_boulders.BoulderTemplate,
    map_boulders.SwitchData,
    map_boulders.OpenedTile,
    map_boulders.HoleData,
)


class StaticIdentityError(ValueError):
    """A supplied build input cannot produce a trustworthy portable identity."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_sha256(value: Any) -> str:
    return _sha256(static_artifact_loader._canonical(value))


def _read_exact(path: os.PathLike[str] | str, limit: int, label: str) -> bytes:
    """Read one bounded regular file once; re-open and path races are refused."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise StaticIdentityError(f"{label} cannot be opened safely") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise StaticIdentityError(f"{label} is not a regular file")
        if info.st_size > limit:
            raise StaticIdentityError(f"{label} exceeds its byte bound")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > limit:
            raise StaticIdentityError(f"{label} exceeds its byte bound")
        return data
    except OSError as exc:
        raise StaticIdentityError(f"{label} read failed") from exc
    finally:
        os.close(fd)


def _captured_index(path: os.PathLike[str] | str) -> tuple[bytes, str, tuple[str, ...],
                                                           tuple[tuple[str, ...], ...]]:
    raw = _read_exact(path, INDEX_MAX_BYTES, "map index")
    try:
        document = static_artifact_loader._parse_json(raw, "map index")
        parsed = MapGroups.model_validate(document)
    except Exception as exc:
        raise StaticIdentityError("map index is not a valid bounded map grouping") from exc
    order = tuple(parsed.group_order)
    groups = tuple(tuple(parsed.groups[name]) for name in order)
    return raw, _sha256(raw), order, groups


def _bind_grouping(
    index_path: os.PathLike[str] | str, provider: Any
) -> tuple[str, list[str], list[int], str]:
    """Return raw index digest, order, sizes and grouping digest."""
    _, index_sha256, captured_order, captured_groups = _captured_index(index_path)
    effective_order, effective_groups = loaded_groups()
    if captured_order != effective_order or captured_groups != effective_groups:
        raise StaticIdentityError("map index order or member names differ from the loaded groups")
    sizes = tuple(provider.group_sizes())
    if type(sizes) is not tuple or any(type(size) is not int or size < 0 for size in sizes):
        raise StaticIdentityError("provider group sizes are not exact non-negative ints")
    if sizes != tuple(len(names) for names in captured_groups):
        raise StaticIdentityError("provider group sizes differ from the loaded groups")
    grouping = [list(captured_order), [list(names) for names in captured_groups]]
    grouping_sha256 = _canonical_sha256(grouping)
    return (
        index_sha256,
        list(captured_order),
        [len(names) for names in captured_groups],
        grouping_sha256,
    )


def _puzzle_value(value: Any) -> Any:
    """Type-preserving projection of owner puzzle dataclasses and tuple-keyed maps."""
    if isinstance(value, bool):
        return ["bool", value]
    if type(value) is int:
        return ["int", value]
    if type(value) is str:
        return ["str", value]
    if value is None:
        return ["none"]
    if isinstance(value, tuple):
        return ["tuple", [_puzzle_value(item) for item in value]]
    if isinstance(value, list):
        return ["list", [_puzzle_value(item) for item in value]]
    if isinstance(value, Mapping):
        pairs = [[_puzzle_value(key), _puzzle_value(item)] for key, item in value.items()]
        pairs.sort(key=lambda pair: static_artifact_loader._canonical(pair[0]))
        return ["map", pairs]
    if type(value) in _PUZZLE_TYPES and is_dataclass(value):
        return [
            "dataclass",
            type(value).__name__,
            [[field.name, _puzzle_value(getattr(value, field.name))] for field in fields(value)],
        ]
    raise StaticIdentityError(f"unsupported puzzle table value {type(value).__name__}")


def _puzzle_digest(table: Any) -> str:
    return _canonical_sha256(_puzzle_value(dict(table)))


def _bind_puzzles(provider: Any, puzzle_path: os.PathLike[str] | str | None) -> str | None:
    """Hash explicit puzzle bytes and prove the effective provider table matches.

    ``None`` deliberately binds an absent puzzle artifact as ``null``.  It is
    not proof that an effective default table is empty; a future graph adapter
    must bind an empty table from the same explicit inputs and must never fall
    back to ``default_puzzles_path`` or ``ensure``.  This helper probes neither.
    """
    if puzzle_path is None:
        return None
    raw = _read_exact(puzzle_path, PUZZLE_MAX_BYTES, "puzzle table")
    try:
        fresh = map_boulders.parse_puzzle_bytes(raw, source=str(puzzle_path))
    except (ValueError, TypeError) as exc:
        raise StaticIdentityError("puzzle table failed owner parsing") from exc
    try:
        effective = provider.puzzles(path=puzzle_path, allow_empty=True)
    except Exception as exc:
        raise StaticIdentityError("effective puzzle table cannot be read") from exc
    if not isinstance(effective, Mapping):
        raise StaticIdentityError("effective puzzle table is not a mapping")
    if _puzzle_digest(fresh) != _puzzle_digest(effective):
        raise StaticIdentityError("effective puzzle table differs from the captured bytes")
    return _sha256(raw)


def _pin_rom(rom: bytes, profile: Any) -> tuple[str, str]:
    if type(rom) is not bytes or not rom:
        raise StaticIdentityError("ROM image must be non-empty exact bytes")
    rom_sha1 = hashlib.sha1(rom).hexdigest()
    rom_sha256 = _sha256(rom)
    try:
        verified = require_supported(identify_rom_image(rom))
    except UnsupportedRomError as exc:
        raise StaticIdentityError("ROM image is not a supported FireRed release") from exc
    if verified.profile_id != getattr(profile, "profile_id", None):
        raise StaticIdentityError("profile does not match the identified ROM")
    verified_rom = getattr(verified, "rom", None)
    if (
        verified_rom is None
        or verified_rom.sha1 != rom_sha1
        or (verified_rom.sha256 is not None and verified_rom.sha256 != rom_sha256)
    ):
        raise StaticIdentityError("identified profile does not match the actual ROM bytes")
    declared = getattr(profile, "rom", None)
    if (
        declared is None
        or declared.sha1 != rom_sha1
        or (declared.sha256 is not None and declared.sha256 != rom_sha256)
    ):
        raise StaticIdentityError("profile ROM hashes do not match the actual bytes")
    return rom_sha1, rom_sha256


def derive_identity(
    rom: bytes,
    profile: Any,
    provider: Any,
    *,
    index_path: os.PathLike[str] | str,
    puzzle_path: os.PathLike[str] | str | None,
) -> tuple[dict[str, Any], str]:
    """Return ``(portable_identity, input_digest)`` for explicit existing inputs.

    The provider is used only to compare the already-loaded effective grouping
    and puzzle table with the captured bytes.  This helper never creates a
    directory, cache or graph and never selects a path from a receipt.
    """
    if not isinstance(provider, FireRedMapProvider):
        raise StaticIdentityError("identity requires the standard FireRed map provider")
    if index_path is None:
        raise StaticIdentityError("an explicit existing map index path is required")
    rom_sha1, rom_sha256 = _pin_rom(rom, profile)
    try:
        profile_json = profile.model_dump(mode="json")
        profile_sha256 = _canonical_sha256(profile_json)
    except Exception as exc:
        raise StaticIdentityError("profile cannot be canonicalized") from exc
    map_index_sha256, order, sizes, grouping_sha256 = _bind_grouping(index_path, provider)
    puzzle_sha256 = _bind_puzzles(provider, puzzle_path)
    generator_sha256 = _generator_fingerprint()
    portable = {
        "schema_version": SCHEMA_VERSION,
        "rom_sha1": rom_sha1,
        "rom_sha256": rom_sha256,
        "profile_sha256": profile_sha256,
        "profile_id": profile.profile_id,
        "generator_sha256": generator_sha256,
        "map_index_sha256": map_index_sha256,
        "group_order": order,
        "group_sizes": sizes,
        "grouping_sha256": grouping_sha256,
        "puzzle_sha256": puzzle_sha256,
    }
    return portable, _canonical_sha256(portable)


__all__ = ["StaticIdentityError", "derive_identity"]
