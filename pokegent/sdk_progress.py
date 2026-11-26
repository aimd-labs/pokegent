"""Opt-in, read-only progress SDK facade over caller-supplied objective facts.

This module observes nothing itself: it accepts one already resolved objective and one
caller-supplied snapshot, feeds both to a fresh storeless, sinkless ``ObjectiveTracker`` and
returns one frozen view bound to that snapshot sequence. Caller inputs are trusted labels; the
facade reads no provider, server, runtime or ROM and claims no digest or native binding. The
existing tracker owns probe semantics unchanged; this facade adds no per-subsystem guard. An
unusable snapshot is returned unevaluated, while a usable snapshot's answer is the tracker's
own, including its hold/no-probe and probe-level unknown behavior. Disabled unless the caller
explicitly opts in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.games.plugin import PluginSelection
from pokegent.goals.tracker import ObjectiveTracker
from pokegent.goals.validate import ResolvedObjective

#: The existing objective schemas and RAM reader are FireRed-specific in this slice.
SUPPORTED_GAME = "firered"
SUPPORTED_PROFILE = "firered-us-rev0"
EVIDENCE_KIND = "snapshot_evaluation"


class ProgressSdkRejected(ValueError):
    """The SDK refused construction or an evaluation request."""


@dataclass(frozen=True, slots=True)
class ProgressView:
    """One snapshot-bound completion answer; False is not proof that every fact is known."""

    objective_id: str
    revision: int
    snapshot_seq: int
    completed: bool | None
    evaluated: bool
    evidence_kind: str = EVIDENCE_KIND
    durable_committed: bool = False


class ProgressSdk:
    """Evaluate a supplied objective against one supplied snapshot; no authority or store."""

    def __init__(self, selection: PluginSelection, *, enabled: bool = False) -> None:
        if not isinstance(selection, PluginSelection):
            raise TypeError("selection must be an admitted PluginSelection")
        if type(enabled) is not bool:
            raise TypeError("enabled must be exactly bool")
        descriptor = selection.descriptor
        if descriptor.game != SUPPORTED_GAME or descriptor.profile_id != SUPPORTED_PROFILE:
            raise ProgressSdkRejected(
                f"progress SDK supports the {SUPPORTED_GAME}/{SUPPORTED_PROFILE} namespace only"
            )
        self._selection = selection
        self._enabled = enabled

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise ProgressSdkRejected("progress SDK is opt-in; construct it with enabled=True")

    def evaluate(
        self,
        resolved: ResolvedObjective,
        snapshot: StateSnapshot,
        *,
        tables: Any = None,
    ) -> ProgressView:
        """Report tracker completion for exactly this objective and snapshot."""
        self._require_enabled()
        if not isinstance(resolved, ResolvedObjective):
            raise ProgressSdkRejected("resolved must be a ResolvedObjective")
        if not isinstance(snapshot, StateSnapshot):
            raise ProgressSdkRejected("snapshot must be a StateSnapshot")
        if snapshot.rom_profile != self._selection.descriptor.profile_id:
            raise ProgressSdkRejected("snapshot profile does not match the selected plugin")
        objective = resolved.objective
        if not snapshot.usable:
            # An unreadable frame cannot say true or false, so no probe is evaluated.
            return ProgressView(
                objective_id=objective.objective_id,
                revision=objective.revision,
                snapshot_seq=snapshot.snapshot_seq,
                completed=None,
                evaluated=False,
            )
        tracker = ObjectiveTracker(tables=tables)
        tracker.set_objective(resolved)
        tracker.update(snapshot)
        return ProgressView(
            objective_id=objective.objective_id,
            revision=objective.revision,
            snapshot_seq=snapshot.snapshot_seq,
            completed=tracker.completed,
            evaluated=True,
        )
