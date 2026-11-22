"""Unused offline atomic publisher for caller-prepared static-graph documents.

This module publishes a JSON graph document that the caller has already
prepared.  It does not run graph analysis, invent a graph schema, validate
graph references/query dependencies, recompute counts, bind a map index or
derive identity from a receipt.  ``expected_identity`` must come independently
from the current trusted ROM/profile/index.  The runtime does not import this
module, and it never reads private game inputs.

Publication order is: bounded/canonical payload bytes to an exclusive mode-0600
temporary file, payload fsync, atomic rename to ``payload-<sha256>.json``,
directory fsync, sealed receipt to an exclusive temporary file, receipt fsync,
atomic replace of ``receipt.json`` LAST and directory fsync.  A receipt is
therefore never visible before its payload is durable.  An existing payload for
the same digest is reused only after the merged loader's trusted-file checks and
an exact byte comparison; a different, corrupt, non-regular or symlink payload
fails closed and is never overwritten or repaired.

Durability limits: success means the directory entries were fsynced where the
filesystem honours fsync for directories and rename.  A failure after the
receipt replace leaves the new artifact published but its final directory fsync
unconfirmed; that error says publication durability is uncertain and no
rollback is claimed.  Parent directories are caller-selected and trusted; only
the final artifact directory is created (mode 0700) and checked here.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any

from pokegent.games.gen3.firered.rom import static_artifact_loader as loader


class ArtifactWriteError(ValueError):
    """A payload, identity, directory or publication step was refused."""


_DIR_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_TEMP_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def _check_payload(value: Any, depth: int, seen: set[int], budget: list[int]) -> None:
    """Refuse anything outside the closed JSON algebra before serialization."""
    budget[0] += 1
    if budget[0] > loader.MAX_JSON_TOKENS:
        raise ArtifactWriteError("payload logical node bound exceeded")
    if depth > loader.MAX_JSON_DEPTH:
        raise ArtifactWriteError("payload nesting bound exceeded")
    kind = type(value)
    if kind is dict:
        if id(value) in seen:
            raise ArtifactWriteError("cyclic payload is unsupported")
        seen.add(id(value))
        for key, item in value.items():
            if type(key) is not str:
                raise ArtifactWriteError("payload object keys must be exact str")
            _check_payload(item, depth + 1, seen, budget)
        seen.remove(id(value))
    elif kind is list:
        if id(value) in seen:
            raise ArtifactWriteError("cyclic payload is unsupported")
        seen.add(id(value))
        for item in value:
            _check_payload(item, depth + 1, seen, budget)
        seen.remove(id(value))
    elif kind is str or kind is bool or kind is int or value is None:
        return
    else:
        raise ArtifactWriteError(
            f"unsupported payload value type {kind.__name__}; only JSON values are accepted"
        )


def _canonical_bytes(value: Any, label: str) -> bytes:
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return text.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ArtifactWriteError(f"{label} is not canonical JSON") from exc


def _payload_bytes(payload: Any) -> bytes:
    if type(payload) is not dict:
        raise ArtifactWriteError("payload top level must be an exact mapping")
    _check_payload(payload, 0, set(), [0])
    raw = _canonical_bytes(payload, "payload")
    if len(raw) > loader.PAYLOAD_MAX_BYTES:
        raise ArtifactWriteError("payload exceeds its byte bound")
    try:
        parsed = loader._parse_json(raw, "payload")
    except loader.ArtifactLoadError as exc:
        raise ArtifactWriteError(f"payload refused: {exc}") from None
    if type(parsed) is not dict or parsed != payload:
        raise ArtifactWriteError("payload failed canonical JSON round-trip validation")
    return raw


def _build_receipt(
    identity: dict[str, Any],
    digest: str,
    raw: bytes,
    counts: Any,
    built_utc: Any,
) -> dict[str, Any]:
    payload_sha = loader._sha256(raw)
    receipt = {
        "schema_version": 1,
        "input_identity": digest,
        "payload_name": f"payload-{payload_sha}.json",
        "payload_sha256": payload_sha,
        "payload_size": len(raw),
        "identity": identity,
        "counts": counts,
        "sealed": True,
        "built_utc": built_utc,
    }
    try:
        return loader._validate_receipt(receipt, identity, digest)
    except loader.ArtifactLoadError as exc:
        raise ArtifactWriteError(f"receipt refused by loader validation: {exc}") from None


def _open_checked_directory(directory: Path) -> int:
    try:
        fd = os.open(directory, _DIR_FLAGS)
    except OSError as exc:
        raise ArtifactWriteError("artifact directory cannot be opened safely") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise ArtifactWriteError("artifact path is not a directory")
        if info.st_uid != os.getuid():
            raise ArtifactWriteError("artifact directory is not owned by the current user")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ArtifactWriteError("artifact directory is group/world writable")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_artifact_directory(directory: Path) -> int:
    try:
        return _open_checked_directory(directory)
    except ArtifactWriteError as exc:
        if not isinstance(exc.__cause__, FileNotFoundError):
            raise
    parent = directory.parent if directory.parent != directory else Path(".")
    try:
        parent_fd = os.open(parent, _DIR_FLAGS)
    except OSError as exc:
        raise ArtifactWriteError("artifact parent directory cannot be opened safely") from exc
    try:
        try:
            os.mkdir(directory.name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ArtifactWriteError("artifact directory cannot be created") from exc
        else:
            os.chmod(directory.name, 0o700, dir_fd=parent_fd)
            try:
                os.fsync(parent_fd)
            except OSError as exc:
                raise ArtifactWriteError(
                    "new artifact directory could not be fsynced in its parent"
                ) from exc
    finally:
        os.close(parent_fd)
    return _open_checked_directory(directory)


def _write_temp(directory_fd: int, data: bytes, label: str) -> str:
    name = f".tmp-{label}-{secrets.token_hex(16)}"
    try:
        fd = os.open(name, _TEMP_FLAGS, 0o600, dir_fd=directory_fd)
    except OSError as exc:
        raise ArtifactWriteError(f"{label} temporary file cannot be created safely") from exc
    failed = True
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise ArtifactWriteError(f"{label} temporary file short write")
            view = view[written:]
        os.fsync(fd)
        failed = False
    except OSError as exc:
        raise ArtifactWriteError(f"{label} temporary file write failed") from exc
    finally:
        os.close(fd)
        if failed:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except OSError:
                pass
    return name


def _existing_payload_matches(directory_fd: int, name: str, data: bytes) -> bool:
    """Reuse a matching payload only after trusted checks on the fsynced inode.

    The loader's read helper closes its descriptor, so this opens once with the
    same no-follow/regular/owner/mode checks, compares bytes and fsyncs that
    verified inode.  The caller fsyncs the directory before publishing a receipt.
    """
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        payload_fd = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ArtifactWriteError("existing payload cannot be opened safely") from exc
    try:
        info = os.fstat(payload_fd)
        if not stat.S_ISREG(info.st_mode):
            raise ArtifactWriteError("existing payload is not a regular file")
        if info.st_uid != os.getuid():
            raise ArtifactWriteError("existing payload is not owned by the current user")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ArtifactWriteError("existing payload is group/world writable")
        if info.st_size != len(data) or info.st_size > loader.PAYLOAD_MAX_BYTES:
            raise ArtifactWriteError(
                "existing content-addressed payload differs; refusing to overwrite or repair it"
            )
        chunks: list[bytes] = []
        remaining = len(data) + 1
        while remaining > 0:
            chunk = os.read(payload_fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if b"".join(chunks) != data:
            raise ArtifactWriteError(
                "existing content-addressed payload differs; refusing to overwrite or repair it"
            )
        os.fsync(payload_fd)
        return True
    except OSError as exc:
        raise ArtifactWriteError("existing payload verification or fsync failed") from exc
    finally:
        os.close(payload_fd)


def build_static_artifact(
    directory: os.PathLike[str] | str,
    expected_identity: Any,
    payload: Any,
    *,
    counts: Any,
    built_utc: Any,
) -> Path:
    """Publish ``payload`` and return the receipt path.

    The caller supplies the independently trusted identity, counts and UTC.
    Only payload integrity is established; graph schema/reference/query and
    counts recomputation remain later work.  See the module docstring for the
    atomicity, durability and trusted-parent limits.
    """
    try:
        identity = loader._validate_identity(expected_identity)
        digest = loader._identity_digest(identity)
        loader._validate_counts(counts)
        loader._validate_utc(built_utc)
    except loader.ArtifactLoadError as exc:
        raise ArtifactWriteError(f"caller input refused: {exc}") from None

    path = Path(directory)
    if os.path.basename(os.path.normpath(os.fspath(path))) != digest:
        raise ArtifactWriteError("artifact directory basename is not the identity digest")

    raw = _payload_bytes(payload)
    receipt = _build_receipt(identity, digest, raw, counts, built_utc)
    receipt_raw = _canonical_bytes(receipt, "receipt")
    if len(receipt_raw) > loader.RECEIPT_MAX_BYTES:
        raise ArtifactWriteError("receipt exceeds its byte bound")

    directory_fd = _open_artifact_directory(path)
    temps: list[str] = []
    published = False
    try:
        payload_name = receipt["payload_name"]
        if not _existing_payload_matches(directory_fd, payload_name, raw):
            temp = _write_temp(directory_fd, raw, "payload")
            temps.append(temp)
            try:
                os.rename(temp, payload_name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            except OSError as exc:
                raise ArtifactWriteError("payload atomic rename failed") from exc
            temps.remove(temp)
        os.fsync(directory_fd)

        temp = _write_temp(directory_fd, receipt_raw, "receipt")
        temps.append(temp)
        try:
            os.replace(
                temp, "receipt.json", src_dir_fd=directory_fd, dst_dir_fd=directory_fd
            )
        except OSError as exc:
            raise ArtifactWriteError("receipt atomic replace failed") from exc
        temps.remove(temp)
        published = True
        os.fsync(directory_fd)
    except OSError as exc:
        if published:
            raise ArtifactWriteError(
                "receipt published but final directory fsync failed; "
                "publication durability is uncertain and no rollback is claimed"
            ) from exc
        raise ArtifactWriteError(f"artifact publication failed: {exc}") from exc
    finally:
        for temp in temps:
            try:
                os.unlink(temp, dir_fd=directory_fd)
            except OSError:
                pass
        os.close(directory_fd)
    return path / "receipt.json"
