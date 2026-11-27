"""#133a: a verified 2..4 warp/edge hop block twice with a closed signature
fires alternating_without_progress; a Center trip, unverified hops, durable
changes and unreadable maps always stay unknown."""

from pokegent.recovery import ProgressMonitor

PP = (("warp:1:95:1", "SA"), ("warp:3:28:2", "SB"))
SEA = (("warp:4:101:1", "B4"), ("warp:4:102:1", "B3"),
       ("warp:4:103:1", "B2"), ("warp:4:102:2", "B3b"))


def _feed(m, block, repeats=2, verified=True):
    for i, (oid, sig) in enumerate(block * repeats):
        m.observe(sig, now=float(i), iteration=i, map_key=(4, 99))
        m.note_decision(verified=verified, option_id=oid, cycle_verified=verified)
    m.observe(block[0][1], now=99.0, iteration=99, map_key=(4, 99))


def test_pp_and_seafoam_blocks_fire():
    m = ProgressMonitor(100, now=0.0)
    _feed(m, PP)
    assert m.alternating_reason() == (
        "alternating_without_progress:hops=2,options=warp:1:95:1/warp:3:28:2")
    assert m.due_reason() == m.alternating_reason()
    m = ProgressMonitor(100, now=0.0)
    _feed(m, SEA)
    assert m.alternating_reason().startswith("alternating_without_progress:hops=4")


def test_never_arm_controls():
    m = ProgressMonitor(100, now=0.0)
    _feed(m, PP, repeats=1)
    assert m.alternating_reason() is None
    m = ProgressMonitor(100, now=0.0)
    _feed(m, PP)
    m.note_decision(verified=False, option_id="warp:3:28:2")
    _feed(m, PP, repeats=1)
    assert m.alternating_reason() is None
    m = ProgressMonitor(100, now=0.0)
    _feed(m, tuple((oid, f"{sig}{i}") for i, (oid, sig) in enumerate(PP * 2)), 1)
    assert m.alternating_reason() is None     # every hop a fresh signature
    m = ProgressMonitor(100, now=0.0)
    _feed(m, PP)
    m.observe("SA", now=99.0, iteration=99, map_key=None)
    assert m.alternating_reason() is None     # unreadable map clears
