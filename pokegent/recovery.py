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
    * ``option_loop``     — a closed CLIENT-side loop: over the FULL claimed
      repetition interval only ``<= loop_distinct_limit`` distinct option ids
      recur, AND the semantic signature RETURNS to an earlier value (a backdrop
      e.g. a warp ping ``warp:a:* <-> warp:b:*`` that hops between two cells
      forever).  This catches loops that *change position* on every step, which
      by design never trip ``decision_limit``, while a monotone grind that
      changes party state each step while reusing one option stays clean.
    """

    def __init__(
        self,
        decision_limit: int,
        *,
        now: float,
        loop_window: int = 64,
        loop_distinct_limit: int = 2,
        loop_signature_limit: int = 4,
        loop_min_cycles: int = 3,
    ) -> None:
        for name, value in (
            ("decision_limit", decision_limit),
            ("loop_window", loop_window),
            ("loop_distinct_limit", loop_distinct_limit),
            ("loop_signature_limit", loop_signature_limit),
            ("loop_min_cycles", loop_min_cycles),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be an integer >= 1")
        self.decision_limit = decision_limit
        self.last_progress_at = now
        self.last_progress_iteration = 0
        self.decisions_since_progress = 0
        self._signature: str | None = None
        self._verified_before: str | None = None
        self.loop_window = loop_window
        self.loop_distinct_limit = loop_distinct_limit
        self.loop_signature_limit = loop_signature_limit
        self.loop_min_cycles = loop_min_cycles
        self._capacity = loop_window * loop_min_cycles
        # Bounded (option_id, signature) pairs covering the FULL claimed interval.
        self._window: list[tuple[str, str]] = []

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
        if verified and option_id and self._signature is not None:
            self._window.append((option_id, self._signature))
            if len(self._window) > self._capacity:
                del self._window[0]

    def due_reason(self) -> str | None:
        if self.decisions_since_progress >= self.decision_limit:
            return f"decision_limit:{self.decisions_since_progress}"
        loop = self.loop_reason()
        if loop is not None:
            return loop
        return None

    def loop_reason(self) -> str | None:
        """``option_loop`` when the CURRENT tail is a closed loop of a few states/options.

        The semantic signature is hashed over position/party/bag/flags, so a warp ping
        that alternates two cells changes position every step and never trips
        ``decision_limit``.  This detector distinguishes a closed loop from durable
        progress using three requirements over the FULL claimed interval
        (``loop_window * loop_min_cycles``) PLUS a current-tail recurrence band:

        * the distinct **option** set is ``<= loop_distinct_limit``;
        * the CURRENT tail enters at most ``loop_signature_limit`` states that were not
          already seen earlier in the interval, and its newest decisions reopen no such
          state — a settled two-exit cycle may walk several tiles per side (more than four
          total states) but opens no new frontier, while a run that is still escaping must
          not be stopped on old recurrences; and
        * the recent tail holds ``>= loop_min_cycles`` GENUINE returns after LEAVING a
          state (a non-adjacent re-entry into a signature already seen earlier in the
          same tail), so ``0,0,1,1,0,0,1,1`` (revisited states) trips but monotone
          ``0,0,1,1,2,2,...`` (adjacent duplicates only, advancing) does not.

        Unrelated earlier history never leaks: the bound is the full interval (a
        ``128 distinct + 64 alternating`` sequence yields ``>= 130`` distinct option
        ids), and a drop-off into steady progress clears the recurrence band.
        """
        if len(self._window) != self._capacity:
            return None  # need the FULL claimed interval, not a tail
        distinct_options = {opt for opt, _ in self._window}
        if len(distinct_options) > self.loop_distinct_limit:
            return None
        distinct_sigs = {sig for _, sig in self._window}

        # Recurrence on the CURRENT tail: count genuine returns-after-leaving.
        # A return is a non-adjacent re-entry: sig[i] when i>0 differs from sig[i-1]
        # AND sig[i] was already present before position i in the tail.
        band_start = max(0, len(self._window) - self.loop_window)
        band = self._window[band_start:]
        earlier_sigs = {sig for _, sig in self._window[:band_start]}
        fresh_sigs = {sig for _, sig in band if sig not in earlier_sigs}
        if len(fresh_sigs) > self.loop_signature_limit:
            return None  # the tail is still opening unseen states -> not a closed loop
        tip = band[-min(len(band), self.loop_signature_limit):]
        if any(sig not in earlier_sigs for _, sig in tip):
            # The newest decisions entered a state not seen earlier in the interval:
            # old recurrences alone must not stop a run that is still escaping.
            return None
        seen = set()
        returns = 0
        for idx, (_, sig) in enumerate(band):
            if idx > 0 and sig != band[idx - 1][1] and sig in seen:
                returns += 1
            else:
                seen.add(sig)
        if returns < self.loop_min_cycles:
            return None
        return (f"option_loop:options={len(distinct_options)},"
                f"sigs={len(distinct_sigs)},returns={returns},window={self._capacity}")

    def reset_window(self, *, now: float, iteration: int, clear_loop: bool = False) -> None:
        self.last_progress_at = now
        self.last_progress_iteration = iteration
        self.decisions_since_progress = 0
        self._verified_before = None
        # Loop evidence is cleared ONLY on durable-progress rearm / recovery arm, never on
        # a positional ping (which changes position yet is still a closed loop, issue #35).
        if clear_loop:
            self._window.clear()

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
