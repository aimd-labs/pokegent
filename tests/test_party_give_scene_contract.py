"""Full-Executor GIVE contract: the bag -> party return transit must not self-interrupt.

Pinned ``party_menu.c`` gives the chosen bag item from the item row itself: the bag closes and
the game transfers the item (or asks the existing-held swap question).  This exercises the real
``Executor`` boundary (``allowed_scenes``/busy policy after every step) instead of calling
``spec.fn`` directly, so an omitted GIVE transit scene is visible as ``E_INTERRUPTED_SCENE``.
"""

from types import SimpleNamespace as NS

import pytest

from pokegent.actions import Registries, register_all
from pokegent.actions._exec.executor import Executor
from pokegent.actions._exec.models import ActionStatus
from pokegent.actions.menu.start_party import refs as R
from pokegent.errors import ErrorCode
from pokegent.games.gen3.firered.reader.ui_schemas import (
    BagInfo,
    FieldEvidence,
    FnRef,
    MainEvidence,
    MenuEvidence,
    ScriptEvidence,
    TaskEvidence,
    TextEvidence,
    UiEvidence,
)
from pokegent.options.registry import BuildContext
from pokegent.pipeline import Observation

# pret/pokefirered 037335f4 include/constants/items.h line 189: ITEM_EXP_SHARE = 182.
EXP_SHARE = 182
TARGET_PID = 0x12345678
OTHER_PID = 0x87654321
BAG_SCENE = "menu.open"


def _bag_ui():
    """The real UI evidence of an open bag (the GIVE transit the detector must classify)."""
    bag = FnRef(ptr=0x08000001, name="CB2_BagMenuRun", verified=True)
    return UiEvidence(
        valid=True,
        callback_id="CB2_BagMenuRun",
        menu_type="bag",
        main=MainEvidence(
            valid=True, confidence=1.0, callback2=bag, in_battle=False
        ),
        tasks=TaskEvidence(valid=True, confidence=1.0, active=(), signatures=(), count=0),
        text=TextEvidence(valid=True, confidence=1.0),
        script=ScriptEvidence(
            valid=True, confidence=1.0, running=False, controls_locked=False
        ),
        menu=MenuEvidence(
            valid=True,
            confidence=1.0,
            bag=BagInfo(
                callback=bag,
                location=0,
                bag_open=True,
                pocket=0,
                items_above=(0, 0, 0, 0, 0),
                cursor_pos=(0, 0, 0, 0, 0),
                active_hint=True,
            ),
        ),
        field=FieldEvidence(
            valid=True, confidence=1.0, fade_active=False, prevent_step=False
        ),
        confidence={"main": 1.0, "tasks": 1.0, "menu": 1.0, "text": 1.0, "field": 1.0},
    )



def _mon(slot, personality, held_item=0):
    return NS(
        slot=slot,
        personality=personality,
        held_item=held_item,
        species=25,
        nickname="Pikachu",
        level=10,
        hp=30,
        max_hp=30,
        moves=(),
        is_egg=False,
    )


def _bag(quantity):
    held = NS(item_id=EXP_SHARE, quantity=quantity)
    items = NS(pocket="items", capacity=30, items=(held,))
    berries = NS(pocket="berries", capacity=30, items=())
    return (items, berries)


def _ui(*, bag=None, bag_ready=False, context=False, cursor_pos=0):
    if bag is None:
        menu = NS(
            window_in_use=True,
            cursor_pos=cursor_pos,
            min_cursor_pos=0,
            max_cursor_pos=2,
            consistent=True,
        )
        party = NS(
            active_hint=True,
            action_ids=(4, 5, 9),
            action_name="give_item",
            slot_id=0,
            item_id=None,
        )
    else:
        menu = NS(
            window_in_use=context,
            cursor_pos=0,
            min_cursor_pos=0,
            max_cursor_pos=1 if context else 2,
            consistent=True,
        )
        party = NS(
            active_hint=False,
            action_ids=(4, 5, 9),
            action_name="give_item",
            slot_id=0,
            item_id=None,
        )
    return NS(
        valid=True,
        main=NS(valid=True, in_battle=False, callback2=None),
        tasks=NS(valid=True, active=()),
        text=None,
        field=NS(valid=True, fade_active=True),
        script=NS(running=False, controls_locked=False),
        menu=NS(
            party=party,
            menu=menu,
            start_menu=None,
            list_menu=NS(
                window_in_use=bag_ready,
                total_items=1,
                max_showed=1,
                scroll_offset=0,
                cursor_row=0,
            )
            if bag_ready
            else None,
            bag=bag,
            yes_no=None,
            multichoice=None,
            summary_screen_open=False,
        ),
    )


def _snapshot(*, held_item=0, quantity=2, bag_ready=False, context=False, cursor_pos=0):
    bag_state = (
        NS(
            bag_open=True,
            active_hint=True,
            pocket=0,
            cursor_pos=(0, 0, 0, 0, 0),
            items_above=(0, 0, 0, 0, 0),
        )
        if bag_ready
        else None
    )
    return NS(
        usable=True,
        emulator_frame=100,
        state_hash=f"held-{held_item}-qty-{quantity}-{'bag' if bag_ready else 'party'}",
        health=NS(valid=True),
        party=(_mon(0, TARGET_PID, held_item),),
        bag=_bag(quantity),
        ui=_ui(bag=bag_state, bag_ready=bag_ready, context=context, cursor_pos=cursor_pos),
        player=NS(map_group=0, map_num=0, money=0),
        map=None,
        flags=None,
    )


def _after_give_snapshot(*, personality=TARGET_PID, quantity=1):
    snapshot = _snapshot(held_item=EXP_SHARE, quantity=quantity)
    snapshot.party = (_mon(0, personality, EXP_SHARE),)
    # The real return leaves the party menu live and input-ready; the verifier judges it.
    snapshot.ui.field.fade_active = False
    return snapshot


def _obs(snapshot, scene_id, *, busy=False):
    return Observation(
        snapshot,
        NS(scene_id=scene_id, confidence=1.0, busy=busy, evidence={}, core_invalid=False),
    )


class _Perceiver:
    """Scripted observations; a repeated fallback keeps bounded cleanup/verification finite."""

    source = None

    def __init__(self, observations, fallback):
        self._observations = list(observations)
        self._fallback = fallback

    def bind_settle(self, _settle):
        return None

    def observe(self):
        return self._observations.pop(0) if self._observations else self._fallback

    def observe_if_changed(self, **_kwargs):
        return self.observe()


class _Actor:
    min_fps = 60.0
    wall_slack_s = 5.0

    def __init__(self):
        self.taps = []

    def tap(self, buttons, hold_frames=2, wait_frames=1):
        self.taps.append(buttons)
        return hold_frames

    def advance_frames(self, frames):
        return frames

    def release_all(self):
        return None


def _registries(monkeypatch):
    monkeypatch.delenv("FIRERED_ROM_PATH", raising=False)
    registries = Registries.new()
    register_all(registries)
    return registries


def _bag_scene(registries):
    """Classify the real open-bag UI evidence with the registered detectors."""
    probe = NS(
        usable=True,
        snapshot_seq=1,
        rom_profile="firered-us-rev0",
        emulator_frame=100,
        state_hash="bag-probe",
        ui=_bag_ui(),
        party=(),
        bag=(),
        battle=None,
        player=None,
        flags=None,
    )
    scene_id = registries.detectors.detect(probe).scene_id
    assert scene_id == BAG_SCENE, scene_id
    return scene_id


def _option_for(registries, before, cursor_key):
    builder = registries.builders.for_scene(R.PARTY_HELD_ITEM)
    assert builder is not None
    draft = builder.build(BuildContext(snapshot=before, scene=NS(scene_id=R.PARTY_HELD_ITEM)))
    option = next(
        option
        for option in draft.options
        if option.action_contract is not None
        and option.action_contract.cursor_key == cursor_key
    )
    spec = registries.executors.get(option.executor_ref)
    assert spec is not None
    return option, spec


def _give_setup(monkeypatch):
    registries = _registries(monkeypatch)
    before = _snapshot()
    option, spec = _option_for(registries, before, "party_action:give")
    assert option.executor_args == {"item_id": EXP_SHARE}
    return registries, option, spec, before, _bag_scene(registries)



def _run(registries, option, spec, before, perceiver):
    actor = _Actor()
    executor = Executor(
        actor,
        registries.executors,
        registries.verifiers,
        perceiver,
        clock=lambda: 0.0,
        interrupt_requested=lambda: False,
    )
    ctx = NS(
        option=option,
        args=option.executor_args,
        frame=NS(scene_id=R.PARTY_HELD_ITEM),
        snapshot=before,
        scene=NS(scene_id=R.PARTY_HELD_ITEM),
    )
    result = executor._run(spec, registries.verifiers.get(option.postcondition_ref), ctx, {})
    return result, actor


def test_full_executor_give_traverses_bag_and_verifies_held_item_delta(monkeypatch):
    registries, option, spec, before, bag_scene = _give_setup(monkeypatch)
    party = _obs(before, R.PARTY_HELD_ITEM)
    bag_ready = _obs(_snapshot(bag_ready=True), bag_scene)
    after = _obs(_after_give_snapshot(), R.PARTY_LIST)
    perceiver = _Perceiver([party, bag_ready, bag_ready, after], after)

    result, actor = _run(registries, option, spec, before, perceiver)

    assert result.status is ActionStatus.APPLIED, result.reason
    assert result.verified_outcome is True
    assert result.interrupted_scene is None
    assert result.after_hash == after.snapshot.state_hash
    # Party GIVE confirmation + the bag item row only.
    assert tuple(actor.taps) == ("a", "a")
    # Same-identity held item and the exact bag decrease are both proven.
    assert option.action_contract.expected[0].personality == TARGET_PID
    assert option.action_contract.expected[0].bag_delta == -1


@pytest.mark.parametrize(
    "scene_id", ["battle.entering", "dialog.yes_no", "party.summary", "evolution.animation"]
)
def test_unrelated_scene_stays_interrupted_and_unverified(monkeypatch, scene_id):
    registries, option, spec, before, bag_scene = _give_setup(monkeypatch)
    party = _obs(before, R.PARTY_HELD_ITEM)
    unrelated = _obs(_snapshot(bag_ready=True), scene_id)
    perceiver = _Perceiver([party], unrelated)

    result, actor = _run(registries, option, spec, before, perceiver)

    assert result.status is ActionStatus.INTERRUPTED
    assert result.error_code is ErrorCode.E_INTERRUPTED_SCENE
    assert result.interrupted_scene == scene_id
    assert result.verified_outcome is not True
    assert tuple(actor.taps) == ("a",)


def test_wrong_bag_delta_cannot_verify_through_full_executor(monkeypatch):
    registries, option, spec, before, bag_scene = _give_setup(monkeypatch)
    party = _obs(before, R.PARTY_HELD_ITEM)
    bag_ready = _obs(_snapshot(bag_ready=True), bag_scene)
    # The held item moved, but the bag quantity did not decrease: the verifier must fail.
    after = _obs(_after_give_snapshot(quantity=2), R.PARTY_LIST)
    perceiver = _Perceiver([party, bag_ready, bag_ready, after], after)

    result, actor = _run(registries, option, spec, before, perceiver)

    assert result.status is not ActionStatus.APPLIED
    assert result.verified_outcome is not True
    assert result.error_code is ErrorCode.E_POSTCONDITION_NOT_MET
    assert tuple(actor.taps) == ("a", "a")


def test_wrong_personality_cannot_verify_through_full_executor(monkeypatch):
    registries, option, spec, before, bag_scene = _give_setup(monkeypatch)
    party = _obs(before, R.PARTY_HELD_ITEM)
    bag_ready = _obs(_snapshot(bag_ready=True), bag_scene)
    wrong = _obs(_after_give_snapshot(personality=OTHER_PID), R.PARTY_LIST)
    perceiver = _Perceiver([party, bag_ready, bag_ready, wrong], wrong)

    result, actor = _run(registries, option, spec, before, perceiver)

    assert result.status is not ActionStatus.APPLIED
    assert result.verified_outcome is not True
    assert tuple(actor.taps) == ("a", "a")


def test_give_spec_allows_only_the_real_bag_transit_scene(monkeypatch):
    _, _, spec, _, bag_scene = _give_setup(monkeypatch)
    assert spec.allowed_scenes == frozenset(
        {
            R.START,
            R.PARTY_LIST,
            R.PARTY_ACTION,
            R.PARTY_REORDER,
            R.PARTY_HELD_ITEM,
            "scene.unknown",
            bag_scene,
        }
    )
    # The dead catalog labels are not runtime scenes; an unrelated screen still interrupts.
    for unexpected in (
        "bag.items",
        "bag.context",
        "battle.entering",
        "dialog.yes_no",
        "party.summary",
        "evolution.animation",
    ):
        assert unexpected not in spec.allowed_scenes


def test_non_give_selection_cannot_apply_from_an_unexpected_menu_scene(monkeypatch):
    """The shared spec allowance must not turn a take into a verified GIVE."""
    registries = _registries(monkeypatch)
    before = _snapshot(held_item=EXP_SHARE, cursor_pos=1)
    option, spec = _option_for(registries, before, "party_action:take")
    assert option.action_contract.expected[0].bag_delta == 1
    # The bag opens under a take: the macro has no take-via-bag branch, so the verifier
    # (held item still present, no bag increase) must fail closed, never APPLIED.
    opened_bag = _obs(_snapshot(held_item=EXP_SHARE, bag_ready=True), BAG_SCENE)
    perceiver = _Perceiver([opened_bag], opened_bag)

    result, actor = _run(registries, option, spec, before, perceiver)

    assert result.status is not ActionStatus.APPLIED
    assert result.verified_outcome is not True
    assert result.error_code is ErrorCode.E_POSTCONDITION_NOT_MET
    assert tuple(actor.taps) == ("a",)
