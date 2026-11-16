"""Scene-agnostic progress measurement.

The monitor detects lack of semantic progress.  It never chooses or mutates game state; the
runner reports the observation and stops so the supervising agent can decide what happens next.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


class ProgressMonitor:
    """Measure semantic progress across action-bearing field decisions."""

    def __init__(self, decision_limit: int, *, now: float) -> None:
        if isinstance(decision_limit, bool) or not isinstance(decision_limit, int):
            raise ValueError("decision_limit must be an integer")
        if decision_limit < 1:
            raise ValueError("decision_limit must be >= 1")
        self.decision_limit = decision_limit
        self.last_progress_at = now
        self.last_progress_iteration = 0
        self.decisions_since_progress = 0
        self._signature: str | None = None
        self._verified_before: str | None = None

    def observe(self, signature: str, *, now: float, iteration: int) -> bool:
        """Return true only for a verified action followed by a semantic state change."""
        if self._signature is None:
            self._signature = signature
            self.last_progress_at = now
            self.last_progress_iteration = iteration
            return False
        progressed = self._verified_before is not None and signature != self._verified_before
        self._signature = signature
        self._verified_before = None
        if progressed:
            self.reset_window(now=now, iteration=iteration)
        return progressed

    def note_decision(self, *, verified: bool) -> None:
        self.decisions_since_progress += 1
        self._verified_before = self._signature if verified else None

    def due_reason(self) -> str | None:
        if self.decisions_since_progress >= self.decision_limit:
            return f"decision_limit:{self.decisions_since_progress}"
        return None

    def reset_window(self, *, now: float, iteration: int) -> None:
        self.last_progress_at = now
        self.last_progress_iteration = iteration
        self.decisions_since_progress = 0
        self._verified_before = None

    @property
    def signature(self) -> str | None:
        return self._signature


def semantic_progress_signature(snapshot: Any, frame: Any | None = None) -> str:
    """Hash only the state classes that count as live-run progress.

    UI/menu/cursor changes intentionally do not count.  Battle frames have no battle block in the
    core snapshot, so their decision-relevant fingerprint is included as the battle-state marker.
    """

    state = getattr(snapshot, "map", None)
    avatar = getattr(state, "player", None)
    player = getattr(snapshot, "player", None)
    identity = getattr(state, "identity", None)
    position = None
    if avatar is not None and identity is not None:
        position = (
            getattr(identity, "group", None),
            getattr(identity, "number", None),
            getattr(avatar, "avatar_x", getattr(avatar, "x", None)),
            getattr(avatar, "avatar_y", getattr(avatar, "y", None)),
            getattr(avatar, "elevation", None),
        )
    elif player is not None:
        position = (
            getattr(player, "map_group", None),
            getattr(player, "map_num", None),
            getattr(player, "x", None),
            getattr(player, "y", None),
            None,
        )

    def dump(value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if isinstance(value, Mapping):
            return {str(key): dump(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [dump(item) for item in value]
        return value

    scene_id = getattr(frame, "scene_id", "") if frame is not None else ""
    payload = {
        "position": position,
        "party": dump(getattr(snapshot, "party", ())),
        "bag": dump(getattr(snapshot, "bag", ())),
        "flags": dump(getattr(snapshot, "flags", None)),
        "battle": (
            getattr(frame, "state_fingerprint", None)
            if isinstance(scene_id, str) and scene_id.startswith("battle.")
            else None
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
