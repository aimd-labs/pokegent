"""Frozen error-code vocabulary (docs/contracts.md section 7).

One enum for every layer, so an ``ActionResult``, an event and a replay all name a failure the
same way. The value equals the member name. A code can be added in a minor revision; an existing
code is never renamed or re-purposed.
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    # -- perception / revalidation: the world is not what the decision assumed -------------
    E_SNAPSHOT_INVALID = "E_SNAPSHOT_INVALID"  # health.valid is False; no data may be used
    E_STALE_SNAPSHOT = "E_STALE_SNAPSHOT"  # fresh snapshot is older than the frame's
    E_SCENE_CHANGED = "E_SCENE_CHANGED"  # fresh scene id differs from the frame's
    E_FINGERPRINT_CHANGED = "E_FINGERPRINT_CHANGED"  # same scene, decision-relevant state differs
    E_SCENE_BUSY = "E_SCENE_BUSY"  # game is not processing input right now
    E_SCENE_UNKNOWN = "E_SCENE_UNKNOWN"
    E_SCENE_UNSUPPORTED = "E_SCENE_UNSUPPORTED"
    # -- option / choice -------------------------------------------------------------------
    E_UNKNOWN_OPTION = "E_UNKNOWN_OPTION"  # option id is not in the frame at all
    E_NOT_CHOOSEABLE = "E_NOT_CHOOSEABLE"  # listed for diagnostics only (UNAVAILABLE/UNKNOWN/...)
    E_OPTION_GONE = "E_OPTION_GONE"  # not offered by the rebuilt frame any more
    E_OPTION_CHANGED = "E_OPTION_CHANGED"  # offered, but executor_ref/args differ
    E_CHOICE_NOT_IN_FRAME = "E_CHOICE_NOT_IN_FRAME"  # decider returned an id outside the frame
    E_CHOICE_MALFORMED = "E_CHOICE_MALFORMED"  # NaN probability, unknown key, bad confidence
    E_STALE_DECISION = "E_STALE_DECISION"  # answer belongs to an older frame generation
    # -- registries ------------------------------------------------------------------------
    E_UNKNOWN_EXECUTOR_REF = "E_UNKNOWN_EXECUTOR_REF"
    E_UNKNOWN_POSTCONDITION = "E_UNKNOWN_POSTCONDITION"
    E_BAD_ARGS = "E_BAD_ARGS"
    # -- execution -------------------------------------------------------------------------
    E_INPUT_REJECTED = "E_INPUT_REJECTED"  # actor refused the button / chord (nothing pressed)
    E_INTERRUPTED_SCENE = "E_INTERRUPTED_SCENE"  # macro met an unexpected scene at a boundary
    E_WAIT_TIMEOUT_FRAMES = "E_WAIT_TIMEOUT_FRAMES"
    E_WAIT_TIMEOUT_WALL = "E_WAIT_TIMEOUT_WALL"
    E_BUDGET_EXCEEDED = "E_BUDGET_EXCEEDED"  # input/frame budget of one action exhausted
    E_POSTCONDITION_NOT_MET = "E_POSTCONDITION_NOT_MET"
    E_EMULATOR = "E_EMULATOR"  # actor stopped / timed out / raised
    # -- decider ---------------------------------------------------------------------------
    E_DECIDER_TIMEOUT = "E_DECIDER_TIMEOUT"
    E_DECIDER_FAILED = "E_DECIDER_FAILED"
    E_PAYLOAD_LEAK = "E_PAYLOAD_LEAK"  # the System One payload would expose internals
    # -- runner ----------------------------------------------------------------------------
    E_NO_BUILDER = "E_NO_BUILDER"
    E_NO_LEGAL_OPTION = "E_NO_LEGAL_OPTION"
    E_NO_PROGRESS = "E_NO_PROGRESS"
    E_BUSY_STUCK = "E_BUSY_STUCK"
