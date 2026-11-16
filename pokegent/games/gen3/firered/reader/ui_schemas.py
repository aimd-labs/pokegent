"""Typed UI evidence block (spec 4.3 ``ui``, 4.4, WP2 inputs; milestone M2.1).

``UiEvidence`` is a bundle of *facts* read from one atomic RAM snapshot. It is not a scene: no
section decides "this is a yes/no dialog". The scene detector (state/scenes.py, later work)
combines several of these facts, and spec 4.4 is explicit that one signal is never proof (a stale
window id or a leftover task entry stays in RAM after the UI closed). Where a stale signal is
known, the section carries the corroborating flag next to the raw value (``window_in_use``,
``task_active``, ``open``) and the docs name the trap.

Conventions, identical in every section:

* ``valid``      False => the section could not be read; its value fields are ``None``/empty and
  ``read_errors`` says why. Consumers treat that as ``unknown``, never as ``False``.
* ``confidence`` 1.0 every symbol used is verified (``verified_by`` in the ROM profile) and every
  sanity check passed; 0.5 a symbol used is still unverified; 0.0 not valid.
* ``bool | None`` / ``int | None`` fields: ``None`` means unknown (symbol absent from the
  profile, or not meaningful in this game mode). A fact that is known to be false is ``False``.
* Function pointers are :class:`FnRef`: the raw word, a resolved symbol name when the profile
  has one at that address, and whether that symbol is verified.

Models are frozen and reject unknown fields, like the rest of the snapshot.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

UI_SCHEMA_VERSION = "ui/1.0"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class UiReadError(_Frozen):
    """Same shape as ``schemas.ReadError`` (kept separate so this module has no import cycle)."""

    subsystem: str
    code: str
    message: str
    addr: int | None = None


class FnRef(_Frozen):
    """A function pointer found in RAM."""

    ptr: int  # the raw 32-bit word, thumb bit included
    name: str | None = None  # profile symbol at ``ptr & ~1`` (None: not in the profile)
    verified: bool = False  # the profile symbol has ``verified_by`` evidence
    thumb_rom: bool = False  # ptr is a Thumb pointer into ROM (0x08000000.., bit 0 set)

    @property
    def label(self) -> str:
        """Stable token: the symbol name, else ``fn:0xADDR`` (never an empty string)."""
        return self.name if self.name else f"fn:0x{self.ptr & ~1:08X}"


class _Section(_Frozen):
    valid: bool
    confidence: float = Field(ge=0.0, le=1.0)
    read_errors: tuple[UiReadError, ...] = ()


# -- gMain --------------------------------------------------------------------------------------


class MainEvidence(_Section):
    callback1: FnRef | None = None
    callback2: FnRef | None = None
    saved_callback: FnRef | None = None
    state: int | None = None  # gMain.state (callback-internal state counter)
    held_keys: int | None = None  # gMain.heldKeys, KEY_BITS
    new_keys: int | None = None  # gMain.newKeys (frame the key went down)
    in_battle: bool | None = None  # gMain.inBattle
    oak_speech_has_player_name: bool | None = None
    oak_speech_page: int | None = None
    naming_screen_state: int | None = None
    naming_screen_page: int | None = None
    naming_cursor_x: int | None = None
    naming_cursor_y: int | None = None


# -- gTasks -------------------------------------------------------------------------------------


class ItemUseAnimInfo(_Frozen):
    """Heap state owned by ``Task_UseItem_Normal`` (pokemon_special_anim.c)."""

    state: int
    cancel_disabled: bool
    item_id: int


class TaskInfo(_Frozen):
    slot: int
    func: FnRef
    priority: int
    prev: int
    next: int
    data: tuple[int, ...]  # 16 s16 words
    item_use_anim: ItemUseAnimInfo | None = None


class TaskEvidence(_Section):
    active: tuple[TaskInfo, ...] = ()
    signatures: tuple[str, ...] = ()  # sorted unique FnRef.label of the active tasks
    count: int | None = None


# -- text printers / windows --------------------------------------------------------------------


class PrinterInfo(_Frozen):
    window_id: int  # index in sTextPrinters == printerTemplate.windowId
    state: int
    state_name: str
    waiting_for_button: bool  # state in {wait, clear, scroll_start}
    text_speed: int
    delay_counter: int
    font_id: int
    current_char_ptr: int


class WindowInfo(_Frozen):
    window_id: int
    bg: int
    left: int
    top: int
    width: int
    height: int


class TextEvidence(_Section):
    printers: tuple[PrinterInfo, ...] = ()  # active printers only
    windows: tuple[WindowInfo, ...] = ()  # windows in use (bg != 0xFF)
    any_active: bool | None = None
    printing: bool | None = None  # an active printer is still drawing/pausing/scrolling
    waiting_for_button: bool | None = None  # an active printer waits for A/B
    # Task_DrawFieldMessageBox is alive: the field message printer (window 0) is running or
    # waiting. None: the task name is not in the profile or the task list is unreadable.
    field_message_task: bool | None = None


# -- field script context -----------------------------------------------------------------------


class ScriptEvidence(_Section):
    status: int | None = None
    status_name: str | None = None  # running / waiting / shutdown
    mode: int | None = None
    mode_name: str | None = None  # stopped / bytecode / native
    running: bool | None = None  # a script is loaded (status != shutdown)
    native: FnRef | None = None  # the native wait function, ONLY while mode == native (else stale)
    script_ptr: int | None = None  # next bytecode command while a field script is loaded
    stack_depth: int | None = None
    controls_locked: bool | None = None  # sLockFieldControls (meaningful in field callbacks only)


# -- menus --------------------------------------------------------------------------------------


class SMenuInfo(_Frozen):
    left: int
    top: int
    cursor_pos: int
    min_cursor_pos: int
    max_cursor_pos: int
    window_id: int
    font_id: int
    option_width: int
    option_height: int
    columns: int
    rows: int
    window_in_use: bool  # gWindows[window_id] is allocated (a stale sMenu usually fails this)
    consistent: bool  # min <= cursor <= max and window_id < 32


class YesNoInfo(_Frozen):
    window_id: int  # sYesNoWindowId (raw; stale after the box closed)
    window_in_use: bool
    task_active: bool | None  # Task_YesNoMenu_HandleInput present (None: symbol unknown)
    input_ready: bool | None  # task timer reached the point where it reads buttons
    # task_active and window_in_use: a FIELD SCRIPT yes/no prompt (script_menu.c). Yes/no boxes
    # of other owners (shop, battle, party) have their own task or controller and are not
    # covered by this verdict; for them only window_in_use is a raw fact.
    field_prompt_open: bool | None


class MultichoiceInfo(_Frozen):
    task_slot: int
    window_id: int
    multichoice_id: int
    ignore_b: bool
    wrap_around: bool
    window_in_use: bool


class ListMenuInfo(_Frozen):
    task_slot: int
    total_items: int
    max_showed: int
    window_id: int
    scroll_offset: int
    cursor_row: int
    window_in_use: bool
    items_ptr: int | None = None


class StartMenuInfo(_Frozen):
    cursor_pos: int
    num_items: int
    window_id: int | None  # 0xFF = not visible
    task_active: bool | None


class PartyMenuInfo(_Frozen):
    exit_callback: FnRef
    task: FnRef
    menu_type: int
    layout: int
    slot_id: int
    slot_id2: int
    action: int
    action_name: str | None  # PARTY_ACTION_* name
    bag_item: int
    active_hint: bool | None  # a party callback2 or party task is running (struct is stale else)
    item_id: int | None = None  # gSpecialVar_ItemId while PARTY_ACTION_USE_ITEM owns this menu
    action_ids: tuple[int, ...] = ()  # sPartyMenuInternal->actions, when the action list is live
    action_num: int | None = None
    # in a battle, action == send_out: the forced replacement after a faint (cancel unavailable);
    # False for the voluntary POKEMON switch (action == choose_mon); None when not in a battle
    forced_send_out: bool | None = None


class BagInfo(_Frozen):
    callback: FnRef
    location: int
    bag_open: bool
    pocket: int
    items_above: tuple[int, ...]
    cursor_pos: tuple[int, ...]
    active_hint: bool | None


class MenuEvidence(_Section):
    menu: SMenuInfo | None = None
    yes_no: YesNoInfo | None = None
    multichoice: MultichoiceInfo | None = None
    list_menu: ListMenuInfo | None = None
    start_menu: StartMenuInfo | None = None
    party: PartyMenuInfo | None = None
    bag: BagInfo | None = None
    summary_screen_open: bool | None = None  # sMonSummaryScreen pointer is set
    quest_log_state: int | None = None  # 0 off, 1 recording, 2 playback, 3 last playback


class FlyMapInfo(_Frozen):
    """RAM-backed state of the region-map cursor while ``Task_FlyMap`` owns input.

    The pointer-backed values are optional because older ROM profiles do not expose the
    region-map heap.  A missing block is deliberately not a selectable destination.
    """

    state: int | None = None
    cursor_x: int | None = None
    cursor_y: int | None = None
    move_counter: int | None = None
    input_handler: str | None = None
    selected_mapsec: int | None = None
    selected_type: int | None = None
    region: int | None = None
    input_ready: bool | None = None


# -- battle -------------------------------------------------------------------------------------


class BattlerController(_Frozen):
    battler: int
    func: FnRef
    phase: str  # action/move/target/party_select/bag_select/idle/busy/unknown
    variant: str | None  # player/oak_old_man/safari/pokedude/dummy; None when unresolved
    accepts_input: bool | None  # phase is an input phase (None: unresolved function)
    player_side: bool  # battler 0 / 2


class BattleEvidence(_Section):
    active: bool | None = None  # gMain.inBattle (callback2 is NOT a safe proxy: the party
    # screen and bag opened from a battle run under their own callback2)
    type_flags: int | None = None
    type_flag_names: tuple[str, ...] = ()
    is_double: bool | None = None
    is_trainer: bool | None = None
    is_safari: bool | None = None
    battlers_count: int | None = None
    active_battler: int | None = None  # loop variable: == battlers_count when nothing dispatches
    exec_flags: int | None = None  # gBattleControllerExecFlags (battlers still executing)
    controllers: tuple[BattlerController, ...] = ()  # battlers 0..battlers_count-1
    action_cursor: tuple[int, ...] = ()  # gActionSelectionCursor[0..3]
    move_cursor: tuple[int, ...] = ()  # gMoveSelectionCursor[0..3]
    target_cursor: int | None = None  # gMultiUsePlayerCursor
    battler_positions: tuple[int, ...] = ()
    party_indexes: tuple[int, ...] = ()
    absent_flags: int | None = None
    input_battlers: tuple[int, ...] = ()  # player-side battlers whose controller reads buttons


# -- field / avatar -----------------------------------------------------------------------------


class FieldEvidence(_Section):
    avatar_flags: int | None = None
    on_foot: bool | None = None
    mach_bike: bool | None = None
    acro_bike: bool | None = None
    surfing: bool | None = None
    underwater: bool | None = None
    # PLAYER_AVATAR_FLAG_CONTROLLABLE: set on ice/forced-movement tiles logic only; it is NOT
    # 'the player may act' (it is 0 on most idle overworld frames). Use script.controls_locked.
    controllable_flag: bool | None = None
    forced_movement: bool | None = None
    running_state: str | None = None  # not_moving / turn_direction / moving
    tile_transition: str | None = None  # not_moving / tile_transition / tile_center
    # Proven by the walk probe: a step is 16 frames of tile_transition (+1 tile_center frame); a
    # turn is 8 frames of running_state=turn_direction. running_state alone is stale (it stays
    # 'moving' while a script has taken over), so `moving` looks at tile_transition only.
    # moving / turning are None outside the field callbacks (the avatar block is stale there)
    moving: bool | None = None  # tile_transition != not_moving
    turning: bool | None = None  # running_state == turn_direction and not stepping
    prevent_step: bool | None = None
    fade_active: bool | None = None  # gPaletteFade.active (screen fade / map transition)
    fade_y: int | None = None
    fade_target_y: int | None = None
    fade_direction: str | None = None  # "out" (target 16, to black) / "in" (target 0) / None
    fanfare_counter: int | None = None  # sound.c sFanfareCounter; zero means WaitFanfare is ready
    # True from the moment Task_WarpAndLoadMap exists (the fade to black before a warp, ~23 frames
    # before CB2_LoadMap) through the map-load callbacks; False when known not; None: unknown.
    warp_pending: bool | None = None


# -- top level ----------------------------------------------------------------------------------


class UiEvidence(_Frozen):
    schema_version: Literal["ui/1.0"] = UI_SCHEMA_VERSION  # type: ignore[assignment]
    valid: bool  # gMain could be read (the anchor of every other section)
    # Spec 4.3 convenience fields, derived from the sections below.
    callback_id: str | None = None  # label of gMain.callback2
    task_signatures: tuple[str, ...] = ()
    menu_type: str | None = None  # None unless a menu is corroborated by task AND window
    text_printer: Literal["printing", "waiting_for_button"] | None = None
    cursor: int | None = None  # sMenu.cursorPos when the menu is corroborated, else None
    controls_locked: bool | None = None  # field callbacks only, else None
    # Sections.
    main: MainEvidence
    tasks: TaskEvidence
    text: TextEvidence
    script: ScriptEvidence
    menu: MenuEvidence
    fly_map: FlyMapInfo | None = None
    battle: BattleEvidence | None = None  # None: no battle active (known); valid=False: unknown
    field: FieldEvidence
    # Per-section confidence (0.0, 0.5 or 1.0) and every read error of every section.
    confidence: dict[str, float] = Field(default_factory=dict)
    read_errors: tuple[UiReadError, ...] = ()
    unverified_symbols: tuple[str, ...] = ()  # profile symbols used that have no verified_by

    def scene_digest(self) -> str:
        """sha256 over the *stable* scene facts (no key presses, counters or task data words).

        Two snapshots of an unchanged UI state digest equal; opening/closing a menu, moving a
        cursor, a printer changing state or a battle controller changing phase digest differently.
        """
        battle = None
        if self.battle is not None:
            battle = {
                "active": self.battle.active,
                "flags": self.battle.type_flags,
                "count": self.battle.battlers_count,
                "ctl": [c.func.ptr for c in self.battle.controllers],
                "action": list(self.battle.action_cursor),
                "move": list(self.battle.move_cursor),
                "target": self.battle.target_cursor,
            }
        menu = self.menu
        payload = {
            "valid": self.valid,
            "cb2": self.main.callback2.ptr if self.main.callback2 else None,
            "tasks": [[t.func.ptr, t.priority] for t in self.tasks.active],
            "printers": [[p.window_id, p.state] for p in self.text.printers],
            "windows": [w.window_id for w in self.text.windows],
            "script": [
                self.script.status,
                self.script.mode,
                self.script.native.ptr if self.script.native else None,
                self.script.controls_locked,
            ],
            "menu": (
                [
                    menu.menu.window_id,
                    menu.menu.cursor_pos,
                    menu.menu.window_in_use,
                ]
                if menu.menu
                else None
            ),
            "yes_no": menu.yes_no.field_prompt_open if menu.yes_no else None,
            "battle": battle,
            "field": [
                self.field.avatar_flags,
                self.field.moving,
                self.field.fade_active,
            ],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return "sha256:" + hashlib.sha256(canonical.encode("ascii")).hexdigest()
