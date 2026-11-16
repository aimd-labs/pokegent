"""Wiring: detectors, builder, executors and verifiers of the field family in one call.

The integrator's ``register_all`` calls :func:`register` with the four shared registries. Nothing is
global; two registrations on separate registries do not interact.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pokegent.actions._exec.registry import ExecutorRegistry, VerifierRegistry
from pokegent.actions.field.attempts import AttemptLedger
from pokegent.actions.field.boulder_options import BoulderPlans
from pokegent.actions.field.builder import FieldBuilder, ObjectiveSource, VisitedMaps
from pokegent.actions.field.detect import CLAIMED_SCENES, build_detectors
from pokegent.actions.field.macros import executor_specs
from pokegent.actions.field.source import FieldSnapshotSource
from pokegent.actions.field.surf import register as surf_register
from pokegent.actions.field.verify import verifier_specs
from pokegent.actions.items import ItemCatalog
from pokegent.actions.menu.dialog.prompts import PromptPolicy
from pokegent.games.gen3.firered.maps.world import load_world_adapter
from pokegent.games.gen3.firered.reader.live import AtomicSnapshotSource
from pokegent.games.gen3.firered.rom.rom_data import RomProfile
from pokegent.navigation.routing import MapGraph
from pokegent.options.registry import BuilderRegistry
from pokegent.state.scenes import DetectorRegistry

FAMILY = "field"

if TYPE_CHECKING:
    from pokegent.navigation.adapter import NavigationAdapter


@dataclass(frozen=True)
class FieldFamily:
    builder: FieldBuilder
    ledger: AttemptLedger
    visited: VisitedMaps
    claimed_scenes: frozenset[str]

    def snapshot_source(
        self, source: AtomicSnapshotSource, profile: RomProfile, *, run_id: str = ""
    ) -> FieldSnapshotSource:
        """Standalone snapshot source (core + ui + map). The integrator's composite source does
        the same one atomic read and also fills the dialog text and battle blocks."""
        return FieldSnapshotSource(source, profile, run_id=run_id)


def _tables_from_env():
    """The ROM tables from ``$FIRERED_ROM_PATH`` (TM/HM learnsets); ``None`` without a ROM."""
    path = os.environ.get("FIRERED_ROM_PATH")
    if not path or not os.path.isfile(path):
        return None
    try:
        from pokegent.games.gen3.firered.rom.rom_tables import GameTables

        return GameTables.from_rom_file(path)
    except Exception:  # no tables means no Fly/HM teaching offers, never a stopped run
        return None


def register(
    *,
    detectors: DetectorRegistry,
    builders: BuilderRegistry,
    executors: ExecutorRegistry,
    verifiers: VerifierRegistry,
    objectives: ObjectiveSource | None = None,
    graph: MapGraph | None = None,
    ledger: AttemptLedger | None = None,
    prompt_policy: PromptPolicy | None = None,
    config=None,
    navigation: NavigationAdapter | None = None,
) -> FieldFamily:
    builders.bind_verifiers(verifiers)
    ledger = ledger if ledger is not None else AttemptLedger()
    visited = VisitedMaps()
    plans = BoulderPlans()
    world = config.get("world") if isinstance(config, Mapping) else None
    if world is None:
        world = load_world_adapter()
    tables = config.get("tables") if isinstance(config, Mapping) else None
    if tables is None and world is not None:
        tables = world.tables
    if tables is None:  # the live app passes none: load them from the ROM as system_flow does
        tables = _tables_from_env()
    item_catalog = ItemCatalog(tables) if tables is not None else None
    builder = FieldBuilder(
        ledger,
        objectives=objectives,
        graph=graph,
        visited=visited,
        plans=plans,
        tables=tables,
        item_catalog=item_catalog,
        navigation=navigation,
        world=world,
    )
    for detector in build_detectors():
        detectors.register(detector)
    builders.register_family(FAMILY, builder)
    # The resumed Fly map is a menu scene owned by the field macro, not by a generic menu family.
    builders.register_scenes({"menu.fly_map"}, builder)
    walk_step_limit = None
    if isinstance(config, Mapping) and config.get("walk_step_limit") is not None:
        walk_step_limit = int(config["walk_step_limit"])
        if walk_step_limit < 1:
            raise ValueError("field walk_step_limit must be >= 1")
    for spec in executor_specs(
        ledger,
        plans,
        prompt_policy,
        item_catalog=item_catalog,
        walk_step_limit=walk_step_limit,
    ):
        executors.register(spec)
    for vspec in verifier_specs(item_catalog=item_catalog):
        verifiers.register(vspec)
    for spec in surf_register.executor_specs(ledger):
        executors.register(spec)
    for vspec in surf_register.verifier_specs():
        verifiers.register(vspec)
    return FieldFamily(builder, ledger, visited, CLAIMED_SCENES)
