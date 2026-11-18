"""Fetch bundled reference data from either a source checkout or an installed wheel.

``references/*.yaml``/``*.json`` are the public, hand-maintained decoder metadata (ROM
profiles, scene catalog, battle item classes, regions). Historically each loader resolved
them with a checkout-relative ``Path(__file__).parents[N]`` expression, which breaks once
pokegent is installed as a wheel (site-packages). This module is the single resolver:

1. an explicit env override (kept for operator flexibility), then
2. the file bundled in the wheel/package via ``importlib.resources`` (backed by package
   data shipped from ``references/``), falling back to
3. the source checkout root so an editable/dev clone also works.

None of these paths is a secret or a credential; they are public ROM decoder metadata.
"""
from __future__ import annotations

import os
from pathlib import Path

try:  # Python 3.9+; resource name lookup
    from importlib.resources import files as _pkg_files
except ImportError:  # pragma: no cover - py<3.9
    _pkg_files = None  # type: ignore[assignment]

# Source checkout / editable install fallback: package directory.
_REPO_REFERENCES = Path(__file__).resolve().parents[0] / "references"


def reference_path(name: str, env: str | None = None) -> Path:
    """Resolve ``references/<name>``.

    ``env`` is an optional environment-variable override specific to the caller (e.g.
    ``POKEGENT_SCENES``); when set, it wins (the operator owns the override). A bundled
    resource first, then the source checkout root.
    """
    if env and os.environ.get(env):
        return Path(os.environ[env])
    # Installed wheel / package tree: importlib.resources resolves package data that we
    # ship under pokegent/references (see pyproject package-data).
    if _pkg_files is not None:
        try:
            res = _pkg_files("pokegent").joinpath("references", name)
            if res.is_file():
                return Path(os.fspath(res))
        except (ModuleNotFoundError, OSError, NotADirectoryError, TypeError):  # pragma: no cover
            pass
    # Source checkout / editable install fallback.
    candidate = _REPO_REFERENCES / name
    if candidate.is_file():
        return candidate
    raise FileNotFoundError(
        f"bundled reference data {name!r} is not available; "
        "install from the pokegent wheel (we ship references/) or use a source checkout"
    )
