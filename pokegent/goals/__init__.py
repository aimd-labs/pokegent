"""The objective file contract: what agent hands the game, how it is validated, stored and tracked.

Documentation, schema and examples for the league run: ``docs/objective-file.md``.
"""

from __future__ import annotations

from pokegent.goals.models import SCHEMA_VERSION, Objective, Target
from pokegent.goals.source import (
    PLAN_SCHEMA,
    SUMMARY_MAX_CHARS,
    FixedGoal,
    GoalSource,
    GoalView,
    ObjectiveSession,
    PlanError,
    Refreshed,
    ScriptedObjectiveSource,
    bound_summary,
    load_plan,
    validate_plan_state,
)
from pokegent.goals.store import (
    ObjectiveStore,
    Proposal,
    StoreWarning,
    atomic_write_json,
)
from pokegent.goals.tracker import (
    GOAL_CHANGED,
    MILESTONE_COMPLETED,
    ObjectiveTracker,
    ProgressEvent,
    ProgressSink,
    evaluate_probe,
)
from pokegent.goals.validate import (
    MapDirectory,
    MapFacts,
    Reason,
    ResolvedObjective,
    ResolvedProbe,
    ResolvedTarget,
    Validation,
    resolve_map,
    validate,
)

__all__ = [
    "GOAL_CHANGED",
    "MILESTONE_COMPLETED",
    "PLAN_SCHEMA",
    "SCHEMA_VERSION",
    "SUMMARY_MAX_CHARS",
    "FixedGoal",
    "GoalSource",
    "GoalView",
    "MapDirectory",
    "MapFacts",
    "Objective",
    "ObjectiveSession",
    "ObjectiveStore",
    "ObjectiveTracker",
    "PlanError",
    "ProgressEvent",
    "ProgressSink",
    "Proposal",
    "Reason",
    "Refreshed",
    "ResolvedObjective",
    "ResolvedProbe",
    "ResolvedTarget",
    "ScriptedObjectiveSource",
    "StoreWarning",
    "Target",
    "Validation",
    "atomic_write_json",
    "bound_summary",
    "evaluate_probe",
    "load_plan",
    "validate_plan_state",
    "resolve_map",
    "validate",
]
