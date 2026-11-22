"""RAM/task based detection for the regular START and field-party menus."""

from __future__ import annotations

from typing import Any

from pokegent.actions.menu.dialog.evidence import (
    party_exit_yes_no_open,
    party_learning_yes_no_task,
)
from pokegent.actions.menu.start_party import refs as R
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.state.scenes import PriorityClass, SceneResult


def _tasks(ui: Any) -> frozenset[str]:
    return frozenset(task.func.name for task in ui.tasks.active if task.func.name is not None)


class StartPartyDetector:
    name = "start_party"
    priority = PriorityClass.MENU
    specificity = 40
    claimed_scenes = R.CLAIMED_SCENES

    def detect(self, snapshot: StateSnapshot) -> SceneResult | None:
        ui = snapshot.ui
        if ui is None or not ui.valid or ui.main.in_battle is not False:
            return None
        if party_exit_yes_no_open(ui) or party_learning_yes_no_task(ui) is not None:
            return None  # The modal dialog owns the input; do not add a busy party claim.
        task_names = _tasks(ui)
        start = ui.menu.start_menu
        if start is not None and start.task_active is True and ui.menu_type == "start_menu":
            fade = ui.field.fade_active is True
            return SceneResult(
                scene_id=R.START,
                confidence=0.98,
                busy=fade,
                evidence={
                    "phase": "fade" if fade else "choose",
                    "cursor": start.cursor_pos,
                    "entries": start.num_items,
                },
            )

        party = ui.menu.party
        if party is None or party.active_hint is not True or party.menu_type != 0:
            return None
        fade = ui.field.fade_active is True
        choose = "Task_HandleChooseMonInput" in task_names
        submenu = "Task_HandleSelectionMenuInput" in task_names
        building = "Task_TryCreateSelectionWindow" in task_names
        if choose and party.action_name == "switch":
            scene, phase = R.PARTY_REORDER, "choose_swap_target"
        elif submenu:
            ids = frozenset(party.action_ids)
            if ids and ids <= frozenset({4, 5, 9}):
                scene = R.PARTY_HELD_ITEM
            elif ids and ids <= frozenset({7, 8, 9}):
                # Mail is deliberately left to a later party increment; do not claim it as
                # covered merely because the common action decoder can label it.
                return None
            else:
                scene = R.PARTY_ACTION
            phase = "choose_action"
        elif building:
            scene, phase = R.PARTY_ACTION, "build_action_menu"
        elif choose:
            scene, phase = R.PARTY_LIST, "choose_mon"
        else:
            scene, phase = R.PARTY_LIST, "load_party"
        busy = fade or building or not (choose or submenu)
        return SceneResult(
            scene_id=scene,
            confidence=0.98 if choose or submenu else 0.9,
            busy=busy,
            evidence={
                "phase": "fade" if fade else phase,
                "slot": party.slot_id,
                "party_action": party.action_name,
                "action_count": len(party.action_ids),
            },
        )


def build_detectors() -> tuple[StartPartyDetector, ...]:
    return (StartPartyDetector(),)
