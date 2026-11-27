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
