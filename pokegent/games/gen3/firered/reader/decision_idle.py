"""Player/selection identity and a conservative byte prefilter for realtime waits."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pokegent.actions.menu.system_flow.symbols import PRE_GAME_CALLBACKS
from pokegent.emulator import AtomicSnapshot
from pokegent.games.gen3.firered.reader import ui_symbols as U
from pokegent.games.gen3.firered.rom import constants as C

if TYPE_CHECKING:
    from pokegent.games.gen3.firered.reader.snapshot_sources import CompositeRead


def observable_state(read: CompositeRead | None, *, allow_invalid_core: bool = False) -> object:
    if read is None:
        raise ValueError("missing composite read")
    snapshot = read.snapshot
    ui = snapshot.ui
    if ui is None or not ui.valid or ui.read_errors:
        raise ValueError("incomplete UI/map evidence")
    pre_game = bool(ui.main.callback2 and ui.main.callback2.name in PRE_GAME_CALLBACKS)
    if read.map_errors and not pre_game:
        raise ValueError("incomplete map evidence")
    # Pre-game snapshots may lack an avatar. The ordinary executor still owns whether such
    # scenes are usable. Idle guards compare their health and complete UI evidence as well.
    if not snapshot.health.valid and not allow_invalid_core:
        raise ValueError("invalid core evidence")
    if ui.main.in_battle:
        if read.battle is None or not read.battle.valid:
            raise ValueError("incomplete battle evidence")
    elif snapshot.usable and snapshot.map is None and not pre_game:
        raise ValueError("missing field map")
    state = snapshot.model_dump(mode="json", exclude={
        "run_id", "snapshot_seq", "emulator_frame", "state_hash", "ui",
    })
    if state.get("player") is not None:
        state["player"].pop("play_time", None)
    if state.get("map") is not None:
        # Retain MapState.player: avatar coordinates can lead/lag the saved position during
        # scripted movement. Wandering NPCs do not change the player's queued selection.
        state["map"].pop("objects", None)
    if state.get("battle") is not None:
        for key in ("turn_marker", "active_battler", "script_ptr", "script_op"):
            state["battle"].pop(key, None)
    evidence = ui.model_dump(mode="json")
    evidence["main"].pop("held_keys", None)
    evidence["main"].pop("new_keys", None)
    for task in evidence["tasks"]["active"]:
        task.pop("data", None)  # stable task identity, as in UiEvidence.scene_digest
    if evidence.get("battle") is not None:
        evidence["battle"].pop("active_battler", None)
    fanfare = evidence["field"].pop("fanfare_counter", None)
    evidence["field"]["fanfare_done"] = None if fanfare is None else fanfare == 0
    evidence["field"].pop("fade_y", None)
    for printer in evidence["text"]["printers"]:
        printer.pop("delay_counter", None)
    return state, evidence, read.text, read.map_errors


class DecisionReadTrace:
    """Record decoder RAM dependencies, including pointer targets, without actor reads.

    Comparing these byte ranges is only a prefilter: any change still gets a complete decode.
    Unlisted clocks may cause extra decoding, never acceptance of changed observable state.
    """

    def __init__(self, atomic: AtomicSnapshot):
        self.atomic = atomic
        self.ranges: list[tuple[int, int]] = []

    def __getattr__(self, name):
        return getattr(self.atomic, name)

    def read_range(self, address: int, size: int) -> bytes:
        if 0x02000000 <= address < 0x04000000:
            self.ranges.append((address, address + size))
        return self.atomic.read_range(address, size)

    def probe_ranges(self, profile, read: CompositeRead) -> tuple[tuple[int, int], ...]:
        """Merge dependencies and omit only raw inputs excluded by observable_state.

        Menu task data stays in the probe because derived cursor/readiness fields use it.
        Ordinary background task counters and non-player object animation do not.
        """
        omitted = []

        def omit(name, offset, size):
            entry = profile.symbols.get(name)
            if entry is not None:
                omitted.append((entry.address + offset, entry.address + offset + size))

        omit("gMain", 12, U.MAIN_STATE - 12)
        for slot in range(U.NUM_TEXT_PRINTERS):
            omit("sTextPrinters", slot * U.TEXT_PRINTER_SIZE + U.TP_DELAY_COUNTER, 1)
        ui = read.snapshot.ui
        tasks = {task.slot: task for task in ui.tasks.active}
        for slot in range(U.NUM_TASKS):
            task = tasks.get(slot)
            kind = U.TASK_KINDS.get(task.func.name) if task else None
            if kind not in ("yes_no", "multichoice", "list_menu", "party_use_item"):
                omit("gTasks", slot * U.TASK_SIZE + U.TASK_DATA,
                     U.TASK_SIZE - U.TASK_DATA)
        # Keep the full player record: even forced steps and player creation must wake decode.
        avatar = profile.symbols.get("gPlayerAvatar")
        player_id = (self.atomic.read_u8(avatar.address + C.AVATAR_OBJECT_EVENT_ID_OFFSET)
                     if avatar else None)
        for slot in range(C.OBJECT_EVENTS_COUNT):
            if slot != player_id:
                # Keep active/is-player bits for the core's avatar validity check.
                omit("gObjectEvents", slot * C.OBJECT_EVENT_SIZE + 3,
                     C.OBJECT_EVENT_SIZE - 3)
                omit("gObjectEvents", slot * C.OBJECT_EVENT_SIZE + 1, 1)
        # SaveBlock2 play time lies within the reader's bulk read, but is not state identity.
        pointer = profile.symbols.get("gSaveBlock2Ptr")
        fields = profile.structs.get("SaveBlock2")
        if pointer and fields:
            sb2 = self.atomic.read_u32(pointer.address)
            for name, field in fields.fields.items():
                if name.startswith("playTime"):
                    omitted.append((sb2 + field.offset, sb2 + field.offset + field.size))
        merged: list[tuple[int, int]] = []
        for start, end in sorted(self.ranges):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        for lo, hi in omitted:
            remaining = []
            for start, end in merged:
                if hi <= start or lo >= end:
                    remaining.append((start, end))
                else:
                    if start < lo:
                        remaining.append((start, lo))
                    if hi < end:
                        remaining.append((hi, end))
            merged = remaining
        return tuple((start, end - start) for start, end in merged if end > start)
