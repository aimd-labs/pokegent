"""Conservative observable-state identity for opt-in realtime decision waits.

This is broader than any one builder fingerprint: a dialog page, unrelated NPC, menu cursor,
party/bag/flags, or battle phase can change without changing that builder's chosen option.
Only explicit clocks/key history/printer delays are excluded. RNG and sprite animation are absent
from the decoded snapshot. Any decode error or missing required block fails closed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pokegent.actions.menu.system_flow.symbols import PRE_GAME_CALLBACKS

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
    evidence = ui.model_dump(mode="json")
    evidence["main"].pop("held_keys", None)
    evidence["main"].pop("new_keys", None)
    fanfare = evidence["field"].pop("fanfare_counter", None)
    evidence["field"]["fanfare_done"] = None if fanfare is None else fanfare == 0
    for printer in evidence["text"]["printers"]:
        printer.pop("delay_counter", None)
    return state, evidence, read.text, read.map_errors
