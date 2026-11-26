"""Offline read-only wheel byte manifest; never builds, extracts or verifies provenance."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import platform
import re
import sys
import zipfile
import zlib
from email import message_from_string
from pathlib import Path

MAX_WHEEL_BYTES, MAX_MEMBER_BYTES, MAX_TOTAL_BYTES = 64 << 20, 16 << 20, 128 << 20
MAX_MEMBERS, MAX_METADATA_BYTES, CHUNK = 10_000, 1 << 20, 65536
COMMIT_RE, DRIVE_RE = re.compile(r"^[0-9a-f]{40}$"), re.compile(r"^[A-Za-z]:")
READ_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError)


class ManifestError(Exception):
    """A rejected wheel or argument; the message never includes member bytes."""


def _fail(path: Path, detail: str) -> None:
    raise ManifestError(f"{path}: {detail}")


def _safe_member_name(name: str) -> bool:
    if not name or name.startswith("/") or "\\" in name or "\x00" in name or DRIVE_RE.match(name):
        return False
    body = name[:-1] if name.endswith("/") else name
    return all(part not in ("", ".", "..") for part in body.split("/"))


def _checked_members(path: Path, archive: zipfile.ZipFile, limit: int) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > limit:
        _fail(path, f"member count exceeds {limit} limit")
    if len({info.filename for info in members}) != len(members):
        _fail(path, "duplicate member name")
    for info in members:
        if not _safe_member_name(info.filename):
            _fail(path, f"unsafe member name {info.filename[:64]!r}")
    return members


def _metadata(path, archive, members, limit) -> dict[str, str | None]:
    directories = {info.filename.rsplit("/", 1)[0] for info in members
                   if info.filename.endswith(".dist-info/METADATA")}
    if len(directories) != 1 or "/" in next(iter(directories), ""):
        _fail(path, "missing or ambiguous .dist-info metadata")
    directory = directories.pop()

    def read(name: str):
        info = next((item for item in members
                     if item.filename == f"{directory}/{name}"), None)
        if info is None:
            _fail(path, f"missing .dist-info {name}")
        try:
            with archive.open(info) as member:
                raw = member.read(limit + 1)
            if len(raw) > limit:
                _fail(path, f"{name} exceeds {limit} byte limit")
            return message_from_string(raw.decode("utf-8"))
        except READ_ERRORS:
            _fail(path, f"cannot read {name}")
        except UnicodeDecodeError:
            _fail(path, f"{name} is not UTF-8")

    def field(message, name: str, required: bool = True) -> str | None:
        values = [value.strip() for value in message.get_all(name) or []]
        if len(values) > 1 or (values and not values[0]) or (required and not values):
            _fail(path, f"missing or ambiguous metadata {name}")
        return values[0] if values else None

    metadatas, wheel = read("METADATA"), read("WHEEL")
    return {"name": field(metadatas, "Name"), "version": field(metadatas, "Version"),
            "generator": field(wheel, "Generator", required=False)}


def _file_rows(path, archive, files, max_member, max_total) -> list[dict]:
    rows, total = [], 0
    for info in files:
        digest, size = hashlib.sha256(), 0
        try:
            with archive.open(info) as member:
                while chunk := member.read(CHUNK):
                    size += len(chunk)
                    if size > max_member or total + size > max_total:
                        _fail(path, f"member {info.filename[:64]!r} exceeds byte limit")
                    digest.update(chunk)
        except READ_ERRORS:
            _fail(path, f"cannot read member {info.filename[:64]!r}")
        total += size
        rows.append({"name": info.filename, "sha256": digest.hexdigest(), "size": size})
    return sorted(rows, key=lambda row: row["name"])


def build_manifest(path: str | Path, *, source_commit: str) -> dict:
    if not isinstance(source_commit, str) or not COMMIT_RE.fullmatch(source_commit):
        raise ManifestError("source commit must be 40 lowercase hex characters")
    wheel = Path(path)
    try:
        handle = open(wheel, "rb")
    except ValueError:
        _fail(wheel, "invalid wheel path")
    with handle:
        data = handle.read(MAX_WHEEL_BYTES + 1)
    if len(data) > MAX_WHEEL_BYTES:
        _fail(wheel, f"wheel exceeds {MAX_WHEEL_BYTES} byte limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError):
        _fail(wheel, "not a readable ZIP wheel")
    with archive:
        members = _checked_members(wheel, archive, MAX_MEMBERS)
        files = [info for info in members if not info.is_dir()]
        if (any(info.file_size > MAX_MEMBER_BYTES for info in files)
                or sum(info.file_size for info in files) > MAX_TOTAL_BYTES):
            _fail(wheel, "declared member or total exceeds byte limit")
        metadata = _metadata(wheel, archive, members, MAX_METADATA_BYTES)
        rows = _file_rows(wheel, archive, files, MAX_MEMBER_BYTES, MAX_TOTAL_BYTES)
    return {
        "tool": "wheel_manifest", "version": 1,
        "wheel": {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)},
        "files": rows, "dist_info": metadata,
        "supplied_source_commit": source_commit, "source_commit_verified": False,
        "manifest_environment": {"python": platform.python_version(),
                                 "zlib": zlib.ZLIB_VERSION,
                                 "build_environment_proof": False},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wheel", metavar="WHEEL")
    parser.add_argument("--source-commit", required=True, metavar="FORTY_HEX")
    args = parser.parse_args(argv)
    try:
        report = build_manifest(args.wheel, source_commit=args.source_commit)
    except (ManifestError, OSError) as exc:
        print(f"wheel_manifest: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
