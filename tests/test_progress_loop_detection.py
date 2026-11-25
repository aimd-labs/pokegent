"""Regression for issue #35 + review gaps: loop detection must be real cycle detection.

live-0016 ran 1320 verified actions using only 2 option ids (``warp:3:3:2 <-> warp:7:2:1``),
oscillating forever with ZERO ``progress.stuck``.  The root cause: the semantic progress
signature hashes position/party/bag/flags, so a warp ping that alternates two cells changes
position on EVERY step, ``observe()`` reports "progressed", and ``decision_limit`` never fires.

The detector must recognise a closed loop WITHOUT false-positiving on legitimate repeated-action
durable progress (review gaps 1-3).
"""
from types import SimpleNamespace

from pokegent.actions._exec.models import ActionResult
from pokegent.events import MemoryEventLog
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


def _signature(i: int) -> str:
    return f"sig-{i:04d}"


def _feed(monitor: ProgressMonitor, pairs):
    """Feed alternating observe()+note_decision for a sequence of (option, signature) pairs."""
    iteration = 0
    for oid, sig in pairs:
        monitor.observe(sig, now=float(iteration), iteration=iteration)
        monitor.note_decision(verified=True, option_id=oid)
        iteration += 1


def test_warp_ping_two_option_loop_is_caught():
    """The exact live-0016 signature: 2 option ids, state ping-pong -> true loop."""
    m = ProgressMonitor(8, now=0.0, loop_window=64, loop_min_cycles=3)
    pairs = [
        ("warp:3:3:2", "cellA") if i % 2 == 0 else ("warp:7:2:1", "cellB")
        for i in range(64 * 3)
    ]
    _feed(m, pairs)
    reason = m.loop_reason()
    assert reason is not None and reason.startswith("option_loop:"), reason
    assert "options=2" in reason


# ---- review gap 1: durable progress reusing one option must NOT loop ----
def test_monotone_progress_reusing_same_option_is_not_a_loop():
    """Many distinct increasing signatures with the SAME verified option = durable progress."""
    m = ProgressMonitor(8, now=0.0, loop_window=64, loop_min_cycles=3)
    _feed(m, [("grass:*", _signature(i)) for i in range(64 * 3 + 20)])
    assert m.loop_reason() is None  # every signature unique -> zero revisits


def test_repeated_id_changing_party_state_is_not_a_loop():
    """A grass grind that levels the party (new signature each step) reusing grass:* is NO loop."""
    m = ProgressMonitor(8, now=0.0, loop_window=64, loop_min_cycles=3)
    _feed(m, [("grass:6:56", _signature(i)) for i in range(64 * 3 + 20)])
    assert m.loop_reason() is None


# ---- review gap 2: the full claimed interval, not a tail ----
def test_128_distinct_then_64_alternating_does_not_loop():
    """Unrelated earlier history (128 distinct) must not leak into a tail loop trigger."""
    m = ProgressMonitor(8, now=0.0, loop_window=64, loop_min_cycles=3)
    earlier = [(_signature(i), _signature(i)) for i in range(128)]  # unrelated distinct actions
    ping = [
        ("warp:a:1", "SA") if i % 2 == 0 else ("warp:b:1", "SB")
        for i in range(64)
    ]
    _feed(m, earlier)
    _feed(m, ping)
    # The FULL 192 window now has 128 distinct options + 2 ping options => well over the limit.
    assert m.loop_reason() is None


def test_full_repetition_interval_boundary():
    """Exactly loop_window*loop_min_cycles alternating pairs fires; full interval inclusive."""
    m = ProgressMonitor(8, now=0.0, loop_window=4, loop_min_cycles=2)
    pairs = [
        ("warp:a:1", "SA") if i % 2 == 0 else ("warp:b:1", "SB")
        for i in range(4 * 2)
    ]
    _feed(m, pairs)
    assert m.loop_reason() is not None


# ---- review gap 3: reset semantics ----
def test_positional_ping_does_not_clear_loop_evidence():
    """observe()-driven reset (position change) must NOT clear the loop fingerprint."""
    m = ProgressMonitor(8, now=0.0, loop_window=4, loop_min_cycles=2)
    pairs = [
        ("warp:a:1", "SA") if i % 2 == 0 else ("warp:b:1", "SB")
        for i in range(4 * 2 + 4)
    ]
    for i, (oid, sig) in enumerate(pairs):
        m.observe(sig, now=float(i), iteration=i)
        m.note_decision(verified=True, option_id=oid)
        if i >= 4 * 2:
            assert m.loop_reason() is not None


def test_durable_progress_rearm_clears_loop_evidence():
    """A durable-progress reset (clear_loop=True) re-arms the loop detector."""
    m = ProgressMonitor(8, now=0.0, loop_window=4, loop_min_cycles=2)
    pairs = [
        ("warp:a:1", "SA") if i % 2 == 0 else ("warp:b:1", "SB")
        for i in range(4 * 2)
    ]
    _feed(m, pairs)
    assert m.loop_reason() is not None
    m.reset_window(now=100.0, iteration=1000, clear_loop=True)
    assert m.loop_reason() is None  # re-armed


def test_invalid_loop_params_raise():
    """Public params reject non-positive/non-int values (review: index safety)."""
    import pytest
    for bad in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            ProgressMonitor(bad, now=0.0)
    for bad in (0, -1):
        with pytest.raises(ValueError):
            ProgressMonitor(8, now=0.0, loop_window=bad)
        with pytest.raises(ValueError):
            ProgressMonitor(8, now=0.0, loop_distinct_limit=bad)
        with pytest.raises(ValueError):
            ProgressMonitor(8, now=0.0, loop_signature_limit=bad)
        with pytest.raises(ValueError):
            ProgressMonitor(8, now=0.0, loop_min_cycles=bad)
    # valid config accepted
    ProgressMonitor(8, now=0.0, loop_window=64, loop_distinct_limit=2,
                    loop_signature_limit=4, loop_min_cycles=3)


# ---- P2 finding 1: unchanged-adjacent must not be a "return"; old ping + fresh must not loop ----
def test_adjacent_duplicate_monotone_one_option_is_not_a_loop():
    """`0,0,1,1,...,95,95` with one option must NOT trip: adjacent duplicates are not returns."""
    m = ProgressMonitor(8, now=0.0, loop_window=64, loop_min_cycles=3)
    # 192 verified decisions; each signature appears twice adjacently then advances.
    pairs = [("walk:*", _signature(i // 2)) for i in range(192)]
    _feed(m, pairs)
    assert m.loop_reason() is None  # adjacent dups only, plus one fresh state per pair


def test_old_short_ping_then_fresh_states_is_not_a_loop():
    """`0,1,0,1,0` prefix followed by fresh states must NOT loop (obsolete revisits)."""
    m = ProgressMonitor(8, now=0.0, loop_window=64, loop_min_cycles=3)
    prefix = [("warp:a:1", "SA"), ("warp:b:1", "SB"),
              ("warp:a:1", "SA"), ("warp:b:1", "SB"), ("warp:a:1", "SA")]
    fresh = [("grass:*", _signature(i)) for i in range(192 - len(prefix))]
    _feed(m, prefix)
    _feed(m, fresh)
    # The old short ping is far history; the CURRENT tail has 187 unique signatures.
    assert m.loop_reason() is None


def test_genuine_reentry_after_leaving_is_a_loop():
    """`0,0,1,1,0,0,1,1,...` (revisited states after LEAVING) trips the loop band."""
    m = ProgressMonitor(8, now=0.0, loop_window=16, loop_min_cycles=3)
    # A ring that revisits the two states repeatedly (real closed loop), single option.
    pairs = [("walk:*", "SA" if (i // 2) % 2 == 0 else "SB") for i in range(16 * 3)]
    _feed(m, pairs)
    assert m.loop_reason() is not None


# ---- issue #133: the same two exits can occupy more than four tiles ----
def _two_exit_tiles(signatures: list[str]):
    """Alternate the two exit options while walking ``signatures`` in a ring."""
    return [
        (("warp:a:1" if sig.startswith("A") else "warp:b:1"), sig)
        for sig in (signatures[i % len(signatures)] for i in range(192))
    ]


def test_two_exit_cycle_with_multiple_tiles_is_caught():
    """A<->B accepted actions that walk 3 tiles on each side are still one closed cycle.

    The old absolute ``distinct signatures <= 4`` gate missed this: only two exit options
    ever recur, every action succeeds and every landing tile was already visited, yet the
    run never leaves the six-state ring.
    """
    m = ProgressMonitor(8, now=0.0, loop_window=64, loop_min_cycles=3)
    _feed(m, _two_exit_tiles(["A0", "A1", "A2", "B0", "B1", "B2"]))
    reason = m.loop_reason()
    assert reason is not None and reason.startswith("option_loop:"), reason
    assert "options=2" in reason


def test_two_exit_cycle_that_still_opens_new_states_is_not_a_loop():
    """Reusing two exits while the current tail keeps discovering new tiles is exploration."""
    m = ProgressMonitor(8, now=0.0, loop_window=64, loop_min_cycles=3)
    # 128 decisions settle the two-exit ring, then every action enters a brand-new tile.
    settled = _two_exit_tiles(["A0", "A1", "A2", "B0", "B1", "B2"])[:128]
    fresh = [
        ("warp:a:1" if i % 2 == 0 else "warp:b:1", f"FRESH-{i:04d}")
        for i in range(64)
    ]
    _feed(m, settled)
    _feed(m, fresh)
    assert m.loop_reason() is None


def test_few_new_tip_states_after_long_loop_are_not_stopped():
    """Escape may start with just 1-4 new states at the tip; old returns must not fire."""
    for new in (1, 4):
        m = ProgressMonitor(8, now=0.0, loop_window=64, loop_min_cycles=3)
        settled = _two_exit_tiles(["A0", "A1", "A2", "B0", "B1", "B2"])[: 192 - new]
        fresh = [("warp:a:1", f"ESCAPE-{i}") for i in range(new)]
        _feed(m, settled)
        _feed(m, fresh)
        assert m.loop_reason() is None, new


# ---- issue #133: the real Runner must stop the same two-exit cycle ----
def _map_cycle_runner(*, max_steps: int = 230):
    """Runner + fake executor cycling two exits through six already-visited tiles."""
    events = MemoryEventLog("synthetic")
    positions = [(0, 0, 0), (0, 0, 1), (0, 0, 2), (0, 1, 0), (0, 1, 1), (0, 1, 2)]
    state = {"idx": 0}
    executions: list[str] = []

    class Decider:
        def decide(self, frame: DecisionFrame) -> ChoiceResult:
            option_id = "warp:a:1" if state["idx"] < 3 else "warp:b:1"
            return ChoiceResult(option_id=option_id, confidence=1.0,
                                model_id="test", provider_id="test",
                                decision_id=frame.decision_id)

    def perceive(decision_id: str, *args):
        seq = len(executions)
        group, num, x = positions[state["idx"]]
        snapshot = StateSnapshot.create(
            rom_profile="synthetic", snapshot_seq=seq, health=Health(valid=True),
            player=Player(name="Test", gender="male", tid=0, sid=0, map_group=group,
                          map_num=num, map_layout_id=0, x=x, y=0, money=0, coins=0),
            flags=FlagSet(), vars=VarTable(),
        )
        frame = DecisionFrame(
            decision_id=decision_id, run_id="synthetic", snapshot_seq=seq,
            scene_id="field.free", scene_confidence=1,
            state_fingerprint="sha256:" + f"{state['idx']:064d}",
            options=tuple(
                Option(id=key, label=key, legality=Legality.LEGAL, executor_ref="test.action")
                for key in ("warp:a:1", "warp:b:1")
            ),
        )
        return Perception(PerceptionKind.READY, snapshot,
                          SceneResult(scene_id="field.free", confidence=1), frame)

    def execute(frame: DecisionFrame, option_id: str) -> ActionResult:
        executions.append(option_id)
        state["idx"] = (state["idx"] + 1) % len(positions)
        # Only the map/tile changed: the executor reports positional progress for a warp,
        # never durable (party/bag/flags) progress.
        return ActionResult(status="applied", option_id=option_id, progressed=True,
                            non_positional_progress=False, verified_outcome=True)

    executor = SimpleNamespace(perceiver=True, last_emulator_frame=None, execute=execute)
    runner = Runner(decider=Decider(), executor=executor, events=events,
                    perceiver=SimpleNamespace(perceive=perceive),
                    config=RunnerConfig(max_steps=max_steps))
    return runner, executions


def test_runner_stops_the_two_exit_tile_cycle_before_unbounded_repeat():
    runner, executions = _map_cycle_runner()
    summary = runner.run()
    assert summary.status is RunStatus.ESCALATED
    assert summary.verdict == "no_progress"
    assert summary.stop_reason == "no_progress"
    assert summary.stop_details["reason"].startswith("option_loop:options=2")
    # The cycle is caught at the full window, not after max_steps, and no input follows stop.
    assert len(executions) == 192
    assert len(executions) < runner.config.max_steps


def test_objective_revision_change_retires_old_loop_evidence(monkeypatch):
    """Loop evidence belongs to one objective revision; a new objective is not stopped."""
    runner, executions = _map_cycle_runner()
    m = runner._progress
    m.loop_window, m.loop_min_cycles, m._capacity = 64, 3, 192
    for i in range(192):
        sig = ["A0", "A1", "A2", "B0", "B1", "B2"][i % 6]
        m.observe(sig, now=float(i), iteration=i)
        m.note_decision(verified=True,
                        option_id="warp:a:1" if sig.startswith("A") else "warp:b:1")
    assert m.loop_reason() is not None  # old objective did reach a full closed loop
    runner._loop_goal_key = {"objective_id": "old", "revision": 1}
    monkeypatch.setattr(runner, "_objective_ids",
                        lambda: {"objective_id": "new", "revision": 2})
    assert runner._iteration() is None  # one new-objective action is allowed
    assert executions == ["warp:a:1"]
    assert m.loop_reason() is None  # old recurrence evidence was retired
