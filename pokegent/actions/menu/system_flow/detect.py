"""Detectors of the system_flow family: one per scene id, all views of :func:`evidence.classify`.

Priority classes (spec 6.0): the learn-move/evolution scenes and the PC are MODAL (they own the
screen); the endgame screens (Hall of Fame, credits, boot/intro, title, main menu) are MENU class.
A detector never presses, waits or reads the emulator.

The reboot after the credits (the runtime contracts): a soft
reset clears the save-block pointers, so the core reader may be invalid while the logo, title and
main menu are on screen. Their detectors need only the UI block and declare ``accepts_invalid_core``
(``perception.scenes``): the registry runs exactly those on an invalid core snapshot with valid UI
evidence. Every other scene of the family (Hall of Fame, credits, evolution, PC) still needs a
valid core.
"""

from __future__ import annotations

from dataclasses import dataclass

from pokegent.actions.menu.system_flow import evidence as E
from pokegent.actions.menu.system_flow import refs as R
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.state.scenes import PriorityClass, SceneResult


@dataclass(frozen=True)
class ClaimSpec:
    scene_id: str
    priority: PriorityClass
    specificity: int
    #: the scene is provable from the UI block alone, so it may be claimed while the core is invalid
    accepts_invalid_core: bool = False
    #: explicit ``--new-game`` may use the scene while the mapped callback lacks an avatar
    accepts_pre_game_core: bool = False


CLAIMS: tuple[ClaimSpec, ...] = (
    ClaimSpec(R.HALL_OF_FAME, PriorityClass.MENU, 20),
    ClaimSpec(R.CREDITS, PriorityClass.MENU, 20),
    ClaimSpec(R.LOGO, PriorityClass.MENU, 20, True, True),
    ClaimSpec(R.TITLE_PRESS_START, PriorityClass.MENU, 20, True, True),
    ClaimSpec(R.TITLE_MAIN, PriorityClass.MENU, 20, True, True),
    ClaimSpec(R.TITLE_CONTINUE, PriorityClass.MENU, 20, True, True),
    ClaimSpec(R.NAMING, PriorityClass.MODAL, 20, False, True),
    ClaimSpec(R.PROFESSOR_TEXT, PriorityClass.MODAL, 20, False, True),
    ClaimSpec(R.GENDER, PriorityClass.MODAL, 20, False, True),
    ClaimSpec(R.RIVAL_NAME, PriorityClass.MODAL, 20, False, True),
    ClaimSpec(R.SCRIPT_MOVEMENT, PriorityClass.MODAL, 20, False, True),
    ClaimSpec(R.EVO_START, PriorityClass.MODAL, 20),
    ClaimSpec(R.EVO_ANIMATION, PriorityClass.MODAL, 20),
    ClaimSpec(R.EVO_DONE, PriorityClass.MODAL, 20),
    ClaimSpec(R.EVO_MOVE, PriorityClass.MODAL, 20),
    ClaimSpec(R.PC_MAIN, PriorityClass.MODAL, 20),
    ClaimSpec(R.MENU_OPEN, PriorityClass.MENU, 1),
    ClaimSpec(R.POKEDEX_LIST, PriorityClass.MENU, 30),
    ClaimSpec(R.PARTY_SUMMARY, PriorityClass.MENU, 30),
    ClaimSpec(R.TRAINER_CARD, PriorityClass.MENU, 30),
    ClaimSpec(R.OPTIONS, PriorityClass.MENU, 30),
)

#: every scene id the detectors of this family can claim
CLAIMED_SCENES = frozenset(c.scene_id for c in CLAIMS)


class SystemFlowDetector:
    """Claims ``scene_id`` iff the family classifier says so. Satisfies ``SceneDetector``."""

    def __init__(self, spec: ClaimSpec, foreign: frozenset[str] = frozenset()) -> None:
        self._foreign = foreign
        self.name = f"system-flow:{spec.scene_id}"
        self.priority = spec.priority
        self.specificity = spec.specificity
        self.accepts_invalid_core = spec.accepts_invalid_core
        self.accepts_pre_game_core = spec.accepts_pre_game_core
        self.scene_id = spec.scene_id
        self.claimed_scenes = frozenset({spec.scene_id})

    def detect(self, snapshot: StateSnapshot) -> SceneResult | None:
        verdict = E.classify(snapshot, self._foreign)
        if verdict is None or verdict.scene_id != self.scene_id:
            return None
        return SceneResult(
            scene_id=verdict.scene_id,
            confidence=verdict.confidence,
            busy=verdict.busy,
            evidence=verdict.evidence,
        )


def build_detectors(foreign: frozenset[str] = frozenset()) -> tuple[SystemFlowDetector, ...]:
    """One detector per claimed scene id, plus one per switched-on foreign menu scene."""
    extra = tuple(ClaimSpec(s, PriorityClass.MENU, 10) for s in sorted(foreign))
    return tuple(SystemFlowDetector(spec, foreign) for spec in (*CLAIMS, *extra))
