"""Registration for the START/party family."""

from __future__ import annotations

from dataclasses import dataclass

from pokegent.actions._exec.registry import ExecutorRegistry, VerifierRegistry, VerifierSpec
from pokegent.actions._exec.verify import verifier_spec
from pokegent.actions.items import ItemCatalog, ItemOptionBuilder
from pokegent.actions.menu.start_party import refs as R
from pokegent.actions.menu.start_party.builder import StartPartyBuilder, decode_owned_menu
from pokegent.actions.menu.start_party.detect import build_detectors
from pokegent.actions.menu.start_party.facts import _tables_from_env
from pokegent.actions.menu.start_party.macros import executor_specs
from pokegent.options.registry import BuilderRegistry
from pokegent.state.scenes import DetectorRegistry


@dataclass(frozen=True)
class StartPartyFamily:
    builder: StartPartyBuilder
    claimed_scenes: frozenset[str]


def register(
    *,
    detectors: DetectorRegistry,
    builders: BuilderRegistry,
    executors: ExecutorRegistry,
    verifiers: VerifierRegistry,
    config=None,
) -> StartPartyFamily:
    del config
    builders.bind_verifiers(verifiers)
    tables = _tables_from_env()
    builder = StartPartyBuilder(
        ItemOptionBuilder(ItemCatalog(tables)) if tables is not None else None
    )
    for detector in build_detectors():
        detectors.register(detector)
    builders.register_scenes(sorted(R.CLAIMED_SCENES), builder)
    for spec in executor_specs():
        executors.register(spec)
    generic = verifier_spec(decode_owned_menu)
    verifiers.register(
        VerifierSpec(
            R.POST_EXPECTED,
            generic.predicate,
            max_frames=360,
            max_wall_s=20.0,
        )
    )
    return StartPartyFamily(builder, R.CLAIMED_SCENES)
