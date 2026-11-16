"""Option builder of the system_flow family: every legal option for the scenes it claims.

Dispatch and the pieces all scenes share live here; the scenes themselves are in
:mod:`.build_endgame`, :mod:`.build_evolution` and :mod:`.build_pc`. The builder re-derives the
verdict
from the snapshot (the same classifier the detectors use), so a frame can never offer an option
for a
screen the evidence does not show. It never presses, never calls a model and never assigns frame
identity.

Rules this family's builders keep (each has a test):

* A is offered only where the game is provably blocked on it (``Task_Hof_ExitOnKeyPressed``, a
waiting
  printer, a title/menu task that reads buttons); everything else is a bounded WAIT or nothing.
* B is offered in exactly four places: the evolution cancel window, the summary screen of a
learn-move
  (give up), the PC screens, and a switched-on foreign menu. Never anywhere else.
* NEW GAME is never an option. When the main menu has no save, the only entry is shown as
UNAVAILABLE
  with the reason (the runner then escalates ``NO_LEGAL_OPTION``).
* Answers that are strategy (evolve or not, learn or keep) are options with facts and an overridable
  default (:class:`~pokegent.actions.menu.system_flow.policy.SystemFlowOptions`), never a fixed
  choice.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pokegent.actions.menu.system_flow import build_endgame, build_evolution, build_pc
from pokegent.actions.menu.system_flow import evidence as E
from pokegent.actions.menu.system_flow import foreign as F
from pokegent.actions.menu.system_flow import refs as R
from pokegent.actions.menu.system_flow.loops import QuestionCounter
from pokegent.actions.menu.system_flow.policy import SystemFlowOptions
from pokegent.options.models import Legality, Option, Risk, fingerprint
from pokegent.options.registry import BuildContext, FrameDraft

Handler = Callable[[BuildContext, Any, E.Verdict, SystemFlowOptions], FrameDraft]


def missing(scene: str, reason: str) -> FrameDraft:
    option = Option(
        id="pause_and_escalate",
        label="Pause and escalate",
        description=f"Nothing can be offered: {reason}.",
        legality=Legality.UNKNOWN,
        risk=Risk.NORMAL,
        facts={"reason": reason},
    )
    return FrameDraft(
        options=(option,),
        state_fingerprint=fingerprint(scene, missing=reason),
        context={"summary": "The screen state could not be established."},
    )


class SystemFlowBuilder:
    def __init__(self, options: SystemFlowOptions | None = None) -> None:
        self.options = options or SystemFlowOptions()
        self._foreign = frozenset(self.options.foreign_menus)
        #: how often each learn-move question was entered in this learn session (loops.py)
        self.loops = QuestionCounter()
        self._handlers: dict[str, Handler] = {
            R.HALL_OF_FAME: build_endgame.hall_of_fame,
            R.CREDITS: build_endgame.credits,
            R.LOGO: build_endgame.logo,
            R.TITLE_PRESS_START: build_endgame.title,
            R.TITLE_MAIN: build_endgame.main_menu,
            R.TITLE_CONTINUE: build_endgame.main_menu,
            R.NAMING: build_endgame.naming,
            R.PROFESSOR_TEXT: build_endgame.professor_text,
            R.GENDER: build_endgame.gender,
            R.RIVAL_NAME: build_endgame.rival_name,
            R.SCRIPT_MOVEMENT: build_endgame.script_movement,
            R.EVO_START: build_evolution.start,
            R.EVO_ANIMATION: build_evolution.animation,
            R.EVO_DONE: build_evolution.done,
            R.EVO_MOVE: build_evolution.move_learning,
            R.PC_MAIN: build_pc.pc_main,
            R.MENU_OPEN: build_pc.foreign_menu,
            **{scene: build_pc.foreign_menu for scene in F.FOREIGN_MENUS},
        }

    def build(self, ctx: BuildContext) -> FrameDraft:
        snapshot, scene = ctx.snapshot, ctx.scene.scene_id
        if scene == R.QUEST_LOG:  # detected by the dialog family; only its options are ours
            self.loops.reset()
            return self._with_clear(
                snapshot, build_endgame.quest_log(ctx, snapshot, None, self.options)
            )
        verdict = E.classify(snapshot, self._foreign)
        if verdict is None or verdict.scene_id != scene:
            return missing(scene, "the evidence no longer shows this screen")
        handler = self._handlers.get(scene)
        if handler is None:
            return missing(scene, f"the system_flow family has no builder for {scene}")
        if scene == R.EVO_MOVE:
            draft = build_evolution.move_learning(ctx, snapshot, verdict, self.options, self.loops)
        else:
            self.loops.reset()  # any other screen ends the learn session
            draft = handler(ctx, snapshot, verdict, self.options)
        return self._with_clear(snapshot, draft)

    @staticmethod
    def _with_clear(snapshot, draft: FrameDraft) -> FrameDraft:
        clear = E.game_clear(snapshot)
        if clear is None:
            return draft
        return FrameDraft(
            options=draft.options,
            state_fingerprint=draft.state_fingerprint,
            context={**draft.context, "game_clear": clear},
            deadline_ms=draft.deadline_ms,
            all_options=draft.all_options,
        )
