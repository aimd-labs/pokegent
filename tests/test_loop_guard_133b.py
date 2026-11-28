"""#133b: alternating hold hides looping exits, never the only exit."""

from pokegent.actions.field.builder import FieldBuilder
from pokegent.options.models import Legality, Option

REASON = "alternating_without_progress:hops=2,options=warp:1:95:1/warp:3:28:2"

def _opt(oid):
    return Option(id=oid, label=oid, legality=Legality.LEGAL, executor_ref="field.warp",
                  facts={"target": {"kind": "warp", "dest": [1, 2]}})

_HOLD = ({"scene_id": "loop_guard", "alternating": REASON},)

def _chooseable(opts):
    return sorted(o.id for o in opts if o.chooseable)

def test_looping_exits_hidden_while_held_and_other_options_stay():
    opts = [_opt("warp:1:95:1"), _opt("warp:3:28:2"), _opt("warp:9:9:9")]
    out = FieldBuilder()._filter_alternating_exits(opts, _HOLD)
    assert _chooseable(out) == ["warp:9:9:9"]

def test_only_exit_control_keeps_a_chooseable_option():
    opts = [_opt("warp:1:95:1"), _opt("warp:3:28:2")]
    assert _chooseable(FieldBuilder()._filter_alternating_exits(opts, _HOLD)) == [
        "warp:1:95:1", "warp:3:28:2"]

def test_no_reason_hides_nothing():
    opts = [_opt("warp:1:95:1"), _opt("warp:9:9:9")]
    assert _chooseable(FieldBuilder()._filter_alternating_exits(opts, ())) == [
        "warp:1:95:1", "warp:9:9:9"]

def test_runner_hold_expires_after_24_frames():
    from pokegent.loop import alternating_hold_records
    assert alternating_hold_records(REASON, 10, 33) == _HOLD
    assert alternating_hold_records(REASON, 10, 35) == ()
    assert alternating_hold_records(None, 10, 11) == ()

def test_one_center_trip_never_arms_a_hold():
    from pokegent.recovery import ProgressMonitor
    m = ProgressMonitor(100, now=0.0)
    for i, (oid, sig) in enumerate((("warp:1:95:1", "SA"), ("warp:3:28:2", "SB"), ("", "SA"))):
        m.observe(sig, now=float(i), iteration=i, map_key=(4, 99))
        m.note_decision(verified=True, option_id=oid, cycle_verified=True) if oid else None
    assert m.alternating_reason() is None
