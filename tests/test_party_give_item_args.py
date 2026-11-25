"""Real give option args must reach the registered party menu executor."""

from types import SimpleNamespace as NS

import pytest

from pokegent.actions import Registries, register_all
from pokegent.actions._exec.models import Tap, WaitUntil
from pokegent.actions._exec.registry import StepContext, VerifyContext
from pokegent.actions._exec.verify import ExpectedStateMismatch
from pokegent.actions.menu.start_party import refs as R
from pokegent.options.registry import BuildContext
from pokegent.pipeline import Observation

# Base-game item 216 is the ordinary EXP Share, not a key item or HM.
EXP_SHARE = 216
TARGET_PID = 0x12345678
OTHER_PID = 0x87654321


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
    items = NS(pocket="items", capacity=30, items=(NS(item_id=EXP_SHARE, quantity=quantity),))
    berries = NS(pocket="berries", capacity=30, items=())
    return (items, berries)


def _party_snapshot(*, held_item=0, quantity=2, slot_id=0):
    menu = NS(
        window_in_use=True,
        visible=True,
        cursor_pos=0,
        min_cursor_pos=0,
        max_cursor_pos=2,
        consistent=True,
    )
    party = NS(
        active_hint=True,
        action_ids=(4, 5, 9),
        action_name="give_item",
        slot_id=slot_id,
        item_id=None,
    )
    ui = NS(
        valid=True,
        menu=NS(party=party, menu=menu, start_menu=None, list_menu=None, bag=None),
        tasks=NS(valid=True, active=()),
    )
    return NS(
        health=NS(valid=True),
        party=(_mon(0, TARGET_PID, held_item),),
        bag=_bag(quantity),
        ui=ui,
        player=None,
        map=None,
    )


def _bag_ready_snapshot(*, held_item=0, quantity=2):
    snapshot = _party_snapshot(held_item=held_item, quantity=quantity)
    snapshot.ui.menu.bag = NS(bag_open=True, active_hint=True, pocket=0)
    snapshot.ui.menu.list_menu = NS(
        window_in_use=True, total_items=1, max_showed=1, scroll_offset=0, cursor_row=0
    )
    snapshot.ui.menu.menu = NS(
        window_in_use=False,
        cursor_pos=0,
        min_cursor_pos=0,
        max_cursor_pos=2,
        consistent=True,
    )
    return snapshot


def _bag_context_snapshot(*, held_item=0, quantity=2):
    snapshot = _bag_ready_snapshot(held_item=held_item, quantity=quantity)
    snapshot.ui.menu.menu = NS(
        window_in_use=True,
        cursor_pos=0,
        min_cursor_pos=0,
        max_cursor_pos=1,
        consistent=True,
    )
    return snapshot


def _after_give_snapshot(*, personality=TARGET_PID, quantity=1):
    snapshot = _party_snapshot(quantity=quantity)
    snapshot.party = (_mon(0, personality, EXP_SHARE),)
    snapshot.ui.menu.party = None
    return snapshot


def _registries(monkeypatch):
    monkeypatch.delenv("FIRERED_ROM_PATH", raising=False)
    registries = Registries.new()
    register_all(registries)
    return registries


def _give_option(registries, snapshot, scene=R.PARTY_HELD_ITEM):
    builder = registries.builders.for_scene(scene)
    assert builder is not None
    draft = builder.build(BuildContext(snapshot=snapshot, scene=NS(scene_id=scene)))
    option = next(
        option
        for option in draft.options
        if option.action_contract is not None
        and option.action_contract.cursor_key == "party_action:give"
    )
    return draft, option


def _drive(generator, responses):
    """Run the registered macro, asserting every RAM gate on the supplied observation."""
    step = next(generator)
    for response in responses:
        if isinstance(step, WaitUntil):
            assert step.predicate(response), "synthetic observation does not satisfy the RAM gate"
        else:
            assert isinstance(step, Tap), step
        try:
            step = generator.send(response)
        except StopIteration as stop:
            return stop.value
    raise AssertionError("macro yielded more steps than the harness supplied")


def test_real_give_option_id_reaches_registered_macro_and_verifier(monkeypatch):
    registries = _registries(monkeypatch)
    scene = NS(scene_id=R.PARTY_HELD_ITEM)
    before = _party_snapshot()
    _, option = _give_option(registries, before)

    spec = registries.executors.get(option.executor_ref)
    assert spec is not None
    assert spec.check_args(option.executor_args) is None

    ctx = StepContext(
        option=option,
        args=option.executor_args,
        frame=NS(scene_id=R.PARTY_HELD_ITEM),
        snapshot=before,
        scene=scene,
    )
    after = _after_give_snapshot()
    result = _drive(
        spec.fn(ctx),
        [
            Observation(before, scene),
            Observation(_bag_ready_snapshot(), scene),
            Observation(_bag_context_snapshot(), scene),
            Observation(after, scene),
            Observation(after, scene),
        ],
    )
    assert option.executor_args == {"item_id": EXP_SHARE}
    assert result.snapshot.party[0].held_item == EXP_SHARE

    verifier = registries.verifiers.get(option.postcondition_ref)
    assert verifier is not None
    verify_ctx = VerifyContext(
        before=before,
        after=after,
        scene=scene,
        option=option,
        frame=ctx.frame,
        args=option.executor_args,
    )
    assert verifier.predicate(verify_ctx) is True

    wrong_target = _after_give_snapshot(personality=OTHER_PID)
    with pytest.raises(ExpectedStateMismatch):
        verifier.predicate(
            VerifyContext(
                before=before,
                after=wrong_target,
                scene=scene,
                option=option,
                frame=ctx.frame,
                args=option.executor_args,
            )
        )

    wrong_delta = _after_give_snapshot(quantity=2)
    with pytest.raises(ExpectedStateMismatch):
        verifier.predicate(
            VerifyContext(
                before=before,
                after=wrong_delta,
                scene=scene,
                option=option,
                frame=ctx.frame,
                args=option.executor_args,
            )
        )


def test_non_give_selections_keep_empty_args_and_take_delta(monkeypatch):
    registries = _registries(monkeypatch)
    draft, _ = _give_option(registries, _party_snapshot())
    assert all(
        option.executor_args == {}
        for option in draft.options
        if option.action_contract is not None
        and option.action_contract.cursor_key != "party_action:give"
    )

    take_draft, _ = _give_option(registries, _party_snapshot(held_item=EXP_SHARE))
    take = next(
        option
        for option in take_draft.options
        if option.action_contract is not None
        and option.action_contract.cursor_key == "party_action:take"
    )
    assert take.executor_args == {}
    (expected,) = take.action_contract.expected
    assert expected.kind == "held_item_changed"
    assert expected.item_id == EXP_SHARE
    assert expected.bag_item_id == EXP_SHARE
    assert expected.bag_delta == 1
