"""Regression for issue #35 + review gaps: loop detection must be real cycle detection.

live-0016 ran 1320 verified actions using only 2 option ids (``warp:3:3:2 <-> warp:7:2:1``),
oscillating forever with ZERO ``progress.stuck``.  The root cause: the semantic progress
signature hashes position/party/bag/flags, so a warp ping that alternates two cells changes
position on EVERY step, ``observe()`` reports "progressed", and ``decision_limit`` never fires.

The detector must recognise a closed loop WITHOUT false-positiving on legitimate repeated-action
durable progress (review gaps 1-3).
"""
from pokegent.recovery import ProgressMonitor


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
            ProgressMonitor(8, now=0.0, loop_min_cycles=bad)
    # valid config accepted
    ProgressMonitor(8, now=0.0, loop_window=64, loop_distinct_limit=2, loop_min_cycles=3)
