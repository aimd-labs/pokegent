"""#408: a scripted perceiver drives the real PC storage macro end to end."""

from types import SimpleNamespace as NS

from pokegent.actions import Registries, register_all
from pokegent.actions._exec.executor import Executor
from pokegent.actions._exec.models import ActionStatus
from pokegent.actions.menu.system_flow import refs as R
from pokegent.actions.menu.system_flow import verify
from pokegent.actions.menu.system_flow.build_pc import PARTY_SIZE, _storage_main
from pokegent.actions.menu.system_flow.evidence import Verdict
from pokegent.actions.menu.system_flow.policy import SystemFlowOptions
from pokegent.options.models import Legality
from pokegent.pipeline import Observation

PID, QID, SCENE = 0x12345678, 0x87654321, NS(scene_id=R.PC_MAIN, busy=False)
_UI = NS(valid=True, main=NS(valid=True, in_battle=False,
                             callback2=NS(name="CB2_PokeStorage", label="CB2_PokeStorage")),
         tasks=NS(valid=True, active=(NS(func=NS(name="Task_PokeStorageMain"), data=()),)),
         text=NS(valid=True, printers=(), any_active=False, printing=False,
                 waiting_for_button=False),
         confidence={"main": 1.0, "tasks": 1.0},
         menu=NS(valid=True, yes_no=None, multichoice=None, list_menu=None,
                 start_menu=None, party=None, bag=None, summary_screen_open=False,
                 menu=NS(window_in_use=True, consistent=True, min_cursor_pos=0, max_cursor_pos=2)))


def _mon(slot, pid):
    return NS(slot=slot, personality=pid, species=25)


def _storage(boxes, bo=0, ca=0, cp=0, popup=None):
    s = NS(boxes=boxes, names=tuple(f"B{i}" for i in range(len(boxes))), box_option=bo,
           cursor_area=ca, cursor_position=cp, cursor_settled=True, ui_state=0,
           current_box=0, deposit_box_cursor=popup, options_button_mode=0,
           get=lambda box, slot: boxes[box][slot])
    return s


def _snap(party, storage, tag):
    return NS(usable=True, emulator_frame=100, rom_profile="firered-us-rev0",
              state_hash=tag, ui=_UI, party=party, storage=storage)


class _P:
    def bind_settle(self, settle):
        return None

    def observe(self):
        return self.obs.pop(0) if self.obs else self._fb


class _A:
    def tap(self, buttons, hold_frames=2, wait_frames=1):
        return hold_frames

    def advance_frames(self, frames):
        return frames


def _drive(monkeypatch, before, args, staged, fallback):
    monkeypatch.delenv("FIRERED_ROM_PATH", raising=False)
    registries = Registries.new()
    register_all(registries)
    perceiver = _P()
    perceiver.obs = [Observation(s, SCENE) for s in staged]
    perceiver._fb = Observation(fallback, SCENE)
    exe = Executor(_A(), registries.executors, registries.verifiers, perceiver,
                   clock=lambda: 0.0, interrupt_requested=lambda: False)
    ctx = NS(option=NS(facts={}, action_contract=None, executor_args=args), args=args,
             frame=NS(scene_id=R.PC_MAIN), snapshot=before, scene=SCENE)
    return exe._run(registries.executors.get(R.REF_PC_STORAGE),
                    registries.verifiers.get(R.POST_PC_STORAGE_CHANGED), ctx, {})


def test_withdraw_refused_before_any_input_when_party_full():
    party = tuple(_mon(s, 100 + s) for s in range(PARTY_SIZE))
    draft = _storage_main(NS(ui=_UI, storage=_storage(((_mon(0, PID),),)), party=party),
                          Verdict("pc.main", 0.95, evidence={}),
                          SystemFlowOptions(tables=None, menus=NS(pokemon_storage=lambda c: (
                              NS(stable_key="pc_storage:withdraw", label="W"),))))
    option = draft.options[0]
    assert option.legality is Legality.UNAVAILABLE and option.executor_ref is None
    assert option.facts["reason"] == "party full: deposit first"


def test_unchanged_counts_never_verify():
    before = _snap((_mon(0, QID),), _storage(((_mon(0, PID), _mon(1, QID)),)), "same")
    for a in ({"pid": PID, "operation": 0}, {"pid": PID, "operation": 1}):
        assert verify._pc_storage_changed(NS(before=before, after=before, args=a)) is False


def test_destination_box_full_fails_without_applying(monkeypatch):
    boxes = (tuple(_mon(s, 10 + s) for s in range(6)), ())
    party = (_mon(0, PID), _mon(1, QID), _mon(2, 3))
    ready = _snap(party, _storage(boxes, 1, 1), "ready")
    popup = _snap(party, _storage(boxes, 1, 1, popup=0), "popup")
    result = _drive(monkeypatch, ready, {"pid": PID, "operation": 1, "box": 0},
                    [ready] * 7 + [popup, popup], popup)
    assert (result.status, result.verified_outcome) != (ActionStatus.APPLIED, True)
    assert "destination box full" in result.reason


def test_withdraw_moves_box_mon_into_party_and_applies(monkeypatch):
    party = (_mon(0, QID), _mon(1, 3))
    boxes = ((_mon(0, PID), _mon(1, 44), None, None, None, None), ())
    before = _snap(party, _storage(boxes, 0, 0, 0), "before")
    after = _snap(party + (_mon(2, PID),),
                  _storage(((None, _mon(1, 44), None, None, None, None), ()), 0, 0, 0), "after")
    result = _drive(monkeypatch, before, {"pid": PID, "operation": 0},
                    [before] * 7 + [after], after)
    assert result.status is ActionStatus.APPLIED and result.verified_outcome is True


def test_deposit_moves_party_member_into_roomy_box_and_applies(monkeypatch):
    party = (_mon(0, PID), _mon(1, QID), _mon(2, 3))
    boxes = ((None,) * 6, ())
    ready = _snap(party, _storage(boxes, 1, 1), "ready")
    popup = _snap(party, _storage(boxes, 1, 1, 0, popup=0), "popup")
    after_party = (_mon(0, QID), _mon(1, 3))
    after = _snap(after_party, _storage(((_mon(0, PID),) + (None,) * 5, ()), 1, 1), "after")
    result = _drive(monkeypatch, ready, {"pid": PID, "operation": 1, "box": 0},
                    [ready] * 7 + [popup], after)
    assert result.status is ActionStatus.APPLIED and result.verified_outcome is True
