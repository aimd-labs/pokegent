"""Task data layouts and state enums of the screens this family reads (pret/pokefirered, pin in
``source-lock.md``). Layout facts only: which ``data[i]`` word of which task means what. They
are read
from ``ui.tasks.active[].data`` (16 s16 words per task), so no extra RAM symbol is needed.
"""

from __future__ import annotations

# -- title screen: Task_TitleScreenMain (src/title_screen.c)
# ----------------------------------------
TITLE_D_SCENE = 0  # tSceneNum
TITLE_D_STATE = 1  # tState
TITLE_SCENE_INIT, TITLE_SCENE_FLASH, TITLE_SCENE_FADE_IN, TITLE_SCENE_RUN = 0, 1, 2, 3
TITLE_SCENE_RESTART, TITLE_SCENE_CRY = 4, 5
TITLE_SCENE_NAMES = {0: "init", 1: "flash", 2: "fade_in", 3: "run", 4: "restart", 5: "cry"}

# -- main menu: the task that reads buttons (src/main_menu.c)
# ------------------------------------------
MENU_D_TYPE = (
    0  # tMenuType: 0 NEWGAME only, 1 CONTINUE + NEW GAME, 2 CONTINUE + NEW GAME + MYSTERY GIFT
)
MENU_D_CURSOR = 1  # tCursorPos
MENU_NEWGAME_ONLY, MENU_CONTINUE, MENU_MYSTERY_GIFT = 0, 1, 2
MENU_TYPE_NAMES = {0: "new_game_only", 1: "continue", 2: "continue_mystery_gift"}
#: entries from the top, per menu type
MENU_ENTRIES = {
    0: ("NEW GAME",),
    1: ("CONTINUE", "NEW GAME"),
    2: ("CONTINUE", "NEW GAME", "MYSTERY GIFT"),
}

# -- evolution: Task_EvolutionScene (src/evolution_scene.c)
# ---------------------------------------------
EVO_D_STATE = 0  # tState
EVO_D_PRE_SPECIES = 1
EVO_D_POST_SPECIES = 2
EVO_D_BITS = 3  # tBits: bit 0 TASK_BIT_CAN_STOP, bit 7 TASK_BIT_LEARN_MOVE
EVO_D_LEARNS_FIRST = 4
EVO_D_LEARN_STATE = 6  # tLearnMoveState (valid in EVOSTATE_REPLACE_MOVE)
EVO_D_YES_STATE = 7  # tLearnMoveYesState
EVO_D_NO_STATE = 8  # tLearnMoveNoState (data[8] is tEvoStopped in the cycle states)
EVO_D_WAS_STOPPED = 9
EVO_D_PARTY_ID = 10
EVO_BIT_CAN_STOP = 1 << 0
EVO_BIT_LEARN_MOVE = 1 << 7

EVOSTATE_NAMES = {
    0: "fade_in",
    1: "intro_msg",
    2: "intro_mon_anim",
    3: "intro_sound",
    4: "start_music",
    5: "start_bg_and_sparkle_spiral",
    6: "sparkle_arc",
    7: "cycle_mon_sprite",
    8: "wait_cycle_mon_sprite",  # the ONLY state in which holding B cancels the evolution
    9: "sparkle_circle",
    10: "sparkle_spray",
    11: "evo_sound",
    12: "restore_screen",
    13: "evo_mon_anim",
    14: "set_mon_evolved",
    15: "try_learn_move",
    16: "end",
    17: "cancel",
    18: "cancel_mon_anim",
    19: "cancel_msg",
    20: "learned_move",
    21: "try_learn_another_move",
    22: "replace_move",
}
EVOSTATE_WAIT_CYCLE = 8
EVOSTATE_SET_MON_EVOLVED = 14
EVOSTATE_TRY_LEARN_MOVE = 15
EVOSTATE_END = 16
EVOSTATE_CANCEL = 17
EVOSTATE_CANCEL_MSG = 19
EVOSTATE_LEARNED_MOVE = 20
EVOSTATE_TRY_LEARN_ANOTHER = 21
EVOSTATE_REPLACE_MOVE = 22
#: tState values after the animation, still part of "the evolution finished or was stopped"
EVO_DONE_STATES = frozenset({14, 15, 16, 17, 18, 19})
EVO_LEARN_STATES = frozenset({20, 21, 22})

# tLearnMoveState inside EVOSTATE_REPLACE_MOVE
MVSTATE_NAMES = {
    0: "intro_msg_1",
    1: "intro_msg_2",
    2: "intro_msg_3",
    3: "print_yes_no",
    4: "handle_yes_no",
    5: "show_move_select",
    6: "handle_move_select",
    7: "forget_msg_1",
    8: "forget_msg_2",
    9: "learned_move",
    10: "ask_cancel",
    11: "cancel",
    12: "retry_after_hm",
}
MVSTATE_HANDLE_YES_NO = 4
MVSTATE_SHOW_MOVE_SELECT = 5
MVSTATE_HANDLE_MOVE_SELECT = 6
MVSTATE_ASK_CANCEL = 10
MVSTATE_CANCEL = 11
MVSTATE_INTRO_1 = 0

# -- PC main menu: Task_PCMainMenu (src/pokemon_storage_system_menu.c)
# ------------------------------
PCMENU_D_STATE = 0  # tState: 0 load, 1 fade in, 2 handle input, 3 error msg, 4 enter pc
PCMENU_D_SELECTED = 1
PCMENU_STATE_HANDLE_INPUT = 2
PCMENU_ENTRIES = ("WITHDRAW POKEMON", "DEPOSIT POKEMON", "MOVE POKEMON", "MOVE ITEMS", "SEE YA!")
