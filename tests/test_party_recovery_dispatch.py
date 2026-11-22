"""Invented party recovery snapshots through every registered runtime family."""
import pytest

from pokegent.actions import Registries, register_all
from pokegent.actions.field.builder import FieldBuilder
from pokegent.games.gen3.firered.maps.map_schema import (
    MapGrid,
    MapIdentity,
    MapLayoutInfo,
    MapState,
    PlayerPlacement,
)
from pokegent.games.gen3.firered.reader import ui_schemas as S
from pokegent.games.gen3.firered.reader.schemas import (
    FlagSet,
    Health,
    Player,
    StateSnapshot,
    VarTable,
)
from pokegent.pipeline import Perceiver


def fn(name):
    return S.FnRef(ptr=0x08000101, name=name, verified=True)


def recovery_snapshot(task_name, *, ack=False, field=False):
    section = dict(valid=True, confidence=1.0)
    tasks = () if field else (S.TaskInfo(slot=0, func=fn(task_name), priority=0,
                                       prev=0xFE, next=0xFF, data=(0,) * 16),)
    menu = S.MenuEvidence(**section) if field else S.MenuEvidence(
        **section,
        party=S.PartyMenuInfo(exit_callback=fn("synthetic_exit"), task=fn(task_name),
            menu_type=0, layout=0, slot_id=0, slot_id2=0, action=3,
            action_name="use_item", bag_item=0, active_hint=True),
        menu=None if ack else S.SMenuInfo(left=0, top=0, cursor_pos=0, min_cursor_pos=0,
            max_cursor_pos=1, window_id=9, font_id=2, option_width=6, option_height=4,
            columns=1, rows=2, window_in_use=True, consistent=True),
        yes_no=None if ack else S.YesNoInfo(window_id=9, window_in_use=True,
            task_active=False, input_ready=None, field_prompt_open=False),
    )
    text = S.TextEvidence(**section, any_active=ack, printing=False,
        waiting_for_button=ack, field_message_task=False,
        windows=(S.WindowInfo(window_id=6, bg=0, left=0, top=0, width=8, height=4),)
                if ack else (),
        printers=(S.PrinterInfo(window_id=6, state=1, state_name="wait",
            waiting_for_button=True, text_speed=1, delay_counter=0, font_id=2,
            current_char_ptr=0x02001000),) if ack else ())
    ui = S.UiEvidence(valid=True, menu_type=None if field else "party_menu",
        main=S.MainEvidence(**section, callback2=fn("CB2_Overworld" if field
                                                  else "CB2_UpdatePartyMenu"), in_battle=False),
        tasks=S.TaskEvidence(**section, active=tasks, count=len(tasks)), text=text,
        menu=menu, script=S.ScriptEvidence(**section, running=False, controls_locked=False,
                                         status_name="shutdown", mode_name="stopped"),
        field=S.FieldEvidence(**section, fade_active=False, forced_movement=False,
                              moving=False, turning=False),
        confidence={name: 1.0 for name in ("main", "tasks", "text", "menu", "field", "script")})
    return StateSnapshot.create(rom_profile="synthetic", snapshot_seq=1, health=Health(valid=True),
        player=Player(name="Test", gender="male", tid=0, sid=0, map_group=0, map_num=0,
                      map_layout_id=1, x=0, y=0, money=0, coins=0),
        flags=FlagSet(set_ids=()), vars=VarTable(nonzero=()), ui=ui,
        map=field_map() if field else None)


def field_map():
    return MapState(
        identity=MapIdentity(group=0, number=0, name="Demo", layout_id=1,
            region_section_id=0, map_type=0, map_type_name="indoor", weather=0, cave=0,
            floor_num=0, battle_type=0, music=0, biking_allowed=False, allow_escaping=False,
            allow_running=True, identity_verified=True),
        layout=MapLayoutInfo(width=1, height=1, border_width=1, border_height=1, border=(0,),
                             primary_tileset=0, secondary_tileset=0, layout_ptr=0),
        grid=MapGrid(columns=15, rows=15, blocks=(0,) * 225, attributes=((0, 0),)),
        player=PlayerPlacement(save_x=0, save_y=0, avatar_x=0, avatar_y=0, prev_x=0, prev_y=0,
            x=0, y=0, position_consistent=True, facing="N", elevation=0, object_event_id=0,
            avatar_flags=0, surfing=False, underwater=False, biking=False,
            controllable_flag=True, forced=False, moving=False, tile_transition_state=0,
            running_state=0))


def test_all_runtime_detectors_dispatch_recovery_back_to_field(monkeypatch):
    monkeypatch.delenv("FIRERED_ROM_PATH", raising=False)
    registries = Registries.new()
    register_all(registries)

    class Source:
        current = None

        def read_snapshot(self):
            return self.current

    source = Source()
    perceiver = Perceiver(source, registries.detectors, registries.builders)
    stages = [
        ("Task_HandleReplaceMoveYesNoInput", False, False, "dialog.yes_no", "no"),
        ("Task_HandleStopLearningMoveYesNoInput", False, False, "dialog.yes_no", "yes"),
        ("Task_ClosePartyMenuAfterText", True, False, "dialog.advance", "advance_text"),
        (None, False, True, "field.free", None),
    ]
    for sequence, (task, ack, field, expected_scene, expected_option) in enumerate(stages, 1):
        source.current = recovery_snapshot(task, ack=ack, field=field).model_copy(
            update={"snapshot_seq": sequence})
        perception = perceiver.perceive("synthetic-party-recovery")
        assert perception.scene.scene_id == expected_scene
        assert not perception.scene.busy
        assert perception.frame is not None
        if expected_option is not None:
            assert [option.id for option in perception.frame.options] == [expected_option]
        else:
            assert isinstance(registries.builders.for_scene(expected_scene), FieldBuilder)


@pytest.mark.parametrize("change", ["unknown", "unverified", "printing", "fade", "window",
                                    "no_window", "no_printer", "invalid"])
def test_all_runtime_detectors_refuse_unproven_party_ack(monkeypatch, change):
    monkeypatch.delenv("FIRERED_ROM_PATH", raising=False)
    registries = Registries.new()
    register_all(registries)
    snap = recovery_snapshot("Task_ClosePartyMenuAfterText", ack=True)
    ui = snap.ui
    if change in {"unknown", "unverified"}:
        task = ui.tasks.active[0]
        function = fn("Task_UnrecognizedPartyOverlay") if change == "unknown" else (
            task.func.model_copy(update={"verified": False}))
        ui = ui.model_copy(update={"tasks": ui.tasks.model_copy(update={
            "active": (task.model_copy(update={"func": function}),)})})
    elif change == "fade":
        ui = ui.model_copy(update={"field": ui.field.model_copy(update={"fade_active": True})})
    elif change == "invalid":
        ui = ui.model_copy(update={"tasks": ui.tasks.model_copy(update={"valid": False})})
    else:
        text = ui.text
        if change == "printing":
            text = text.model_copy(update={"printers": (text.printers[0].model_copy(update={
                "state": 0, "state_name": "handle_char", "waiting_for_button": False}),),
                "printing": True, "waiting_for_button": False})
        elif change == "window":
            text = text.model_copy(update={"printers": (text.printers[0].model_copy(
                update={"window_id": 5}),)})
        elif change == "no_window":
            text = text.model_copy(update={"windows": ()})
        else:
            text = text.model_copy(update={"printers": (), "any_active": False,
                                           "waiting_for_button": False})
        ui = ui.model_copy(update={"text": text})

    class Source:
        def read_snapshot(self):
            return snap.with_ui(ui)

    perception = Perceiver(Source(), registries.detectors, registries.builders).perceive(
        "synthetic-party-refusal")
    assert perception.scene.busy or perception.scene.is_unknown
    assert perception.frame is None
