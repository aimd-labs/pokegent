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
SUPPORTED_CONTRACT_VERSIONS = frozenset({1})
_SHA1 = re.compile(r"[0-9a-f]{40}")


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
        if not (self.plugin_id and self.game and self.profile_id):
            raise ValueError("plugin_id, game and profile_id must be non-empty")
        if self.contract_version < 1:
            raise ValueError("contract_version must be positive")
        if not _SHA1.fullmatch(self.rom_sha1):
            raise ValueError("rom_sha1 must be 40 lowercase hex characters")
        capabilities = frozenset(self.capabilities)
        if len(capabilities) > MAX_REQUIRED_CAPABILITIES:
            raise ValueError("too many declared capabilities")
        if any(not isinstance(item, str) or not item for item in capabilities):
            raise ValueError("capabilities must be non-empty strings")
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

    def select(
        self,
        *,
        rom_bytes: bytes | bytearray | memoryview,
        game: str,
        profile_id: str,
        required_capabilities: Iterable[str] = (),
    ) -> PluginSelection:
        """Admit exact private ROM bytes, then compose; no callback before admission."""
        if not self._enabled:
            raise PluginRefusal(RefusalCode.DISABLED, "registry is not explicitly enabled")
        if not isinstance(rom_bytes, (bytes, bytearray, memoryview)):
            raise TypeError("rom_bytes must be bytes-like")
        if len(rom_bytes) > MAX_ROM_BYTES:
            raise ValueError("rom_bytes exceeds the bounded input size")
        required = tuple(islice(required_capabilities, MAX_REQUIRED_CAPABILITIES + 1))
        if len(required) > MAX_REQUIRED_CAPABILITIES:
            raise ValueError("too many required capabilities")
        if any(not isinstance(item, str) or not item for item in required):
            raise ValueError("required capabilities must be non-empty strings")

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
        return PluginSelection(descriptor=descriptor, services=plugin.compose())
