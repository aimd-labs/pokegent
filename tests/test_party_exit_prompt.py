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
    assert not _prompt_closed(NS(after=snap))


@pytest.mark.parametrize("change", ["task", "unverified", "callback", "window", "range",
                                    "cursor", "fade", "invalid", "battle", "party"])
def test_unproven_party_overlay_is_not_a_confirmation(change):
    ui = snapshot().ui
    if change == "task":
        ui.tasks.active[0].func.name = "Task_HandleReplaceMoveYesNoInput"
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
