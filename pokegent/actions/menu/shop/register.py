"""Registration for the shop family."""

from __future__ import annotations

from dataclasses import dataclass

from pokegent.actions._exec.registry import ExecutorRegistry, VerifierRegistry
from pokegent.actions.menu.shop import facts
from pokegent.actions.menu.shop import refs as R
from pokegent.actions.menu.shop.builder import ShopBuilder
from pokegent.actions.menu.shop.detect import CLAIMED_SCENES, build_detectors
from pokegent.actions.menu.shop.macros import executor_specs
from pokegent.actions.menu.shop.verify import verifier_specs
from pokegent.games.gen3.firered.maps.world import load_world_adapter
from pokegent.options.registry import BuilderRegistry
from pokegent.state.scenes import DetectorRegistry

CONFIRM_SCENE_IDS = frozenset({R.BUY_CONFIRM, R.SELL_CONFIRM})


@dataclass(frozen=True)
class ShopFamily:
    builder: ShopBuilder
    claimed_scenes: frozenset[str]


def register(
    *,
    detectors: DetectorRegistry,
    builders: BuilderRegistry,
    executors: ExecutorRegistry,
    verifiers: VerifierRegistry,
    config=None,
) -> ShopFamily:
    facts.register_renderer()
    world = config.get("world") if isinstance(config, dict) else None
    if world is None:
        world = load_world_adapter()
    builder = ShopBuilder(world)
    for detector in build_detectors():
        detectors.register(detector)
    builders.register_scenes(sorted(CLAIMED_SCENES), builder)
    for spec in executor_specs():
        executors.register(spec)
    for spec in verifier_specs():
        verifiers.register(spec)
    return ShopFamily(builder, CLAIMED_SCENES)
