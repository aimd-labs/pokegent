"""Scene ids, option ids, executor and postcondition references of the system_flow family.

Scene ids come from ``references/scenes.yaml`` (the catalog is generated from the spec; a
missing id is
a proposal, never an edit). Executor and postcondition references are the only names an option may
carry (the model sees none of them).
"""

from __future__ import annotations

FAMILY = "system_flow"
MENU_OPEN = "menu.open"
NAMING = "naming.keyboard"
PROFESSOR_TEXT = "intro.professor_text"
GENDER = "intro.gender"
RIVAL_NAME = "intro.rival_name"
SCRIPT_MOVEMENT = "intro.script_movement"
# Generic busy transition owned by the dialog family; naming-screen hand-offs must never use the
# intro-specific scene above.
FIELD_SCRIPTED_MOVEMENT = "field.scripted_movement"

# -- scene ids (references/scenes.yaml) --------------------------------------------------------
HALL_OF_FAME = "special.hall_of_fame"
CREDITS = "system.credits"
LOGO = "system.logo"
TITLE_PRESS_START = "title.press_start"
TITLE_MAIN = "title.main"
TITLE_CONTINUE = "title.continue"
EVO_START = "evolution.start"
EVO_ANIMATION = "evolution.animation"
EVO_DONE = "evolution.done"
EVO_MOVE = "evolution.move_learning"
PC_MAIN = "pc.main"
POKEDEX_LIST = "pokedex.list"
PARTY_SUMMARY = "party.summary"
TRAINER_CARD = "menu.trainer_card"
OPTIONS = "menu.options"
#: DETECTED by the dialog family (gQuestLogState 2/3); only its OPTIONS are built here, see
# quest_log
QUEST_LOG = "system.quest_log_playback"

ENDGAME_SCENES = frozenset(
    {HALL_OF_FAME, CREDITS, LOGO, TITLE_PRESS_START, TITLE_MAIN, TITLE_CONTINUE}
)
EVOLUTION_SCENES = frozenset({EVO_START, EVO_ANIMATION, EVO_DONE, EVO_MOVE})
CLAIMED_SCENES = (
    ENDGAME_SCENES
    | EVOLUTION_SCENES
    | {
        PC_MAIN,
        NAMING,
        PROFESSOR_TEXT,
        GENDER,
        RIVAL_NAME,
        SCRIPT_MOVEMENT,
    }
)

# -- executor refs -----------------------------------------------------------------------------
REF_WAIT = "system_flow.wait_phase"
REF_ACK_CLEAR = "system_flow.acknowledge_clear"
REF_PRESS_A = "system_flow.press_a"
REF_ADVANCE_INTRO = "system_flow.advance_intro"
REF_PRESS_START = "system_flow.press_start"
REF_PRESS_B = "system_flow.press_b"
REF_BACK_OUT_FOREIGN = "system_flow.back_out_foreign"
REF_PC_STORAGE_BACK = "system_flow.pc_storage_back"
REF_CLOSE_MENU = "system_flow.close_menu"
REF_CONTINUE = "system_flow.continue_game"
REF_NEW_GAME = "system_flow.new_game"
REF_SELECT_PRESET = "system_flow.select_name_preset"
REF_ACCEPT_PRESET = "system_flow.accept_name_preset"
REF_CONFIRM_NAME = "system_flow.confirm_name"
REF_CHOOSE_GENDER = "system_flow.choose_gender"
REF_CANCEL_EVOLUTION = "system_flow.cancel_evolution"
REF_EVO_ANSWER = "system_flow.evolution_answer"
REF_FORGET_MOVE = "system_flow.forget_move"
REF_PC_STORAGE = "system_flow.pc_storage"
REF_PC_STORAGE_ERROR = "system_flow.pc_storage_error"
REF_PC_MOVE = "system_flow.pc_move"
REF_PC_MOVE_PLACE = "system_flow.pc_move_place"
REF_PC_MOVE_CANCEL = "system_flow.pc_move_cancel"
REF_PC_MENU_CHOICE = "system_flow.pc_menu_choice"
REF_PC_PLAYER = "system_flow.pc_player"
REF_LEAVE_PC = "system_flow.leave_pc"
REF_SKIP_QUEST_LOG = "system_flow.skip_quest_log"

# -- postcondition refs --------------------------------------------------------------------------
POST_MOVED_ON = "system_flow.moved_on"
POST_FOREIGN_SCREEN_LEFT = "system_flow.foreign_screen_left"
POST_MENU_CONFIRMED = "system_flow.menu_confirmed"
POST_INPUT_PROCESSED = "system_flow.input_processed"
POST_NAME_ACCEPTED = "system_flow.name_accepted"
POST_LEFT_SCENE = "system_flow.left_scene"
POST_EVO_CANCELLED = "system_flow.evolution_cancelled"
POST_EVO_ANSWERED = "system_flow.evolution_answered"
POST_MOVE_FORGOTTEN = "system_flow.move_forgotten"
POST_PC_STORAGE_CHANGED = "system_flow.pc_storage_changed"
POST_PC_STORAGE_MODE = "system_flow.pc_storage_mode"
POST_PC_STORAGE_ERROR_DISMISSED = "system_flow.pc_storage_error_dismissed"
POST_PC_MOVE_PICKED = "system_flow.pc_move_picked"
POST_PC_MOVE_PLACED = "system_flow.pc_move_placed"
POST_PC_MOVE_CANCELLED = "system_flow.pc_move_cancelled"
POST_PC_ITEM_CHANGED = "system_flow.pc_item_changed"
POST_FIELD_FREE = "system_flow.field_free_unchanged"
POST_PC_CHOICE = "system_flow.pc_choice"
POST_PC_PLAYER_EXIT = "system_flow.pc_player_exit"
POST_PC_PREVIOUS_MENU = "system_flow.pc_previous_menu"
POST_MENU_CLOSED = "system_flow.menu_closed"
POST_QUEST_LOG_LEFT = "system_flow.quest_log_left"

# -- option ids ----------------------------------------------------------------------------------
OPT_WAIT = "wait_for_phase"
OPT_ACK_CLEAR = "acknowledge_clear"
OPT_ADVANCE_CREDITS = "advance_if_prompted"
OPT_SKIP_INTRO = "skip_intro"
OPT_PRESS_START = "press_start"
OPT_CONTINUE = "continue_game"
OPT_NEW_GAME = "new_game"
OPT_SELECT_PRESET = "select_name_preset"
OPT_ALLOW_EVOLUTION = "allow_evolution"
OPT_CANCEL_EVOLUTION = "cancel_evolution"
OPT_ADVANCE_TEXT = "advance_text"
OPT_ACK_EVOLUTION = "acknowledge_evolution"
OPT_LOG_OFF = "log_off"
OPT_BACK_OUT = "back_out"
OPT_SKIP_QUEST_LOG = "skip_quest_log"
