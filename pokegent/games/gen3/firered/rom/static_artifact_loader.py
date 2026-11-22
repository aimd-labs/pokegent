"""Unused bounded loader for integrity-verified static-graph artifacts.
This is file-integrity plus typed-symbolic admission only.  It does not
materialize a NavigationGraph, validate payload graph schemas/references/query
dependencies, write artifacts or run in the runtime.  A returned payload is
*integrity verified only*; a later versioned graph schema must admit its facts.
Caller obligations: ``expected_identity`` must come independently from the
current trusted ROM/profile/index, never from the receipt; directory basename
and receipt ``input_identity`` must equal its canonical JSON SHA-256.
``grouping_sha256`` commits to group member names absent from the portable
identity, so this loader only format-checks and binds it through
``input_identity``; the caller must bind the installed map index separately.
Receipt is read first (16 KiB bound); the immutable content-addressed payload
opens only after receipt validation (16 MiB bound) and is hashed before parse.
Both documents get strict UTF-8, lexical nesting/token/number bounds before
``json.loads``; duplicate keys, floats, NaN and Infinity refuse.  Receipt and
payload symlinks, non-regular/other-owned/group-world-writable files refuse.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime
from typing import Any

from pokegent.games.gen3.firered.rom.script_effects import Interpretation
from pokegent.games.gen3.firered.rom.static_symbolic_codec import (
    CodecError,
    decode_interpretation,
)

RECEIPT_MAX_BYTES = 16 * 1024
PAYLOAD_MAX_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 128
MAX_JSON_TOKENS = 500_000
MAX_NUMBER_TOKEN = 128
MAX_MAPS = 1_024
MAX_COUNT_KEYS = 32
MAX_COUNT_VALUE = 4_000_000
MAX_INTERPRETATIONS = 1_000_000

# Guard.operator consumers: script_scan.CONDITIONS and script_effects
# (lt/eq/gt/le/ge/ne), navigation.graph dispatch at lines 948-954 and
# story_generation's eq/ne checks.  The current producer emits exactly these.
ALLOWED_OPERATORS = frozenset({"lt", "eq", "gt", "le", "ge", "ne"})

_IDENTITY_KEYS = frozenset(
    {
        "schema_version",
        "rom_sha1",
        "rom_sha256",
        "profile_sha256",
        "profile_id",
        "generator_sha256",
        "map_index_sha256",
        "group_order",
        "group_sizes",
        "grouping_sha256",
        "puzzle_sha256",
    }
)
_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "input_identity",
        "payload_name",
        "payload_sha256",
        "payload_size",
        "identity",
        "counts",
        "sealed",
        "built_utc",
    }
)
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_PAYLOAD_NAME = re.compile(r"payload-[0-9a-f]{64}\.json\Z")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")


class ArtifactLoadError(ValueError):
    """An artifact receipt, identity, file or document failed admission."""


def _canonical(value: Any) -> bytes:
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ArtifactLoadError("identity is not canonical JSON") from exc
    return text.encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _exact_int(value: Any, *, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ArtifactLoadError(f"{label} must be an exact int in {minimum}..{maximum}")
    return value


def _hash(
    value: Any, pattern: re.Pattern[str], label: str, *, optional: bool = False
) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ArtifactLoadError(f"{label} must be lowercase hexadecimal text")
    return value


def _text(value: Any, label: str, *, maximum: int = 128, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise ArtifactLoadError(f"{label} must be exact str")
    if not value and not allow_empty:
        raise ArtifactLoadError(f"{label} must not be empty")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ArtifactLoadError(f"{label} is not valid UTF-8 text") from None
    if size > maximum:
        raise ArtifactLoadError(f"{label} exceeds {maximum} UTF-8 bytes")
    return value


def _validate_identity(identity: Any) -> dict[str, Any]:
    if type(identity) is not dict:
        raise ArtifactLoadError("portable identity must be an exact mapping")
    if set(identity) != _IDENTITY_KEYS or any(type(key) is not str for key in identity):
        raise ArtifactLoadError("portable identity keys mismatch")
    _exact_int(identity["schema_version"], minimum=1, maximum=1, label="identity schema_version")
    _hash(identity["rom_sha1"], _HEX40, "rom_sha1")
    for field in (
        "rom_sha256",
        "profile_sha256",
        "generator_sha256",
        "map_index_sha256",
        "grouping_sha256",
    ):
        _hash(identity[field], _HEX64, field)
    _hash(identity["puzzle_sha256"], _HEX64, "puzzle_sha256", optional=True)
    _text(identity["profile_id"], "profile_id")
    order = identity["group_order"]
    sizes = identity["group_sizes"]
    if type(order) is not list or not 1 <= len(order) <= 256:
        raise ArtifactLoadError("group_order must be a non-empty bounded list")
    if type(sizes) is not list or len(sizes) != len(order):
        raise ArtifactLoadError("group_sizes must be a list matching group_order")
    names = [_text(name, "group_order name") for name in order]
    if len(set(names)) != len(names):
        raise ArtifactLoadError("group_order entries must be unique")
    total = 0
    for size in sizes:
        total += _exact_int(size, minimum=0, maximum=MAX_MAPS, label="group size")
    if total > MAX_MAPS:
        raise ArtifactLoadError("group sizes exceed the map ceiling")
    return identity


def _identity_digest(identity: dict[str, Any]) -> str:
    return _sha256(_canonical(identity))


def _validate_counts(counts: Any) -> None:
    if type(counts) is not dict or len(counts) > MAX_COUNT_KEYS:
        raise ArtifactLoadError("counts must be a small exact mapping")
    for key, value in counts.items():
        _text(key, "count key", maximum=64)
        _exact_int(value, minimum=0, maximum=MAX_COUNT_VALUE, label="count value")


def _validate_utc(value: Any) -> None:
    _text(value, "built_utc", maximum=20)
    if _UTC.fullmatch(value) is None:
        raise ArtifactLoadError("built_utc must use the UTC Z format")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ArtifactLoadError("built_utc is not a valid UTC timestamp") from exc


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactLoadError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_number(token: str) -> Any:
    # Floats are outside the artifact algebra; refusing them also stops
    # ``1e999`` from reaching json.loads' silent ``inf`` conversion.
    raise ArtifactLoadError(f"non-integer JSON number refused: {token}")


def _preflight(text: str) -> None:
    """Lexically bound JSON before json.loads can allocate nested values."""
    depth = 0
    tokens = 0
    index = 0
    size = len(text)
    while index < size:
        char = text[index]
        if char in " \t\r\n":
            index += 1
            continue
        if char == '"':
            tokens += 1
            index += 1
            while index < size:
                inner = text[index]
                if inner == "\\":
                    index += 2
                    continue
                if inner == '"':
                    index += 1
                    break
                index += 1
        elif char in "{[":
            depth += 1
            tokens += 1
            if depth > MAX_JSON_DEPTH:
                raise ArtifactLoadError("JSON nesting bound exceeded")
            index += 1
        elif char in "}]":
            depth -= 1
            tokens += 1
            index += 1
        elif char in ",:":
            tokens += 1
            index += 1
        else:
            start = index
            while index < size and text[index] not in ' \t\r\n{}[],:"':
                index += 1
            token = text[start:index]
            tokens += 1
            if (
                token
                and (token[0].isdigit() or token[0] == "-")
                and len(token) > MAX_NUMBER_TOKEN
            ):
                raise ArtifactLoadError("JSON number token bound exceeded")
        if tokens > MAX_JSON_TOKENS:
            raise ArtifactLoadError("JSON token bound exceeded")


def _parse_json(raw: bytes, label: str) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ArtifactLoadError(f"{label} is not valid UTF-8") from None
    _preflight(text)
    try:
        return json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_number,
            parse_float=_reject_number,
        )
    except ArtifactLoadError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ArtifactLoadError(f"{label} is not a bounded JSON document") from exc


def _read_bounded(directory_fd: int, name: str, limit: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise ArtifactLoadError(f"{label} cannot be opened safely") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ArtifactLoadError(f"{label} is not a regular file")
        if info.st_uid != os.getuid():
            raise ArtifactLoadError(f"{label} is not owned by the current user")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ArtifactLoadError(f"{label} is group/world writable")
        if info.st_size > limit:
            raise ArtifactLoadError(f"{label} exceeds its byte bound")
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
            raise ArtifactLoadError(f"{label} exceeds its byte bound")
        return data
    except OSError as exc:
        raise ArtifactLoadError(f"{label} read failed") from exc
    finally:
        os.close(fd)


def _validate_receipt(
    receipt: Any, expected: dict[str, Any], expected_digest: str
) -> dict[str, Any]:
    if type(receipt) is not dict:
        raise ArtifactLoadError("receipt must be an exact mapping")
    if set(receipt) != _RECEIPT_KEYS or any(type(key) is not str for key in receipt):
        raise ArtifactLoadError("receipt keys mismatch")
    _exact_int(receipt["schema_version"], minimum=1, maximum=1, label="receipt schema_version")
    if receipt["sealed"] is not True:
        raise ArtifactLoadError("receipt must be sealed with exact True")
    identity = _validate_identity(receipt["identity"])
    if identity != expected or _identity_digest(identity) != expected_digest:
        raise ArtifactLoadError("receipt identity does not match the supplied identity")
    input_identity = _hash(receipt["input_identity"], _HEX64, "input_identity")
    if input_identity != expected_digest:
        raise ArtifactLoadError("receipt input_identity does not match the supplied identity")
    payload_sha = _hash(receipt["payload_sha256"], _HEX64, "payload_sha256")
    payload_name = receipt["payload_name"]
    if type(payload_name) is not str or _PAYLOAD_NAME.fullmatch(payload_name) is None:
        raise ArtifactLoadError("payload_name is not a content-addressed payload name")
    if payload_name != f"payload-{payload_sha}.json":
        raise ArtifactLoadError("payload_name does not match payload_sha256")
    _exact_int(
        receipt["payload_size"],
        minimum=0,
        maximum=PAYLOAD_MAX_BYTES,
        label="payload_size",
    )
    _validate_counts(receipt["counts"])
    _validate_utc(receipt["built_utc"])
    return receipt


def load_verified_document(directory: os.PathLike[str] | str, expected_identity: Any) -> dict:
    """Return the payload only if receipt, identity and bytes all verify.

    The result is integrity-verified content, not an admitted NavigationGraph.
    See the module docstring for the trusted-identity and grouping obligations.
    """
    expected = _validate_identity(expected_identity)
    expected_digest = _identity_digest(expected)
    basename = os.path.basename(os.path.normpath(os.fspath(directory)))
    if basename != expected_digest:
        raise ArtifactLoadError("directory basename is not the supplied identity digest")
    try:
        directory_fd = os.open(
            directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        )
    except OSError as exc:
        raise ArtifactLoadError("artifact directory cannot be opened") from exc
    try:
        if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
            raise ArtifactLoadError("artifact path is not a directory")
        receipt_raw = _read_bounded(directory_fd, "receipt.json", RECEIPT_MAX_BYTES, "receipt")
        receipt = _validate_receipt(_parse_json(receipt_raw, "receipt"), expected, expected_digest)
        payload_raw = _read_bounded(
            directory_fd, receipt["payload_name"], PAYLOAD_MAX_BYTES, "payload"
        )
    finally:
        os.close(directory_fd)
    if len(payload_raw) != receipt["payload_size"]:
        raise ArtifactLoadError("payload size does not match the receipt")
    if _sha256(payload_raw) != receipt["payload_sha256"]:
        raise ArtifactLoadError("payload digest does not match the receipt")
    payload = _parse_json(payload_raw, "payload")
    if type(payload) is not dict:
        raise ArtifactLoadError("payload top level must be an exact mapping")
    return payload


def decode_interpretations(encoded_rows: Any) -> tuple[Interpretation, ...]:
    """Admit encoded Interpretation rows after full operator pinning.

    Every effect and every Guard.operator is checked, including hidden guards
    in later effects, before any row is returned.  Corpus/schema completeness
    remains the caller's responsibility.
    """
    if type(encoded_rows) is not list:
        raise ArtifactLoadError("encoded interpretations must be an exact list")
    if len(encoded_rows) > MAX_INTERPRETATIONS:
        raise ArtifactLoadError("interpretation count exceeds its bound")
    decoded: list[Interpretation] = []
    for index, row in enumerate(encoded_rows):
        try:
            value = decode_interpretation(row)
        except CodecError as exc:
            raise ArtifactLoadError(f"interpretation {index} refused: {exc}") from None
        for effect in value.effects:
            for guard in effect.guards:
                if guard.operator not in ALLOWED_OPERATORS:
                    raise ArtifactLoadError(
                        f"interpretation {index} uses unsupported operator {guard.operator!r}"
                    )
        decoded.append(value)
    return tuple(decoded)
