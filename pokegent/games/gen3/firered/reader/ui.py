"""UI evidence reader: gMain, tasks, text printers, script context, menus, battle, avatar.

``UiEvidenceReader(port, profile).read()`` returns a
:class:`~pokegent.games.gen3.firered.reader.ui_schemas.UiEvidence` from **one** atomic RAM
image (an ``AtomicSnapshotPort`` or a ``SnapshotMemoryReader``).
It never raises for bad game memory: every violated sanity check becomes a ``UiReadError`` in the
affected section, the section turns ``valid=False`` and its value fields stay ``None``/empty
(= unknown). A symbol that is absent from the profile makes only its own fields unknown.

What this module does NOT do: it does not name scenes. It reports facts and, where the game keeps
stale values (spec 4.4), the corroborating flag next to the raw value. See
``the runtime contracts`` for the situation -> evidence table and the traps found.

Nothing here writes memory. Function names come from the profile (``RomProfile.symbols`` entries
in ROM address space); a pointer without a profile symbol stays an unnamed ``FnRef``.
"""

from __future__ import annotations

import struct

from pokegent.games.gen3.firered.reader import ui_symbols as U
from pokegent.games.gen3.firered.reader.memory_port import (
    EWRAM_END,
    EWRAM_START,
    MemoryPort,
    MemoryReadError,
)
from pokegent.games.gen3.firered.reader.ui_schemas import (
    BagInfo,
    BattleEvidence,
    BattlerController,
    FieldEvidence,
    FlyMapInfo,
    FnRef,
    ItemUseAnimInfo,
    ListMenuInfo,
    MainEvidence,
    MenuEvidence,
    MultichoiceInfo,
    PartyMenuInfo,
    PrinterInfo,
    ScriptEvidence,
    SMenuInfo,
    StartMenuInfo,
    TaskEvidence,
    TaskInfo,
    TextEvidence,
    UiEvidence,
    UiReadError,
    WindowInfo,
    YesNoInfo,
)
from pokegent.games.gen3.firered.rom.rom_data import RomProfile

ROM_START = 0x0800_0000
ROM_END = 0x0A00_0000
_YESNO_INPUT_DELAY_FRAMES = 5  # script_menu.c: Task_YesNoMenu_HandleInput reads buttons at tTimer 5


class _Sec:
    """Per-section bookkeeping: read errors and the profile symbols that backed the section."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.errors: list[UiReadError] = []
        self.used: set[str] = set()

    def err(self, code: str, message: str, addr: int | None = None) -> None:
        self.errors.append(UiReadError(subsystem=self.name, code=code, message=message, addr=addr))

    @property
    def ok(self) -> bool:
        return not self.errors


class UiEvidenceReader:
    def __init__(self, port: MemoryPort, profile: RomProfile) -> None:
        if not profile.supported:
            raise ValueError(f"profile {profile.profile_id} is not supported; refusing to read")
        self._port = port
        self._profile = profile
        self._fn_by_addr: dict[int, str] = {}
        for name, entry in profile.symbols.items():
            if entry.address >= ROM_START:
                self._fn_by_addr.setdefault(entry.address, name)
        self._unverified: set[str] = set()

    # -- low level -----------------------------------------------------------------------

    def _sym(self, sec: _Sec, name: str, *, required: bool = True) -> int | None:
        entry = self._profile.symbols.get(name)
        if entry is None:
            if required:
                sec.err("symbol_missing", f"profile {self._profile.profile_id} has no {name}")
            return None
        sec.used.add(name)
        return entry.address

    def _read(self, sec: _Sec, addr: int, size: int, what: str) -> bytes | None:
        try:
            raw = self._port.read_range(addr, size)
        except MemoryReadError as exc:
            sec.err("read_failed", f"{what}: {exc}", exc.addr)
            return None
        if len(raw) != size:  # a port must never return a short read
            sec.err("short_read", f"{what}: got {len(raw)} of {size} bytes", addr)
            return None
        return raw

    def _fn(self, sec: _Sec, ptr: int) -> FnRef:
        thumb_rom = ROM_START <= ptr < ROM_END and bool(ptr & 1)
        # Some emulator snapshots expose a callback without Thumb bit even though the ROM
        # entry point is Thumb. Symbol lookup is still safe after clearing bit 0; retain the
        # bit as evidence metadata but do not discard an otherwise exact profile match.
        name = self._fn_by_addr.get(ptr & ~1) if ROM_START <= (ptr & ~1) < ROM_END else None
        verified = False
        if name is not None:
            sec.used.add(name)
            verified = bool(self._profile.symbols[name].verified_by)
        return FnRef(ptr=ptr, name=name, verified=verified, thumb_rom=thumb_rom)

    def _has(self, name: str) -> bool:
        return name in self._profile.symbols

    def _task_kind_active(self, tasks: TaskEvidence, kind: str) -> bool | None:
        """Is a task of ``kind`` active? None when no symbol of that kind is in the profile."""
        if not tasks.valid or not any(
            U.TASK_KINDS.get(n) == kind and n in self._profile.symbols for n in U.TASK_KINDS
        ):
            return None
        return any(t.func.name and U.TASK_KINDS.get(t.func.name) == kind for t in tasks.active)

    def _finish(self, sec: _Sec) -> tuple[bool, float, tuple[UiReadError, ...]]:
        """(valid, confidence, errors); also records unverified symbols the section relied on."""
        unverified = {
            n
            for n in sec.used
            if n in self._profile.symbols and not self._profile.symbols[n].verified_by
        }
        self._unverified |= unverified
        if sec.errors:
            return False, 0.0, tuple(sec.errors)
        return True, (0.5 if unverified else 1.0), ()

    # -- public ---------------------------------------------------------------------------

    def read(self) -> UiEvidence:
        main_sec = _Sec("ui.main")
        main, main_raw_cb2 = self._read_main(main_sec)
        callback2_label = main.callback2.name if main.callback2 else None

        task_sec = _Sec("ui.tasks")
        tasks = self._read_tasks(task_sec)

        text_sec = _Sec("ui.text")
        windows_raw = self._read_windows(text_sec)
        text = self._read_text(text_sec, windows_raw, tasks)

        script_sec = _Sec("ui.script")
        script = self._read_script(script_sec)

        menu_sec = _Sec("ui.menu")
        menu = self._read_menu(menu_sec, windows_raw, tasks, main)

        battle = self._read_battle(main)
        field = self._read_field(main, callback2_label, tasks)
        fly_map = self._read_fly_map(callback2_label, tasks)

        # torn-read guard: gMain.callback2 must not have changed while we were reading
        if main_raw_cb2 is not None:
            again = _Sec("ui.main")
            raw = (
                self._read(
                    again,
                    self._profile.symbol("gMain").address + U.MAIN_CALLBACK2,
                    4,
                    "gMain.callback2 (again)",
                )
                if self._has("gMain")
                else None
            )
            if raw is not None and struct.unpack("<I", raw)[0] != main_raw_cb2:
                main = main.model_copy(
                    update={
                        "valid": False,
                        "confidence": 0.0,
                        "read_errors": main.read_errors
                        + (
                            UiReadError(
                                subsystem="ui.main",
                                code="torn_read",
                                message="gMain.callback2 changed while the evidence was read",
                            ),
                        ),
                    }
                )

        sections = {
            "main": main,
            "tasks": tasks,
            "text": text,
            "script": script,
            "menu": menu,
            "field": field,
        }
        if battle is not None:
            sections["battle"] = battle
        confidence = {name: sec.confidence for name, sec in sections.items()}
        errors: list[UiReadError] = []
        for sec in sections.values():
            errors.extend(sec.read_errors)

        controls_locked = None
        if main.callback2 is not None and main.callback2.name in U.FIELD_CALLBACKS:
            controls_locked = script.controls_locked
        menu_type, cursor = self._menu_hint(main, tasks, menu, battle)
        text_printer = None
        if text.valid:
            if text.waiting_for_button:
                text_printer = "waiting_for_button"
            elif text.printing:
                text_printer = "printing"

        return UiEvidence(
            valid=main.valid,
            callback_id=main.callback2.label if main.callback2 else None,
            task_signatures=tasks.signatures if tasks.valid else (),
            menu_type=menu_type,
            text_printer=text_printer,
            cursor=cursor,
            controls_locked=controls_locked,
            main=main,
            tasks=tasks,
            text=text,
            script=script,
            menu=menu,
            fly_map=fly_map,
            battle=battle,
            field=field,
            confidence=confidence,
            read_errors=tuple(errors),
            unverified_symbols=tuple(sorted(self._unverified)),
        )

    # -- region map / Fly -----------------------------------------------------------------

    def _read_fly_map(self, callback2_label: str | None, tasks: TaskEvidence) -> FlyMapInfo | None:
        """Read the live region-map objects while Task_FlyMap owns the input loop.

        The pointers are heap-backed and are normally NULL outside this scene, so this is
        intentionally gated by the two independent scene anchors.  A failed pointer/field
        read returns no info: callers must then expose Cancel only, never guess a destination.
        """
        if callback2_label != "CB2_RegionMap" or not tasks.valid:
            return None
        if not any(t.func.name == "Task_FlyMap" for t in tasks.active):
            return None
        sec = _Sec("ui.fly_map")
        ptrs: dict[str, int] = {}
        for name in ("sFlyMap", "sMapCursor", "sRegionMap"):
            address = self._sym(sec, name)
            raw = self._read(sec, address, 4, name) if address is not None else None
            if raw is None:
                continue
            ptr = struct.unpack("<I", raw)[0]
            if not (EWRAM_START <= ptr < EWRAM_END):
                sec.err("pointer_invalid", f"{name}=0x{ptr:08X} is not an EWRAM pointer", address)
            else:
                ptrs[name] = ptr
        fly = region_type = None
        if "sFlyMap" in ptrs:
            fly = self._read(sec, ptrs["sFlyMap"], 3, "FlyMap")
        if "sMapCursor" in ptrs:
            cursor_prefix = self._read(
                sec,
                ptrs["sMapCursor"],
                U.MAP_CURSOR_SELECTED_MAPSEC_TYPE + 2,
                "MapCursor fields",
            )
        else:
            cursor_prefix = None
        if "sRegionMap" in ptrs:
            region_type = self._read(
                sec,
                ptrs["sRegionMap"] + U.REGION_MAP_TYPE,
                1,
                "RegionMap type",
            )
            selected = self._read(
                sec,
                ptrs["sRegionMap"] + U.REGION_MAP_SELECTED_REGION,
                1,
                "RegionMap selected region",
            )
        else:
            selected = None
        handler = None
        if cursor_prefix is not None:
            handler_ptr = struct.unpack_from("<I", cursor_prefix, U.MAP_CURSOR_INPUT_HANDLER)[0]
            handler = self._fn(sec, handler_ptr)
        valid, _confidence, _errors = self._finish(sec)
        if (
            not valid
            or fly is None
            or cursor_prefix is None
            or region_type is None
            or selected is None
        ):
            return None
        state = fly[U.FLY_STATE]
        cursor_x, cursor_y = struct.unpack_from("<hh", cursor_prefix, U.MAP_CURSOR_X)
        move_counter = struct.unpack_from("<H", cursor_prefix, U.MAP_CURSOR_MOVE_COUNTER)[0]
        mapsec = struct.unpack_from("<H", cursor_prefix, U.MAP_CURSOR_SELECTED_MAPSEC)[0]
        mapsec_type = struct.unpack_from("<H", cursor_prefix, U.MAP_CURSOR_SELECTED_MAPSEC_TYPE)[0]
        region_id = selected[0]
        if state > 7 or cursor_x < 0 or cursor_y < 0 or cursor_x > 21 or cursor_y > 14:
            return None
        return FlyMapInfo(
            state=state,
            cursor_x=cursor_x,
            cursor_y=cursor_y,
            move_counter=move_counter,
            input_handler=handler.name,
            selected_mapsec=mapsec,
            selected_type=mapsec_type,
            region=region_id,
            input_ready=(
                state == 4
                and move_counter == 0
                and handler.name == "HandleRegionMapInput"
                and region_id == 0
                and region_type[0] in (0, 1, 2)
            ),
        )

    # -- gMain -----------------------------------------------------------------------------

    def _read_main(self, sec: _Sec) -> tuple[MainEvidence, int | None]:
        base = self._sym(sec, "gMain")
        raw = self._read(sec, base, U.MAIN_READ_SIZE, "gMain") if base is not None else None
        if raw is None:
            valid, conf, errs = self._finish(sec)
            return MainEvidence(valid=valid, confidence=conf, read_errors=errs), None
        cb1, cb2, saved = struct.unpack_from("<III", raw, 0)
        held, new = struct.unpack_from("<HH", raw, U.MAIN_HELD_KEYS)
        for label, ptr in (("callback1", cb1), ("callback2", cb2)):
            if ptr and not (ROM_START <= ptr < ROM_END and ptr & 1):
                sec.err(
                    "callback_invalid",
                    f"gMain.{label}=0x{ptr:08X} is not a Thumb ROM pointer",
                    base,
                )
        if saved and not (ROM_START <= saved < ROM_END and saved & 1):
            sec.err(
                "callback_invalid",
                f"gMain.savedCallback=0x{saved:08X} is not a Thumb ROM pointer",
                base,
            )
        if held > 0x3FF or new > 0x3FF:
            sec.err(
                "keys_invalid", f"gMain keys held=0x{held:X} new=0x{new:X} exceed 10 bits", base
            )
        if sec.errors:
            valid, conf, errs = self._finish(sec)
            return MainEvidence(valid=valid, confidence=conf, read_errors=errs), cb2
        fns = (self._fn(sec, cb1), self._fn(sec, cb2), self._fn(sec, saved))
        valid, conf, errs = self._finish(sec)
        oak_has_name = None
        oak_page = None
        naming_state = None
        naming_page = None
        naming_cursor_x = None
        naming_cursor_y = None
        oak_symbol = self._sym(sec, "sOakSpeechResources", required=False)
        if oak_symbol is not None:
            try:
                oak_ptr = struct.unpack("<I", self._port.read_range(oak_symbol, 4))[0]
                if EWRAM_START <= oak_ptr and oak_ptr + 20 <= EWRAM_END:
                    oak_raw = self._port.read_range(oak_ptr + 16, 4)
                    has_name, page = struct.unpack("<HH", oak_raw)
                    if has_name in (0, 1) and page < 32:
                        oak_has_name = bool(has_name)
                        oak_page = page
            except MemoryReadError:
                pass
        naming_symbol = self._sym(sec, "sNamingScreen", required=False)
        if naming_symbol is not None:
            try:
                naming_ptr = struct.unpack("<I", self._port.read_range(naming_symbol, 4))[0]
                if EWRAM_START <= naming_ptr and naming_ptr + 0x1E24 <= EWRAM_END:
                    naming_raw = self._port.read_range(naming_ptr + 0x1E10, 0x14)
                    candidate = naming_raw[0]
                    if candidate <= 9:
                        naming_state = candidate
                        naming_page = naming_raw[0x12]
                        sprite_id = naming_raw[0x13]
                        sprites = self._sym(sec, "gSprites", required=False)
                        if sprites is not None and sprite_id < 64:
                            cursor = self._port.read_range(sprites + sprite_id * 0x44 + 0x2E, 4)
                            naming_cursor_x, naming_cursor_y = struct.unpack("<hh", cursor)
            except MemoryReadError:
                pass
        main = MainEvidence(
            valid=valid,
            confidence=conf,
            read_errors=errs,
            callback1=fns[0],
            callback2=fns[1],
            saved_callback=fns[2],
            state=raw[U.MAIN_STATE],
            held_keys=held,
            new_keys=new,
            in_battle=bool(raw[U.MAIN_FLAGS] & U.MAIN_IN_BATTLE_BIT),
            oak_speech_has_player_name=oak_has_name,
            oak_speech_page=oak_page,
            naming_screen_state=naming_state,
            naming_screen_page=naming_page,
            naming_cursor_x=naming_cursor_x,
            naming_cursor_y=naming_cursor_y,
        )
        return main, cb2

    # -- gTasks ----------------------------------------------------------------------------

    def _read_tasks(self, sec: _Sec) -> TaskEvidence:
        base = self._sym(sec, "gTasks")
        raw = (
            self._read(sec, base, U.NUM_TASKS * U.TASK_SIZE, "gTasks") if base is not None else None
        )
        active: list[TaskInfo] = []
        if raw is not None:
            for slot in range(U.NUM_TASKS):
                off = slot * U.TASK_SIZE
                is_active = raw[off + U.TASK_IS_ACTIVE]
                if is_active == 0:
                    continue
                if is_active != 1:
                    sec.err("task_active_invalid", f"task {slot} isActive={is_active}", base + off)
                    continue
                func = struct.unpack_from("<I", raw, off + U.TASK_FUNC)[0]
                if not (ROM_START <= func < ROM_END and func & 1):
                    sec.err(
                        "task_func_invalid",
                        f"task {slot} func=0x{func:08X} is not a Thumb ROM pointer",
                        base + off,
                    )
                    continue
                prev, nxt = raw[off + U.TASK_PREV], raw[off + U.TASK_NEXT]
                for label, link in (("prev", prev), ("next", nxt)):
                    if link >= U.NUM_TASKS and link not in U.TASK_LIST_SENTINELS:
                        sec.err("task_link_invalid", f"task {slot} {label}={link}", base + off)
                data = struct.unpack_from("<16h", raw, off + U.TASK_DATA)
                fn = self._fn(sec, func)
                item_use_anim = None
                if fn.name == "Task_UseItem_Normal":
                    work = struct.unpack_from("<I", raw, off + U.TASK_DATA)[0]
                    if not EWRAM_START <= work <= EWRAM_END - U.PSA_WORK_MIN_SIZE:
                        sec.err(
                            "item_use_work_invalid",
                            f"Task_UseItem_Normal work=0x{work:08X}",
                            base + off + U.TASK_DATA,
                        )
                    else:
                        anim = self._read(
                            sec,
                            work + U.PSA_CANCEL_DISABLED,
                            U.PSA_WORK_READ_SIZE,
                            "PokemonSpecialAnim",
                        )
                        if anim is not None:
                            cancel_disabled = anim[0]
                            state = struct.unpack_from(
                                "<H", anim, U.PSA_STATE - U.PSA_CANCEL_DISABLED
                            )[0]
                            item_id = struct.unpack_from(
                                "<H", anim, U.PSA_ITEM_ID - U.PSA_CANCEL_DISABLED
                            )[0]
                            if cancel_disabled > 1 or state > U.PSA_LAST_STATE:
                                sec.err(
                                    "item_use_state_invalid",
                                    f"cancel={cancel_disabled} state={state}",
                                    work + U.PSA_CANCEL_DISABLED,
                                )
                            else:
                                item_use_anim = ItemUseAnimInfo(
                                    state=state,
                                    cancel_disabled=bool(cancel_disabled),
                                    item_id=item_id,
                                )
                active.append(
                    TaskInfo(
                        slot=slot,
                        func=fn,
                        priority=raw[off + U.TASK_PRIORITY],
                        prev=prev,
                        next=nxt,
                        data=tuple(data),
                        item_use_anim=item_use_anim,
                    )
                )
        valid, conf, errs = self._finish(sec)
        if not valid:
            return TaskEvidence(valid=False, confidence=0.0, read_errors=errs)
        signatures = tuple(sorted({t.func.label for t in active}))
        return TaskEvidence(
            valid=True,
            confidence=conf,
            active=tuple(active),
            signatures=signatures,
            count=len(active),
        )

    # -- windows / text printers ----------------------------------------------------------------

    def _read_windows(self, sec: _Sec) -> bytes | None:
        base = self._sym(sec, "gWindows")
        if base is None:
            return None
        return self._read(sec, base, U.NUM_WINDOWS * U.WINDOW_SIZE, "gWindows")

    def _read_text(self, sec: _Sec, windows_raw: bytes | None, tasks: TaskEvidence) -> TextEvidence:
        base = self._sym(sec, "sTextPrinters")
        raw = (
            self._read(sec, base, U.NUM_TEXT_PRINTERS * U.TEXT_PRINTER_SIZE, "sTextPrinters")
            if base is not None
            else None
        )
        printers: list[PrinterInfo] = []
        windows: list[WindowInfo] = []
        if windows_raw is not None:
            for wid in range(U.NUM_WINDOWS):
                w = windows_raw[wid * U.WINDOW_SIZE : (wid + 1) * U.WINDOW_SIZE]
                if w[U.WIN_BG] == U.WINDOW_FREE_BG:
                    continue
                if w[U.WIN_BG] > 3:
                    sec.err("window_bg_invalid", f"window {wid} bg={w[U.WIN_BG]}")
                    continue
                windows.append(
                    WindowInfo(
                        window_id=wid,
                        bg=w[U.WIN_BG],
                        left=w[U.WIN_LEFT],
                        top=w[U.WIN_TOP],
                        width=w[U.WIN_WIDTH],
                        height=w[U.WIN_HEIGHT],
                    )
                )
        if raw is not None:
            for wid in range(U.NUM_TEXT_PRINTERS):
                p = raw[wid * U.TEXT_PRINTER_SIZE : (wid + 1) * U.TEXT_PRINTER_SIZE]
                active = p[U.TP_ACTIVE]
                if active == 0:
                    continue
                state = p[U.TP_STATE]
                if active != 1 or state not in U.RENDER_STATE_NAMES or p[U.TP_WINDOW_ID] != wid:
                    sec.err(
                        "printer_invalid",
                        f"printer {wid}: active={active} state={state} win={p[U.TP_WINDOW_ID]}",
                        (base or 0) + wid * U.TEXT_PRINTER_SIZE,
                    )
                    continue
                printers.append(
                    PrinterInfo(
                        window_id=wid,
                        state=state,
                        state_name=U.RENDER_STATE_NAMES[state],
                        waiting_for_button=state in U.RENDER_STATES_WAITING_FOR_BUTTON,
                        text_speed=p[U.TP_TEXT_SPEED],
                        delay_counter=p[U.TP_DELAY_COUNTER],
                        font_id=p[U.TP_FONT_ID],
                        current_char_ptr=struct.unpack_from("<I", p, U.TP_CURRENT_CHAR)[0],
                    )
                )
        valid, conf, errs = self._finish(sec)
        if not valid:
            return TextEvidence(valid=False, confidence=0.0, read_errors=errs)
        return TextEvidence(
            valid=True,
            confidence=conf,
            printers=tuple(printers),
            windows=tuple(windows),
            any_active=bool(printers),
            printing=any(not p.waiting_for_button for p in printers),
            waiting_for_button=any(p.waiting_for_button for p in printers),
            field_message_task=self._task_kind_active(tasks, "field_message_box"),
        )

    # -- field script context ----------------------------------------------------------------

    def _read_script(self, sec: _Sec) -> ScriptEvidence:
        status_addr = self._sym(sec, "sGlobalScriptContextStatus")
        ctx_addr = self._sym(sec, "sGlobalScriptContext")
        lock_addr = self._sym(sec, "sLockFieldControls")
        status = ctx = lock = None
        if status_addr is not None:
            raw = self._read(sec, status_addr, 1, "sGlobalScriptContextStatus")
            status = raw[0] if raw else None
        if ctx_addr is not None:
            ctx = self._read(sec, ctx_addr, U.SCRIPT_CONTEXT_SIZE, "sGlobalScriptContext")
        if lock_addr is not None:
            raw = self._read(sec, lock_addr, 1, "sLockFieldControls")
            lock = raw[0] if raw else None
        mode = native_ptr = script_ptr = depth = None
        if ctx is not None:
            depth = ctx[U.SCTX_STACK_DEPTH]
            mode = ctx[U.SCTX_MODE]
            native_ptr = struct.unpack_from("<I", ctx, U.SCTX_NATIVE_PTR)[0]
            script_ptr = struct.unpack_from("<I", ctx, U.SCTX_SCRIPT_PTR)[0]
            if mode not in U.SCRIPT_MODE_NAMES:
                sec.err("script_mode_invalid", f"script mode {mode}", ctx_addr)
        if status is not None and status not in U.SCRIPT_STATUS_NAMES:
            sec.err("script_status_invalid", f"script status {status}", status_addr)
        if lock is not None and lock > 1:
            sec.err("lock_invalid", f"sLockFieldControls={lock}", lock_addr)
        native = None
        if mode == 2 and native_ptr is not None:
            if not (ROM_START <= native_ptr < ROM_END and native_ptr & 1):
                sec.err(
                    "native_invalid", f"native ptr 0x{native_ptr:08X} while mode=native", ctx_addr
                )
            else:
                native = self._fn(sec, native_ptr)
        valid, conf, errs = self._finish(sec)
        if not valid:
            # symbols that were readable still report their part as unknown-safe None values
            return ScriptEvidence(valid=False, confidence=0.0, read_errors=errs)
        return ScriptEvidence(
            valid=True,
            confidence=conf,
            status=status,
            status_name=U.SCRIPT_STATUS_NAMES.get(status) if status is not None else None,
            mode=mode,
            mode_name=U.SCRIPT_MODE_NAMES.get(mode) if mode is not None else None,
            running=(status != 2) if status is not None else None,
            native=native,
            script_ptr=script_ptr,
            stack_depth=depth,
            controls_locked=bool(lock) if lock is not None else None,
        )

    # -- menus -----------------------------------------------------------------------------

    def _window_in_use(self, windows_raw: bytes | None, window_id: int) -> bool | None:
        if windows_raw is None:
            return None
        if not 0 <= window_id < U.NUM_WINDOWS:
            return False
        return windows_raw[window_id * U.WINDOW_SIZE + U.WIN_BG] != U.WINDOW_FREE_BG

    def _read_menu(
        self,
        sec: _Sec,
        windows_raw: bytes | None,
        tasks: TaskEvidence,
        main: MainEvidence,
    ) -> MenuEvidence:
        menu_info = yes_no = multichoice = list_menu = start_menu = party = bag = None
        summary_open = quest_log = None

        def task_with_kind(kind: str) -> list[TaskInfo]:
            if not tasks.valid:
                return []
            return [
                t for t in tasks.active if t.func.name and U.TASK_KINDS.get(t.func.name) == kind
            ]

        def kind_resolvable(kind: str) -> bool:
            return any(U.TASK_KINDS.get(n) == kind and self._has(n) for n in U.TASK_KINDS)

        # sMenu
        base = self._sym(sec, "sMenu")
        raw = self._read(sec, base, U.SMENU_SIZE, "sMenu") if base is not None else None
        if raw is not None:
            cursor, lo, hi = (
                struct.unpack_from("<b", raw, o)[0]
                for o in (U.SMENU_CURSOR_POS, U.SMENU_MIN_CURSOR_POS, U.SMENU_MAX_CURSOR_POS)
            )
            wid = raw[U.SMENU_WINDOW_ID]
            menu_info = SMenuInfo(
                left=raw[U.SMENU_LEFT],
                top=raw[U.SMENU_TOP],
                cursor_pos=cursor,
                min_cursor_pos=lo,
                max_cursor_pos=hi,
                window_id=wid,
                font_id=raw[U.SMENU_FONT_ID],
                option_width=raw[U.SMENU_OPTION_WIDTH],
                option_height=raw[U.SMENU_OPTION_HEIGHT],
                columns=raw[U.SMENU_COLUMNS],
                rows=raw[U.SMENU_ROWS],
                window_in_use=bool(self._window_in_use(windows_raw, wid)),
                consistent=lo <= cursor <= hi and wid < U.NUM_WINDOWS,
            )

        # yes/no
        base = self._sym(sec, "sYesNoWindowId")
        raw = self._read(sec, base, 1, "sYesNoWindowId") if base is not None else None
        if raw is not None:
            wid = raw[0]
            in_use = bool(self._window_in_use(windows_raw, wid))
            yn_tasks = task_with_kind("yes_no")
            task_active: bool | None
            ready = None
            if not tasks.valid or not kind_resolvable("yes_no"):
                task_active = None
            else:
                task_active = bool(yn_tasks)
                if yn_tasks:
                    ready = yn_tasks[0].data[U.YESNO_TASK_TIMER] >= _YESNO_INPUT_DELAY_FRAMES
            yes_no = YesNoInfo(
                window_id=wid,
                window_in_use=in_use,
                task_active=task_active,
                input_ready=ready,
                field_prompt_open=(task_active and in_use) if task_active is not None else None,
            )

        # multichoice
        for task in task_with_kind("multichoice")[:1]:
            wid = task.data[U.MULTICHOICE_TASK_WINDOW_ID]
            multichoice = MultichoiceInfo(
                task_slot=task.slot,
                window_id=wid & 0xFF,
                multichoice_id=task.data[U.MULTICHOICE_TASK_ID] & 0xFFFF,
                ignore_b=bool(task.data[U.MULTICHOICE_TASK_IGNORE_B]),
                wrap_around=bool(task.data[U.MULTICHOICE_TASK_WRAP]),
                window_in_use=bool(self._window_in_use(windows_raw, wid & 0xFF)),
            )

        # list menu (struct ListMenu is stored in the dummy task's data words)
        # ``Task_FieldListMenuHandleInput`` is an input task, not a ``struct ListMenu``.
        # During the elevator script handoff it can outlive the real ListMenuDummyTask and
        # its first data words happen to decode as plausible menu fields.  Prefer the dummy
        # task (and an in-use window) so scrolling/cursor readback cannot use stale input-task
        # data.  See pokefirered src/field_specials.c:1407-1450.
        list_tasks = task_with_kind("list_menu")

        def list_task_priority(task: TaskInfo) -> tuple[bool, bool]:
            window_id = (
                task.data[U.LISTMENU_WINDOW_ID]
                if len(task.data) > U.LISTMENU_WINDOW_ID
                else U.NUM_WINDOWS
            )
            return (
                task.func.name != "ListMenuDummyTask",
                not bool(self._window_in_use(windows_raw, window_id)),
            )

        list_tasks.sort(key=list_task_priority)
        for task in list_tasks[:1]:
            blob = struct.pack("<16h", *task.data)
            items_ptr = struct.unpack_from("<I", blob, 0)[0]
            total, shown = struct.unpack_from("<HH", blob, U.LISTMENU_TOTAL_ITEMS)
            wid = blob[U.LISTMENU_WINDOW_ID]
            scroll, row = struct.unpack_from("<HH", blob, U.LISTMENU_CURSOR_POS)
            list_menu = ListMenuInfo(
                task_slot=task.slot,
                total_items=total,
                max_showed=shown,
                window_id=wid,
                scroll_offset=scroll,
                cursor_row=row,
                window_in_use=bool(self._window_in_use(windows_raw, wid)),
                items_ptr=items_ptr,
            )

        # start menu
        cur_a = self._sym(sec, "sStartMenuCursorPos", required=False)
        num_a = self._sym(sec, "sNumStartMenuItems", required=False)
        win_a = self._sym(sec, "sStartMenuWindowId", required=False)
        if cur_a is not None and num_a is not None:
            cur = self._read(sec, cur_a, 1, "sStartMenuCursorPos")
            num = self._read(sec, num_a, 1, "sNumStartMenuItems")
            win = self._read(sec, win_a, 1, "sStartMenuWindowId") if win_a is not None else None
            if cur is not None and num is not None:
                start_menu = StartMenuInfo(
                    cursor_pos=cur[0],
                    num_items=num[0],
                    window_id=win[0] if win is not None else None,
                    task_active=(
                        bool(task_with_kind("start_menu"))
                        if tasks.valid and kind_resolvable("start_menu")
                        else None
                    ),
                )

        cb2_kind = U.CALLBACK2_KINDS.get(main.callback2.name or "") if main.callback2 else None

        # party menu struct (persists after the screen closed -> active_hint)
        pm_a = self._sym(sec, "gPartyMenu", required=False)
        if pm_a is not None:
            raw = self._read(sec, pm_a, U.PARTY_MENU_SIZE, "gPartyMenu")
            if raw is not None:
                item_id = None
                item_a = self._sym(sec, "gSpecialVar_ItemId", required=False)
                if item_a is not None:
                    item_raw = self._read(sec, item_a, 2, "gSpecialVar_ItemId")
                    if item_raw is not None:
                        item_id = struct.unpack_from("<H", item_raw)[0]
                exit_cb, task_fn = struct.unpack_from("<II", raw, 0)
                hint: bool | None = None
                if (
                    main.valid
                    and tasks.valid
                    and kind_resolvable("party_choose_mon")
                    and cb2_kind is not None
                ):
                    hint = (
                        cb2_kind == "party_menu"
                        or bool(task_with_kind("party_choose_mon"))
                        or bool(task_with_kind("party_submenu"))
                        or bool(task_with_kind("party_use_item"))
                    )
                party = PartyMenuInfo(
                    exit_callback=self._fn(sec, exit_cb),
                    task=self._fn(sec, task_fn),
                    menu_type=raw[U.PM_TYPE_LAYOUT] & 0x0F,
                    layout=(raw[U.PM_TYPE_LAYOUT] >> 4) & 0x03,
                    slot_id=struct.unpack_from("<b", raw, U.PM_SLOT_ID)[0],
                    slot_id2=struct.unpack_from("<b", raw, U.PM_SLOT_ID2)[0],
                    action=raw[U.PM_ACTION],
                    action_name=U.PARTY_ACTION_NAMES.get(raw[U.PM_ACTION]),
                    forced_send_out=(
                        (raw[U.PM_ACTION] == U.PARTY_ACTION_SEND_OUT and bool(hint))
                        if main.in_battle
                        else None
                    ),
                    bag_item=struct.unpack_from("<H", raw, U.PM_BAG_ITEM)[0],
                    active_hint=hint,
                    item_id=item_id,
                )
                internal_a = self._sym(sec, "sPartyMenuInternal", required=False)
                if internal_a is not None:
                    internal_ptr = self._read(sec, internal_a, 4, "sPartyMenuInternal")
                    if internal_ptr is not None:
                        ptr = struct.unpack_from("<I", internal_ptr, 0)[0]
                        if EWRAM_START <= ptr and ptr + 0x18 <= EWRAM_END:
                            internal = self._read(sec, ptr, 0x18, "PartyMenuInternal")
                            if internal is not None:
                                num = internal[0x17]
                                if num <= 8:
                                    party = party.model_copy(
                                        update={
                                            "action_ids": tuple(internal[0x0F : 0x0F + num]),
                                            "action_num": num,
                                        }
                                    )

        # bag state
        bg_a = self._sym(sec, "gBagMenuState", required=False)
        if bg_a is not None:
            raw = self._read(sec, bg_a, U.BAG_STATE_SIZE, "gBagMenuState")
            if raw is not None:
                callback = struct.unpack_from("<I", raw, U.BAG_CALLBACK)[0]
                hint = None
                if main.valid and tasks.valid and cb2_kind is not None:
                    hint = cb2_kind == "bag"
                bag = BagInfo(
                    callback=self._fn(sec, callback),
                    location=raw[U.BAG_LOCATION],
                    bag_open=bool(raw[U.BAG_OPEN]),
                    pocket=struct.unpack_from("<H", raw, U.BAG_POCKET)[0],
                    items_above=struct.unpack_from("<3H", raw, U.BAG_ITEMS_ABOVE),
                    cursor_pos=struct.unpack_from("<3H", raw, U.BAG_CURSOR_POS),
                    active_hint=hint,
                )

        # summary screen pointer, quest log
        sm_a = self._sym(sec, "sMonSummaryScreen", required=False)
        if sm_a is not None:
            raw = self._read(sec, sm_a, 4, "sMonSummaryScreen")
            if raw is not None:
                ptr = struct.unpack("<I", raw)[0]
                summary_open = EWRAM_START <= ptr < EWRAM_END
        ql_a = self._sym(sec, "gQuestLogState", required=False)
        if ql_a is not None:
            raw = self._read(sec, ql_a, 1, "gQuestLogState")
            quest_log = raw[0] if raw else None

        valid, conf, errs = self._finish(sec)
        if not valid:
            return MenuEvidence(valid=False, confidence=0.0, read_errors=errs)
        return MenuEvidence(
            valid=True,
            confidence=conf,
            menu=menu_info,
            yes_no=yes_no,
            multichoice=multichoice,
            list_menu=list_menu,
            start_menu=start_menu,
            party=party,
            bag=bag,
            summary_screen_open=summary_open,
            quest_log_state=quest_log,
        )

    # -- battle ------------------------------------------------------------------------------

    def _read_battle(self, main: MainEvidence) -> BattleEvidence | None:
        sec = _Sec("ui.battle")
        if not main.valid or main.in_battle is None:
            sec.err(
                "battle_gate_unknown", "gMain unreadable: cannot tell whether a battle is active"
            )
            valid, conf, errs = self._finish(sec)
            return BattleEvidence(valid=False, confidence=0.0, read_errors=errs)
        if not main.in_battle:
            return None

        flags_a = self._sym(sec, "gBattleTypeFlags")
        active_a = self._sym(sec, "gActiveBattler")
        count_a = self._sym(sec, "gBattlersCount")
        ctl_a = self._sym(sec, "gBattlerControllerFuncs")
        exec_a = self._sym(sec, "gBattleControllerExecFlags", required=False)
        party_a = self._sym(sec, "gBattlerPartyIndexes", required=False)
        pos_a = self._sym(sec, "gBattlerPositions", required=False)
        absent_a = self._sym(sec, "gAbsentBattlerFlags", required=False)
        act_cur_a = self._sym(sec, "gActionSelectionCursor", required=False)
        mov_cur_a = self._sym(sec, "gMoveSelectionCursor", required=False)
        tgt_cur_a = self._sym(sec, "gMultiUsePlayerCursor", required=False)

        def rd(addr: int | None, size: int, what: str) -> bytes | None:
            return self._read(sec, addr, size, what) if addr is not None else None

        flags_raw = rd(flags_a, 4, "gBattleTypeFlags")
        active_raw = rd(active_a, 1, "gActiveBattler")
        count_raw = rd(count_a, 1, "gBattlersCount")
        ctl_raw = rd(ctl_a, 4 * U.MAX_BATTLERS_COUNT, "gBattlerControllerFuncs")
        exec_raw = rd(exec_a, 4, "gBattleControllerExecFlags")
        party_raw = rd(party_a, 2 * U.MAX_BATTLERS_COUNT, "gBattlerPartyIndexes")
        pos_raw = rd(pos_a, U.MAX_BATTLERS_COUNT, "gBattlerPositions")
        absent_raw = rd(absent_a, 1, "gAbsentBattlerFlags")
        act_raw = rd(act_cur_a, U.MAX_BATTLERS_COUNT, "gActionSelectionCursor")
        mov_raw = rd(mov_cur_a, U.MAX_BATTLERS_COUNT, "gMoveSelectionCursor")
        tgt_raw = rd(tgt_cur_a, 1, "gMultiUsePlayerCursor")

        type_flags = struct.unpack("<I", flags_raw)[0] if flags_raw else None
        count = count_raw[0] if count_raw else None
        active = active_raw[0] if active_raw else None
        if count is not None and count > U.MAX_BATTLERS_COUNT:
            sec.err("battlers_count_invalid", f"gBattlersCount={count}", count_a)
        # gActiveBattler is a loop variable: it rests at gBattlersCount (2 in singles, 4 in
        # doubles) when no command is being dispatched, so MAX_BATTLERS_COUNT is a legal value.
        if active is not None and active > U.MAX_BATTLERS_COUNT:
            sec.err("active_battler_invalid", f"gActiveBattler={active}", active_a)
        controllers: list[BattlerController] = []
        if ctl_raw is not None and count is not None and count <= U.MAX_BATTLERS_COUNT:
            for battler in range(count):
                ptr = struct.unpack_from("<I", ctl_raw, battler * 4)[0]
                if not (ROM_START <= ptr < ROM_END and ptr & 1):
                    sec.err(
                        "controller_invalid",
                        f"gBattlerControllerFuncs[{battler}]=0x{ptr:08X} is no Thumb ROM pointer",
                        ctl_a,
                    )
                    continue
                fn = self._fn(sec, ptr)
                phase, variant = U.CONTROLLER_PHASES.get(fn.name or "", ("unknown", None))
                controllers.append(
                    BattlerController(
                        battler=battler,
                        func=fn,
                        phase=phase,
                        variant=variant,
                        accepts_input=(phase in U.CONTROLLER_INPUT_PHASES)
                        if phase != "unknown"
                        else None,
                        player_side=battler in U.PLAYER_SIDE_BATTLERS,
                    )
                )
        valid, conf, errs = self._finish(sec)
        if not valid:
            return BattleEvidence(valid=False, confidence=0.0, read_errors=errs, active=True)
        names = tuple(
            n
            for n, bit in U.BATTLE_TYPE_FLAGS.items()
            if type_flags is not None and type_flags & bit
        )
        return BattleEvidence(
            valid=True,
            confidence=conf,
            active=True,
            type_flags=type_flags,
            type_flag_names=names,
            is_double=bool(type_flags & U.BATTLE_TYPE_FLAGS["double"])
            if type_flags is not None
            else None,
            is_trainer=bool(type_flags & U.BATTLE_TYPE_FLAGS["trainer"])
            if type_flags is not None
            else None,
            is_safari=bool(type_flags & U.BATTLE_TYPE_FLAGS["safari"])
            if type_flags is not None
            else None,
            battlers_count=count,
            active_battler=active,
            exec_flags=struct.unpack("<I", exec_raw)[0] if exec_raw else None,
            controllers=tuple(controllers),
            action_cursor=tuple(act_raw) if act_raw else (),
            move_cursor=tuple(mov_raw) if mov_raw else (),
            target_cursor=tgt_raw[0] if tgt_raw else None,
            battler_positions=tuple(pos_raw) if pos_raw else (),
            party_indexes=struct.unpack("<4H", party_raw) if party_raw else (),
            absent_flags=absent_raw[0] if absent_raw else None,
            input_battlers=tuple(
                c.battler for c in controllers if c.player_side and c.accepts_input
            ),
        )

    # -- field / avatar ---------------------------------------------------------------------

    def _read_field(
        self, main: MainEvidence, callback2_name: str | None, tasks: TaskEvidence
    ) -> FieldEvidence:
        sec = _Sec("ui.field")
        av_a = self._sym(sec, "gPlayerAvatar")
        raw = self._read(sec, av_a, 8, "gPlayerAvatar") if av_a is not None else None
        fade_a = self._sym(sec, "gPaletteFade", required=False)
        fade = self._read(sec, fade_a, U.PF_SIZE, "gPaletteFade") if fade_a is not None else None
        fanfare_a = self._sym(sec, "sFanfareCounter", required=False)
        fanfare = (
            self._read(sec, fanfare_a, 2, "sFanfareCounter") if fanfare_a is not None else None
        )
        flags = running = transition = prevent = None
        if raw is not None:
            flags = raw[U.AVATAR_FLAGS]
            running = raw[U.AVATAR_RUNNING_STATE]
            transition = raw[U.AVATAR_TILE_TRANSITION]
            prevent = raw[U.AVATAR_PREVENT_STEP]
            if running not in U.RUNNING_STATE_NAMES or transition not in U.TILE_TRANSITION_NAMES:
                sec.err(
                    "avatar_state_invalid",
                    f"avatar runningState={running} tileTransitionState={transition}",
                    av_a,
                )
        fade_active = fade_y = fade_target = None
        if fade is not None:
            word_yt = struct.unpack_from("<H", fade, U.PF_Y_TARGET_WORD)[0]
            word_active = struct.unpack_from("<H", fade, U.PF_ACTIVE_WORD)[0]
            fade_active = bool(word_active & U.PF_ACTIVE_MASK)
            fade_y = (word_yt >> 6) & 0x1F
            fade_target = (word_yt >> 11) & 0x1F
        valid, conf, errs = self._finish(sec)
        if not valid:
            return FieldEvidence(valid=False, confidence=0.0, read_errors=errs)
        kind = U.CALLBACK2_KINDS.get(callback2_name or "") if callback2_name else None
        # the avatar block keeps its last values under battle / menu callbacks: walking facts only
        # mean something while the field callback runs
        in_field = callback2_name in U.FIELD_CALLBACKS
        warp_pending: bool | None = None
        warp_task = self._task_kind_active(tasks, "warp")
        if main.valid and callback2_name is not None and kind == "map_load":
            warp_pending = True
        elif main.valid and warp_task is not None:
            warp_pending = warp_task
        elif main.valid and kind is not None:
            warp_pending = False
        fade_direction = None
        if fade_active:
            fade_direction = "out" if (fade_target or 0) > 0 else "in"
        assert flags is not None and running is not None and transition is not None
        return FieldEvidence(
            valid=True,
            confidence=conf,
            avatar_flags=flags,
            on_foot=bool(flags & 0x01),
            mach_bike=bool(flags & 0x02),
            acro_bike=bool(flags & 0x04),
            surfing=bool(flags & 0x08),
            underwater=bool(flags & 0x10),
            controllable_flag=bool(flags & 0x20),
            forced_movement=bool(flags & 0x40),
            running_state=U.RUNNING_STATE_NAMES[running],
            tile_transition=U.TILE_TRANSITION_NAMES[transition],
            moving=(transition != 0) if in_field else None,
            turning=(running == 1 and transition == 0) if in_field else None,
            prevent_step=bool(prevent),
            fade_active=fade_active,
            fade_y=fade_y,
            fade_target_y=fade_target,
            fade_direction=fade_direction,
            fanfare_counter=struct.unpack("<H", fanfare)[0] if fanfare is not None else None,
            warp_pending=warp_pending,
        )

    # -- convenience hints ------------------------------------------------------------------

    def _menu_hint(
        self,
        main: MainEvidence,
        tasks: TaskEvidence,
        menu: MenuEvidence,
        battle: BattleEvidence | None,
    ) -> tuple[str | None, int | None]:
        """(menu_type, cursor): only corroborated menus; everything else stays None."""
        cb2_kind = U.CALLBACK2_KINDS.get(main.callback2.name or "") if main.callback2 else None
        if cb2_kind in {
            "party_menu",
            "bag",
            "shop",
            "pc_storage",
            "pokedex",
            "easy_chat",
            "evolution",
            "naming",
            "region_map",
            "summary",
        }:
            return cb2_kind, None
        if not menu.valid:
            return None, None
        sm = menu.menu
        if menu.yes_no and menu.yes_no.field_prompt_open:
            return "yes_no", sm.cursor_pos if sm and sm.window_in_use and sm.consistent else None
        if menu.multichoice and menu.multichoice.window_in_use:
            return (
                "multichoice",
                sm.cursor_pos if sm and sm.window_in_use and sm.consistent else None,
            )
        if menu.list_menu and menu.list_menu.window_in_use:
            return "list", menu.list_menu.scroll_offset + menu.list_menu.cursor_row
        if menu.start_menu and menu.start_menu.task_active:
            # the start-menu task also stays alive under its own sub-dialogs (SAVE prompt, ...);
            # sMenu belongs to the start menu itself only while it sits in the start-menu window
            if sm and sm.window_in_use and sm.window_id == menu.start_menu.window_id:
                return "start_menu", menu.start_menu.cursor_pos
            return "start_menu_subprompt", sm.cursor_pos if sm and sm.window_in_use else None
        if battle is not None and battle.valid and battle.input_battlers:
            lead = battle.input_battlers[0]
            ctl = next(c for c in battle.controllers if c.battler == lead)
            if ctl.phase == "action" and lead < len(battle.action_cursor):
                return "battle_action", battle.action_cursor[lead]
            if ctl.phase == "move" and lead < len(battle.move_cursor):
                return "battle_move", battle.move_cursor[lead]
            if ctl.phase == "target":
                return "battle_target", battle.target_cursor
            return f"battle_{ctl.phase}", None
        return None, None


def read_ui_evidence(port: MemoryPort, profile: RomProfile) -> UiEvidence:
    """One-shot helper: ``UiEvidenceReader(port, profile).read()``."""
    return UiEvidenceReader(port, profile).read()
