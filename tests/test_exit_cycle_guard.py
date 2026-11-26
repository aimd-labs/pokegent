"""Issue #133: default-off three-action/two-map exit-cycle guard.

The legacy loop detector allows at most two option ids over its full 192-decision
window, so a cycle that needs three actions (two exits plus a ``strength:`` action)
is structurally outside it.  The new guard is an exact opt-in and recognises ONLY
three identical consecutive blocks of three, each entry carrying the option id, the
full semantic signature and the exact map identity.
"""
from types import SimpleNamespace

import pytest

from pokegent.actions._exec.models import ActionResult
from pokegent.app import config_from_mapping
from pokegent.events import MemoryEventLog
from pokegent.games.gen3.firered.maps.map_schema import (
    MapGrid,
    MapIdentity,
    MapLayoutInfo,
    MapState,
    PlayerPlacement,
)
from pokegent.games.gen3.firered.reader.schemas import (
    FlagSet,
    Health,
    Player,
    StateSnapshot,
    VarTable,
)
from pokegent.loop import Runner, RunnerConfig, RunStatus
from pokegent.options.models import ChoiceResult, DecisionFrame, Legality, Option
from pokegent.pipeline import Perception, PerceptionKind
from pokegent.recovery import ProgressMonitor
from pokegent.state.scenes import SceneResult

# One block: exit on map A, strength on map A, exit back from map B.
BLOCK = (
    ("warp:a:1", "sig-A", (1, 1)),
    ("strength:b:1", "sig-B", (1, 1)),
    ("warp:c:1", "sig-C", (2, 2)),
)


def _feed(monitor, blocks=3, *, block=BLOCK, cycle_verified=True, start=0):
    iteration = start
    for _ in range(blocks):
        for oid, sig, map_key in block:
            monitor.observe(sig, now=float(iteration), iteration=iteration, map_key=map_key)
            monitor.note_decision(
                verified=True, option_id=oid, cycle_verified=cycle_verified
            )
            iteration += 1
    return iteration


def _close_cycle(monitor, start=9):
    """The post-action observation that proves the return to block[0]."""
    monitor.observe("sig-A", now=float(start), iteration=start, map_key=(1, 1))


# ---- opt-in wiring -----------------------------------------------------------------------------
def test_guard_is_off_by_default_and_rejects_non_bool():
    assert RunnerConfig().exit_cycle_guard is False
    with pytest.raises(ValueError):
        RunnerConfig(exit_cycle_guard=1)
    with pytest.raises(ValueError):
        ProgressMonitor(8, now=0.0, exit_cycle_guard=1)


def test_runner_config_parser_propagates_exact_bool():
    assert config_from_mapping({}).runner_config().exit_cycle_guard is False
    assert (
        config_from_mapping({"runner": {"exit_cycle_guard": True}})
        .runner_config()
        .exit_cycle_guard
        is True
    )


# ---- detector ----------------------------------------------------------------------------------
def test_three_exact_blocks_need_the_final_return_observation():
    m = ProgressMonitor(100, now=0.0, exit_cycle_guard=True)
    _feed(m)
    assert m.due_reason() is None  # nine note-only entries are not a closed return
    _close_cycle(m)
    assert m.exit_cycle_reason() == "exit_cycle:entries=9,actions=3,maps=2"
    assert m.due_reason() == "exit_cycle:entries=9,actions=3,maps=2"


def test_ninth_action_escaping_to_a_new_frontier_does_not_fire():
    m = ProgressMonitor(100, now=0.0, exit_cycle_guard=True)
    _feed(m)
    m.observe("sig-NEW", now=9.0, iteration=9, map_key=(3, 3))
    assert m.exit_cycle_reason() is None
    assert m.due_reason() is None


def test_guard_off_is_inert_for_the_same_entries():
    m = ProgressMonitor(100, now=0.0)
    _feed(m)
    assert m.exit_cycle_reason() is None
    assert m.due_reason() is None


def test_two_blocks_or_repeated_single_action_do_not_fire():
    m = ProgressMonitor(100, now=0.0, exit_cycle_guard=True)
    _feed(m, blocks=2)
    assert m.due_reason() is None

    repeated = ProgressMonitor(100, now=0.0, exit_cycle_guard=True)
    _feed(repeated, block=(("warp:a:1", "sig", (1, 1)),) * 3)
    assert repeated.due_reason() is None


def test_unknown_or_unusable_map_clears_and_disables_the_new_ring():
    m = ProgressMonitor(100, now=0.0, exit_cycle_guard=True)
    _feed(m, blocks=2)
    m.observe("sig-A", now=6.0, iteration=6, map_key=None)
    m.note_decision(verified=True, option_id="warp:a:1", cycle_verified=True)
    _feed(m, blocks=2, start=7)
    assert m.due_reason() is None

    bool_map = ProgressMonitor(100, now=0.0, exit_cycle_guard=True)
    _feed(bool_map, block=(("warp:a:1", "sig-A", (True, 1)),) + BLOCK[1:])
    assert bool_map.due_reason() is None


def test_unverified_or_interrupted_action_clears_the_new_ring():
    m = ProgressMonitor(100, now=0.0, exit_cycle_guard=True)
    _feed(m, blocks=2)
    m.observe("sig-A", now=6.0, iteration=6, map_key=(1, 1))
    m.note_decision(verified=True, option_id="warp:a:1", cycle_verified=False)
    _feed(m, blocks=2, start=7)
    assert m.due_reason() is None


def test_fourth_action_same_maps_or_changed_state_do_not_fire():
    fourth = ProgressMonitor(100, now=0.0, exit_cycle_guard=True)
    _feed(fourth, block=(
        ("warp:a:1", "sig-A", (1, 1)),
        ("strength:b:1", "sig-B", (1, 1)),
        ("warp:c:1", "sig-C", (2, 2)),
        ("warp:d:1", "sig-D", (2, 2)),
    ))
    assert fourth.due_reason() is None

    same_map = ProgressMonitor(100, now=0.0, exit_cycle_guard=True)
    _feed(same_map, block=tuple((oid, sig, (1, 1)) for oid, sig, _ in BLOCK))
    assert same_map.due_reason() is None

    changed = ProgressMonitor(100, now=0.0, exit_cycle_guard=True)
    for block_no in range(3):
        _feed(
            changed,
            blocks=1,
            block=tuple((oid, f"{sig}-{block_no}", map_key) for oid, sig, map_key in BLOCK),
            start=block_no * 3,
        )
    assert changed.due_reason() is None

    not_strength = ProgressMonitor(100, now=0.0, exit_cycle_guard=True)
    _feed(not_strength, block=(BLOCK[0], ("walk:b:1", "sig-B", (1, 1)), BLOCK[2]))
    assert not_strength.due_reason() is None


def test_clear_loop_clears_but_positional_reset_retains_cycle_evidence():
    m = ProgressMonitor(100, now=0.0, exit_cycle_guard=True)
    _feed(m)
    _close_cycle(m)
    m.reset_window(now=20.0, iteration=20)
    assert m.exit_cycle_reason() is not None
    m.reset_window(now=21.0, iteration=21, clear_loop=True)
    assert m.exit_cycle_reason() is None


def test_legacy_neighbour_still_detects_two_option_ping():
    m = ProgressMonitor(100, now=0.0, exit_cycle_guard=True, loop_window=4, loop_min_cycles=2)
    for i in range(8):
        oid = "warp:a:1" if i % 2 == 0 else "warp:b:1"
        sig = "SA" if i % 2 == 0 else "SB"
        m.observe(sig, now=float(i), iteration=i)
        m.note_decision(verified=True, option_id=oid)
    assert m.loop_reason() is not None


# ---- real Runner wiring ------------------------------------------------------------------------
def _map_state(
    group: int,
    number: int,
    x: int,
    *,
    identity_verified: bool = True,
    position_consistent: bool = True,
) -> MapState:
    identity = MapIdentity(
        group=group, number=number, name="Synthetic", layout_id=1,
        region_section_id=0, map_type=0, map_type_name="indoor", weather=0,
        cave=0, floor_num=0, battle_type=0, music=0, biking_allowed=False,
        allow_escaping=False, allow_running=True, identity_verified=identity_verified,
    )
    return MapState(
        identity=identity,
        layout=MapLayoutInfo(
            width=1, height=1, border_width=1, border_height=1, border=(0,),
            primary_tileset=0, secondary_tileset=0, layout_ptr=0,
        ),
        grid=MapGrid(columns=15, rows=15, blocks=(0,) * 225, attributes=((0, 0),)),
        player=PlayerPlacement(
            save_x=x, save_y=0, avatar_x=x, avatar_y=0, prev_x=x, prev_y=0,
            x=x, y=0, position_consistent=position_consistent, facing="N", elevation=0,
            object_event_id=0, avatar_flags=0, surfing=False, underwater=False,
            biking=False, controllable_flag=True, forced=False, moving=False,
            tile_transition_state=0, running_state=0,
        ),
    )


def _snapshot_with_map(*, identity_verified=True, position_consistent=True):
    return StateSnapshot.create(
        rom_profile="synthetic", snapshot_seq=0, health=Health(valid=True),
        player=Player(
            name="Test", gender="male", tid=0, sid=0, map_group=1,
            map_num=1, map_layout_id=0, x=0, y=0, money=0, coins=0,
        ),
        flags=FlagSet(), vars=VarTable(),
        map=_map_state(
            1, 1, 0, identity_verified=identity_verified,
            position_consistent=position_consistent,
        ),
    )


def test_snapshot_map_key_requires_usable_verified_consistent_map():
    assert Runner._snapshot_map_key(_snapshot_with_map()) == (1, 1)
    assert Runner._snapshot_map_key(_snapshot_with_map(identity_verified=False)) is None
    assert Runner._snapshot_map_key(_snapshot_with_map(position_consistent=False)) is None
    assert Runner._snapshot_map_key(
        SimpleNamespace(usable=False, map=_map_state(1, 1, 0))
    ) is None


def _exit_cycle_runner(
    *,
    guard: bool,
    max_steps: int,
    identity_verified: bool = True,
    position_consistent: bool = True,
    escape_on_ninth: bool = False,
):
    events = MemoryEventLog("synthetic")
    states = [(1, 1, 0), (1, 1, 1), (2, 2, 0)]
    if escape_on_ninth:
        states.append((3, 3, 2))
    options = ("warp:a:1", "strength:b:1", "warp:c:1")
    state = {"idx": 0}
    executions: list[str] = []

    class Decider:
        def decide(self, frame: DecisionFrame) -> ChoiceResult:
            return ChoiceResult(
                option_id=options[state["idx"] % len(options)], confidence=1.0,
                model_id="test", provider_id="test", decision_id=frame.decision_id,
            )

    def perceive(decision_id: str, *args):
        seq = len(executions)
        group, number, x = states[state["idx"]]
        snapshot = StateSnapshot.create(
            rom_profile="synthetic", snapshot_seq=seq, health=Health(valid=True),
            player=Player(
                name="Test", gender="male", tid=0, sid=0, map_group=group,
                map_num=number, map_layout_id=0, x=x, y=0, money=0, coins=0,
            ),
            flags=FlagSet(), vars=VarTable(),
            map=_map_state(
                group, number, x, identity_verified=identity_verified,
                position_consistent=position_consistent,
            ),
        )
        frame = DecisionFrame(
            decision_id=decision_id, run_id="synthetic", snapshot_seq=seq,
            scene_id="field.free", scene_confidence=1,
            state_fingerprint="sha256:" + f"{state['idx']:064d}",
            options=tuple(
                Option(id=key, label=key, legality=Legality.LEGAL, executor_ref="test.action")
                for key in options
            ),
        )
        return Perception(
            PerceptionKind.READY, snapshot,
            SceneResult(scene_id="field.free", confidence=1), frame,
        )

    def execute(frame: DecisionFrame, option_id: str) -> ActionResult:
        executions.append(option_id)
        if escape_on_ninth and len(executions) == 9:
            state["idx"] = 3
        else:
            state["idx"] = (state["idx"] + 1) % 3
        return ActionResult(
            status="applied", option_id=option_id, progressed=True,
            non_positional_progress=False, verified_outcome=True,
        )

    executor = SimpleNamespace(perceiver=True, last_emulator_frame=None, execute=execute)
    runner = Runner(
        decider=Decider(), executor=executor, events=events,
        perceiver=SimpleNamespace(perceive=perceive),
        config=RunnerConfig(max_steps=max_steps, exit_cycle_guard=guard),
    )
    return runner, executions


def test_runner_stops_the_three_action_cycle_after_nine_actions():
    runner, executions = _exit_cycle_runner(guard=True, max_steps=30)
    summary = runner.run()
    assert summary.status is RunStatus.ESCALATED
    assert summary.stop_reason == "no_progress"
    assert summary.stop_details["reason"] == "exit_cycle:entries=9,actions=3,maps=2"
    assert len(executions) == 9


def test_runner_ninth_action_landing_new_frontier_keeps_playing():
    runner, executions = _exit_cycle_runner(
        guard=True, max_steps=10, escape_on_ninth=True
    )
    summary = runner.run()
    assert summary.status is RunStatus.MAX_STEPS
    assert summary.stop_reason != "no_progress"
    assert len(executions) == 10


def test_runner_unverified_map_or_inconsistent_position_does_not_arm_guard():
    for variant in (
        {"identity_verified": False},
        {"position_consistent": False},
    ):
        runner, executions = _exit_cycle_runner(
            guard=True, max_steps=12, **variant
        )
        summary = runner.run()
        assert summary.status is RunStatus.MAX_STEPS, variant
        assert len(executions) == 12, variant


def test_runner_guard_off_keeps_legacy_behavior():
    runner, executions = _exit_cycle_runner(guard=False, max_steps=12)
    summary = runner.run()
    assert summary.status is RunStatus.MAX_STEPS
    assert len(executions) == 12
