"""Registration seam for the optional scripted-puzzle family.

Submodules register into the shared registries through this one function. Missing modules are
normal while the work package is being rolled out; import errors from present modules propagate.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass

from pokegent.actions._exec.registry import ExecutorRegistry, VerifierRegistry
from pokegent.local_data import read_data
from pokegent.options.registry import BuilderRegistry
from pokegent.state.scenes import DetectorRegistry

_MODULES = ("elevators", "quiz", "switch_puzzle")


@dataclass(frozen=True)
class SpecialFamily:
    """Handle returned by the optional-family loader."""

    claimed_scenes: frozenset[str]


def register(
    *,
    detectors: DetectorRegistry,
    builders: BuilderRegistry,
    executors: ExecutorRegistry,
    verifiers: VerifierRegistry,
    objectives=None,
    navigation=None,
) -> SpecialFamily:
    """Register each installed special module; keep the integrator out of submodule wiring."""
    claimed: set[str] = set()
    missing = []
    for name in _MODULES:
        filename = {"elevators": "elevators.yaml", "quiz": "cinnabar_quiz.yaml",
                    "switch_puzzle": "puzzles.yaml"}[name]
        if read_data(filename, optional=True) is None:
            missing.append(name)
            continue
        module_name = f"{__package__}.{name}"
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name == module_name:
                continue
            raise
        register_module = getattr(module, "register", None)
        if register_module is not None:
            kwargs = dict(
                detectors=detectors,
                builders=builders,
                executors=executors,
                verifiers=verifiers,
            )
            if name in {"elevators", "switch_puzzle"}:
                kwargs.update(objectives=objectives, navigation=navigation)
            register_module(**kwargs)
        claimed.update(getattr(module, "CLAIMED_SCENES", ()))
    if missing:
        logging.getLogger(__name__).warning(
            "Scripted special actions disabled for missing local data: %s; see docs/local-data.md",
            ", ".join(missing),
        )
    return SpecialFamily(frozenset(claimed))


__all__ = ["SpecialFamily", "register"]
