"""Wiring: ``register(...)`` adds the battle family to the shared registries.

The integrator calls this from ``pokegent.actions.register_all``. Nothing here runs at import time.
"""

from __future__ import annotations

from pokegent.actions._exec.registry import ExecutorRegistry, VerifierRegistry
from pokegent.actions.battle.adapter import CompleteBattleBuilder
from pokegent.actions.battle.detect import BattleDetector
from pokegent.actions.battle.item_domain import BattleItemDomain
from pokegent.actions.battle.item_facts import register_renderer as register_item_renderer
from pokegent.actions.battle.macros import executors
from pokegent.actions.battle.verifiers import verifiers
from pokegent.actions.battle.view import MoveFactsProvider
from pokegent.options.registry import BuilderRegistry
from pokegent.state.scenes import DetectorRegistry

FAMILY = "battle"


def register(
    detectors: DetectorRegistry,
    builders: BuilderRegistry,
    executor_registry: ExecutorRegistry,
    verifier_registry: VerifierRegistry,
    *,
    facts: MoveFactsProvider | None = None,
    items: BattleItemDomain | None = None,
) -> CompleteBattleBuilder:
    """Register the detector, the builder (family ``battle``), every executor and verifier."""
    register_item_renderer()
    detectors.register(BattleDetector())
    builder = CompleteBattleBuilder(facts, items)
    builders.register_family(FAMILY, builder)
    for spec in executors():
        executor_registry.register(spec)
    for vspec in verifiers():
        verifier_registry.register(vspec)
    return builder
