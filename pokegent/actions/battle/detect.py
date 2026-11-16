"""Battle scene detection (spec 6.0 item 3: "battle only after checking the ACTIVE controller
phase").

One detector, :class:`BattleDetector`, claims exactly one ``battle.*`` scene id (or stays silent)
per
snapshot from the UI evidence (``snapshot.ui``) and, where the snapshot carries one, the battle
block
(:class:`~pokegent.actions.battle.reader.BattleSnapshot`). It never presses and never reads
the
emulator.

The decision table (all gated on ``gMain.inBattle`` except the first row; stale battle RAM is never
evidence, m21 trap 3):

====================================  ===================================================  =====
evidence                              scene                                                ready
====================================  ===================================================  =====
callback2 = CB2_InitBattle or         ``battle.entering`` (inBattle still clear)           busy
  CB2_HandleStartBattle, inBattle
  clear
callback2 = battle init               ``battle.entering``                                  busy
callback2 = CB2_BattleMain
  controller = HandleInputChooseAction  ``battle.action``                                 yes
  controller = HandleInputChooseMove    ``battle.moves_ui``                                yes
  controller = HandleInputChooseTarget  ``battle.choose_target``                           yes
  a battle printer waits for A/B        ``battle.message`` (``battle.ended`` once the        yes
                                        outcome is set)
  script opcode yesnobox, state 1       ``battle.switch_offer`` / ``battle.faint`` (by text) yes
  script opcode yesnoboxlearnmove ...   ``battle.learn_move``                                yes
  script opcode drawlvlupbox, 6 or 8    ``battle.level_up``                                  yes
  caught-mon Pokedex task state 11      ``battle.message`` (registration page)                yes
  script opcode trygivecaughtmonnick    ``battle.ended`` (nickname prompt: decline)          yes
  text printing                         ``battle.message``                                   busy
  anything else                         ``battle.entering`` / ``battle.faint`` /             busy
                                        ``battle.ended`` / ``battle.animation``
party screen from a battle
  Task_HandleChooseMonInput, no fade    ``battle.forced_switch`` (send_out) / ``battle.party``  yes
  selection submenu                     same scene, phase ``submenu``                        yes
  anything else                         same scene                                           busy
bag opened from a battle
  Task_BagMenu_HandleInput, no fade     ``battle.bag``                                       yes
====================================  ===================================================  =====

Special local controllers (double, Safari, ghost, and scripted tutorial) are claimed as their own
scene so the adapter can expose the controller's real question; link play remains unsupported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pokegent.actions.battle import state as S
from pokegent.actions.battle.reader import battle_of
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.games.gen3.firered.reader.ui_schemas import UiEvidence
from pokegent.state.scenes import PriorityClass, SceneResult

#: callback2 words seen only while a battle is starting / re-showing after a menu. Provenance:
#: the runtime contracts section 3 (battle start) and the live traces recorded in
#: the runtime contracts (the three `Reshow` hops after a party/bag screen).
#:
#: The two start-up callbacks are named in the profile (`CB2_InitBattle`, `CB2_HandleStartBattle`,
#: src/battle_main.c; evidence in `references/rom_profiles.yaml`). The trainer/wild intro walks
#: ``CB2_OverworldBasic`` (transition swirl, a field claim) -> ``CB2_InitBattle`` (about 8
#: frames) ->
#: ``CB2_HandleStartBattle`` (about 45 frames) -> ``CB2_BattleMain``, and ``gMain.inBattle`` is only
#: set INSIDE ``CB2_HandleStartBattle`` (live: frame 262 of 44 frames), so the first two stretches
#: have ``in_battle`` false and were ``scene.unknown`` (25-29 iterations of every league run).
CB2_INIT_BATTLE = 0x0800FD9C
CB2_BATTLE_INIT = 0x08010508  # CB2_HandleStartBattle (the name this module used before M2b-fix)
CB2_BATTLE_START = frozenset({CB2_INIT_BATTLE, CB2_BATTLE_INIT})
#: ``0x08030AEC`` is the hand-over after USE in the bag: two frames between ``CB2_BagMenuRun`` and
#: ``CB2_BattleMain`` (``inBattle`` set, no task, no menu), seen at the boundary of every item use
#: once the press took a frame longer (the runtime contracts row 12)
CB2_RESHOW = frozenset({0x08030ADC, 0x08030AEC, 0x08077764, 0x080777E8})  # party/bag -> battle
CB2_BAG_SETUP = frozenset({0x08107EB8, 0x08107F10})  # bag opening

#: gBattleMainFunc values below the first turn-selection function are the battle intro sequence
#: (battle_main.c ROM order: intro functions precede HandleTurnActionSelectionState = 0x08014041).
MAIN_FUNC_TURN_LOOP = 0x08014041
MAIN_FUNC_INTRO_LOW = 0x08010000

#: every scene id :class:`BattleDetector` can claim (ownership table, tests keep it honest: every
#: ``"battle.*"`` literal in this module and every claim over the corpus is a member)
CLAIMED_SCENES = frozenset(
    {
        "battle.entering",
        "battle.animation",
        "battle.action",
        "battle.moves_ui",
        "battle.choose_target",
        "battle.double_target",
        "battle.message",
        "battle.ended",
        "battle.faint",
        "battle.switch_offer",
        "battle.learn_move",
        "battle.level_up",
        "battle.party",
        "battle.forced_switch",
        "battle.item_target",
        "battle.bag",
        "battle.double_turn",
        "battle.safari",
        "battle.ghost",
        "battle.tutorial_scripted",
        "battle.link",
    }
)

#: catalog scenes this family does not detect on purpose: scene -> where its options/facts live
FOLDED_SCENES = {
    "battle.capture": "ball:* options of battle.action [ExecutorSpec:battle.throw_ball]",
    "battle.escape": "escape option of battle.action [ExecutorSpec:battle.attempt_escape]",
    "battle.command_ui": (
        "FIGHT/BAG/POKEMON/RUN live inside the battle.action macros [ExecutorSpec:battle.use_move]"
    ),
    "battle.trainer": (
        "fact context['battle'] = trainer, not a scene "
        "[pokegent.actions.battle.builder:BattleBuilder.build]"
    ),
    "battle.wild": (
        "fact context['battle'] = wild, not a scene "
        "[pokegent.actions.battle.builder:BattleBuilder.build]"
    ),
}
#: catalog scenes the family knows it does not cover
KNOWN_GAPS: dict[str, str] = {}

#: claimed scenes that are only ever BUSY: the runner waits them out, no builder is needed
BUSY_ONLY_SCENES = frozenset({"battle.entering", "battle.animation"})

KIND_SCENE = {
    "double": "battle.double_turn",
    "safari": "battle.safari",
    "ghost": "battle.ghost",
    "tutorial": "battle.tutorial_scripted",
    "link": "battle.link",
    "other_unsupported": "battle.link",
}

PARTY_SCENES = {"send_out": "battle.forced_switch", "use_item": "battle.item_target"}


@dataclass(frozen=True)
class Claim:
    scene_id: str
    confidence: float
    busy: bool
    # : what kind of sub-state the scene is in
    # (``choose``/``submenu``/``list``/``context``/``prompt``...)
    phase: str = ""
    why: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def result(self) -> SceneResult:
        data = {"phase": self.phase, "why": self.why, **self.evidence}
        return SceneResult(
            scene_id=self.scene_id, confidence=self.confidence, busy=self.busy, evidence=data
        )


# -- helpers --------------------------------------------------------------------------------------


def _task_active(ui: UiEvidence, *names: str) -> bool:
    wanted = set(names)
    return any(t.func.name in wanted for t in ui.tasks.active)


def _printer_waiting(ui: UiEvidence) -> bool:
    return any(p.waiting_for_button for p in ui.text.printers)


def _printer_active(ui: UiEvidence) -> bool:
    return bool(ui.text.printers)


def _fade_active(ui: UiEvidence) -> bool:
    return bool(ui.field.fade_active)


def _controller0(ui: UiEvidence):
    if ui.battle is None:
        return None
    for c in ui.battle.controllers:
        if c.battler == 0:
            return c
    return None


def _input_controller(ui: UiEvidence):
    """Return the player controller whose menu is reading buttons right now.

    In a double battle battler 0 can already be parked on PlayerBufferRunCommand while the
    partner battler 2 owns the next action menu.  ``input_battlers`` is derived from the live
    controller function pointers, so it is the authoritative chooser rather than a turn-order
    guess.
    """
    if ui.battle is None:
        return None
    input_battlers = set(ui.battle.input_battlers)
    return next(
        (c for c in ui.battle.controllers if c.battler in input_battlers),
        None,
    )


def yes_no_state(block: S.BattleBlock | None) -> str | None:
    """Which battle yes/no box is awaiting an answer, from the script opcode and its state byte.

    The box is awaiting input only while its command is in state 1 (state 0 draws the box and resets
    the cursor to 0 or 1, so a stale cursor outside {0, 1} means "not drawn yet").
    """
    if block is None or not block.valid or block.script_op is None:
        return None
    cursor = block.yes_no_cursor
    op = block.script_op
    if op == S.OP_YESNOBOX and block.comm[S.COMM_MULTIUSE_STATE] == 1 and cursor in (0, 1):
        return "yesnobox"
    if op == S.OP_YESNOBOXLEARNMOVE and block.learn_move_state == 1 and cursor in (0, 1):
        return "learn_move"
    if op == S.OP_YESNOBOXSTOPLEARNING and block.learn_move_state == 1 and cursor in (0, 1):
        return "stop_learning"
    if (
        op == S.OP_TRYGIVECAUGHTMONNICK
        and block.comm[S.COMM_MULTIUSE_STATE] == 1
        and cursor in (0, 1)
    ):
        return "nickname"
    return None


def classify_yes_no(prompt: str, block: S.BattleBlock) -> str:
    """Name the ``yesnobox`` prompt by the text the player reads (the opcode is shared)."""
    text = block.message.lower()
    if "use next" in text:
        return "use_next"
    if "change" in text or "about to use" in text:
        return "switch_offer"
    return "unknown_yes_no"


# -- the classifier -------------------------------------------------------------------------------


def starting_claim(ui: UiEvidence) -> Claim | None:
    """The hand-over into a battle, before ``gMain.inBattle`` is set (always busy, never input).

    ``callback2`` is the pointer the main loop is calling right now, so it cannot be stale the way
    ``gBattle*`` RAM is: while it is ``CB2_InitBattle`` / ``CB2_HandleStartBattle`` the game is
    loading the battle and reads no button. ``in_battle`` must be KNOWN to be clear; with it set
    the normal path below classifies (and keeps its own ``battle.entering`` rule).
    """
    cb2 = ui.main.callback2
    if ui.main.in_battle is not False or cb2 is None or not cb2.thumb_rom:
        return None
    if (cb2.ptr & ~1) not in CB2_BATTLE_START:
        return None
    return Claim(
        "battle.entering",
        0.9,
        True,
        "start",
        "battle start-up callback, inBattle not set yet",
        {"callback2": cb2.label},
    )


def classify(snapshot: StateSnapshot) -> Claim | None:
    ui = snapshot.ui
    if ui is None or not ui.valid:
        return None
    if ui.main.in_battle is not True:
        return starting_claim(ui)
    ev = ui.battle
    if ev is None or not ev.valid:
        return None
    block = battle_of(snapshot)
    if block is not None and not block.valid:
        block = None
    cb2 = ui.main.callback2
    cb2_ptr = (cb2.ptr & ~1) if cb2 is not None else 0
    cb2_name = cb2.name if cb2 is not None else None
    c0 = _controller0(ui)

    if block is not None:
        kind, supported = block.kind, block.supported
    else:
        variant = c0.variant if c0 is not None else None
        kind, _reason, supported = S.classify_battle(ev.type_flags or 0, player_variant=variant)

    claim = _classify_inner(ui, block, cb2_name, cb2_ptr, c0)
    if claim is None:
        return None
    if kind in KIND_SCENE and kind not in ("wild", "trainer"):
        # Keep the local special controller visible as its own question.  The complete adapter
        # consumes the inner readiness/phase; the legacy builder remains available for fallback.
        # A double battle's tactical menus are special, but script-owned modal scenes (level-up,
        # message, prompts, etc.) must retain their concrete scene so they cannot be mistaken for
        # a ready action frame merely because battler 2 is already prepared.
        if (
            kind == "double"
            and not claim.busy
            and claim.scene_id
            not in {
                "battle.action",
                "battle.moves_ui",
                "battle.choose_target",
            }
        ):
            return claim
        special_scene = KIND_SCENE.get(kind, "battle.link")
        if kind == "double" and claim.scene_id == "battle.choose_target":
            special_scene = "battle.double_target"
        return Claim(
            special_scene,
            claim.confidence,
            claim.busy,
            phase=claim.phase,
            why=f"{kind}: {claim.why}",
            evidence={**claim.evidence, "inner_scene": claim.scene_id},
        )
    return claim


def _classify_inner(ui, block, cb2_name, cb2_ptr, c0) -> Claim | None:  # noqa: C901
    base = {"callback2": cb2_name or f"0x{cb2_ptr:08X}"}

    # The evolution scene keeps Task_EvolutionScene alive while its summary callback owns the
    # screen.  Battle RAM can still contain gMoveToLearn from the preceding level-up, so that
    # stale block must not manufacture a second learn-move claim (the modal system detector owns
    # this frame).
    evolution_active = any(
        getattr(getattr(task, "func", None), "name", None) == "Task_EvolutionScene"
        for task in ui.tasks.active
    )

    # -- battle starting / re-showing after a menu ---------------------------------------------
    if cb2_ptr in CB2_BATTLE_START:
        return Claim("battle.entering", 0.9, True, "init", "battle init callback", base)
    if cb2_ptr in CB2_RESHOW or cb2_ptr in CB2_BAG_SETUP:
        return Claim("battle.animation", 0.85, True, "reshow", "menu <-> battle hand-over", base)

    # -- party screen opened from the battle -------------------------------------------------------
    if cb2_name in ("CB2_InitPartyMenu", "CB2_UpdatePartyMenu"):
        return _party(ui, cb2_name, base)

    # -- bag opened from the battle --------------------------------------------------------------
    if cb2_name == "CB2_BagMenuRun":
        return _bag(ui, base)

    if block is not None and block.move_to_learn and not evolution_active:
        if cb2_name == "CB2_SetUpPSS":
            return Claim(
                "battle.learn_move", 0.93, True, "summary_loading", "move summary loading", base
            )
        if cb2_name == "obs_cb2_pokemon_summary_screen":
            return Claim(
                "battle.learn_move", 0.97, False, "forget_move", "choose move to forget", base
            )

    if cb2_name != "CB2_BattleMain" or c0 is None:
        return None  # summary screen, evolution, naming ...: other families own them

    # A newly caught species opens its one-shot Pokedex page without leaving BattleMainCB2.
    # Cmd_displaydexinfo holds the battle script at 0xF2 while pret's
    # Task_DexScreen_RegisterMonToPokedex progresses through states 0..11; only state 11 reads
    # A/B (src/pokedex_screen.c).  Treating all controller-idle frames as animation made the
    # executor wait forever at the one state that actually required input.
    if block is not None and block.script_op == S.OP_DISPLAYDEXINFO:
        state = block.dex_registration_state
        evidence = {**base, "script_op": block.script_op, "dex_state": state}
        if state == 11:
            return Claim(
                "battle.message",
                0.97,
                False,
                "dex_registration",
                "caught-mon Pokedex page waits for A/B",
                evidence,
            )
        return Claim(
            "battle.animation",
            0.93,
            True,
            "dex_registration",
            "caught-mon Pokedex page is loading or closing",
            evidence,
        )

    chooser = _input_controller(ui)
    controller = chooser or c0
    phase = controller.phase
    base["controller"] = controller.func.label

    # The player controller can be prepared for the next battler while the battle script still
    # owns the screen.  Script state is authoritative here: Cmd_drawlvlupbox states 6 and 8 wait
    # for a key to advance/close the level-up box, and a printer waiting for A/B likewise owns the
    # input.  Check both before exposing the controller's action/move/target phase.
    if block is not None and block.script_op == S.OP_DRAWLVLUPBOX and block.lvlup_state in (6, 8):
        base = {**base, "script_op": block.script_op, "lvlup_state": block.lvlup_state}
        return Claim(
            "battle.level_up", 0.93, False, "level_up_box", "level-up box waits for a key", base
        )
    if _printer_waiting(ui):
        scene = "battle.ended" if block is not None and block.outcome else "battle.message"
        return Claim(scene, 0.95, False, "text_wait", "printer waits for A/B", base)

    # -- the three input menus ------------------------------------------------------------------
    if phase == "action":
        return Claim("battle.action", 0.97, False, "action", "HandleInputChooseAction", base)
    if phase == "move":
        return Claim("battle.moves_ui", 0.97, False, "move", "HandleInputChooseMove", base)
    if phase == "target":
        return Claim("battle.choose_target", 0.95, False, "target", "HandleInputChooseTarget", base)

    outcome = block.outcome if block is not None else 0

    # -- script prompts (proven by opcode + command state) -----------------------------------------
    prompt = yes_no_state(block)
    if prompt is not None and phase in ("idle", "unknown", "text"):
        assert block is not None
        base = {**base, "script_op": block.script_op, "cursor": block.yes_no_cursor}
        if prompt == "yesnobox":
            kind = classify_yes_no(prompt, block)
            if kind == "switch_offer":
                return Claim(
                    "battle.switch_offer", 0.93, False, "prompt", "yesnobox: switch?", base
                )
            if kind == "use_next":
                return Claim("battle.faint", 0.93, False, "prompt", "yesnobox: use next?", base)
            return None  # an unnamed yes/no: never guessed (the runner reports scene.unknown)
        if prompt in ("learn_move", "stop_learning"):
            return Claim("battle.learn_move", 0.93, False, prompt, f"script {prompt}", base)
        if prompt == "nickname":
            return Claim(
                "battle.ended", 0.9, False, "nickname", "nickname prompt after a catch", base
            )

    # -- busy phases -----------------------------------------------------------------------------
    if outcome:
        return Claim("battle.ended", 0.9, True, "ending", f"outcome {outcome}", base)
    if block is not None and MAIN_FUNC_INTRO_LOW <= block.main_func < MAIN_FUNC_TURN_LOOP:
        return Claim("battle.entering", 0.88, True, "intro", "battle intro sequence", base)
    if phase == "text" or _printer_active(ui):
        return Claim("battle.message", 0.9, True, "text_print", "text printing", base)
    if block is not None and _faint_visible(block):
        return Claim("battle.faint", 0.85, True, "faint", "a battler has fainted", base)
    return Claim("battle.animation", 0.88, True, phase or "idle", "battle script / animation", base)


def _faint_visible(block: S.BattleBlock) -> bool:
    return block.message.rstrip().lower().endswith("fainted!")


def _party(ui: UiEvidence, cb2_name: str, base: dict[str, Any]) -> Claim | None:
    party = ui.menu.party
    if party is None:
        return None
    action = party.action_name or "choose_mon"
    scene = PARTY_SCENES.get(action, "battle.party")
    base = {**base, "party_action": action, "party_slot": party.slot_id}
    if cb2_name == "CB2_InitPartyMenu":
        return Claim(scene, 0.85, True, "loading", "party screen loading", base)
    if _printer_waiting(ui):
        return Claim(
            "battle.message", 0.93, False, "party_text_wait", "party message waits for A", base
        )
    fade = _fade_active(ui)
    if _task_active(ui, "Task_HandleChooseMonInput") and not fade:
        return Claim(scene, 0.96, False, "choose", "Task_HandleChooseMonInput", base)
    if _task_active(ui, "Task_HandleSelectionMenuInput") and not fade:
        return Claim(scene, 0.94, False, "submenu", "Task_HandleSelectionMenuInput", base)
    return Claim(scene, 0.85, True, "settling", "party screen busy", base)


def _bag(ui: UiEvidence, base: dict[str, Any]) -> Claim | None:
    bag = ui.menu.bag
    if bag is None:
        return None
    base = {**base, "pocket": bag.pocket}
    if _printer_waiting(ui):
        return Claim(
            "battle.message", 0.93, False, "bag_text_wait", "bag message waits for A", base
        )
    fade = _fade_active(ui)
    if bag.bag_open and not fade and not _printer_active(ui):
        # the item's message (Guard Spec., X items ...) is printed and the continuation task keeps
        # polling for A/B: live-0050..0052 waited 6 hours in the "settling" claim below
        if _task_active(ui, "Task_ContinueTaskAfterMessagePrints"):
            return Claim(
                "battle.message",
                0.9,
                False,
                "bag_text_wait",
                "bag message after use waits for A",
                base,
            )
    if bag.bag_open and not fade:
        if _task_active(ui, "Task_FieldItemContextMenuHandleInput"):
            return Claim("battle.bag", 0.94, False, "context", "item context menu", base)
        if _task_active(ui, "Task_BagMenu_HandleInput"):
            return Claim("battle.bag", 0.96, False, "list", "Task_BagMenu_HandleInput", base)
    return Claim("battle.bag", 0.85, True, "settling", "bag screen busy", base)


class BattleDetector:
    """The family's detector (class BATTLE, specificity above the generic party/bag rows)."""

    name = "battle.scene"
    priority = PriorityClass.BATTLE
    specificity = 80
    claimed_scenes = CLAIMED_SCENES

    def detect(self, snapshot: StateSnapshot) -> SceneResult | None:
        claim = classify(snapshot)
        return claim.result() if claim is not None else None
