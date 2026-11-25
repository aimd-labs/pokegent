"""The bounded fishing flow: the rod is used from the bag, then the game owns the cast.

Source: pret/pokefirered @ 037335f4 ``src/item_use.c`` (``CanFish`` / ``StartFishing``) and
``src/field_player_avatar.c`` (``Task_Fishing``).  The bite path prints its message in window 0
and then calls ``FishingWildEncounter`` without reading a button; the no-bite and got-away paths
return to a free field.  There is no prompt to parse and no task symbol to guess: the cast is
observed by the reader's existing ``prevent_step`` fact (``Fishing1``) and resolves at the battle
family's ``battle.*`` claim or at a settled ``field.free``/``field.darkness``.
"""

from __future__ import annotations

from typing import Any, Generator

from pokegent.actions._exec.models import WaitUntil

BATTLE_PREFIX = "battle."
FREE_SCENES = frozenset({"field.free", "field.darkness"})

#: ``Task_Fishing`` dot rounds are 20 frames per dot; the terminal wait spans all of them.
CAST_FRAMES = 300
TERMINAL_FRAMES = 2400
#: A bite unlocks the controls and starts the wild battle in the same step.  Requiring the free
#: field to stay settled rejects that one-frame window; the no-bite/got-away path is stable.
FREE_SETTLE_CHECKS = 60


def _ui(obs: Any) -> Any:
    ui = getattr(getattr(obs, "snapshot", None), "ui", None)
    return ui if ui is not None and getattr(ui, "valid", False) else None


def cast_started(obs: Any) -> bool:
    """``Task_Fishing`` step 1: the avatar refuses ordinary steps while the rod task runs."""
    ui = _ui(obs)
    field = None if ui is None else ui.field
    return bool(field is not None and field.valid is True and field.prevent_step is True)


def free_field_settled(obs: Any) -> bool:
    """A readable field with no owned task, no text box, and no locked controls."""
    if getattr(getattr(obs, "scene", None), "scene_id", None) not in FREE_SCENES:
        return False
    ui = _ui(obs)
    if ui is None or ui.field is None or ui.script is None or ui.text is None:
        return False
    if not (ui.field.valid is True and ui.script.valid is True and ui.text.valid is True):
        return False
    return bool(
        ui.field.prevent_step is False
        and ui.script.controls_locked is False
        and ui.text.any_active is False
        and ui.text.waiting_for_button is False
    )


def battle_started(obs: Any) -> bool:
    return getattr(getattr(obs, "scene", None), "scene_id", "").startswith(BATTLE_PREFIX)


class TerminalTracker:
    """The fishing terminal after the cast: a battle, or a settled no-battle field."""

    def __init__(self) -> None:
        self._free_checks = 0
        self.outcome: str | None = None

    def __call__(self, obs: Any) -> bool:
        if battle_started(obs):
            self.outcome = "battle"
            return True
        if free_field_settled(obs):
            self._free_checks += 1
            if self._free_checks >= FREE_SETTLE_CHECKS:
                self.outcome = "no_battle"
                return True
        else:
            self._free_checks = 0
        return False


def drive(obs: Any) -> Generator[WaitUntil, Any, str]:
    """Wait for the cast, then for a terminal; never yields an input step."""
    if not cast_started(obs):
        obs = yield WaitUntil(
            cast_started,
            max_frames=CAST_FRAMES,
            failure_reason="fishing cast was not observed (prevent_step never set)",
        )
    tracker = TerminalTracker()
    obs = yield WaitUntil(
        tracker,
        max_frames=TERMINAL_FRAMES,
        failure_reason="fishing attempt neither entered battle nor returned to a settled field",
    )
    if tracker.outcome is None:
        if battle_started(obs):
            tracker.outcome = "battle"
        elif free_field_settled(obs):
            tracker.outcome = "no_battle"
    return tracker.outcome or "unknown"


def resolved(ctx: Any) -> bool:
    """Postcondition: the action ended in a battle or a settled free field after a real attempt.

    The option is only legal while facing fishable water in ``field.free``; the macro can only
    reach its terminal after ``cast_started`` was observed.  A no-battle also has to have spent at
    least the settle window, so a pre-existing free snapshot cannot satisfy it.
    """
    scene = getattr(getattr(ctx, "scene", None), "scene_id", "")
    if scene.startswith(BATTLE_PREFIX):
        return True
    if not free_field_settled(_Obs(ctx)):
        return False
    before = getattr(ctx, "before", None)
    after = getattr(ctx, "after", None)
    if before is None or after is None:
        return False
    elapsed = getattr(after, "emulator_frame", 0) - getattr(before, "emulator_frame", 0)
    return elapsed >= FREE_SETTLE_CHECKS * 2


class _Obs:
    def __init__(self, ctx: Any) -> None:
        self.snapshot = getattr(ctx, "after", None)
        self.scene = getattr(ctx, "scene", None)
