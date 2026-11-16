"""Small core hook for optional local addons.

The core runner only knows this hook and the tap callbacks it supplies.  Concrete addon modules are
loaded lazily by the hook so a normal run imports no ``addons.*`` package at all.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

PORT_MIN = 19700
PORT_MAX = 19799
LIVE_PORTS = frozenset({9876, 9877})
LOOPBACK_HOST = "127.0.0.1"


class AddonConfigError(ValueError):
    """An addon setting is invalid or unsafe."""


class AddonStartError(RuntimeError):
    """An addon could not bind or start."""


@dataclass(frozen=True)
class AddonConfig:
    """Shared settings for local addon listeners; binding is always IPv4 loopback."""

    port: int | None = None
    public_only: bool = False  # deprecated viewer-alias compatibility
    realtime: bool | None = None
    static: str | None = None
    frame_hz: float = 30.0
    fps: float = 60.0
    linger_s: float = 3.0

    @property
    def enabled(self) -> bool:
        return self.port is not None

    @property
    def realtime_on(self) -> bool:
        return self.enabled if self.realtime is None else bool(self.realtime)

    def validated(self) -> tuple["AddonConfig", list[str]]:
        warnings: list[str] = []
        if self.port is not None:
            if isinstance(self.port, bool) or not isinstance(self.port, int):
                raise AddonConfigError(f"addon port must be an integer, not {self.port!r}")
            if self.port in LIVE_PORTS:
                raise AddonConfigError(
                    f"addon port {self.port} belongs to the live system; use {PORT_MIN}-{PORT_MAX}"
                )
            if not PORT_MIN <= self.port <= PORT_MAX:
                raise AddonConfigError(f"addon port {self.port} is outside {PORT_MIN}-{PORT_MAX}")
        if not 1.0 <= self.frame_hz <= 60.0:
            raise AddonConfigError("addon frame_hz must be within 1..60")
        if not 5.0 <= self.fps <= 240.0:
            raise AddonConfigError("addon fps must be within 5..240")
        if not 0.0 <= self.linger_s <= 600.0:
            raise AddonConfigError("addon linger_s must be within 0..600 seconds")
        if self.static and self.enabled and not Path(self.static).expanduser().is_dir():
            raise AddonConfigError(f"addon static directory not found: {self.static}")
        return self, warnings

    def static_dir(self) -> Path | None:
        return Path(self.static).expanduser() if self.static else None


def bind_socket(config: AddonConfig) -> socket.socket:
    """Bind a validated addon port on loopback before starting its server thread."""
    if config.port is None:
        raise AddonConfigError("no addon port")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((LOOPBACK_HOST, config.port))
    except OSError as exc:
        sock.close()
        raise AddonStartError(
            f"cannot bind addon to {LOOPBACK_HOST}:{config.port}: {exc}"
        ) from None
    sock.set_inheritable(True)
    return sock


def default_static_dir() -> Path | None:
    candidate = Path(__file__).resolve().parent / "dashboard" / "static"
    return candidate if candidate.is_dir() else None


def is_loopback(host: str) -> bool:
    if host == LOOPBACK_HOST:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


ViewerConfig = AddonConfig
ViewerConfigError = AddonConfigError
ViewerStartError = AddonStartError


@dataclass(frozen=True)
class AddonTaps:
    """Runner-owned callbacks and metadata consumed by an optional addon runtime."""

    run_id: str
    decider: str
    max_steps: int
    log: Callable[[str], None]


def start_addons(config: Mapping[str, Any], taps: AddonTaps) -> Any | None:
    """Start the configured local surfaces without importing concrete addons in core."""
    if not any(
        getattr(config.get(name), "enabled", False) for name in ("viewer", "stream", "dashboard")
    ):
        return None
    from addons.runtime import start_addons as _start

    return _start(config, taps)


__all__ = [
    "AddonConfig",
    "AddonConfigError",
    "AddonStartError",
    "AddonTaps",
    "LIVE_PORTS",
    "LOOPBACK_HOST",
    "PORT_MAX",
    "PORT_MIN",
    "ViewerConfig",
    "ViewerConfigError",
    "ViewerStartError",
    "bind_socket",
    "default_static_dir",
    "is_loopback",
    "start_addons",
]
