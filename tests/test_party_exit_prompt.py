"""Synthetic party field-move modal: never infer confirmation from stale text."""
import struct
from types import SimpleNamespace as NS

import pytest

from pokegent.actions.menu.dialog import evidence as E
from pokegent.actions.menu.dialog.builder import DialogBuilder
from pokegent.actions.menu.dialog.macros import CursorMismatch, answer_yes_no_from
from pokegent.actions.menu.dialog.text import DialogText
from pokegent.actions.menu.dialog.verify import _box_moved_on, _prompt_closed
from pokegent.actions.menu.start_party.detect import StartPartyDetector
from pokegent.games.gen3.firered.reader import ui_symbols as U
from pokegent.games.gen3.firered.reader.memory_port import SnapshotMemoryReader
from pokegent.games.gen3.firered.reader.ui import UiEvidenceReader, _Sec
from pokegent.games.gen3.firered.reader.ui_schemas import TaskEvidence
from pokegent.games.gen3.firered.rom.rom_data import load_profiles
from pokegent.state.scenes import SceneResult


def snapshot():
    menu = NS(window_id=5, cursor_pos=0, min_cursor_pos=0, max_cursor_pos=1,
              window_in_use=True, consistent=True)
    ui = NS(
        valid=True,
        main=NS(valid=True, in_battle=False, callback2=NS(name="CB2_UpdatePartyMenu")),
        tasks=NS(valid=True, active=(NS(func=NS(name=E.PARTY_EXIT_TASK, verified=True)),)),
        menu=NS(valid=True, menu=menu, yes_no=NS(window_id=5, window_in_use=True,
                  field_prompt_open=False, task_active=False), multichoice=None,
                party=NS(active_hint=True, menu_type=0)),
        text=NS(valid=True), field=NS(valid=True, fade_active=False),
    )
    return NS(ui=ui, rom_profile="firered-us-rev0")


def test_party_exit_is_ready_modal_and_builds_both_answers_without_text():
    snap = snapshot()
    verdict = E.classify(snap)
    assert verdict.scene_id == E.YES_NO and not verdict.busy
    assert StartPartyDetector().detect(snap) is None
    ctx = NS(snapshot=snap, scene=NS(scene_id=E.YES_NO), goal_id=None, objective_kind=None)
    draft = DialogBuilder().build(ctx)
    assert [o.id for o in draft.options] == ["yes", "no"]
    assert all(o.legality.chooseable and o.source_task == E.PARTY_EXIT_TASK
               for o in draft.options)
    assert draft.options[0].executor_args == {"entry": 0}
    assert draft.options[1].executor_args == {"entry": 1}
    assert "leave this area" in draft.options[0].effect_description
    assert not _prompt_closed(NS(after=snap, before=snap))


@pytest.mark.parametrize("change", ["task", "unverified", "callback", "window", "range",
                                    "cursor", "fade", "invalid", "battle", "party"])
def test_unproven_party_overlay_is_not_a_confirmation(change):
    ui = snapshot().ui
    if change == "task":
        ui.tasks.active[0].func.name = "Task_UnrecognizedPartyOverlay"
    elif change == "unverified":
        ui.tasks.active[0].func.verified = False
    elif change == "callback":
        ui.main.callback2.name = "CB2_BagMenuRun"
    elif change == "window":
        ui.menu.yes_no.window_id = 6
    elif change == "range":
        ui.menu.menu.max_cursor_pos = 2
    elif change == "cursor":
        ui.menu.menu.cursor_pos = 3
    elif change == "fade":
        ui.field.fade_active = True
    elif change == "invalid":
        ui.tasks.valid = False
    elif change == "battle":
        ui.main.in_battle = True
    else:
        ui.menu.party.menu_type = 1
    assert not E.party_exit_yes_no_open(ui)
    snap = NS(ui=ui)
    assert E.classify(snap, DialogText(("Use DIG?",), source="synthetic")) is None


def test_profile_resolves_the_source_bound_input_task():
    profile = load_profiles().get("firered-us-rev0")
    # Name resolution itself does not need a native emulator or any RAM reads.
    reader = UiEvidenceReader(None, profile)
    sec = NS(used=set())
    entry = profile.symbols[E.PARTY_EXIT_TASK]
    ref = reader._fn(sec, entry.address | 1)
    assert ref.name == E.PARTY_EXIT_TASK and ref.verified


def test_negative_answer_reads_back_cursor_before_confirmation():
    snap = snapshot()
    ctx = NS(option=NS(facts={}), frame=NS(goal_id=None))
    steps = answer_yes_no_from(ctx, snap, 1)
    assert next(steps).buttons == "down"
    moved = snapshot()
    moved.ui.menu.menu.cursor_pos = 1
    assert steps.send(NS(snapshot=moved)).buttons == "a"
    with pytest.raises(StopIteration):
        next(steps)


def test_cursor_did_not_move_never_confirms():
    snap = snapshot()
    steps = answer_yes_no_from(NS(option=NS(facts={}), frame=NS(goal_id=None)), snap, 1)
    assert next(steps).buttons == "down"
    with pytest.raises(CursorMismatch):
        steps.send(NS(snapshot=snap))


LEARNING_TASKS = (
    ("Task_HandleReplaceMoveYesNoInput", "no"),
    ("Task_HandleStopLearningMoveYesNoInput", "yes"),
)


@pytest.mark.parametrize("task,safe_answer", LEARNING_TASKS)
def test_resumed_party_learning_prompt_exposes_only_safe_escape(task, safe_answer):
    snap = snapshot()
    snap.ui.tasks.active[0].func.name = task
    verdict = E.classify(snap)
    assert verdict is not None and verdict.scene_id == E.YES_NO and not verdict.busy
    assert StartPartyDetector().detect(snap) is None
    draft = DialogBuilder().build(NS(snapshot=snap, scene=NS(scene_id=E.YES_NO),
                                    goal_id=None, objective_kind=None))
    assert [o.id for o in draft.options if o.legality.chooseable] == [safe_answer]
    assert all(o.source_task == task for o in draft.options)
    chosen = next(o for o in draft.options if o.id == safe_answer)
    assert not chosen.irreversible
    assert not _prompt_closed(NS(after=snap, before=snap, option=chosen))


@pytest.mark.parametrize("task,safe_answer", LEARNING_TASKS)
@pytest.mark.parametrize("change", ["unverified", "callback", "window", "range", "cursor",
                                    "fade", "invalid", "battle", "party"])
def test_party_learning_prompt_requires_current_input_evidence(task, safe_answer, change):
    snap = snapshot()
    ui = snap.ui
    ui.tasks.active[0].func.name = task
    if change == "unverified":
        ui.tasks.active[0].func.verified = False
    elif change == "callback":
        ui.main.callback2.name = "CB2_BagMenuRun"
    elif change == "window":
        ui.menu.yes_no.window_id = 6
    elif change == "range":
        ui.menu.menu.max_cursor_pos = 2
    elif change == "cursor":
        ui.menu.menu.cursor_pos = 3
    elif change == "fade":
        ui.field.fade_active = True
    elif change == "invalid":
        ui.tasks.valid = False
    elif change == "battle":
        ui.main.in_battle = True
    else:
        ui.menu.party.menu_type = 1
    assert E.classify(snap) is None


@pytest.mark.parametrize("task,safe_answer", LEARNING_TASKS)
def test_profile_resolves_party_learning_input_owner(task, safe_answer):
    profile = load_profiles().get("firered-us-rev0")
    reader = UiEvidenceReader(None, profile)
    entry = profile.symbols.get(task)
    assert entry is not None
    ref = reader._fn(NS(used=set()), entry.address | 1)
    assert ref.name == task and ref.verified


def test_consecutive_learning_prompts_have_distinct_fingerprints_and_verify_owner_exit():
    before = snapshot()
    before.ui.tasks.active[0].func.name = "Task_HandleReplaceMoveYesNoInput"
    after = snapshot()
    after.ui.tasks.active[0].func.name = "Task_HandleStopLearningMoveYesNoInput"
    def build(snap):
        return DialogBuilder().build(NS(snapshot=snap, scene=NS(scene_id=E.YES_NO),
                                        goal_id=None, objective_kind=None))
    first, second = build(before), build(after)
    assert first.state_fingerprint != second.state_fingerprint
    option = next(o for o in first.options if o.legality.chooseable)
    assert _prompt_closed(NS(before=before, after=after, option=option))
    # An unreadable cursor is not proof the original input task consumed the answer.
    after.ui.tasks.active[0].func.name = "Task_HandleReplaceMoveYesNoInput"
    after.ui.menu.menu.cursor_pos = 3
    assert not _prompt_closed(NS(before=before, after=after, option=option))


def test_unknown_party_task_and_fade_remain_busy():
    snap = snapshot()
    snap.ui.menu.start_menu = None
    snap.ui.menu.party.action_name = "use_item"
    snap.ui.menu.party.action_ids = ()
    snap.ui.menu.party.slot_id = 0
    snap.ui.tasks.active[0].func.name = "Task_UnrecognizedPartyOverlay"
    scene = StartPartyDetector().detect(snap)
    assert scene.scene_id == "party.list" and scene.busy
    snap.ui.tasks.active[0].func.name = "Task_HandleChooseMonInput"
    scene = StartPartyDetector().detect(snap)
    assert scene.scene_id == "party.list" and not scene.busy
    snap.ui.field.fade_active = True
    assert StartPartyDetector().detect(snap).busy


def party_close_message():
    snap = snapshot()
    ui = snap.ui
    ui.tasks.active[0].func.name = "Task_ClosePartyMenuAfterText"
    ui.menu.start_menu = None
    ui.menu.party.action_name = "use_item"
    ui.menu.party.action_ids = ()
    ui.menu.party.slot_id = 0
    ui.text.printers = (NS(window_id=6, state_name="wait", waiting_for_button=True,
                           current_char_ptr=0x02001000),)
    ui.text.windows = (NS(window_id=6),)
    ui.script = NS(native=None)
    return snap


def test_party_learning_recovery_reaches_owned_final_acknowledgement():
    # Source sequence: decline replacement, stop learning, acknowledge the final message.
    drafts = []
    for task, _answer in LEARNING_TASKS:
        snap = snapshot()
        snap.ui.tasks.active[0].func.name = task
        verdict = E.classify(snap)
        scene = SceneResult(scene_id=verdict.scene_id, confidence=verdict.confidence,
                            evidence=verdict.evidence)
        drafts.append(DialogBuilder().build(NS(snapshot=snap, scene=scene,
                                               goal_id=None, objective_kind=None)))
    snap = party_close_message()
    verdict = E.classify(snap)
    assert verdict is not None and verdict.scene_id == E.ADVANCE and not verdict.busy
    assert StartPartyDetector().detect(snap) is None
    scene = SceneResult(scene_id=verdict.scene_id, confidence=verdict.confidence,
                        evidence=verdict.evidence)
    drafts.append(DialogBuilder().build(NS(snapshot=snap, scene=scene)))
    assert [[o.id for o in draft.options if o.legality.chooseable] for draft in drafts] == [
        ["no"], ["yes"], ["advance_text"],
    ]
    option = drafts[-1].options[0]
    assert option.executor_ref == "dialog.advance_text"
    assert option.source_task == "Task_ClosePartyMenuAfterText"
    assert not option.irreversible
    assert not _box_moved_on(NS(before=snap, after=snap))
    acknowledged = party_close_message()
    acknowledged.ui.text.printers = ()
    assert _box_moved_on(NS(before=snap, after=acknowledged))


@pytest.mark.parametrize("change", ["unknown", "unverified", "callback", "window", "no_window",
                                    "no_printer", "printing", "fade", "invalid", "text_invalid",
                                    "battle", "party"])
def test_final_party_ack_refuses_unproven_or_busy_input(change):
    snap = party_close_message()
    ui = snap.ui
    if change == "unknown":
        ui.tasks.active[0].func.name = "Task_UnrecognizedPartyOverlay"
    elif change == "unverified":
        ui.tasks.active[0].func.verified = False
    elif change == "callback":
        ui.main.callback2.name = "CB2_InitPartyMenu"
    elif change == "window":
        ui.text.printers[0].window_id = 5
    elif change == "no_window":
        ui.text.windows = ()
    elif change == "no_printer":
        ui.text.printers = ()
    elif change == "printing":
        ui.text.printers[0].waiting_for_button = False
    elif change == "fade":
        ui.field.fade_active = True
    elif change == "invalid":
        ui.tasks.valid = False
    elif change == "text_invalid":
        ui.text.valid = False
    elif change == "battle":
        ui.main.in_battle = True
    else:
        ui.menu.party.menu_type = 1
    assert E.classify(snap) is None
    if change in {"unknown", "unverified", "printing", "fade", "no_window", "no_printer"}:
        assert StartPartyDetector().detect(snap).busy


def test_profile_resolves_post_learning_close_task():
    profile = load_profiles().get("firered-us-rev0")
    reader = UiEvidenceReader(None, profile)
    entry = profile.symbols.get("Task_ClosePartyMenuAfterText")
    assert entry is not None
    ref = reader._fn(NS(used=set()), entry.address | 1)
    assert ref.name == "Task_ClosePartyMenuAfterText" and ref.verified


@pytest.mark.parametrize("state,ready", [(0, False), (1, True), (4, False), (5, False), (6, False)])
def test_raw_party_printer_pause_until_press_is_input_ready(state, ready):
    # pret text.c: EXT_CTRL_CODE_PAUSE_UNTIL_PRESS installs RENDER_STATE_WAIT (1).
    # All memory here is invented, including the printer table and text addresses.
    profile = load_profiles().get("firered-us-rev0")
    symbol = profile.symbols["sTextPrinters"].model_copy(update={"address": 0x02002000})
    profile = profile.model_copy(update={"symbols": {"sTextPrinters": symbol}})
    raw = bytearray(U.NUM_TEXT_PRINTERS * U.TEXT_PRINTER_SIZE)
    offset = 6 * U.TEXT_PRINTER_SIZE
    struct.pack_into("<I", raw, offset + U.TP_CURRENT_CHAR, 0x02004000)
    raw[offset + U.TP_WINDOW_ID] = 6
    raw[offset + U.TP_ACTIVE] = 1
    raw[offset + U.TP_STATE] = state
    windows = bytearray(U.NUM_WINDOWS * U.WINDOW_SIZE)
    for index in range(U.NUM_WINDOWS):
        windows[index * U.WINDOW_SIZE + U.WIN_BG] = U.WINDOW_FREE_BG
    windows[6 * U.WINDOW_SIZE + U.WIN_BG] = 0
    reader = UiEvidenceReader(SnapshotMemoryReader({0x02002000: bytes(raw)}), profile)
    text = reader._read_text(_Sec("text"), bytes(windows),
                             TaskEvidence(valid=True, confidence=1.0))
    assert text.valid and text.printers[0].waiting_for_button is ready
    snap = party_close_message()
    snap.ui.text = text
    verdict = E.classify(snap)
    assert (verdict is not None and verdict.scene_id == E.ADVANCE) is ready
    if not ready:
        assert StartPartyDetector().detect(snap).busy
