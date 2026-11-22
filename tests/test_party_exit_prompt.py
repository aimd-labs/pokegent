"""Synthetic party field-move modal: never infer confirmation from stale text."""
from types import SimpleNamespace as NS

import pytest

from pokegent.actions.menu.dialog import evidence as E
from pokegent.actions.menu.dialog.builder import DialogBuilder
from pokegent.actions.menu.dialog.macros import CursorMismatch, answer_yes_no_from
from pokegent.actions.menu.dialog.text import DialogText
from pokegent.actions.menu.dialog.verify import _prompt_closed
from pokegent.actions.menu.start_party.detect import StartPartyDetector
from pokegent.games.gen3.firered.reader.ui import UiEvidenceReader
from pokegent.games.gen3.firered.rom.rom_data import load_profiles


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
