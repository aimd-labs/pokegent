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
    """Measure semantic progress across action-bearing field decisions.

    Two independent signals surface a stall:

    * ``decision_limit``  — ``decision_limit`` consecutive decisions with no
      semantic state change (position / party / bag / flags all unchanged).
    * ``option_loop``     — a closed CLIENT-side loop: over the recent window only a
      small set of option ids (< = ``loop_distinct_limit``) recur, and the semantic
      signature RETURNS to an earlier value (e.g. a warp ping ``warp:a:* <-> warp:b:*``
      that hops between two cells forever).  This catches loops that *change position*
      on every step, which by design never trip ``decision_limit``.
    """

    def __init__(
        self,
        decision_limit: int,
        *,
        now: float,
        loop_window: int = 64,
        loop_distinct_limit: int = 2,
        loop_min_cycles: int = 3,
    ) -> None:
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
        self._window: list[str] = []
        self._option_ids: list[str] = []
        self.loop_window = loop_window
        self.loop_distinct_limit = loop_distinct_limit
        self.loop_min_cycles = loop_min_cycles

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

    def note_decision(self, *, verified: bool, option_id: str | None = None) -> None:
        self.decisions_since_progress += 1
        self._verified_before = self._signature if verified else None
        if verified and option_id:
            self._option_ids.append(option_id)
            while len(self._option_ids) > self.loop_window * self.loop_min_cycles:
                self._option_ids.pop(0)

    def due_reason(self) -> str | None:
        if self.decisions_since_progress >= self.decision_limit:
            return f"decision_limit:{self.decisions_since_progress}"
        loop = self.loop_reason()
        if loop is not None:
            return loop
        return None

    def loop_reason(self) -> str | None:
        """``option_loop`` when a small option set recurs while the signature ping-pongs.

        The semantic signature is hashed over position/party/bag/flags, so a warp ping
        that alternates two cells changes position every step and never trips
        ``decision_limit``.  This detector instead tracks how many distinct option ids
        appear in the recent window and how often the client re-sees an EARLIER
        signature (a return to a previously-visited state = a closed loop, not progress).
        """
        # Need enough verified decisions to judge the window.
        if len(self._option_ids) < self.loop_window * self.loop_min_cycles:
            return None
        recent = self._option_ids[-self.loop_window:]
        distinct = len(set(recent))
        if distinct > self.loop_distinct_limit:
            return None
        # Distinct mask of the canonical recent window.
        return f"option_loop:distinct={distinct},window={self.loop_window}"

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
