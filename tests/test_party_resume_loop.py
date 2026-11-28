"""Party list after resume is ready for close even when the choose task is missing (#165)."""

from types import SimpleNamespace

from pokegent.actions.menu.start_party import refs as R
from pokegent.actions.menu.start_party.builder import StartPartyBuilder
from pokegent.actions.menu.start_party.detect import StartPartyDetector
from pokegent.options.registry import BuildContext


def _party_snapshot(*task_names: str, fade: bool = False) -> SimpleNamespace:
    tasks = tuple(
        SimpleNamespace(func=SimpleNamespace(name=name, verified=False))
        for name in task_names
    )
    party = SimpleNamespace(active_hint=True, menu_type=0, slot_id=0, action_name="",
                            action_ids=(), slot_id2=0, item_id=None, forced_send_out=None)
    ui = SimpleNamespace(
        valid=True,
        main=SimpleNamespace(valid=True, in_battle=False, callback2=None),
        tasks=SimpleNamespace(valid=True, active=tasks), menu_type=None,
        menu=SimpleNamespace(valid=True, yes_no=None, menu=None, multichoice=None,
                             party=party, start_menu=None),
        text=SimpleNamespace(valid=True, windows=(), printers=()),
        field=SimpleNamespace(valid=True, fade_active=fade),
        controls_locked=False,
    )
    mon = SimpleNamespace(
        personality=0x12345678, slot=0, nickname="Pikachu", is_egg=False,
        held_item=0, species=25, level=5, hp=20, max_hp=20,
        moves=(SimpleNamespace(move_id=33, pp=35),),
    )
    return SimpleNamespace(health=SimpleNamespace(valid=True), party=(mon,), ui=ui)


def _frame(snapshot) -> object:
    scene = StartPartyDetector().detect(snapshot)
    return StartPartyBuilder(item_options=None).build(
        BuildContext(snapshot=snapshot, scene=scene, objective="Test")
    )


def test_resumed_party_list_is_ready_close_only() -> None:
    scene = StartPartyDetector().detect(_party_snapshot("Task_HandleMenuInput"))
    assert scene is not None and scene.scene_id == R.PARTY_LIST
    assert not scene.busy
    assert scene.evidence["phase"] == "close_only"


def test_resumed_party_list_offers_close_only() -> None:
    frame = _frame(_party_snapshot("Task_HandleMenuInput"))
    assert {opt.id for opt in frame.options} == {"party:cancel"}


def test_loading_party_menu_stays_busy() -> None:
    busy = StartPartyDetector().detect
    assert busy(_party_snapshot("Task_TryCreateSelectionWindow")).busy is True
    assert busy(_party_snapshot("Task_HandleChooseMonInput", fade=True)).busy is True


def test_choose_party_list_still_offers_select_mon() -> None:
    snapshot = _party_snapshot("Task_HandleChooseMonInput")
    scene = StartPartyDetector().detect(snapshot)
    assert not scene.busy and scene.evidence["phase"] == "choose_mon"
    frame = _frame(snapshot)
    assert {opt.id for opt in frame.options} >= {"party:cancel", "party:12345678"}


def _give_ack_snapshot(*task_names: str) -> SimpleNamespace:
    """Synthetic copy of the live #274 post-give dump (stale yes/no flag, choose_mon action)."""
    snap = _party_snapshot(*task_names)
    ui = snap.ui
    ui.main.callback2 = SimpleNamespace(name="CB2_UpdatePartyMenu", verified=True)
    ui.menu.party.action_name, ui.menu.party.slot_id, ui.menu.party.item_id = "choose_mon", 5, 182
    ui.menu.yes_no = SimpleNamespace(window_in_use=True, window_id=6)
    holder = SimpleNamespace(**{**vars(snap.party[0]), "slot": 5, "held_item": 182})
    snap.party = (*snap.party, holder)
    return snap


def test_post_give_message_offers_only_a() -> None:
    snap = _give_ack_snapshot("Task_UpdateHeldItemSprite", "Task_PrintAndWaitForText")
    scene = StartPartyDetector().detect(snap)
    assert scene is not None and not scene.busy and scene.evidence["phase"] == "ack_message"
    options = _frame(snap).options
    assert [(o.id, o.verb) for o in options] == [("acknowledge_party_message", "ADVANCE_TEXT")]


def test_post_give_message_without_sprite_task_stays_busy() -> None:
    scene = StartPartyDetector().detect(_give_ack_snapshot("Task_PrintAndWaitForText"))
    assert scene is not None and scene.busy and scene.evidence["phase"] == "load_party"


def test_post_give_message_with_a_yes_no_task_stays_busy() -> None:
    snap = _give_ack_snapshot(
        "Task_UpdateHeldItemSprite", "Task_PrintAndWaitForText", "Task_SwitchItemsYesNo"
    )
    scene = StartPartyDetector().detect(snap)
    assert scene is not None and scene.evidence["phase"] != "ack_message"


def test_post_give_message_without_proven_transfer_stays_busy() -> None:
    snap = _give_ack_snapshot("Task_UpdateHeldItemSprite", "Task_PrintAndWaitForText")
    snap.party[1].held_item = 0
    scene = StartPartyDetector().detect(snap)
    assert scene is not None and scene.busy and scene.evidence["phase"] == "load_party"
