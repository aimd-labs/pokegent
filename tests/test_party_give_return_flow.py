"""Owning RED for the real party-origin GIVE return flow.

The reviewed macro assumed that selecting a bag row opens a ``bag.context`` GIVE menu and
emits a second bag confirmation.  Pinned pret/pokefirered 037335f4 disagrees:

* ``src/item_menu.c`` ``Task_ItemContext_FieldGive`` closes the bag for an ordinary item;
* ``src/party_menu.c`` then runs ``CB2_GiveHoldItem``: an empty held slot transfers directly
  (``Task_GiveHoldItem``), while an existing held item goes through a swap prompt that waits
  for YES/NO input (``Task_SwitchHoldItemsPrompt`` -> ``Task_HandleSwitchItemsYesNoInput``).

These tests drive the registered ``StartPartyDetector`` (real party/busy classification) and the
full ``Executor`` boundary, not ``spec.fn`` alone, so the live failure mode is visible: the generic
post-step busy wait treats an input-waiting party prompt as busy and exhausts its frame budget.
Synthetic evidence only; it is not proof of live RAM, named live tasks or a live transfer.
"""

from types import SimpleNamespace as NS

import pytest

from pokegent.actions import Registries, register_all
from pokegent.actions._exec.executor import Executor
from pokegent.actions._exec.models import ActionStatus, ExecutionKind
from pokegent.actions._exec.registry import VerifyContext
from pokegent.actions._exec.verify import ExpectedStateMismatch
from pokegent.actions.menu.start_party import refs as R
from pokegent.actions.menu.start_party.detect import StartPartyDetector
from pokegent.errors import ErrorCode
from pokegent.games.gen3.firered.reader.ui_schemas import (
    BagInfo,
    FieldEvidence,
    FnRef,
    ListMenuInfo,
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
OLD_ITEM = 181  # a different verified catalog item id, used only as "already held".
TARGET_PID = 0x12345678
OTHER_PID = 0x87654321
BAG_SCENE = "menu.open"


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


def _bag(quantity, item_id=EXP_SHARE):
    held = NS(item_id=item_id, quantity=quantity)
    items = NS(pocket="items", capacity=30, items=(held,))
    berries = NS(pocket="berries", capacity=30, items=())
    return (items, berries)


def _task(name=None, verified=False):
    return NS(func=NS(name=name, verified=verified), data=(), priority=0)


def _smenu(cursor=0, low=0, high=2):
    return NS(
        window_in_use=True,
        cursor_pos=cursor,
        min_cursor_pos=low,
        max_cursor_pos=high,
        consistent=True,
        window_id=1,
    )


def _party_ui(
    *,
    held_item=0,
    cursor=0,
    action_ids=(4, 5, 9),
    action_name="give_item",
    tasks=(),
    smenu=None,
    fade=False,
):
    party = NS(
        active_hint=True,
        menu_type=0,
        slot_id=0,
        item_id=None,
        action_ids=action_ids,
        action_name=action_name,
    )
    return NS(
        valid=True,
        menu_type=None,
        main=NS(valid=True, in_battle=False, callback2=None),
        tasks=NS(valid=True, active=tuple(tasks)),
        text=NS(
            valid=True,
            windows=(),
            printers=(),
            any_active=False,
            printing=False,
            waiting_for_button=False,
        ),
        field=NS(valid=True, fade_active=fade),
        script=NS(valid=True, running=False, controls_locked=False),
        menu=NS(
            valid=True,
            party=party,
            menu=smenu,
            start_menu=None,
            list_menu=None,
            bag=NS(bag_open=False, active_hint=False, pocket=0),
            yes_no=None,
            multichoice=None,
            summary_screen_open=False,
        ),
    )


def _bag_ui(*, list_open=True):
    """Real UI evidence of the open bag (the GIVE item picker), with its live list."""
    bag = FnRef(ptr=0x08000001, name="CB2_BagMenuRun", verified=True)
    return UiEvidence(
        valid=True,
        callback_id="CB2_BagMenuRun",
        menu_type="bag",
        main=MainEvidence(valid=True, confidence=1.0, callback2=bag, in_battle=False),
        tasks=TaskEvidence(valid=True, confidence=1.0, active=(), signatures=(), count=0),
        text=TextEvidence(valid=True, confidence=1.0),
        script=ScriptEvidence(valid=True, confidence=1.0, running=False, controls_locked=False),
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
            list_menu=ListMenuInfo(
                task_slot=0,
                total_items=1,
                max_showed=1,
                window_id=5,
                scroll_offset=0,
                cursor_row=0,
                window_in_use=list_open,
            ),
        ),
        field=FieldEvidence(valid=True, confidence=1.0, fade_active=False, prevent_step=False),
        confidence={"main": 1.0, "tasks": 1.0, "menu": 1.0, "text": 1.0, "field": 1.0},
    )


def _snapshot(*, held_item=0, quantity=2, ui, tag):
    return NS(
        usable=True,
        snapshot_seq=1,
        rom_profile="firered-us-rev0",
        emulator_frame=100,
        state_hash=tag,
        health=NS(valid=True),
        ui=ui,
        party=(_mon(0, TARGET_PID, held_item),),
        bag=_bag(quantity),
        player=NS(map_group=0, map_num=0, money=0),
        map=None,
        flags=None,
        battle=None,
    )


def _party_snapshot(*, held_item=0, quantity=2, tag="party", **ui_kw):
    return _snapshot(
        held_item=held_item,
        quantity=quantity,
        ui=_party_ui(held_item=held_item, **ui_kw),
        tag=tag,
    )


def _bag_snapshot(*, held_item=0, quantity=2, tag="bag"):
    return _snapshot(
        held_item=held_item,
        quantity=quantity,
        ui=_bag_ui(),
        tag=tag,
    )


def _after_snapshot(*, held_item=EXP_SHARE, quantity=1, personality=TARGET_PID):
    snapshot = _party_snapshot(
        held_item=held_item,
        quantity=quantity,
        tag="after",
        action_ids=(5, 9),
        action_name="take_item",
        tasks=(_task("Task_HandleSelectionMenuInput", True),),
        smenu=_smenu(0, 0, 1),
    )
    snapshot.party = (_mon(0, personality, held_item),)
    return snapshot


def _prompt_snapshot(*, held_item=OLD_ITEM, cursor=0, quantity=2):
    """A live two-entry party prompt under CB2_UpdatePartyMenu (swap-items structural shape).

    The task is deliberately unnamed: the current profile does not prove a live symbol here, and
    the settled prompt's printer is inactive, so neither text activity nor a task name can prove
    that YES means swap.  The macro may therefore only withhold input.
    """
    return _party_snapshot(
        held_item=held_item,
        quantity=quantity,
        tag=f"prompt-{cursor}",
        action_ids=(),
        action_name=None,
        tasks=(_task(None, False),),
        smenu=_smenu(cursor, 0, 1),
    )


def _obs(snapshot, scene):
    return Observation(snapshot, scene)


def _party_scene(snapshot):
    scene = StartPartyDetector().detect(snapshot)
    assert scene is not None, "the registered party detector must classify this synthetic state"
    return scene


def _bag_scene(registries, snapshot):
    verdict = registries.detectors.detect(snapshot)
    assert verdict.scene_id == BAG_SCENE, verdict.scene_id
    return verdict


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


def _give_setup(monkeypatch, *, held_item=0):
    registries = _registries(monkeypatch)
    before = _party_snapshot(
        held_item=held_item,
        tasks=(_task("Task_HandleSelectionMenuInput", True),),
        smenu=_smenu(0, 0, 2),
    )
    option, spec = _option_for(registries, before, "party_action:give")
    assert option.executor_args == {"item_id": EXP_SHARE}
    return registries, option, spec, before


def test_empty_held_give_returns_from_the_bag_and_verifies_transfer(monkeypatch):
    """The real source flow: item-list A closes the bag and the game gives the item itself."""
    registries, option, spec, before = _give_setup(monkeypatch)
    bag = _bag_snapshot(held_item=0, quantity=2)
    transition = _party_snapshot(
        held_item=0, quantity=2, tag="giving", action_ids=(), tasks=(_task(None),)
    )
    after = _after_snapshot()
    perceiver = _Perceiver(
        [
            _obs(before, _party_scene(before)),
            _obs(bag, _bag_scene(registries, bag)),
            _obs(bag, _bag_scene(registries, bag)),
            _obs(transition, _party_scene(transition)),
            _obs(after, _party_scene(after)),
        ],
        _obs(after, _party_scene(after)),
    )

    result, actor = _run(registries, option, spec, before, perceiver)

    assert result.status is ActionStatus.APPLIED, result.reason
    assert result.verified_outcome is True
    assert result.interrupted_scene is None
    # Party GIVE confirmation + the bag item row.  No fictional bag-context GIVE press.
    assert tuple(actor.taps) == ("a", "a")


def test_existing_held_item_prompt_is_refused_without_confirmation(monkeypatch):
    """Partial support: existing-held swap needs identity the profile cannot prove, so no YES."""
    registries, option, spec, before = _give_setup(monkeypatch, held_item=OLD_ITEM)
    bag = _bag_snapshot(held_item=OLD_ITEM, quantity=2)
    prompt = _prompt_snapshot(held_item=OLD_ITEM, cursor=0)
    perceiver = _Perceiver(
        [
            _obs(before, _party_scene(before)),
            _obs(bag, _bag_scene(registries, bag)),
            _obs(bag, _bag_scene(registries, bag)),
            _obs(prompt, _party_scene(prompt)),
        ],
        _obs(prompt, _party_scene(prompt)),
    )

    result, actor = _run(registries, option, spec, before, perceiver)

    assert result.status is ActionStatus.INTERRUPTED
    assert result.error_code is ErrorCode.E_INTERRUPTED_SCENE
    assert result.verified_outcome is not True
    assert "unverified swap prompt" in result.reason
    # Party GIVE confirmation + bag item row only: never a YES/NO answer.
    assert tuple(actor.taps) == ("a", "a")


def test_unrelated_two_entry_party_prompt_gets_zero_confirmation(monkeypatch):
    """Even with an empty held slot, an anonymous two-entry party menu is not an authorized YES."""
    registries, option, spec, before = _give_setup(monkeypatch)
    bag = _bag_snapshot(held_item=0, quantity=2)
    prompt = _prompt_snapshot(held_item=0, cursor=0)
    perceiver = _Perceiver(
        [
            _obs(before, _party_scene(before)),
            _obs(bag, _bag_scene(registries, bag)),
            _obs(bag, _bag_scene(registries, bag)),
            _obs(prompt, _party_scene(prompt)),
        ],
        _obs(prompt, _party_scene(prompt)),
    )

    result, actor = _run(registries, option, spec, before, perceiver)

    assert result.status is ActionStatus.INTERRUPTED
    assert result.error_code is ErrorCode.E_INTERRUPTED_SCENE
    assert result.verified_outcome is not True
    assert tuple(actor.taps) == ("a", "a")


def test_unknown_party_transition_never_presses_a_verification_input(monkeypatch):
    registries, option, spec, before = _give_setup(monkeypatch)
    bag = _bag_snapshot(held_item=0, quantity=2)
    transition = _party_snapshot(
        held_item=0, quantity=2, tag="unknown-task", action_ids=(), tasks=(_task(None),)
    )
    perceiver = _Perceiver(
        [
            _obs(before, _party_scene(before)),
            _obs(bag, _bag_scene(registries, bag)),
            _obs(bag, _bag_scene(registries, bag)),
            _obs(transition, _party_scene(transition)),
        ],
        _obs(transition, _party_scene(transition)),
    )

    result, actor = _run(registries, option, spec, before, perceiver)

    assert result.status is not ActionStatus.APPLIED
    assert result.verified_outcome is not True
    assert tuple(actor.taps) == ("a", "a")


@pytest.mark.parametrize(
    "after",
    [
        _after_snapshot(personality=OTHER_PID),
        _after_snapshot(quantity=2),
    ],
    ids=["wrong-member", "wrong-bag-delta"],
)
def test_identity_or_quantity_negative_never_applies(monkeypatch, after):
    registries, option, spec, before = _give_setup(monkeypatch)
    bag = _bag_snapshot(held_item=0, quantity=2)
    transition = _party_snapshot(
        held_item=0, quantity=2, tag="giving", action_ids=(), tasks=(_task(None),)
    )
    perceiver = _Perceiver(
        [
            _obs(before, _party_scene(before)),
            _obs(bag, _bag_scene(registries, bag)),
            _obs(bag, _bag_scene(registries, bag)),
            _obs(transition, _party_scene(transition)),
            _obs(after, _party_scene(after)),
        ],
        _obs(after, _party_scene(after)),
    )

    result, actor = _run(registries, option, spec, before, perceiver)

    assert result.status is not ActionStatus.APPLIED
    assert result.verified_outcome is not True
    assert tuple(actor.taps) == ("a", "a")


def test_registered_verifier_rejects_wrong_member_and_bag_delta(monkeypatch):
    registries, option, _spec, before = _give_setup(monkeypatch)
    verifier = registries.verifiers.get(option.postcondition_ref)
    assert verifier is not None
    frame = NS(scene_id=R.PARTY_HELD_ITEM)
    for after in (_after_snapshot(personality=OTHER_PID), _after_snapshot(quantity=2)):
        context = VerifyContext(
            before=before,
            after=after,
            scene=NS(scene_id=R.PARTY_HELD_ITEM),
            option=option,
            frame=frame,
            args=option.executor_args,
        )
        with pytest.raises(ExpectedStateMismatch):
            verifier.predicate(context)


def test_give_gets_a_dedicated_busy_owning_spec_and_other_selections_do_not(monkeypatch):
    registries = _registries(monkeypatch)
    before = _party_snapshot(
        held_item=EXP_SHARE,
        tasks=(_task("Task_HandleSelectionMenuInput", True),),
        smenu=_smenu(0, 0, 2),
        action_ids=(4, 5, 9),
        action_name="give_item",
    )
    give, give_spec = _option_for(registries, before, "party_action:give")
    take, take_spec = _option_for(registries, before, "party_action:take")

    assert give.executor_ref == R.REF_GIVE
    assert give_spec.kind is ExecutionKind.MACRO
    assert give_spec.macro_manages_busy is True
    assert take.executor_ref == R.REF_SELECT
    assert take_spec.kind is ExecutionKind.DETERMINISTIC
    assert take_spec.macro_manages_busy is False
    assert take_spec.allowed_scenes == give_spec.allowed_scenes
