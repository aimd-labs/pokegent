"""UI / task / battle-controller RAM layout and symbolic-name tables (spec 4.2, 4.4; M2.1).

Everything here is *data about the game*, with the decompile file it came from. No address lives
in this module: addresses are profile data (``references/rom_profiles.yaml``, ``symbols:`` of
the ROM profile) and carry a ``source`` and ``verified_by`` each. This module only knows

* struct layouts (offsets inside a struct, from the pret/pokefirered decompile at
  ``constants.PRET_COMMIT``; the layouts do not change between USA revisions),
* the *names* of the symbols the UI evidence reader needs (``DATA_SYMBOLS``), and
* coarse roles of function symbols (``CALLBACK2_KINDS`` ...) used to derive hints.

A function role is only a lookup by symbol *name*. If the profile has no (or an unverified)
entry for a name, the reader reports the raw pointer and ``name=None`` / ``verified=False``;
it never infers a role from an address.
"""

from __future__ import annotations

from pokegent.games.gen3.firered.rom.constants import PRET_COMMIT as PRET_COMMIT  # noqa: F401

# ---------------------------------------------------------------------------------------------
# Data symbols the reader reads. ``required`` ones must exist in the profile for a section to be
# readable at all; a missing optional one makes only its own field ``unknown``.
# ---------------------------------------------------------------------------------------------

MAIN_SYMBOLS = ("gMain",)
TASK_SYMBOLS = ("gTasks",)
TEXT_SYMBOLS = ("sTextPrinters", "gWindows")
SCRIPT_SYMBOLS = ("sGlobalScriptContextStatus", "sGlobalScriptContext", "sLockFieldControls")
MENU_SYMBOLS = ("sMenu", "sYesNoWindowId")
MENU_OPTIONAL_SYMBOLS = (
    "sStartMenuCursorPos",
    "sNumStartMenuItems",
    "sStartMenuWindowId",
    "gPartyMenu",
    "gBagMenuState",
    "sMonSummaryScreen",
    "gQuestLogState",
    "sPartyMenuInternal",
)
BATTLE_SYMBOLS = (
    "gBattleTypeFlags",
    "gActiveBattler",
    "gBattlersCount",
    "gBattlerControllerFuncs",
)
BATTLE_OPTIONAL_SYMBOLS = (
    "gBattleControllerExecFlags",
    "gBattlerPartyIndexes",
    "gBattlerPositions",
    "gAbsentBattlerFlags",
    "gActionSelectionCursor",
    "gMoveSelectionCursor",
    "gMultiUsePlayerCursor",
)
FIELD_SYMBOLS = ("gPlayerAvatar",)
FIELD_OPTIONAL_SYMBOLS = ("gPaletteFade", "sFanfareCounter")
FLY_MAP_SYMBOLS = ("sRegionMap", "sMapCursor", "sFlyMap", "HandleRegionMapInput")

# src/region_map.c (pret pokefirered): the heap objects are reached through the three
# EWRAM pointers above.  These offsets are deliberately kept here, rather than in the
# detector/builder, so the reader is the only code that interprets native state.
FLY_STATE = 0x00
FLY_SELECTED_DESTINATION = 0x02
MAP_CURSOR_X = 0x00
MAP_CURSOR_Y = 0x02
MAP_CURSOR_MOVE_COUNTER = 0x0C
MAP_CURSOR_INPUT_HANDLER = 0x10
MAP_CURSOR_SELECTED_MAPSEC = 0x14
MAP_CURSOR_SELECTED_MAPSEC_TYPE = 0x16
REGION_MAP_TYPE = 0x2F96
REGION_MAP_SELECTED_REGION = 0x2F9B

# ---------------------------------------------------------------------------------------------
# struct Main (include/main.h)
# ---------------------------------------------------------------------------------------------
MAIN_CALLBACK1 = 0x00
MAIN_CALLBACK2 = 0x04
MAIN_SAVED_CALLBACK = 0x08
MAIN_HELD_KEYS = 0x2C  # heldKeys (with L=A remapping)
MAIN_NEW_KEYS = 0x2E  # newKeys
MAIN_STATE = 0x438
MAIN_FLAGS = 0x439  # bit 0 oamLoadDisabled, bit 1 inBattle
MAIN_IN_BATTLE_BIT = 1 << 1
MAIN_READ_SIZE = 0x43A

KEY_BITS = {
    "A": 1 << 0,
    "B": 1 << 1,
    "SELECT": 1 << 2,
    "START": 1 << 3,
    "RIGHT": 1 << 4,
    "LEFT": 1 << 5,
    "UP": 1 << 6,
    "DOWN": 1 << 7,
    "R": 1 << 8,
    "L": 1 << 9,
}

# ---------------------------------------------------------------------------------------------
# struct Task (include/task.h): func, isActive, prev, next, priority, s16 data[16]; size 0x28
# ---------------------------------------------------------------------------------------------
NUM_TASKS = 16
TASK_SIZE = 0x28
TASK_FUNC = 0x00
TASK_IS_ACTIVE = 0x04
TASK_PREV = 0x05
TASK_NEXT = 0x06
TASK_PRIORITY = 0x07
TASK_DATA = 0x08
NUM_TASK_DATA = 16

# struct PokemonSpecialAnim, owned through Task_UseItem_Normal's word task argument.
# pokemon_special_anim_internal.h / pokemon_special_anim.c at PRET_COMMIT.
PSA_CANCEL_DISABLED = 0x91
PSA_STATE = 0x92
PSA_ITEM_ID = 0x96
PSA_WORK_READ_SIZE = PSA_ITEM_ID + 2 - PSA_CANCEL_DISABLED
PSA_WORK_MIN_SIZE = PSA_ITEM_ID + 2
PSA_LAST_STATE = 14
TASK_LIST_SENTINELS = (0xFE, 0xFF)  # HEAD_SENTINEL / TAIL_SENTINEL

# ---------------------------------------------------------------------------------------------
# struct TextPrinter (include/text.h), sTextPrinters[32], indexed by window id; size 0x24
# ---------------------------------------------------------------------------------------------
NUM_TEXT_PRINTERS = 32
TEXT_PRINTER_SIZE = 0x24
TP_CURRENT_CHAR = 0x00  # printerTemplate.currentChar (const u8 *)
TP_WINDOW_ID = 0x04
TP_FONT_ID = 0x05
TP_CURRENT_X = 0x08
TP_CURRENT_Y = 0x09
TP_CALLBACK = 0x10
TP_ACTIVE = 0x1B
TP_STATE = 0x1C
TP_TEXT_SPEED = 0x1D
TP_DELAY_COUNTER = 0x1E

# enum in include/text.h (RENDER_STATE_*)
RENDER_STATE_NAMES = {
    0: "handle_char",  # printing glyphs / running control codes
    1: "wait",  # {PAUSE_UNTIL_PRESS}: waits for A or B
    2: "clear",  # box full / {PARAGRAPH}: down arrow, waits for A or B, then clears
    3: "scroll_start",  # {SCROLL}: down arrow, waits for A or B, then scrolls
    4: "scroll",  # scrolling the box (animation)
    5: "wait_se",  # waits for a sound effect to end
    6: "pause",  # {PAUSE n}: timed pause
}
# States in which the printer is blocked on a button press (text.c: TextPrinterWait,
# TextPrinterWaitWithDownArrow in RENDER_STATE_WAIT / _CLEAR / _SCROLL_START).
RENDER_STATES_WAITING_FOR_BUTTON = frozenset({1, 2, 3})

# ---------------------------------------------------------------------------------------------
# struct Window (include/window.h): WindowTemplate (8) + tileData pointer; gWindows[32]
# ---------------------------------------------------------------------------------------------
NUM_WINDOWS = 32
WINDOW_SIZE = 0x0C
WIN_BG = 0x00  # 0xFF == WINDOW_NONE / free (RemoveWindow stores DUMMY_WIN_TEMPLATE)
WIN_LEFT = 0x01
WIN_TOP = 0x02
WIN_WIDTH = 0x03
WIN_HEIGHT = 0x04
WIN_PALETTE = 0x05
WIN_BASE_BLOCK = 0x06
WIN_TILE_DATA = 0x08
WINDOW_FREE_BG = 0xFF

# ---------------------------------------------------------------------------------------------
# struct Menu (src/menu.c) is sMenu; size 0x0C
# ---------------------------------------------------------------------------------------------
SMENU_SIZE = 0x0C
SMENU_LEFT = 0x00
SMENU_TOP = 0x01
SMENU_CURSOR_POS = 0x02  # s8
SMENU_MIN_CURSOR_POS = 0x03  # s8
SMENU_MAX_CURSOR_POS = 0x04  # s8
SMENU_WINDOW_ID = 0x05
SMENU_FONT_ID = 0x06
SMENU_OPTION_WIDTH = 0x07
SMENU_OPTION_HEIGHT = 0x08
SMENU_COLUMNS = 0x09
SMENU_ROWS = 0x0A

# script_menu.c: Task_YesNoMenu_HandleInput data (tTimer = data[2]); Task_MultichoiceMenu_
# HandleInput data (tIgnoreBPress = data[4], tWrapAround = data[5], tWindowId = data[6],
# tMultichoiceId = data[7]).
YESNO_TASK_TIMER = 2
MULTICHOICE_TASK_IGNORE_B = 4
MULTICHOICE_TASK_WRAP = 5
MULTICHOICE_TASK_WINDOW_ID = 6
MULTICHOICE_TASK_ID = 7
# list_menu.c: struct ListMenu lives in the (ListMenuDummy) task's data: template 0x18 bytes,
# then u16 cursorPos (scroll offset), u16 itemsAbove (row). Task.data starts at +0x08.
LISTMENU_TOTAL_ITEMS = 0x0C  # template.totalItems (u16), relative to the template start
LISTMENU_MAX_SHOWED = 0x0E  # template.maxShowed (u16)
LISTMENU_WINDOW_ID = 0x10  # template.windowId (u8)
LISTMENU_CURSOR_POS = 0x18  # struct ListMenu.cursorPos (u16): scroll offset
LISTMENU_ITEMS_ABOVE = 0x1A  # struct ListMenu.itemsAbove (u16): cursor row in the window

# ---------------------------------------------------------------------------------------------
# struct ScriptContext (include/script.h); sGlobalScriptContext size 0x74
# ---------------------------------------------------------------------------------------------
SCRIPT_CONTEXT_SIZE = 0x74
SCTX_STACK_DEPTH = 0x00
SCTX_MODE = 0x01
SCTX_NATIVE_PTR = 0x04
SCTX_SCRIPT_PTR = 0x08
SCRIPT_MODE_NAMES = {0: "stopped", 1: "bytecode", 2: "native"}
# sGlobalScriptContextStatus (src/script.c): CONTEXT_RUNNING, CONTEXT_WAITING, CONTEXT_SHUTDOWN
SCRIPT_STATUS_NAMES = {0: "running", 1: "waiting", 2: "shutdown"}

# ---------------------------------------------------------------------------------------------
# struct PartyMenu (include/party_menu.h) gPartyMenu, struct BagStruct (item_menu.h)
# ---------------------------------------------------------------------------------------------
PARTY_MENU_SIZE = 0x14
PM_EXIT_CALLBACK = 0x00
PM_TASK = 0x04
PM_TYPE_LAYOUT = 0x08  # menuType:4, layout:2, chooseMonsBattleType:2
PM_SLOT_ID = 0x09  # s8
PM_SLOT_ID2 = 0x0A  # s8
PM_ACTION = 0x0B
PM_BAG_ITEM = 0x0C  # u16
BAG_STATE_SIZE = 0x14
BAG_CALLBACK = 0x00
BAG_LOCATION = 0x04
BAG_OPEN = 0x05
BAG_POCKET = 0x06  # u16
BAG_ITEMS_ABOVE = 0x08  # u16[3]
BAG_CURSOR_POS = 0x0E  # u16[3]
NUM_BAG_POCKETS_NO_CASES = 3

# include/constants/party_menu.h PARTY_ACTION_*
PARTY_ACTION_NAMES = {
    0: "choose_mon",  # voluntary switch from the battle action menu / plain party screen
    1: "send_out",  # forced replacement after a faint (no cancel)
    2: "cant_switch",
    3: "use_item",
    4: "ability_prevents",
    5: "give_item",
    6: "give_pc_item",
    7: "give_mailbox_mail",
    8: "switch",
    9: "switching",
    10: "softboiled",
    11: "choose_and_close",
    12: "move_tutor",
    13: "minigame",
    14: "reusable_item",
}
PARTY_ACTION_SEND_OUT = 1
PARTY_SLOT_CANCEL = 7  # PARTY_SIZE + 1: gPartyMenu.slotId while the cursor is on CANCEL

# ---------------------------------------------------------------------------------------------
# struct PlayerAvatar (include/global.fieldmap.h)
# ---------------------------------------------------------------------------------------------
AVATAR_FLAGS = 0x00
AVATAR_TRANSITION_FLAGS = 0x01
AVATAR_RUNNING_STATE = 0x02  # NOT_MOVING 0, TURN_DIRECTION 1, MOVING 2
AVATAR_TILE_TRANSITION = 0x03  # T_NOT_MOVING 0, T_TILE_TRANSITION 1, T_TILE_CENTER 2
AVATAR_PREVENT_STEP = 0x06
RUNNING_STATE_NAMES = {0: "not_moving", 1: "turn_direction", 2: "moving"}
TILE_TRANSITION_NAMES = {0: "not_moving", 1: "tile_transition", 2: "tile_center"}

# struct PaletteFadeControl (include/palette.h), gcc little-endian bitfield packing:
# u16 @+4 = delayCounter:6 | y:5 | targetY:5 ; u16 @+6 = blendColor:15 | active:1.
PF_SIZE = 0x10
PF_Y_TARGET_WORD = 0x04
PF_ACTIVE_WORD = 0x06
PF_ACTIVE_MASK = 0x8000

# ---------------------------------------------------------------------------------------------
# battle (include/constants/battle.h, src/battle_main.c)
# ---------------------------------------------------------------------------------------------
MAX_BATTLERS_COUNT = 4
BATTLE_TYPE_FLAGS = {
    "double": 1 << 0,
    "link": 1 << 1,
    "is_master": 1 << 2,
    "trainer": 1 << 3,
    "first_battle": 1 << 4,
    "link_in_battle": 1 << 5,
    "multi": 1 << 6,
    "safari": 1 << 7,
    "battle_tower": 1 << 8,
    "old_man_tutorial": 1 << 9,
    "roamer": 1 << 10,
    "ereader_trainer": 1 << 11,
    "kyogre_groudon": 1 << 12,
    "legendary": 1 << 13,  # also GHOST_UNVEILED
    "regi": 1 << 14,
    "ghost": 1 << 15,
    "pokedude": 1 << 16,
    "wild_scripted": 1 << 17,
    "legendary_frlg": 1 << 18,
    "trainer_tower": 1 << 19,
}

# ---------------------------------------------------------------------------------------------
# Name tables. Keys are *symbol names* of the ROM profile; the values are coarse roles.
# ---------------------------------------------------------------------------------------------

# Named pre-field callbacks whose decomp flow legitimately precedes creation of the player
# ObjectEvent.  Core snapshot validation resolves these names through the ROM profile and compares
# their addresses to gMain.callback2; the strings are never used as live evidence themselves.
NEW_GAME_SCENE_CALLBACK = "CB2_NewGameScene"
NAMING_CALLBACKS = ("CB2_LoadNamingScreen", "CB2_NamingScreen", "CB2_ReturnFromNamingScreen")
NEW_GAME_MAP_CALLBACK = "CB2_NewGame"
PRE_FIELD_AVATAR_CALLBACKS = (
    NEW_GAME_SCENE_CALLBACK,
    *NAMING_CALLBACKS,
    NEW_GAME_MAP_CALLBACK,
)

# gMain.callback2 -> coarse mode of the game
CALLBACK2_KINDS: dict[str, str] = {
    "CB2_Overworld": "field",  # the field reads input
    "CB2_OverworldBasic": "field_transition",  # field runs, e.g. wild-encounter intro
    "CB2_SetUpSeagallopScene": "seagallop_transition",
    "MainCB2_SeaGallop": "seagallop_transition",
    "CB2_LoadMap": "map_load",
    "CB2_LoadMap2": "map_load",
    "CB2_DoChangeMap": "map_load",
    "CB2_ReturnToField": "map_load",
    "CB2_ReturnToFieldLocal": "map_load",
    "CB2_ReturnToFieldWithOpenMenu": "map_load",
    "CB2_ReturnToFieldContinueScript": "map_load",
    "CB2_ContinueSavedGame": "map_load",
    "CB2_WhiteOut": "map_load",
    "CB2_BattleMain": "battle",
    "CB2_InitPartyMenu": "party_menu",
    "CB2_UpdatePartyMenu": "party_menu",
    "CB2_SetUpPSS": "summary",
    "CB2_ReturnToPartyMenuFromFlyMap": "party_menu",
    "CB2_BagMenuRun": "bag",
    "CB2_BuyMenu": "shop",
    "CB2_PokeStorage": "pc_storage",
    "CB2_PokedexScreen": "pokedex",
    "CB2_EasyChatScreen": "easy_chat",
    "CB2_EvolutionSceneUpdate": "evolution",
    NAMING_CALLBACKS[0]: "naming",
    NAMING_CALLBACKS[1]: "naming",
    NEW_GAME_SCENE_CALLBACK: "new_game",
    "CB2_RegionMap": "region_map",
    "obs_cb2_pokemon_summary_screen": "summary",
}
MAP_LOAD_CALLBACKS = frozenset(name for name, kind in CALLBACK2_KINDS.items() if kind == "map_load")
SEAGALLOP_CALLBACKS = frozenset(
    name for name, kind in CALLBACK2_KINDS.items() if kind == "seagallop_transition"
)
FIELD_CALLBACKS = frozenset({"CB2_Overworld", "CB2_OverworldBasic"})

# Task function -> what the task is. Only tasks that read input or define a UI are listed; the
# background tasks of the field (weather, per-step callbacks, ...) are deliberately not roles.
TASK_KINDS: dict[str, str] = {
    "Task_StartMenuHandleInput": "start_menu",
    "Task_YesNoMenu_HandleInput": "yes_no",
    "Task_ScriptShowMonPic": "script_picture",
    "Task_MultichoiceMenu_HandleInput": "multichoice",
    "ListMenuDummyTask": "list_menu",
    "Task_FieldListMenuHandleInput": "list_menu",
    "Task_ShopMenu": "shop",
    "Task_ReturnToShopMenu": "shop",
    "Task_BuyMenu": "shop",
    "Task_BuyHowManyDialogueHandleInput": "shop_quantity",
    "Task_ReturnToItemListAfterItemPurchase": "shop",
    "Task_ExitBuyMenu": "shop",
    "Task_BagMenu_HandleInput": "bag",
    "Task_FieldItemContextMenuHandleInput": "bag_context",
    "Task_HandleChooseMonInput": "party_choose_mon",
    "Task_TryCreateSelectionWindow": "party_submenu",
    "Task_HandleSelectionMenuInput": "party_submenu",
    "Task_UseItem_Normal": "party_use_item",
    "Task_PCMainMenu": "pc_main_menu",
    "Task_TopMenuHandleInput": "player_pc_menu",
    "Task_TopMenu_ItemStorageSubmenu_HandleInput": "player_pc_item_menu",
    "Task_PokeStorageMain": "pc_storage",
    "Task_RunEasyChat": "easy_chat",
    "Task_EvolutionScene": "evolution",
    "Task_NamingScreen": "naming",
    "Task_ControlsGuide_HandleInput": "new_game",
    "Task_NewGameScene": "new_game",
    "Task_PikachuIntro_HandleInput": "new_game",
    "Task_OakSpeech_HandleGenderInput": "new_game_gender",
    "Task_OakSpeech_HandleRivalNameInput": "new_game_name_choices",
    "Task_OakSpeech_ConfirmName": "new_game_name_confirm",
    "Task_FlyMap": "fly_map",
    "Task_DrawFieldMessageBox": "field_message_box",
    "Task_WarpAndLoadMap": "warp",
}

# Battler controller function -> (phase, controller variant). Phases:
#   action / move / target : the controller reads buttons for that menu
#   party_select / bag_select : waiting for a party-menu / bag choice that was opened from battle
#   idle : controller is parked on its command dispatcher (a battle script, text or animation
#          is driving the battle; the player cannot act)
#   text : the controller waits for the battle text printer (window 0) to finish; with the
#          printer in a waiting state this is the "press A" battle message
#   transition : one-frame-to-seconds hand-over states while a menu is drawn or opened
CONTROLLER_PHASES: dict[str, tuple[str, str]] = {
    "HandleInputChooseAction": ("action", "player"),
    "HandleInputChooseMove": ("move", "player"),
    "HandleInputChooseTarget": ("target", "player"),
    "WaitForMonSelection": ("party_select", "player"),
    "CompleteWhenChoseItem": ("bag_select", "player"),
    "PlayerBufferRunCommand": ("idle", "player"),
    "CompleteOnInactiveTextPrinter": ("text", "player"),
    "HandleChooseActionAfterDma3": ("transition", "player"),
    "HandleChooseMoveAfterDma3": ("transition", "player"),
    "OpenPartyMenuToChooseMon": ("transition", "player"),
    "OpenBagAndChooseItem": ("transition", "player"),
    "OpponentBufferRunCommand": ("idle", "opponent"),
    "OakOldManHandleInputChooseAction": ("action", "oak_old_man"),
    "OakOldManHandleInputChooseMove": ("move", "oak_old_man"),
    "OakOldManBufferRunCommand": ("idle", "oak_old_man"),
    "SafariHandleInputChooseAction": ("action", "safari"),
    "PokedudeHandleInputChooseMove": ("move", "pokedude"),
}
# Phases in which the battle controller itself reads the buttons. party_select / bag_select wait
# for a choice made in the party / bag UI, which runs under its own callback2 and task.
CONTROLLER_INPUT_PHASES = frozenset({"action", "move", "target"})

# Battler indices the player controls in singles/doubles. Battler 0 is always the player's lead;
# in a double battle battler 2 is the partner slot (the player's second mon, or an ally NPC).
PLAYER_SIDE_BATTLERS = (0, 2)
