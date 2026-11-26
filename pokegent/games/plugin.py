"""Opt-in, exact-ROM Python game plugin selection.

Inert prototype seam: nothing imports it from the runtime, it performs no module/package
discovery, and admitting a synthetic plugin is not a title-support claim. Callers must opt in
with ``enabled=True`` and register explicit, reviewed plugin objects.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from itertools import islice
from typing import Any, Protocol, runtime_checkable

MAX_ROM_BYTES = 32 * 1024 * 1024
MAX_REQUIRED_CAPABILITIES = 16
# Plugin, game, profile and capability names are exact, non-blank, bounded strings.
MAX_IDENTIFIER_CHARS = 64
SUPPORTED_CONTRACT_VERSIONS = frozenset({1})
_SHA1 = re.compile(r"[0-9a-f]{40}")


def _identifier(value: object, field: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field} must be a non-blank string")
    if len(value) > MAX_IDENTIFIER_CHARS:
        raise ValueError(f"{field} must be at most {MAX_IDENTIFIER_CHARS} characters")


def _capabilities(values: object, *, limit: int, field: str) -> frozenset[str]:
    if isinstance(values, (str, bytes, bytearray, memoryview)):
        raise ValueError(f"{field} must be an iterable of strings, not a string container")
    try:
        items = tuple(islice(iter(values), limit + 1))  # type: ignore[arg-type]
    except TypeError as error:
        raise ValueError(f"{field} must be a finite iterable of strings") from error
    if len(items) > limit:
        raise ValueError(f"too many {field}")
    for item in items:
        _identifier(item, field)
    return frozenset(items)


class RefusalCode(str, Enum):
    DISABLED = "disabled"
    UNKNOWN_IDENTITY = "unknown_identity"
    PROFILE_MISMATCH = "profile_mismatch"
    UNSUPPORTED_CONTRACT = "unsupported_contract"
    MISSING_CAPABILITY = "missing_capability"


class PluginRefusal(Exception):
    def __init__(self, code: RefusalCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class GamePluginDescriptor:
    plugin_id: str
    game: str
    profile_id: str
    contract_version: int
    rom_sha1: str
    capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        _identifier(self.plugin_id, "plugin_id")
        _identifier(self.game, "game")
        _identifier(self.profile_id, "profile_id")
        if type(self.contract_version) is not int or self.contract_version < 1:
            raise ValueError("contract_version must be a positive plain int")
        if type(self.rom_sha1) is not str or not _SHA1.fullmatch(self.rom_sha1):
            raise ValueError("rom_sha1 must be 40 lowercase hex characters")
        capabilities = _capabilities(
            self.capabilities, limit=MAX_REQUIRED_CAPABILITIES, field="capabilities"
        )
        object.__setattr__(self, "capabilities", capabilities)


@runtime_checkable
class PythonGamePluginProtocol(Protocol):
    """Minimal reviewed Python plugin surface; no dynamic import or arbitrary config."""

    @property
    def descriptor(self) -> GamePluginDescriptor: ...

    def compose(self) -> Any:
        """Return plugin-owned services after the registry admits this plugin."""
        ...


@dataclass(frozen=True, slots=True)
class PluginSelection:
    descriptor: GamePluginDescriptor
    services: Any


class ReviewedPluginRegistry:
    """Explicit registry for trusted plugin objects; disabled unless opted in."""

    def __init__(self, *, enabled: bool = False) -> None:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be exactly bool")
        self._enabled = enabled
        self._plugins: dict[str, tuple[GamePluginDescriptor, PythonGamePluginProtocol]] = {}
        self._owners: set[tuple[str, str]] = set()

    def register(self, plugin: PythonGamePluginProtocol) -> None:
        descriptor = getattr(plugin, "descriptor", None)
        if not isinstance(descriptor, GamePluginDescriptor):
            raise TypeError("plugin must expose a GamePluginDescriptor")
        if not callable(getattr(plugin, "compose", None)):
            raise TypeError("plugin must expose compose()")
        owner = (descriptor.game, descriptor.profile_id)
        if descriptor.rom_sha1 in self._plugins or owner in self._owners:
            raise ValueError(f"duplicate plugin ownership for {owner!r}")
        self._plugins[descriptor.rom_sha1] = (descriptor, plugin)
        self._owners.add(owner)

    def descriptors(self) -> tuple[GamePluginDescriptor, ...]:
        """Return frozen descriptors captured at register(), sorted by plugin_id."""
        if not self._enabled:
            raise PluginRefusal(RefusalCode.DISABLED, "registry is not explicitly enabled")
        return tuple(
            sorted(
                (descriptor for descriptor, _ in self._plugins.values()),
                key=lambda descriptor: (descriptor.plugin_id, descriptor.rom_sha1),
            )
        )

    def select(
        self,
        *,
        rom_bytes: bytes | bytearray | memoryview,
        game: str,
        profile_id: str,
        required_capabilities: Iterable[str] = (),
        expected_rom_sha1: str | None = None,
    ) -> PluginSelection:
        """Admit exact private ROM bytes, then compose; no callback before admission."""
        if not self._enabled:
            raise PluginRefusal(RefusalCode.DISABLED, "registry is not explicitly enabled")
        if not isinstance(rom_bytes, (bytes, bytearray, memoryview)):
            raise TypeError("rom_bytes must be bytes-like")
        rom_size = memoryview(rom_bytes).nbytes
        if rom_size > MAX_ROM_BYTES:
            raise ValueError("rom_bytes exceeds the bounded input size")
        _identifier(game, "game")
        _identifier(profile_id, "profile_id")
        required = _capabilities(
            required_capabilities,
            limit=MAX_REQUIRED_CAPABILITIES,
            field="required capabilities",
        )
        if expected_rom_sha1 is not None:
            if (
                type(expected_rom_sha1) is not str
                or _SHA1.fullmatch(expected_rom_sha1) is None
            ):
                raise ValueError(
                    "expected_rom_sha1 must be a lowercase 40-char hex digest"
                )

        digest = hashlib.sha1(rom_bytes).hexdigest()
        admitted = self._plugins.get(digest)
        if admitted is None:
            raise PluginRefusal(RefusalCode.UNKNOWN_IDENTITY, "ROM digest is not registered")
        descriptor, plugin = admitted
        if descriptor.game != game or descriptor.profile_id != profile_id:
            detail = (
                f"requested {game}/{profile_id}, registered "
                f"{descriptor.game}/{descriptor.profile_id}"
            )
            raise PluginRefusal(RefusalCode.PROFILE_MISMATCH, detail)
        if descriptor.contract_version not in SUPPORTED_CONTRACT_VERSIONS:
            detail = f"contract version {descriptor.contract_version} is not supported"
            raise PluginRefusal(RefusalCode.UNSUPPORTED_CONTRACT, detail)
        missing = frozenset(required) - descriptor.capabilities
        if missing:
            detail = "missing capabilities: " + ", ".join(sorted(missing))
            raise PluginRefusal(RefusalCode.MISSING_CAPABILITY, detail)
        if expected_rom_sha1 is not None and descriptor.rom_sha1 != expected_rom_sha1:
            raise PluginRefusal(
                RefusalCode.PROFILE_MISMATCH,
                "registered ROM digest conflicts with the profile ROM digest",
            )
        return PluginSelection(descriptor=descriptor, services=plugin.compose())
