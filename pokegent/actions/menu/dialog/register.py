"""Wiring: detectors, builder, executors and verifiers of the dialog family in one call.

The integrator's ``register_all`` calls :func:`register` with the four shared registries. Nothing
here is global; two registrations on separate registries do not interact.
"""

from __future__ import annotations

from dataclasses import dataclass

from pokegent.actions._exec.registry import ExecutorRegistry, VerifierRegistry
from pokegent.actions.menu.dialog import evidence as E
from pokegent.actions.menu.dialog.builder import DialogBuilder
from pokegent.actions.menu.dialog.detect import CLAIMED_SCENES, build_detectors
from pokegent.actions.menu.dialog.macros import executor_specs
from pokegent.actions.menu.dialog.prompts import PromptPolicy
from pokegent.actions.menu.dialog.source import DialogSnapshotSource
from pokegent.actions.menu.dialog.text import TextSidecar
from pokegent.actions.menu.dialog.verify import verifier_specs
from pokegent.games.gen3.firered.reader.live import AtomicSnapshotSource
from pokegent.games.gen3.firered.rom.rom_data import RomProfile
from pokegent.options.registry import BuilderRegistry
from pokegent.state.scenes import DetectorRegistry

FAMILY = "dialog"

#: scenes of other namespaces whose builder this package registers by explicit scene id. Pass
#: ``system_scenes=False`` if another branch owns them.
SYSTEM_BUILDER_SCENES = (E.WHITEOUT, E.QUEST_LOG)

#: scenes that behave like a YES/NO and must be confirmed over stable snapshots (spec 4.4) although
#: the catalog's YES+NO rule does not cover them (their verbs are HEAL_TEAM / DECLINE_HEAL).
CONFIRM_SCENE_IDS = frozenset({E.HEAL_OFFER})


@dataclass(frozen=True)
class DialogFamily:
    sidecar: TextSidecar
    policy: PromptPolicy
    builder: DialogBuilder
    claimed_scenes: frozenset[str]
    confirm_scene_ids: frozenset[str]

    def snapshot_source(
        self, source: AtomicSnapshotSource, profile: RomProfile, *, run_id: str = ""
    ) -> DialogSnapshotSource:
        """The snapshot source that fills this family's text sidecar (use it for the Perceiver)."""
        return DialogSnapshotSource(source, profile, self.sidecar, run_id=run_id)


def register(
    *,
    detectors: DetectorRegistry,
    builders: BuilderRegistry,
    executors: ExecutorRegistry,
    verifiers: VerifierRegistry,
    policy: PromptPolicy | None = None,
    sidecar: TextSidecar | None = None,
    system_scenes: bool = True,
    quest_log_builder: bool = True,
) -> DialogFamily:
    sidecar = sidecar if sidecar is not None else TextSidecar()
    policy = policy or PromptPolicy()
    builder = DialogBuilder(sidecar, policy)
    for detector in build_detectors(sidecar):
        detectors.register(detector)
    builders.register_family(FAMILY, builder)
    if system_scenes:
        # ``quest_log_builder=False``: dialog keeps DETECTING system.quest_log_playback and another
        # family (system_flow) builds its options (it can skip the recap; dialog's builder only
        # reports it as unsupported)
        builders.register_scenes(
            tuple(s for s in SYSTEM_BUILDER_SCENES if quest_log_builder or s != E.QUEST_LOG),
            builder,
        )
    for spec in executor_specs(sidecar, policy):
        executors.register(spec)
    for vspec in verifier_specs(sidecar):
        verifiers.register(vspec)
    return DialogFamily(sidecar, policy, builder, CLAIMED_SCENES, CONFIRM_SCENE_IDS)
