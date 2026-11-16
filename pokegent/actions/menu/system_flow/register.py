"""Wiring: detectors, builder, executors and verifiers of the system_flow family in one call.

``register_optional("system_flow", ...)`` of the integrator's ``register_all`` finds this
``register``
by its parameter names. Nothing is global except the facts renderer (idempotent).
"""

from __future__ import annotations

from dataclasses import dataclass

from pokegent.actions._exec.registry import ExecutorRegistry, VerifierRegistry
from pokegent.actions.menu.system_flow import refs as R
from pokegent.actions.menu.system_flow.builder import SystemFlowBuilder
from pokegent.actions.menu.system_flow.detect import CLAIMED_SCENES, build_detectors
from pokegent.actions.menu.system_flow.facts import GameClearWatch, register_renderers
from pokegent.actions.menu.system_flow.foreign import FOREIGN_MENUS
from pokegent.actions.menu.system_flow.macros import executor_specs
from pokegent.actions.menu.system_flow.policy import SystemFlowOptions, coerce_options
from pokegent.actions.menu.system_flow.verify import verifier_specs
from pokegent.options.registry import BuilderRegistry
from pokegent.state.scenes import DetectorRegistry

#: scenes with a yes/no or an irreversible choice: seen identically over stable snapshots first
CONFIRM_SCENE_IDS = frozenset({R.EVO_START, R.EVO_MOVE, R.GENDER})


@dataclass(frozen=True)
class SystemFlowFamily:
    """The handle ``FamilyBundle.plugins["system_flow"]`` returns."""

    builder: SystemFlowBuilder
    options: SystemFlowOptions
    claimed_scenes: frozenset[str]
    game_clear_watch: GameClearWatch


def register(
    *,
    detectors: DetectorRegistry,
    builders: BuilderRegistry,
    executors: ExecutorRegistry,
    verifiers: VerifierRegistry,
    config=None,
) -> SystemFlowFamily:
    options = coerce_options(config)
    unknown = sorted(set(options.foreign_menus) - set(FOREIGN_MENUS))
    if unknown:
        raise ValueError(f"no foreign-menu entry for {unknown}; known: {sorted(FOREIGN_MENUS)}")
    foreign = frozenset(options.foreign_menus)
    register_renderers()
    builder = SystemFlowBuilder(options)
    # Known foreign screens are always recoverable; only the START-menu safety net
    # remains opt-in because it overlaps the generic open-menu detector.
    claimed = (
        CLAIMED_SCENES
        | frozenset({R.POKEDEX_LIST, R.PARTY_SUMMARY, R.TRAINER_CARD, R.OPTIONS})
        | foreign
    )
    for detector in build_detectors(foreign):
        detectors.register(detector)
    # explicit scene ids: the namespaces special/system/title/evolution/pc are shared with other
    # families (WP-B owns special.*), and an explicit id never collides with a family builder
    builders.register_scenes(sorted(claimed), builder)
    for spec in executor_specs(foreign, options.menus):
        executors.register(spec)
    for vspec in verifier_specs(foreign):
        verifiers.register(vspec)
    return SystemFlowFamily(builder, options, claimed, GameClearWatch())
