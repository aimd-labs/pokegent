"""Regression for issue #35: a warp-ping loop must surface as ``progress.stuck``.

live-0016 ran 1320 verified actions using only 2 option ids (``warp:3:3:2 <-> warp:7:2:1``),
oscillating forever with ZERO ``progress.stuck``.  The root cause: the semantic progress
signature hashes position/party/bag/flags, so a warp ping that alternates two cells changes
position on EVERY step, ``observe()`` reports "progressed", and ``decision_limit`` never fires.
The detector must recognise a closed client-side option loop regardless of the position ping.
"""
from pokegent.recovery import ProgressMonitor


def _feed(monitor: ProgressMonitor, ids, *, now: float = 1000.0, start: int = 0):
    iteration = start
    for idx, oid in enumerate(ids):
        monitor.observe(f"sig-{idx}", now=now + idx, iteration=iteration)
        monitor.note_decision(verified=True, option_id=oid)
        iteration += 1
    return monitor


def test_warp_ping_two_option_loop_is_caught():
    """The exact live-0016 signature: 2 option ids, no position advance, must trip option_loop."""
    monitor = ProgressMonitor(8, now=0.0)
    # > loop_min_cycles * loop_window (3*64 = 192) verified decisions all drawn from
    # exactly the two warp ids from the live run.
    loop = ["warp:3:3:2", "warp:7:2:1"]
    _feed(monitor, (loop[i % 2] for i in range(60)), now=0.0)  # warm-up < window*cycles
    assert monitor.loop_reason() is None
    _feed(monitor, (loop[i % 2] for i in range(200)), now=0.0)
    reason = monitor.loop_reason()
    assert reason is not None and reason.startswith("option_loop:"), reason
    assert "distinct=2" in reason


def test_mostly_single_option_then_advance_does_not_false_positive():
    """A monotonically advancing option set (no return to a prior signature) is not a loop."""
    monitor = ProgressMonitor(8, now=0.0)
    ids = [f"walk:{i}" for i in range(40)]  # distinct option per step -> advances
    _feed(monitor, ids, now=0.0)
    assert monitor.loop_reason() is None


def test_loop_requires_repeated_ambient_window():
    """Fewer than loop_window*loop_min_cycles verified decisions cannot trip a loop."""
    monitor = ProgressMonitor(8, now=0.0, loop_window=4, loop_min_cycles=2)
    loop = ["warp:a:1", "warp:b:1"]
    _feed(monitor, (loop[i % 2] for i in range(6)), now=0.0)  # 6 < 4*2=8
    assert monitor.loop_reason() is None
    _feed(monitor, (loop[i % 2] for i in range(8)), now=0.0)
    assert monitor.loop_reason() is not None


def test_reset_window_keeps_loop_fingerprint():
    """Position ping-pong resets the window (progress) but must NOT clear option_loop."""
    monitor = ProgressMonitor(8, now=0.0, loop_window=4, loop_min_cycles=2)
    loop = ["warp:a:1", "warp:b:1"]
    for i in range(20):
        sig = f"sig-{i % 2}"  # ping-pong signature (position changes every step)
        monitor.observe(sig, now=float(i), iteration=i)
        monitor.note_decision(verified=True, option_id=loop[i % 2])
        # decision_limit never fires (progress resets it), but the loop detector must.
        if i >= 8:
            assert monitor.loop_reason() is not None


def test_invalid_loop_params_without_option_ids_safe():
    """note_decision without option_id leaves the loop detector inert but monotonic."""
    monitor = ProgressMonitor(2, now=0.0, loop_window=4, loop_min_cycles=2)
    for i in range(10):
        monitor.observe("same", now=float(i), iteration=i)
        monitor.note_decision(verified=True)  # no option_id supplied
    # Without option ids we cannot prove a loop; decision_limit still eventually fires.
    assert monitor.due_reason() is not None
