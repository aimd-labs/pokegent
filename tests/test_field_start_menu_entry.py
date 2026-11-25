"""The field.free START-menu entry: one typed opener that stops at menu.start.

Synthetic RAM/UI only. The builder offers the opener from a proven controllable
overworld; the registered executor presses START at most once and then waits,
without input, for the existing RAM decoder to prove an input-ready START menu.
"""

from types import SimpleNamespace

import pytest
from test_field_targets_hidden_flags import _state
from test_hybrid_route_first import _decider, _draft, _frame
from test_probe_primitives import snapshot

from pokegent.actions._exec.executor import Executor
from pokegent.actions._exec.models import ActionStatus
from pokegent.actions._exec.registry import (
    ExecutorRegistry,
    StepContext,
    VerifierRegistry,
)
from pokegent.actions.field import builder as B
from pokegent.actions.field.macros import executor_specs
from pokegent.actions.field.verify import verifier_specs
from pokegent.actions.menu.start_party.builder import StartPartyBuilder
from pokegent.games.gen3.firered.reader.ui_schemas import UiEvidence
from pokegent.goals.models import Objective
from pokegent.options.models import Legality, Option, Risk
from pokegent.options.registry import BuildContext
from pokegent.pipeline import Observation
from pokegent.state.scenes import SceneResult

START_MENU = {"cursor_pos": 0, "num_items": 2, "window_id": 0, "task_active": True}


def _fn(name):
    return {"ptr": 0x08000101, "name": name, "verified": True}


def _ui(*, start=None, party=None, callback="CB2_Overworld", menu_type=None):
    section = {"valid": True, "confidence": 1}
    return UiEvidence.model_validate(
        {
            "valid": True,
            "callback_id": callback,
            "menu_type": menu_type,
            "main": {**section, "callback2": _fn(callback), "in_battle": False},
            "tasks": {**section, "active": []},
            "text": section,
            "script": {**section, "running": False, "controls_locked": False},
            "menu": {**section, "start_menu": start, "party": party},
            "field": {
                **section,
                "fade_active": False,
                "moving": False,
                "turning": False,
            },
        }
    )


def _member(slot):
    return SimpleNamespace(
        slot=slot,
        personality=slot + 1,
        species=1,
        moves=(),
        level=5,
        hp=10,
        max_hp=10,
        held_item=0,
        is_egg=False,
        nickname=f"M{slot}",
    )


def _snap(*, members=2, ui=None):
    return snapshot().model_copy(
        update={
            "map": _state(()),
            "party": tuple(_member(slot) for slot in range(members)),
            "ui": _ui() if ui is None else ui,
        }
    )


def _draft_options(*, members=2, goal_id=None, field_menu=True, scene="field.free",
                   busy=False, ui=None):
    objective = (
        Objective(
            objective_id="goal", revision=1, kind="reach", target={"map": "DemoMap"}
        )
        if goal_id is not None
        else None
    )
    return B.FieldBuilder(
        field_menu=field_menu,
        objectives=(lambda _goal: objective) if objective is not None else None,
    ).build(
        BuildContext(
            snapshot=_snap(members=members, ui=ui),
            scene=SceneResult(
                scene_id=scene, confidence=1, busy=busy, detector="invented"
            ),
            goal_id=goal_id,
        )
    ).options


def _open(options):
    return next((option for option in options if option.id == "open_start"), None)


@pytest.mark.parametrize("members", [1, 2])
@pytest.mark.parametrize("goal_id", [None, "goal"])
def test_field_free_offers_one_open_start_with_any_party_size(members, goal_id):
    options = _draft_options(members=members, goal_id=goal_id)
    option = _open(options)
    assert option is not None
    assert option.label == "Open START menu"
    assert option.legality is Legality.LEGAL
    assert option.risk is Risk.LOW
    assert option.executor_ref == "field.open_start"
    assert option.postcondition_ref == "field.menu_open"
    assert option.facts["menu_open"] is True
    assert sum(candidate.id == "open_start" for candidate in options) == 1


def test_field_menu_flag_keeps_the_entry_opt_out():
    assert _open(_draft_options(field_menu=False)) is None


def test_busy_unknown_and_unreadable_field_frames_offer_no_entry():
    assert _open(_draft_options(busy=True)) is None
    assert _open(_draft_options(scene="scene.unknown")) is None
    assert _open(_draft_options(ui=SimpleNamespace(valid=False))) is None


def test_existing_swap_entry_is_preserved():
    options = _draft_options(members=2)
    assert any(option.id.startswith("swap:") for option in options)


def _verify_ctx(after, scene="menu.start", busy=False):
    return SimpleNamespace(
        scene=SceneResult(scene_id=scene, confidence=1, busy=busy),
        before=SimpleNamespace(),
        after=after,
        args={},
        option=SimpleNamespace(facts={}),
    )


def test_menu_open_verifier_requires_an_input_ready_decoded_start_menu():
    from pokegent.actions.field.verify import start_menu_open

    good = _snap(ui=_ui(start=START_MENU, menu_type="start_menu"))
    assert start_menu_open(_verify_ctx(good))
    assert not start_menu_open(_verify_ctx(good, scene="field.free"))
    assert not start_menu_open(_verify_ctx(good, busy=True))
    assert not start_menu_open(_verify_ctx(_snap(ui=_ui())))
    assert not start_menu_open(
        _verify_ctx(
            _snap(
                ui=_ui(
                    start={**START_MENU, "num_items": 0}, menu_type="start_menu"
                )
            )
        )
    )
    assert not start_menu_open(
        _verify_ctx(
            _snap(
                ui=_ui(
                    start={**START_MENU, "cursor_pos": 9}, menu_type="start_menu"
                )
            )
        )
    )


class _ScriptedPerceiver:
    def __init__(self, view):
        self.view = view

    def bind_settle(self, _settle):
        return None

    def observe(self):
        return self.view()


class _ScriptedActor:
    min_fps = 60.0
    wall_slack_s = 5.0

    def __init__(self, on_tap):
        self.taps = []
        self.on_tap = on_tap
        self.frames = 0

    def tap(self, buttons, hold_frames=2, wait_frames=1):
        self.taps.append(buttons)
        self.on_tap()
        return hold_frames

    def advance_frames(self, frames):
        self.frames += frames
        return frames

    def release_all(self):
        return None


def _run_open(specs, verifiers, *, open_menu):
    free = _snap()
    state = {"opened": False}

    def view():
        if not state["opened"]:
            return Observation(free, SceneResult(scene_id="field.free", confidence=1))
        return open_menu()

    perceiver = _ScriptedPerceiver(view)
    actor = _ScriptedActor(lambda: state.update(opened=True))
    executor = Executor(
        actor,
        ExecutorRegistry(),
        verifiers,
        perceiver,
        clock=lambda: 0.0,
        interrupt_requested=lambda: False,
    )
    spec = next(item for item in specs if item.ref == "field.open_start")
    ctx = StepContext(
        option=SimpleNamespace(facts={}, executor_args={}, action_contract=None),
        args={},
        frame=SimpleNamespace(),
        snapshot=free,
        scene=SceneResult(scene_id="field.free", confidence=1),
    )
    result = executor._run(spec, verifiers.get("field.menu_open"), ctx, {})
    return result, actor


def test_registered_executor_stops_at_decoded_menu_start_with_one_press():
    verifiers = VerifierRegistry()
    for vspec in verifier_specs():
        verifiers.register(vspec)
    menu = _snap(ui=_ui(start=START_MENU, menu_type="start_menu"))
    result, actor = _run_open(
        executor_specs(),
        verifiers,
        open_menu=lambda: Observation(
            menu, SceneResult(scene_id="menu.start", confidence=1)
        ),
    )
    assert result.status is ActionStatus.APPLIED
    assert result.verified_outcome is True
    assert actor.taps == ["start"]


def test_cursor_unready_timeout_sends_no_extra_input():
    verifiers = VerifierRegistry()
    for vspec in verifier_specs():
        verifiers.register(vspec)
    unreadable = _snap(ui=_ui(start={**START_MENU, "num_items": 0}))
    result, actor = _run_open(
        executor_specs(),
        verifiers,
        open_menu=lambda: Observation(
            unreadable, SceneResult(scene_id="menu.start", confidence=1)
        ),
    )
    assert result.status is not ActionStatus.APPLIED
    assert result.verified_outcome is not True
    assert actor.taps == ["start"]


def test_foreign_scene_after_start_is_interrupted_without_extra_input():
    verifiers = VerifierRegistry()
    for vspec in verifier_specs():
        verifiers.register(vspec)
    result, actor = _run_open(
        executor_specs(),
        verifiers,
        open_menu=lambda: Observation(
            _snap(), SceneResult(scene_id="scene.unknown", confidence=0)
        ),
    )
    assert result.status is ActionStatus.INTERRUPTED
    assert actor.taps == ["start"]


def _open_start_option(**changes):
    fields = {
        "id": "open_start",
        "label": "Open START menu",
        "legality": Legality.LEGAL,
        "facts": {"menu_open": True},
        "executor_ref": "field.open_start",
        "postcondition_ref": "field.menu_open",
        "risk": Risk.LOW,
    }
    fields.update(changes)
    return Option(**fields)


def test_route_first_ignores_only_the_exact_benign_start_entry(monkeypatch):
    frame = _frame(_draft(wall=True), options=(_open_start_option(),))
    choice = _decider(monkeypatch).deterministic_route_step(frame)
    assert choice is not None and choice.option_id == "warp:0:0:0"


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "open_menu"},
        {"facts": {}},
        {"facts": {"menu_open": False}},
        {"executor_ref": "field.open_menu"},
        {"postcondition_ref": "field.menu_closed"},
        {"risk": Risk.HIGH},
    ],
)
def test_non_exact_start_shapes_still_veto_route_first(monkeypatch, changes):
    frame = _frame(_draft(wall=True), options=(_open_start_option(**changes),))
    assert _decider(monkeypatch).deterministic_route_step(frame) is None


def test_start_party_offers_pokemon_then_party_item_choice_after_the_stop():
    menu = _snap(ui=_ui(start=START_MENU, menu_type="start_menu"))
    start = StartPartyBuilder().build(
        BuildContext(
            snapshot=menu,
            scene=SceneResult(scene_id="menu.start", confidence=1),
            goal_id=None,
        )
    )
    assert "start:pokemon" in {option.id for option in start.options}

    party = {
        "exit_callback": _fn("CB2_UpdatePartyMenu"),
        "task": _fn("Task_HandleChooseMonInput"),
        "menu_type": 0,
        "layout": 0,
        "slot_id": 0,
        "slot_id2": 0,
        "action": 0,
        "action_name": "give",
        "bag_item": 0,
        "active_hint": True,
        "item_id": None,
        "action_ids": (4,),
        "action_num": 1,
        "forced_send_out": False,
    }
    action = _snap(ui=_ui(party=party, menu_type="party_action")).model_copy(
        update={
            "bag": (
                SimpleNamespace(
                    pocket="items",
                    items=(SimpleNamespace(item_id=1, quantity=1),),
                ),
            )
        }
    )
    action_frame = StartPartyBuilder().build(
        BuildContext(
            snapshot=action,
            scene=SceneResult(scene_id="party.held_item", confidence=1),
            goal_id=None,
        )
    )
    assert any(option.id.startswith("give:") for option in action_frame.options)
