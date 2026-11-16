"""Function symbols (callbacks and tasks) the family reads BY NAME through the ROM profile.

Addresses live in ``references/rom_profiles.yaml`` with ``source`` and ``verified_by``; this
module only
lists the names, so a profile that lacks one is detectable (:func:`missing_symbols`) and the
detectors
stay silent instead of guessing (an unnamed pointer shows up as ``fn:0x...`` and is never a claim).
"""

from __future__ import annotations

from pokegent.games.gen3.firered.reader import ui_symbols as U
from pokegent.games.gen3.firered.rom.rom_data import RomProfile

# -- Hall of Fame (src/hall_of_fame.c) ---------------------------------------------------------
HOF_INIT_CALLBACKS = ("CB2_DoHallOfFameScreen", "CB2_DoHallOfFameScreenDontSaveData")
HOF_IDLE_CALLBACK = "CB2_HofIdle"
HOF_EXIT_TASK = "Task_Hof_ExitOnKeyPressed"  # the only task that reads a button (A)
#: tasks after the A: the palette fade and the cleanup that ends in SetWarpsToRollCredits
HOF_LEAVING_TASKS = ("Task_Hof_HandlePaletteOnExit", "Task_Hof_HandleExit")
HOF_SAVING_TASKS = (
    "Task_Hof_InitMonData",
    "Task_Hof_InitTeamSaveData",
    "Task_Hof_TrySaveData",
    "Task_Hof_DelayAfterSave",
)
HOF_TASKS = (
    *HOF_SAVING_TASKS,
    "Task_Hof_DisplayMon",
    "Task_Hof_PlayMonCryAndPrintInfo",
    "Task_Hof_TryDisplayAnotherMon",
    "Task_Hof_PaletteFadeAndPrintWelcomeText",
    "Task_Hof_ApplauseAndConfetti",
    "Task_Hof_WaitBorderFadeAway",
    "Task_Hof_SpawnPlayerPic",
    "Task_Hof_WaitAndPrintPlayerInfo",
    HOF_EXIT_TASK,
    *HOF_LEAVING_TASKS,
)

# -- credits, boot, intro, title, main menu (src/credits.c, intro.c, title_screen.c, main_menu.c)
CREDITS_CALLBACK = "CB2_Credits"
BOOT_CALLBACKS = (
    "CB2_InitCopyrightScreenAfterBootup",
    "CB2_InitCopyrightScreenAfterTitleScreen",
    "CB2_WaitFadeBeforeSetUpIntro",
    "CB2_SetUpIntro",
)
INTRO_CALLBACK = "CB2_Intro"
TITLE_INIT_CALLBACK = "CB2_InitTitleScreen"
TITLE_CALLBACK = "CB2_TitleScreenRun"
TITLE_TASK = "Task_TitleScreenMain"
MAIN_MENU_INIT_CALLBACK = "CB2_InitMainMenu"
MAIN_MENU_CALLBACK = "CB2_MainMenu"
NEW_GAME_CALLBACK = U.NEW_GAME_SCENE_CALLBACK
NEW_GAME_NAME_TASK = "Task_OakSpeech_HandleRivalNameInput"
NEW_GAME_CONFIRM_TASK = "Task_OakSpeech_HandleConfirmNameInput"
NEW_GAME_CONTROLS_TASK = "Task_ControlsGuide_HandleInput"
NEW_GAME_PIKACHU_TASK = "Task_PikachuIntro_HandleInput"
NEW_GAME_GENDER_TASK = "Task_OakSpeech_HandleGenderInput"
NEW_GAME_TASKS = (
    "Task_NewGameScene",
    NEW_GAME_CONTROLS_TASK,
    NEW_GAME_PIKACHU_TASK,
    NEW_GAME_GENDER_TASK,
    NEW_GAME_NAME_TASK,
    "Task_OakSpeech_ConfirmName",
    NEW_GAME_CONFIRM_TASK,
)
#: the main menu task that reads the buttons; the others are set-up / hand-over phases (busy)
MAIN_MENU_INPUT_TASK = "Task_HandleMenuInput"
MAIN_MENU_TASKS = (
    "Task_SetWin0BldRegsAndCheckSaveFile",
    "Task_SaveErrorStatus_RunPrinterThenWaitButton",
    "Task_SetWin0BldRegsNoSaveFileCheck",
    "Task_WaitFadeAndPrintMainMenuText",
    "Task_PrintMainMenuText",
    "Task_WaitDma3AndFadeIn",
    "Task_UpdateVisualSelection",
    MAIN_MENU_INPUT_TASK,
    "Task_ExecuteMainMenuSelection",
    "Task_MysteryGiftError",
    "Task_ReturnToTileScreen",
)
NAMING_CALLBACKS = U.NAMING_CALLBACKS
NAMING_INPUT_CALLBACK = "CB2_NamingScreen"
NAMING_TASK = "Task_NamingScreen"
NEW_GAME_MAP_CALLBACK = U.NEW_GAME_MAP_CALLBACK
NEW_GAME_TRANSITION_CALLBACKS = (
    "CB2_LoadMap",
    "CB2_DoChangeMap",
    "CB2_LoadMap2",
    "CB2_Overworld",
)

# A cold boot has no valid avatar yet.  Only these named callback owners may participate in the
# explicit pre-game perception phase; an unnamed or unrelated callback restores the strict core
# validity gate immediately.
PRE_GAME_CALLBACKS = frozenset(
    (
        *BOOT_CALLBACKS,
        INTRO_CALLBACK,
        TITLE_INIT_CALLBACK,
        TITLE_CALLBACK,
        MAIN_MENU_INIT_CALLBACK,
        MAIN_MENU_CALLBACK,
        NEW_GAME_CALLBACK,
        *NAMING_CALLBACKS,
        NEW_GAME_MAP_CALLBACK,
        *NEW_GAME_TRANSITION_CALLBACKS,
    )
)

# -- evolution (src/evolution_scene.c) -----------------------------------------------------------
EVOLUTION_CALLBACK = "CB2_EvolutionSceneUpdate"
EVOLUTION_LOAD_CALLBACKS = ("CB2_BeginEvolutionScene", "CB2_EvolutionSceneLoadGraphics")
EVOLUTION_TASK = "Task_EvolutionScene"
SUMMARY_CALLBACK = "obs_cb2_pokemon_summary_screen"  # = CB2_RunPokemonSummaryScreen
SUMMARY_SETUP_CALLBACK = "CB2_SetUpPSS"  # loads the summary screen (the move-to-forget choice)

# -- PC (src/pokemon_storage_system*.c) -----------------------------------------------------------
PC_STORAGE_CALLBACK = "CB2_PokeStorage"
PC_STORAGE_TASK = "Task_PokeStorageMain"
PC_STORAGE_SELECTED_MON_TASK = "Task_OnSelectedMon"
PC_MENU_TASK = "Task_PCMainMenu"
PLAYER_PC_MENU_TASK = "Task_TopMenuHandleInput"
PLAYER_PC_ITEM_MENU_TASK = "Task_TopMenu_ItemStorageSubmenu_HandleInput"

#: every name the family needs; ``missing_symbols`` reports what the profile lacks
REQUIRED_SYMBOLS: tuple[str, ...] = tuple(
    dict.fromkeys(
        (
            *HOF_INIT_CALLBACKS,
            HOF_IDLE_CALLBACK,
            *HOF_TASKS,
            CREDITS_CALLBACK,
            *BOOT_CALLBACKS,
            INTRO_CALLBACK,
            TITLE_INIT_CALLBACK,
            TITLE_CALLBACK,
            TITLE_TASK,
            MAIN_MENU_INIT_CALLBACK,
            MAIN_MENU_CALLBACK,
            NEW_GAME_CALLBACK,
            NEW_GAME_MAP_CALLBACK,
            *NEW_GAME_TASKS,
            *NEW_GAME_TRANSITION_CALLBACKS,
            *NAMING_CALLBACKS,
            *MAIN_MENU_TASKS,
            EVOLUTION_CALLBACK,
            *EVOLUTION_LOAD_CALLBACKS,
            EVOLUTION_TASK,
            SUMMARY_CALLBACK,
            SUMMARY_SETUP_CALLBACK,
            PC_STORAGE_CALLBACK,
            PC_STORAGE_TASK,
            PC_STORAGE_SELECTED_MON_TASK,
            PC_MENU_TASK,
            PLAYER_PC_MENU_TASK,
            PLAYER_PC_ITEM_MENU_TASK,
        )
    )
)


def missing_symbols(profile: RomProfile) -> tuple[str, ...]:
    """Names the profile does not carry (empty for Rev0). A missing name never becomes a guess."""
    return tuple(n for n in REQUIRED_SYMBOLS if not profile.has_symbol(n))
