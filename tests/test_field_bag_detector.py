"""Default-off field-bag detector: input-ready item list only, synthetic evidence only.

The component is intentionally not registered. Its positive shape is a canonical
FireRed USA Rev0 snapshot whose existing UI fields prove the field bag's list owns
input; everything else (party/battle/shop origins, unknown callbacks, unreadable
sections, setup/context phases, bad cursors) stays unclaimed.
"""

from __future__ import annotations

import inspect

import pytest

from pokegent.actions import Registries, register_all
from pokegent.actions.menu import field_bag_detect as F
from pokegent.games.gen3.firered.reader.schemas import (
    FlagSet,
    Health,
    Player,
    ReadError,
    StateSnapshot,
    VarTable,
)
from pokegent.games.gen3.firered.reader.ui_schemas import (
    BagInfo,
    FieldEvidence,
    FnRef,
    ListMenuInfo,
    MainEvidence,
    MenuEvidence,
    ScriptEvidence,
    TaskEvidence,
    TaskInfo,
    TextEvidence,
    UiEvidence,
)
from pokegent.state.catalog import default_catalog
from pokegent.state.scenes import SCENE_UNKNOWN, DetectorRegistry

PROFILE = "firered-us-rev0"
BAG_RUN = FnRef(ptr=0x08107EE0, name="CB2_BagMenuRun", verified=True, thumb_rom=True)
# CB2_BAG_SETUP (battle.detect) is an existing verified constant; this pointer is
# known to the source even when the profile cannot map it to a name.
KNOWN_UNMAPPED = FnRef(ptr=0x08107EB8, name=None, verified=False, thumb_rom=True)


def _fn(name: str, ptr: int = 0x08100001, *, verified: bool = True) -> FnRef:
    return FnRef(ptr=ptr, name=name, verified=verified, thumb_rom=True)


def _task(slot: int, func: FnRef) -> TaskInfo:
    return TaskInfo(slot=slot, func=func, priority=0, prev=0, next=0, data=(0,) * 16)


def _ui(
    *,
    callback: FnRef | None = BAG_RUN,
    location: int = 0,
    pocket: int = 0,
    vector=(0, 0, 0, 0, 0, 0),
    bag_open: bool = True,
    fade: bool | None = False,
    in_battle: bool | None = False,
    main_valid: bool = True,
    tasks_valid: bool = True,
    menu_valid: bool = True,
    field_valid: bool = True,
    input_task: bool = True,
    context_task: bool = False,
    list_task: bool = True,
    listing: bool = True,
    cursor_row: int = 0,
    scroll: int = 0,
    total_items: int = 3,
    max_showed: int = 3,
) -> UiEvidence:
    above, cursor = vector[:3], vector[3:6]
    tasks = []
    if input_task:
        tasks.append(_task(3, _fn("Task_BagMenu_HandleInput")))
    if context_task:
        tasks.append(_task(3, _fn("Task_FieldItemContextMenuHandleInput")))
    if list_task:
        tasks.append(_task(4, _fn("ListMenuDummyTask", ptr=0x08106ECD)))
    return UiEvidence(
        valid=True,
        callback_id=callback.name if callback is not None else None,
        main=MainEvidence(
            valid=main_valid,
            confidence=1.0 if main_valid else 0.0,
            callback2=callback,
            in_battle=in_battle,
        ),
        tasks=TaskEvidence(
            valid=tasks_valid,
            confidence=1.0 if tasks_valid else 0.0,
            active=tuple(tasks),
            signatures=tuple(task.func.label for task in tasks),
            count=len(tasks),
        ),
        text=TextEvidence(valid=True, confidence=1.0),
        script=ScriptEvidence(valid=True, confidence=1.0, running=False),
        menu=MenuEvidence(
            valid=menu_valid,
            confidence=1.0 if menu_valid else 0.0,
            bag=BagInfo(
                callback=FnRef(ptr=0x080568A8, name="CB2_ReturnToFieldWithOpenMenu", verified=True),
                location=location,
                bag_open=bag_open,
                pocket=pocket,
                items_above=above,
                cursor_pos=cursor,
                active_hint=None,
            ),
            list_menu=(
                ListMenuInfo(
                    task_slot=4,
                    total_items=total_items,
                    max_showed=max_showed,
                    window_id=1,
                    scroll_offset=scroll,
                    cursor_row=cursor_row,
                    window_in_use=True,
                )
                if listing
                else None
            ),
        ),
        field=FieldEvidence(
            valid=field_valid,
            confidence=1.0 if field_valid else 0.0,
            fade_active=fade,
        ),
        confidence={"main": 1.0, "tasks": 1.0, "menu": 1.0, "field": 1.0},
    )


def _snapshot(*, ui: UiEvidence | None = None, usable: bool = True, profile: str = PROFILE):
    if not usable:
        return StateSnapshot.create(
            rom_profile=profile,
            snapshot_seq=1,
            health=Health(
                valid=False,
                read_errors=(ReadError(subsystem="test", code="bad", message="invented"),),
            ),
            ui=ui,
        )
    return StateSnapshot.create(
        rom_profile=profile,
        snapshot_seq=1,
        health=Health(valid=True),
        player=Player(
            name="Test",
            gender="male",
            tid=0,
            sid=0,
            map_group=0,
            map_num=0,
            map_layout_id=0,
            x=0,
            y=0,
            money=0,
            coins=0,
        ),
        flags=FlagSet(),
        vars=VarTable(),
        ui=_ui() if ui is None else ui,
    )


def _detect(snapshot, *, enabled=True):
    registry = DetectorRegistry(
        [F.FieldBagDetector(enabled=enabled)], catalog=default_catalog()
    )
    return registry.detect(snapshot)


def test_disabled_by_default_and_builders_stay_off():
    assert F.FieldBagDetector().detect(_snapshot()) is None
    assert F.build_detectors() == ()
    assert F.build_detectors(enabled=False) == ()
    assert len(F.build_detectors(enabled=True)) == 1
    with pytest.raises(TypeError):
        F.FieldBagDetector(enabled=1)
    for falsey in (0, "", [], None):
        with pytest.raises(TypeError):
            F.build_detectors(enabled=falsey)


def test_real_registry_and_catalog_claim_only_the_ready_field_item_list():
    scene = _detect(_snapshot())
    assert scene.scene_id == "bag.items"
    assert scene.busy is False
    assert scene.evidence["location"] == 0
    assert scene.evidence["pocket"] == 0
    assert scene.evidence["cursor"] == (0, 0, 0)
    assert _detect(_snapshot(), enabled=False).scene_id == SCENE_UNKNOWN


def test_scrolled_cursor_is_relative_to_scroll_offset_and_list_total():
    # Same bag vector (above=2, cursor=1): absolute row 3 is outside a 3-item
    # list but valid when the owning list reports 4 items.
    at_total = _snapshot(ui=_ui(vector=(2, 0, 0, 1, 0, 0), total_items=3))
    within_total = _snapshot(ui=_ui(vector=(2, 0, 0, 1, 0, 0), total_items=4))
    assert _detect(at_total).scene_id == SCENE_UNKNOWN
    assert _detect(within_total).scene_id == "bag.items"


def test_unmapped_known_pointer_is_not_a_verified_callback():
    assert _detect(_snapshot(ui=_ui(callback=KNOWN_UNMAPPED))).scene_id == SCENE_UNKNOWN


@pytest.mark.parametrize("location", [1, 2, 3, 5])
def test_party_shop_battle_and_other_locations_are_not_stolen(location):
    assert _detect(_snapshot(ui=_ui(location=location))).scene_id == SCENE_UNKNOWN


@pytest.mark.parametrize(
    "changes",
    [
        {"callback": FnRef(ptr=0x08123456, name=None, verified=False)},
        {"callback": _fn("CB2_BagMenuRun", verified=False)},
        {"callback": _fn("CB2_BuyMenu", ptr=0x0809ADE4)},
        {"main_valid": False},
        {"tasks_valid": False},
        {"menu_valid": False},
        {"field_valid": False},
        {"in_battle": True},
        {"in_battle": None},
        {"fade": True},
        {"fade": None},
        {"input_task": False},
        {"context_task": True, "input_task": False},
        {"listing": False},
        {"list_task": False},
        {"bag_open": False},
        {"pocket": 3},
        {"vector": (0, 0)},
        {"vector": (0, 0, 0, 3, 0, 0)},
        {"vector": (0, 0, 0, -1, 0, 0)},
        {"cursor_row": 3},
        {"scroll": 4},
    ],
)
def test_incomplete_or_foreign_evidence_fails_closed(changes):
    assert _detect(_snapshot(ui=_ui(**changes))).scene_id == SCENE_UNKNOWN


@pytest.mark.parametrize("profile", ["synthetic", "firered-us-rev1", ""])
def test_foreign_profiles_are_not_claimed(profile):
    assert _detect(_snapshot(profile=profile)).scene_id == SCENE_UNKNOWN


def test_invalid_core_is_not_claimed():
    assert _detect(_snapshot(ui=_ui(), usable=False)).scene_id != "bag.items"


def test_module_has_no_actor_or_emulator_dependency():
    assert list(inspect.signature(F.FieldBagDetector.detect).parameters) == ["self", "snapshot"]
    assert not any(name.startswith("pokegent.emulator") for name in vars(F))


def test_registered_default_registry_does_not_claim_the_bag(monkeypatch):
    monkeypatch.delenv("FIRERED_ROM_PATH", raising=False)
    registries = Registries.new()
    register_all(registries)
    assert registries.detectors.detect(_snapshot()).scene_id != "bag.items"
