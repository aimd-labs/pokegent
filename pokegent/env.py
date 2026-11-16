"""Environment compatibility helpers for the pokegent rename."""

from __future__ import annotations

import logging
import os
import warnings
from typing import Mapping

log = logging.getLogger("pokegent.env")
_system1_warned_names: set[str] = set()
_legacy_warned_names: set[str] = set()


def reject_removed_env(environ: Mapping[str, str] | None = None) -> None:
    values = os.environ if environ is None else environ
    removed = sorted(name for name in values if name.startswith(("VON_", "POKEVON_")))
    if removed:
        raise ValueError(
            f"removed environment variable(s): {', '.join(removed)}; "
            "use SYSTEM1_* environment names and provider 'http'"
        )


def get_env(
    name: str, default: str | None = None, *, environ: Mapping[str, str] | None = None
) -> str | None:
    """Read a current ``POKEGENT_*`` variable, falling back to ``POKEJEV_*`` once."""
    reject_removed_env(environ)
    values = os.environ if environ is None else environ
    old_name = name.replace("POKEGENT_", "POKEJEV_", 1) if name.startswith("POKEGENT_") else None
    value = values.get(name)
    old_value = values.get(old_name) if old_name else None
    if value is not None and old_value is not None and value != old_value:
        raise ValueError(f"{name} and {old_name} differ")
    if value is not None:
        return value
    if old_value is not None:
        _warn_legacy(old_name, name)
        return old_value
    return default


def _warn_legacy(old_name: str, new_name: str) -> None:
    if old_name not in _legacy_warned_names:
        _legacy_warned_names.add(old_name)
        warnings.warn(f"{old_name} is deprecated; use {new_name}", DeprecationWarning, stacklevel=3)


def apply_legacy_env_aliases() -> None:
    """Expose old names to legacy direct ``os.environ`` consumers for one release."""
    for old_name, old_value in list(os.environ.items()):
        if not old_name.startswith("POKEJEV_"):
            continue
        new_name = old_name.replace("POKEJEV_", "POKEGENT_", 1)
        new_value = os.environ.get(new_name)
        if new_value is not None and new_value != old_value:
            raise ValueError(f"{new_name} and {old_name} differ")
        if new_value is None:
            os.environ[new_name] = old_value
            _warn_legacy(old_name, new_name)


def get_system1_env(
    name: str, default: str | None = None, *, environ: Mapping[str, str] | None = None
) -> str | None:
    """Read a canonical SYSTEM1_* setting."""
    values = os.environ if environ is None else environ
    value = values.get(name)
    if value is not None:
        return value
    return default
