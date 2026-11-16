"""Which field scene does this snapshot show? One classifier, thin detectors on top.

Follows the situation table of ``the runtime contracts`` section 3 for the overworld rows, and
the trap list below it:

=====================  ===========================================================================
Situation              Evidence (all of it)
=====================  ===========================================================================
controllable overworld ``callback2 = CB2_Overworld``, script ``shutdown`` / mode ``stopped``,
                       ``controls_locked`` False, avatar not mid-step, no fade, no UI task, no
                       active
                       text printer
stepping / turning     the same, but ``field.moving`` (tile transition) or ``field.turning``:
                       claimed as ``field.free`` with ``busy=True`` (wait, never press)
script running         locked or running script, nothing on screen: NOT ours (the dialog family
claims
                       ``field.scripted_movement`` / ``field.map_transition`` as BUSY); the one
                       refinement made here is ``field.trainer_vision`` (a trainer has the player in
                       its line of sight), also BUSY, more specific
transition             map-load callbacks, ``Task_WarpAndLoadMap``, a locked fade: dialog family
=====================  ===========================================================================

Traps avoided (m21 section 3): stale ``sMenu`` / ``sYesNoWindowId`` (never read here), stale
``gBattle*`` (``in_battle`` is the gate), stale script ``native`` pointer (only looked at through
``script.running``), stale avatar block (``running_state`` alone is never used; ``moving`` looks
at the
tile transition and is ``None`` outside field callbacks), ``PLAYER_AVATAR_FLAG_CONTROLLABLE``
(not "the
player may act"), leftover/background tasks (only tasks with a role in ``TASK_KINDS`` count, the
unnamed background and map-arrival groups are ignored).

Everything is a pure function of the snapshot: no press, no wait, no emulator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pokegent.games.gen3.firered.maps.map_behaviors import behavior_info
from pokegent.games.gen3.firered.maps.map_schema import MapObject, MapState
from pokegent.games.gen3.firered.reader import ui_symbols as U
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.games.gen3.firered.reader.ui_schemas import UiEvidence

FREE = "field.free"
CURRENT = "field.current"
DARKNESS = "field.darkness"
TRAINER_VISION = "field.trainer_vision"

#: the only callback in which the field reads the d-pad (``CB2_OverworldBasic`` runs the field
# during
#: the wild-encounter intro: the screen is alive, the player is not controllable)
CONTROLLABLE_CALLBACK = "CB2_Overworld"
BASIC_CALLBACK = "CB2_OverworldBasic"

_QUEST_LOG_PLAYBACK = (2, 3)

#: native script waits that belong to a text box (src/scrcmd.c, src/field_message_box.c)
MESSAGE_BOX_WAITS = frozenset({"WaitForAorBPress", "IsFieldMessageBoxHidden"})


@dataclass(frozen=True)
class Verdict:
    scene_id: str
    confidence: float
    busy: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)


def _snapshot_map_keys(snapshot) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    """Read player and header identities during a map transition; either may lag."""
    player = getattr(snapshot, "player", None)
    player_key = (player.map_group, player.map_num) if player is not None else None
    state = getattr(snapshot, "map", None)
    identity_key = (state.identity.group, state.identity.number) if state is not None else None
    return player_key, identity_key


# -- small views -----------------------------------------------------------------------------


def _task_kinds(ui: UiEvidence) -> tuple[str, ...]:
    """Roles of the active tasks that define a UI (named kinds). Unnamed tasks are background."""
    kinds = []
    for task in ui.tasks.active:
        name = task.func.name
        kind = U.TASK_KINDS.get(name) if name else None
        if kind is not None:
            kinds.append(kind)
    return tuple(kinds)


def _callback_name(ui: UiEvidence) -> str | None:
    cb = ui.main.callback2
    return cb.name if cb is not None else None


def _sections_valid(ui: UiEvidence) -> bool:
    return bool(
        ui.valid
        and ui.main.valid
        and ui.tasks.valid
        and ui.text.valid
        and ui.script.valid
        and ui.menu.valid
        and ui.field.valid
    )


def trainer_in_sight(state: MapState) -> tuple[int, ...]:
    """Local ids of trainer objects that have the player in their line of sight right now.

    Same row/column, distance within the trainer's sight range, facing the player (type 1) or any
    facing (type 2 sees all directions), and no wall in between (grid collision). Approximate on
    purpose: it only labels a wait (``field.trainer_vision``), it never gates an input.
    """
    player = state.player
    if player is None:
        return ()
    px, py = player.x, player.y
    out: list[int] = []
    for obj in state.objects:
        if not _is_active_trainer(obj):
            continue
        dx, dy = px - obj.x, py - obj.y
        if (dx != 0 and dy != 0) or (dx == 0 and dy == 0):
            continue
        dist = abs(dx) + abs(dy)
        if dist > max(obj.trainer_range, 1):
            continue
        direction = ("E" if dx > 0 else "W") if dy == 0 else ("S" if dy > 0 else "N")
        facing = obj.live.facing if obj.live is not None else None
        if obj.trainer_type == 1 and facing is not None and facing != direction:
            continue
        if _wall_between(state, (obj.x, obj.y), (px, py)):
            continue
        out.append(obj.local_id)
    return tuple(sorted(out))


def _is_active_trainer(obj: MapObject) -> bool:
    if obj.role != "trainer" or obj.trainer_type not in (1, 2):
        return False
    if obj.live is None or obj.live.invisible:
        return False
    return not (obj.hidden_flag_id and obj.hidden_flag_set)


def _wall_between(state: MapState, a: tuple[int, int], b: tuple[int, int]) -> bool:
    (ax, ay), (bx, by) = a, b
    sx = (bx > ax) - (bx < ax)
    sy = (by > ay) - (by < ay)
    x, y = ax + sx, ay + sy
    while (x, y) != (bx, by):
        if state.collision(x, y):
            return True
        x, y = x + sx, y + sy
    return False


# -- the classifier -----------------------------------------------------------------------------


def classify(snapshot: StateSnapshot) -> Verdict | None:
    """The one field claim for ``snapshot``, or ``None`` (not ours / not provable)."""
    ui = snapshot.ui
    if ui is None or not _sections_valid(ui):
        return None
    if ui.main.in_battle:
        return None  # the gate: nothing here claims during a battle
    cb = _callback_name(ui)
    if cb == BASIC_CALLBACK:
        return Verdict(FREE, 0.8, True, {"phase": "overworld_basic", "callback": cb})
    if cb != CONTROLLABLE_CALLBACK:
        return None  # map-load callbacks, menus, party, bag ...: someone else's screen

    # a UI task, a text printer or a quest-log playback owns the screen: not ours
    if _task_kinds(ui):
        return None
    if ui.text.printers or ui.text.any_active:
        return None
    if ui.menu.quest_log_state in _QUEST_LOG_PLAYBACK:
        return None

    script, fe = ui.script, ui.field
    # a message box that stays up after its text (script native wait on A/B) or is closing: the
    # script IS running and the controls ARE locked, but the game reads A/B right now. That is
    # the dialog family's ``dialog.end``; a BUSY claim here would mark it busy (the registry marks
    # a ready scene busy next to any confident BUSY-class claim) and the runner would wait for a
    # box that only a press can close (soak: talk to a defeated trainer, 14 fixtures).
    # ``script.native`` is only set while the script mode is native, so it cannot be stale.
    if script.native is not None and script.native.name in MESSAGE_BOX_WAITS:
        return None
    if script.running or script.controls_locked:
        # a script (cutscene, trainer approach, scripted walk) holds the controls
        # a trainer approach is a RUNNING script with the controls locked and no fade; a locked
        # fade-in with a trainer standing in line (pre-30fps-cadence) is an ordinary arrival
        seen = trainer_in_sight(snapshot.map) if snapshot.map is not None else ()
        if seen and script.running and script.controls_locked and not fe.fade_active:
            return Verdict(
                TRAINER_VISION,
                0.75,
                True,
                {
                    "callback": cb,
                    "phase": "trainer_approach",
                    "trainers": list(seen),
                    "script": script.status_name,
                },
            )
        return None  # field.scripted_movement / field.map_transition are the dialog family's

    # script shut down, controls free
    base: dict[str, Any] = {"callback": cb, "script": script.status_name}
    if fe.forced_movement and snapshot.map is not None and snapshot.map.player is not None:
        player = snapshot.map.player
        info = behavior_info(snapshot.map.behavior(player.avatar_x, player.avatar_y))
        if info.kind == "current":
            return Verdict(CURRENT, 0.95, True, {**base, "phase": "current", "behavior": info.name})
    if fe.forced_movement:
        return Verdict(FREE, 0.85, True, {**base, "phase": "forced_movement"})
    if fe.fade_active:
        return Verdict(FREE, 0.85, True, {**base, "phase": "fade", "fade": fe.fade_direction})
    if fe.moving or fe.turning:
        return Verdict(FREE, 0.95, True, {**base, "phase": "turning" if fe.turning else "stepping"})
    if snapshot.map is None or snapshot.map.player is None:
        # the map reader fails for a few frames around a warp; nothing can be planned without it
        return Verdict(FREE, 0.8, True, {**base, "phase": "map_unavailable"})
    if not snapshot.map.player.position_consistent:
        return Verdict(FREE, 0.9, True, {**base, "phase": "position_settling"})
    return Verdict(FREE, 0.95, False, {**base, "phase": "controllable"})
