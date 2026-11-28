from types import SimpleNamespace as N

from pokegent.actions.menu.system_flow.build_pc import MAX_ROSTER, _storage_main
from pokegent.actions.menu.system_flow.evidence import Verdict
from pokegent.actions.menu.system_flow.policy import SystemFlowOptions
from pokegent.options.models import DecisionFrame, to_system_one_payload


def _mon(species, level, moves=()):
    return N(species=species, personality=101, nickname="BOXNICK", level=level,
             moves=tuple(N(move_id=m, pp=30) for m in moves))


def _draft(boxes, goals=()):
    storage = N(boxes=tuple(tuple(b) for b in boxes),
                names=tuple(f"B{i}" for i in range(len(boxes))))
    ui = N(valid=True, menu=N(menu=N(window_in_use=True, consistent=True,
                                     min_cursor_pos=0, max_cursor_pos=2)))
    menus = N(pokemon_storage=lambda c: (N(stable_key="pc_storage:withdraw", label="x"),))
    tables = N(species=lambda s: N(name=f"S{s}", types=(4,)),
               type_name=lambda t: "Electric" if t == 4 else f"t{t}",
               move_name=lambda m: {1: "Pound", 2: "Growl"}.get(m, f"m{m}"))
    return _storage_main(N(ui=ui, storage=storage),
                         Verdict("pc.main", 0.95, evidence={}),
                         SystemFlowOptions(tables=tables, menus=menus), goals)


def test_storage_main_exposes_a_bounded_box_roster():
    draft = _draft([[_mon(1, 5, (1, 2)), None, _mon(2, 7)], [_mon(3, 12)]])
    roster = draft.options[0].facts["box_roster"]
    assert [tuple(e.values()) for e in roster["entries"]] == [
        (0, 0, "S1", 5, ("Electric",), ("Pound", "Growl")),
        (0, 2, "S2", 7, ("Electric",), ()), (1, 0, "S3", 12, ("Electric",), ())]
    assert set(roster["entries"][0]) == {"box", "slot", "species_name", "level", "types", "moves"}
    assert roster["total"] == 3 and roster["truncated"] is False
    big = _draft([[_mon(i, 50)] * 30 for i in range(6)]).options[0].facts["box_roster"]
    assert (len(big["entries"]), big["total"], big["truncated"]) == (MAX_ROSTER, 180, True)
    assert _draft([]).options[0].facts["box_roster"] == {"entries": (), "total": 0,
                                                         "truncated": False}
    frame = DecisionFrame(decision_id="decision-pc-box-roster-406", run_id="run-pc-406",
                          snapshot_seq=1, scene_id="pc.storage", scene_confidence=1.0,
                          state_fingerprint=draft.state_fingerprint, context={"box_roster": roster})
    payload = to_system_one_payload(frame)
    assert "BOXNICK" not in str(payload) and "personality" not in str(payload)


def _entries(goals, level):
    return _draft([[_mon(1, level)]], goals).options[0].facts["box_roster"]["entries"]


def test_box_roster_flags_rows_below_an_all_party_level_goal():
    low, high = _entries(((None, None, 30),), 25)[0], _entries(((None, None, 30),), 35)[0]
    assert (low["below_goal"], low["goal_level"]) == (True, 30)
    assert high["below_goal"] is False and "goal_level" not in high
    assert "below_goal" not in _entries((), 25)[0]
    # Personality or slot goals address party members, rows carry neither: ignored.
    assert "below_goal" not in _entries(((101, None, 30), (None, 2, 30)), 25)[0]
