"""The composite snapshot source: ONE atomic read per perception step feeds every family (M2.5).

Before M2.5 each family shipped its own source (``DialogSnapshotSource``: core + ``ui`` + text
sidecar; ``BattleSnapshotSource``: core + ``ui`` + ``battle`` block) and a plain
``LiveSnapshotSource`` had no ``ui`` block at all, so a runner wired with two families would have
read the emulator twice per perception step and could not have mixed the two. This module reads the
emulator once and builds everything from that single
:class:`~pokegent.emulator.AtomicSnapshot` (one frozen copy of EWRAM, IWRAM and the
ROM image), so every block describes the same frame.

Who owns which block (readers stay where their owner put them; this module only orchestrates):

* core (player, party, bag, flags, vars): the WP1 reader, ``firered.live.LiveReader``; plain
  ``StateSnapshot`` fields.
* ``ui`` (UI evidence): M2.1 perception, shared by all families, ``UiEvidenceReader``;
  ``snapshot.ui``, ``state_hash`` covers ``ui.scene_digest()``.
* ``map``: the M2.2 map reader, ``firered.map_data.MapReader`` + ``RomMapCache``, consumed by the
  field family; ``snapshot.map``, ``state_hash`` covers ``map.digest``; not read inside a battle.
* ``battle``: the battle family, ``actions.battle.reader.BattleReader``; ``snapshot.battle``
  (a ``BattleSnapshot`` subclass of ``StateSnapshot``), NOT part of ``state_hash``.
* dialog text: the dialog family, ``pokegent.actions.menu.dialog.text.read_dialog_text``; a
``TextSidecar``
  keyed by ``(snapshot_seq, state_hash)`` because the frozen snapshot has no text field.

Contract notes (docs/contracts.md v1.1):

* ``snapshot_seq`` / ``emulator_frame`` / ``state_hash`` are exactly those of the core read; the
  text sidecar is filed under the FINAL snapshot's identity (after ``ui`` and ``map`` changed the
  hash), so a detector or builder that looks a snapshot up finds its own moment.
* ``battle`` is gated on ``ui.main.in_battle`` (stale battle RAM exists in the overworld) and the
  map is not read while a battle is on: both are policy of the owning readers, not of this module.
* A block that cannot be read never invalidates the snapshot: the ``battle`` block carries its own
  ``valid``/``read_errors``; a failed map read leaves ``snapshot.map`` empty.
* Read-only. Nothing here presses, writes RAM or touches the ROM file.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Callable, Literal

from pokegent.actions.battle import state as battle_state
from pokegent.actions.battle.state import BattleBlock
from pokegent.emulator import AtomicRangeRead, AtomicSnapshot, MemoryAccessError
from pokegent.games.gen3.firered.maps import map_constants
from pokegent.games.gen3.firered.maps.map_data import MapReader, RomMapCache
from pokegent.games.gen3.firered.reader import ui_symbols as ui_layout
from pokegent.games.gen3.firered.reader.battle_reader import BattleReader, BattleSnapshot
from pokegent.games.gen3.firered.reader.live import (
    AtomicSnapshotPort,
    AtomicSnapshotSource,
    LiveReader,
    live_snapshot_seq,
)
from pokegent.games.gen3.firered.reader.memory_port import (
    EWRAM_END,
    EWRAM_START,
    MemoryReadError,
)
from pokegent.games.gen3.firered.reader.reader import PartySource
from pokegent.games.gen3.firered.reader.schemas import ReadError, StateSnapshot
from pokegent.games.gen3.firered.reader.storage import (
    S_STORAGE_CURSOR_AREA_ADDRESS,
    S_STORAGE_MOVE_STATE_SIZE,
)
from pokegent.games.gen3.firered.reader.ui import UiEvidenceReader
from pokegent.games.gen3.firered.rom import constants as core_constants
from pokegent.games.gen3.firered.rom.dialog_text import (
    DialogText,
    TextSidecar,
    read_dialog_text,
)
from pokegent.games.gen3.firered.rom.rom_data import RomProfile

MapMode = Literal["off", "field", "always"]

#: block -> owner, for docs, the coverage table and tests (kept next to the code that builds them)
BLOCK_OWNERS: dict[str, str] = {
    "core": "WP1 reader (firered.reader / firered.live)",
    "ui": "M2.1 perception (shared by all families)",
    "map": "M2.2 map reader (consumed by the field family)",
    "battle": "battle family (actions.battle.reader)",
    "dialog_text": "dialog family (pokegent.actions.menu.dialog.text, TextSidecar)",
}


@dataclass(frozen=True)
class CompositeRead:
    """Everything one perception step produced, for tools and tests (``source.last``)."""

    snapshot: StateSnapshot
    atomic_frame: int
    battle: BattleBlock | None
    text: DialogText | None
    map_errors: tuple[ReadError, ...] = ()


class _RangeReadPort:
    """Memory-port adapter for an actor's sparse atomic range read."""

    def __init__(self, sample: AtomicRangeRead) -> None:
        self._sample = sample

    def read_range(self, addr: int, size: int) -> bytes:
        try:
            return self._sample.read_range(addr, size)
        except MemoryAccessError as exc:
            raise MemoryReadError(addr, size, str(exc)) from exc

    def read_u32(self, addr: int) -> int:
        return int.from_bytes(self.read_range(addr, 4), "little")


class CompositeSnapshotSource:
    """``SnapshotSource`` yielding core + ``ui`` (+ ``map``) (+ ``battle``) + dialog text sidecar.

    ``map_mode``: ``"field"`` (default) reads the map block whenever no battle is on, ``"always"``
    also inside battles, ``"off"`` never (cheaper; the field family then has no map). The map
    reader runs on the same atomic snapshot and costs about 2-4 ms with a warm ROM cache.
    """

    def __init__(
        self,
        source: AtomicSnapshotSource,
        profile: RomProfile,
        *,
        sidecar: TextSidecar | None = None,
        run_id: str = "",
        party_source: PartySource = "live",
        map_mode: MapMode = "field",
        map_cache: RomMapCache | None = None,
        include_battle: bool = True,
    ) -> None:
        if map_mode not in ("off", "field", "always"):
            raise ValueError(f"map_mode must be off|field|always, not {map_mode!r}")
        self._source = source
        self._profile = profile
        self._sidecar = sidecar
        self._live = LiveReader(source, profile, run_id=run_id, party_source=party_source)
        self._map_mode: MapMode = map_mode
        self._map_cache = map_cache if map_cache is not None else RomMapCache()
        self._include_battle = include_battle
        self.reads = 0  # perception reads made through this source (atomic snapshots taken)
        self.probes = 0  # cheap executor polls; these do not decode a snapshot
        self.last: CompositeRead | None = None
        self._last_atomic: AtomicSnapshot | None = None
        self._last_probes: dict[str, tuple[bytes, ...]] = {}
        self._save_blocks: tuple[int, int] | None = None
        self._player_object_id: int | None = None
        self._fn_names = {
            symbol.address: name
            for name, symbol in profile.symbols.items()
            if symbol.address >= 0x0800_0000
        }

    @property
    def sidecar(self) -> TextSidecar | None:
        return self._sidecar

    @property
    def profile(self) -> RomProfile:
        return self._profile

    @property
    def party_source(self) -> PartySource:
        """Expose the source contract used by the assembled runner."""
        return self._live.party_source

    def read_snapshot(self) -> StateSnapshot:
        atomic = self._source.read_atomic_snapshot()  # the ONE emulator read of this step
        self.reads += 1
        return self.build(atomic)

    def decision_guard(
        self, snapshot_seq: int, *, allow_invalid_core: bool = False
    ) -> Callable[[AtomicSnapshot], bool]:
        """Compare every idle frame with the exact atomic sample behind the offered frame.

        A separate decoder owns its caches and text sidecar on the emulator thread. No live
        actor reads, route planning, builders, provider calls or input happen in this guard.
        Always decode the map here, even if normal perception has map_mode=off.
        """
        from pokegent.games.gen3.firered.reader.decision_idle import (
            DecisionReadTrace,
            observable_state,
        )

        baseline = self._last_atomic
        if baseline is None or live_snapshot_seq(baseline) != snapshot_seq:
            raise ValueError("decision idle needs the exact decision snapshot")
        decoder = CompositeSnapshotSource(
            self._source, self._profile, sidecar=TextSidecar(),
            party_source=self.party_source, map_mode="field", include_battle=True,
        )
        trace = DecisionReadTrace(baseline)
        decoder.build(trace)
        expected = observable_state(decoder.last, allow_invalid_core=allow_invalid_core)
        ranges = trace.probe_ranges(self._profile, decoder.last)
        previous = tuple(baseline.read_range(addr, size) for addr, size in ranges)

        def unchanged(atomic: AtomicSnapshot) -> bool:
            nonlocal ranges, previous
            if atomic.load_epoch != baseline.load_epoch:
                return False
            current = tuple(atomic.read_range(addr, size) for addr, size in ranges)
            if current == previous:
                return True
            trace = DecisionReadTrace(atomic)
            decoder.build(trace)
            matches = observable_state(
                decoder.last, allow_invalid_core=allow_invalid_core
            ) == expected
            if matches:
                # Pointer targets and active menus can alter the dependency set. Rebuild it
                # after every decode; compare against the last equivalent frame, not a timer.
                ranges = trace.probe_ranges(self._profile, decoder.last)
                previous = tuple(atomic.read_range(addr, size) for addr, size in ranges)
            return matches

        return unchanged

    def read_snapshot_if_changed(self, *, mode: str = "wait") -> StateSnapshot | None:
        """Cheap executor poll: decode fully only when decision-relevant RAM changed.

        The probe contains the raw inputs used by executor waits (callbacks and active tasks,
        window/printer/menu state, script locks, player/map/party/bag/storage state, and battle
        commitment words).  It deliberately omits animation counters and the battle-script PC,
        which churn every frame without making an executor condition true.  ``Executor`` still
        forces a full snapshot every 30 frames and at every frame/wall limit.
        """
        self.probes += 1
        if mode not in ("wait", "input"):
            raise ValueError(f"unknown probe mode {mode!r}")
        read_ranges = getattr(self._source, "read_ranges", None)
        if callable(read_ranges) and self._save_blocks is not None:
            sample = read_ranges(self._probe_ranges(*self._save_blocks))
            probe = self._probe(
                _RangeReadPort(sample), sample.frame, save_blocks=self._save_blocks, mode=mode
            )
            if probe == self._last_probes.get(mode):
                return None
            return self.read_snapshot()

        atomic = self._source.read_atomic_snapshot()
        probe = self._probe(AtomicSnapshotPort(atomic), atomic.frame, mode=mode)
        if probe == self._last_probes.get(mode):
            return None
        self.reads += 1
        return self.build(atomic)

    def build(self, atomic: AtomicSnapshot) -> StateSnapshot:
        """The composite snapshot of an atomic snapshot you already hold (tools, replays)."""
        self._last_atomic = atomic
        port = AtomicSnapshotPort(atomic)
        self._save_blocks = self._read_save_blocks(port)
        try:
            self._player_object_id = port.read_u8(
                self._profile.symbol("gPlayerAvatar").address
                + core_constants.AVATAR_OBJECT_EVENT_ID_OFFSET
            )
        except MemoryReadError:
            self._player_object_id = None
        self._last_probes = {
            mode: self._probe(port, atomic.frame, save_blocks=self._save_blocks, mode=mode)
            for mode in ("wait", "input")
        }
        # The core carrier is never exposed: UI/map are attached below and compute the one final
        # hash. Avoid hashing the same immutable party/bag/flags payload twice per full read.
        core = self._live.read(atomic, defer_hash=True).snapshot
        ui = UiEvidenceReader(port, self._profile).read()

        in_battle = bool(ui.main.in_battle) if ui is not None and ui.valid else False
        map_errors: tuple[ReadError, ...] = ()
        map_state = None
        if (
            core.usable
            and self._map_mode != "off"
            and (self._map_mode == "always" or not in_battle)
        ):
            result = MapReader(port, self._profile, cache=self._map_cache).read()
            if result.state is not None:
                map_state = result.state
            else:
                map_errors = tuple(result.errors)
        snapshot = core.with_blocks(ui=ui, map_state=map_state)

        text = None
        if self._sidecar is not None:
            text = read_dialog_text(port, self._profile, ui)
            self._sidecar.put(snapshot, text)  # keyed by the FINAL (seq, state_hash)

        block = None
        if self._include_battle:
            block = BattleReader(port, self._profile).read(ui, snapshot)
            snapshot = BattleSnapshot.attach(snapshot, block)

        self.last = CompositeRead(snapshot, atomic.frame, block, text, map_errors)
        return snapshot

    def probe_hint_matches(self, hint: tuple[str, object]) -> bool | None:
        """Evaluate a conservative macro hint directly from a few atomic RAM bytes."""
        read_ranges = getattr(self._source, "read_ranges", None)
        if not callable(read_ranges):
            return None
        if hint[0] == "player_idle":
            address = (
                self._profile.symbol("gPlayerAvatar").address + ui_layout.AVATAR_TILE_TRANSITION
            )
            sample = read_ranges(((address, 1),))
            self.probes += 1
            try:
                return sample.read_range(address, 1) == b"\x00"
            except MemoryAccessError:
                return None
        if hint[0] != "player_xy" or self._player_object_id is None:
            return None
        target = hint[1]
        if (
            not isinstance(target, tuple)
            or len(target) != 2
            or not all(isinstance(value, int) and not isinstance(value, bool) for value in target)
        ):
            return None
        field = self._profile.field("ObjectEvent", "currentCoords")
        address = (
            self._profile.symbol("gObjectEvents").address
            + self._player_object_id * core_constants.OBJECT_EVENT_SIZE
            + field.offset
        )
        sample = read_ranges(((address, field.size),))
        self.probes += 1
        try:
            x, y = struct.unpack("<hh", sample.read_range(address, field.size))
        except (MemoryAccessError, struct.error):
            return None
        return (x - map_constants.MAP_OFFSET, y - map_constants.MAP_OFFSET) == target

    def _read_save_blocks(
        self, port: AtomicSnapshotPort | _RangeReadPort
    ) -> tuple[int, int] | None:
        try:
            sb1 = port.read_u32(self._profile.symbol("gSaveBlock1Ptr").address)
            sb2 = port.read_u32(self._profile.symbol("gSaveBlock2Ptr").address)
        except MemoryReadError:
            return None
        if EWRAM_START <= sb1 < EWRAM_END and EWRAM_START <= sb2 < EWRAM_END:
            return sb1, sb2
        return None

    def _probe_ranges(self, sb1: int, _sb2: int) -> tuple[tuple[int, int], ...]:
        """The small RAM ranges needed to notice an executor-visible transition."""
        ranges: list[tuple[int, int]] = []

        def symbol(name: str, size: int) -> None:
            entry = self._profile.symbols.get(name)
            if entry is not None:
                ranges.append((entry.address, size))

        symbol("gSaveBlock1Ptr", 4)
        symbol("gSaveBlock2Ptr", 4)
        # The live party is EWRAM, not the saved party in SaveBlock1.  Storage actions update
        # this region while the PC UI is still open, so it must wake the next-frame read.
        symbol("gPlayerPartyCount", 1)
        symbol("gPlayerParty", core_constants.PARTY_SIZE * core_constants.PARTY_MON_SIZE)
        for name in ("pos", "location", "mapLayoutId"):
            field = self._profile.field("SaveBlock1", name)
            ranges.append((sb1 + field.offset, field.size))
        field = self._profile.field("SaveBlock1", "dynamicWarp")
        ranges.append((sb1 + field.offset, field.size))
        for name, size in (
            ("gMain", ui_layout.MAIN_READ_SIZE),
            ("gTasks", ui_layout.NUM_TASKS * ui_layout.TASK_SIZE),
            ("sTextPrinters", ui_layout.NUM_TEXT_PRINTERS * ui_layout.TEXT_PRINTER_SIZE),
            ("gWindows", ui_layout.NUM_WINDOWS * ui_layout.WINDOW_SIZE),
            ("sGlobalScriptContextStatus", 1),
            ("sGlobalScriptContext", 8),
            ("sLockFieldControls", 1),
            ("sMenu", ui_layout.SMENU_SIZE),
            ("sYesNoWindowId", 1),
            ("sStartMenuCursorPos", 1),
            ("sNumStartMenuItems", 1),
            ("sStartMenuWindowId", 1),
            ("gPartyMenu", ui_layout.PARTY_MENU_SIZE),
            ("gBagMenuState", ui_layout.BAG_STATE_SIZE),
            ("sMonSummaryScreen", 4),
            ("gQuestLogState", 1),
            ("gPlayerAvatar", 8),
            (
                "gObjectEvents",
                core_constants.OBJECT_EVENTS_COUNT * core_constants.OBJECT_EVENT_SIZE,
            ),
            ("gPaletteFade", ui_layout.PF_SIZE),
            ("gStorage", 4),
            ("gBattleTypeFlags", 4),
            ("gBattlersCount", 1),
            ("gBattlerControllerFuncs", 4 * ui_layout.MAX_BATTLERS_COUNT),
            ("gBattleControllerExecFlags", 4),
            ("gBattlerPartyIndexes", 2 * ui_layout.MAX_BATTLERS_COUNT),
            ("gAbsentBattlerFlags", 1),
            ("gActionSelectionCursor", ui_layout.MAX_BATTLERS_COUNT),
            ("gMoveSelectionCursor", ui_layout.MAX_BATTLERS_COUNT),
            ("gMultiUsePlayerCursor", 1),
            ("gBattleMons", battle_state.BM_SIZE * battle_state.MAX_BATTLERS),
            ("gChosenActionByBattler", battle_state.MAX_BATTLERS),
            ("gChosenMoveByBattler", 2 * battle_state.MAX_BATTLERS),
            ("gRandomTurnNumber", 2),
            ("gLastUsedItem", 2),
            ("gBattleOutcome", 1),
            ("gMoveToLearn", 2),
        ):
            symbol(name, size)
        # These cursor bytes are at fixed EWRAM addresses in the storage UI.
        ranges.append((S_STORAGE_CURSOR_AREA_ADDRESS, S_STORAGE_MOVE_STATE_SIZE))
        return tuple(ranges)

    def _probe(
        self,
        port: AtomicSnapshotPort | _RangeReadPort,
        frame: int,
        *,
        save_blocks: tuple[int, int] | None = None,
        mode: str = "wait",
    ) -> tuple[bytes, ...]:
        """Raw, allocation-light signature for the state executor predicates wait on."""
        parts: list[bytes] = []

        def read(addr: int, size: int) -> bytes:
            try:
                return port.read_range(addr, size)
            except (MemoryReadError, ValueError):
                # A failed probe must never suppress a full health-checked snapshot.
                return b"!" + frame.to_bytes(8, "little", signed=False)

        def symbol(name: str, size: int) -> bytes:
            entry = self._profile.symbols.get(name)
            return read(entry.address, size) if entry is not None else b""

        # Core snapshot fields, without unrelated SaveBlock clocks/counters.
        pointer_raw = symbol("gSaveBlock1Ptr", 4) + symbol("gSaveBlock2Ptr", 4)
        parts.append(pointer_raw)
        blocks = save_blocks if save_blocks is not None else self._read_save_blocks(port)
        if blocks is None:
            return (b"probe-invalid", frame.to_bytes(8, "little"))
        sb1, _sb2 = blocks
        for name in ("pos", "location", "mapLayoutId"):
            field = self._profile.field("SaveBlock1", name)
            parts.append(read(sb1 + field.offset, field.size))
        parts.append(symbol("gPlayerPartyCount", 1))
        parts.append(
            symbol("gPlayerParty", core_constants.PARTY_SIZE * core_constants.PARTY_MON_SIZE)
        )

        # Player/object movement and scene gates. Task data is retained only for input/menu tasks;
        # background animation tasks are represented by identity, not their per-frame timers.
        main = symbol("gMain", ui_layout.MAIN_READ_SIZE)
        parts.append(main[:12] + main[ui_layout.MAIN_STATE : ui_layout.MAIN_FLAGS + 1])
        tasks = symbol("gTasks", ui_layout.NUM_TASKS * ui_layout.TASK_SIZE)
        task_probe = bytearray()
        for slot in range(ui_layout.NUM_TASKS):
            off = slot * ui_layout.TASK_SIZE
            if len(tasks) < off + ui_layout.TASK_SIZE or not tasks[off + ui_layout.TASK_IS_ACTIVE]:
                continue
            func = struct.unpack_from("<I", tasks, off + ui_layout.TASK_FUNC)[0]
            task_probe.extend(bytes((slot, tasks[off + ui_layout.TASK_IS_ACTIVE])))
            task_probe.extend(func.to_bytes(4, "little"))
            kind = ui_layout.TASK_KINDS.get(self._fn_names.get(func & ~1, ""))
            if kind not in (None, "warp", "field_message_box", "script_picture"):
                task_probe.extend(tasks[off + ui_layout.TASK_DATA : off + ui_layout.TASK_SIZE])
        parts.append(bytes(task_probe))

        printers = symbol(
            "sTextPrinters", ui_layout.NUM_TEXT_PRINTERS * ui_layout.TEXT_PRINTER_SIZE
        )
        printer_probe = bytearray()
        for window in range(ui_layout.NUM_TEXT_PRINTERS):
            off = window * ui_layout.TEXT_PRINTER_SIZE
            if (
                len(printers) >= off + ui_layout.TEXT_PRINTER_SIZE
                and printers[off + ui_layout.TP_ACTIVE]
            ):
                printer_probe.extend(
                    bytes(
                        (
                            window,
                            printers[off + ui_layout.TP_ACTIVE],
                            printers[off + ui_layout.TP_STATE],
                        )
                    )
                )
        objects = symbol(
            "gObjectEvents",
            core_constants.OBJECT_EVENTS_COUNT * core_constants.OBJECT_EVENT_SIZE,
        )
        object_probe = bytearray()
        object_fields = (
            "flags1",
            "flags2",
            "graphicsId",
            "movementType",
            "localId",
            "mapNum",
            "mapGroup",
            "currentCoords",
            "range",
        )
        for slot in range(core_constants.OBJECT_EVENTS_COUNT):
            off = slot * core_constants.OBJECT_EVENT_SIZE
            if len(objects) < off + core_constants.OBJECT_EVENT_SIZE or not objects[off]:
                continue
            object_probe.append(slot)
            # Movement flags and directions churn during every tile animation. Current tile
            # coordinates wake Tap(until=...) exactly when its destination can be verified.
            object_probe.append(objects[off] & core_constants.OBJ_EVENT_ACTIVE_BIT)
            for name in object_fields:
                field = self._profile.field("ObjectEvent", name)
                object_probe.extend(objects[off + field.offset : off + field.offset + field.size])
        avatar = symbol("gPlayerAvatar", 8)
        avatar_probe = avatar[:1] + avatar[5:6]
        if mode == "wait":
            avatar_probe += avatar[3:4]

        parts.extend(
            (
                bytes(printer_probe),
                symbol("gWindows", ui_layout.NUM_WINDOWS * ui_layout.WINDOW_SIZE),
                symbol("sGlobalScriptContextStatus", 1),
                symbol("sGlobalScriptContext", 8),
                symbol("sLockFieldControls", 1),
                symbol("sMenu", ui_layout.SMENU_SIZE),
                symbol("sYesNoWindowId", 1),
                symbol("sStartMenuCursorPos", 1),
                symbol("sNumStartMenuItems", 1),
                symbol("sStartMenuWindowId", 1),
                symbol("gPartyMenu", ui_layout.PARTY_MENU_SIZE),
                symbol("gBagMenuState", ui_layout.BAG_STATE_SIZE),
                symbol("sMonSummaryScreen", 4),
                symbol("gQuestLogState", 1),
                avatar_probe,
                bytes(object_probe),
            )
        )
        fade = symbol("gPaletteFade", ui_layout.PF_SIZE)
        if len(fade) == ui_layout.PF_SIZE:
            yt = int.from_bytes(
                fade[ui_layout.PF_Y_TARGET_WORD : ui_layout.PF_Y_TARGET_WORD + 2], "little"
            )
            active = int.from_bytes(
                fade[ui_layout.PF_ACTIVE_WORD : ui_layout.PF_ACTIVE_WORD + 2], "little"
            )
            parts.append(((yt >> 11) | (active & ui_layout.PF_ACTIVE_MASK)).to_bytes(2, "little"))

        storage_ptr_raw = symbol("gStorage", 4)
        parts.append(storage_ptr_raw)
        parts.append(read(S_STORAGE_CURSOR_AREA_ADDRESS, S_STORAGE_MOVE_STATE_SIZE))

        # Battle state that can satisfy a commitment/postcondition. Per-frame battle script and
        # animation counters are intentionally absent; controller/choice/HP/turn changes wake the
        # full verifier, and the periodic full refresh remains the safety net.
        for name, size in (
            ("gBattleTypeFlags", 4),
            ("gBattlersCount", 1),
            ("gBattlerControllerFuncs", 4 * ui_layout.MAX_BATTLERS_COUNT),
            ("gBattleControllerExecFlags", 4),
            ("gBattlerPartyIndexes", 2 * ui_layout.MAX_BATTLERS_COUNT),
            ("gAbsentBattlerFlags", 1),
            ("gActionSelectionCursor", ui_layout.MAX_BATTLERS_COUNT),
            ("gMoveSelectionCursor", ui_layout.MAX_BATTLERS_COUNT),
            ("gMultiUsePlayerCursor", 1),
            ("gBattleMons", battle_state.BM_SIZE * battle_state.MAX_BATTLERS),
            ("gChosenActionByBattler", battle_state.MAX_BATTLERS),
            ("gChosenMoveByBattler", 2 * battle_state.MAX_BATTLERS),
            ("gRandomTurnNumber", 2),
            ("gLastUsedItem", 2),
            ("gBattleOutcome", 1),
            ("gMoveToLearn", 2),
        ):
            parts.append(symbol(name, size))
        return tuple(parts)
