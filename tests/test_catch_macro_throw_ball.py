"""#411b: a scripted perceiver drives REF_THROW_BALL through a catch and a break-free."""
from types import SimpleNamespace as NS

from pokegent.actions import Registries, register_all
from pokegent.actions._exec.executor import Executor
from pokegent.actions._exec.models import ActionStatus
from pokegent.actions.battle import refs as R
from pokegent.actions.battle import state as S
from pokegent.pipeline import Observation

BALL, ME, CAUGHT = 9, 0x1111, 0x3333


def _mon(pid, slot=0, nickname=""):
    return NS(slot=slot, personality=pid, species=25, nickname=nickname)


def _bag(qty):
    return (NS(pocket="items", items=()), NS(pocket="key_items", items=()),
            NS(pocket="poke_balls", items=(NS(item_id=BALL, quantity=qty),)))


def _obs(party, qty, scene, *, phase="", action=(0,), pocket=0, op=None, comm=(), used=0):
    ui_battle = NS(action_cursor=action, input_battlers=(0,), controllers=())
    block = S.BattleBlock(valid=True, active=True, input_battlers=(0,),
                          last_used_item=used, script_op=op, comm=comm)
    menu = NS(bag=NS(pocket=pocket, active_hint=False),
              list_menu=NS(scroll_offset=0, cursor_row=0, window_in_use=False),
              menu=NS(window_in_use=True, consistent=True, cursor_pos=0, max_cursor_pos=1),
              party=NS(slot_id=None, active_hint=False), yes_no=None, multichoice=None,
              start_menu=None, summary_screen_open=False)
    ui = NS(valid=True, main=NS(valid=True, held_keys=None, callback2=NS(name="X")),
            battle=ui_battle, menu=menu, text=NS(any_active=False, printing=False,
                                                 waiting_for_button=False, printers=()))
    snap = NS(usable=True, emulator_frame=100, state_hash="h", ui=ui, party=party,
              bag=_bag(qty), battle=block)
    return Observation(snap, NS(scene_id=scene, busy=False, evidence={"phase": phase}))


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


def _drive(monkeypatch, before, args, staged, fallback, ref, post):
    monkeypatch.delenv("FIRED_ROM_PATH", raising=False)
    registries = Registries.new()
    register_all(registries)
    perceiver = _P()
    perceiver.obs, perceiver._fb = list(staged), fallback
    exe = Executor(_A(), registries.executors, registries.verifiers, perceiver,
                   clock=lambda: 0.0, interrupt_requested=lambda: False)
    ctx = NS(option=NS(facts={}, action_contract=None, executor_args=args), args=args,
             frame=NS(scene_id="battle.action"), snapshot=before.snapshot, scene=before.scene)
    return exe._run(registries.executors.get(ref), registries.verifiers.get(post), ctx, {})


def _throw(before):
    return [before, _obs([ME], 3, "battle.action", action=(1,)),
            before, _obs([ME], 3, "battle.bag", phase="list", pocket=0),
            before, _obs([ME], 3, "battle.bag", phase="list", pocket=1),
            before, _obs([ME], 3, "battle.bag", phase="list", pocket=2),
            before, _obs([ME], 3, "battle.bag", phase="context", pocket=2)]


def test_caught_throw_verifies_bag_minus_one_mon_added_nickname_declined(monkeypatch):
    before = _obs([ME], 3, "battle.action")
    caught = _obs([ME], 2, "battle.message", used=BALL)
    thrown = _drive(monkeypatch, before, {"item_id": BALL}, _throw(before) + [caught],
                    caught, R.THROW_BALL, R.PC_BALL)
    assert thrown.status is ActionStatus.APPLIED and thrown.verified_outcome is True
    box = _obs([ME], 2, "battle.message", op=S.OP_TRYGIVECAUGHTMONNICK, comm=(1, 0))
    no = _obs([ME], 2, "battle.message", op=S.OP_TRYGIVECAUGHTMONNICK, comm=(1, 1))
    done = _obs([_mon(ME), _mon(CAUGHT)], 2, "battle.ended")
    declined = _drive(monkeypatch, box, {"box": R.PROMPT_NICKNAME, "cursor": 1},
                      [box, box, no, box, done], done, R.ANSWER, R.PC_ANSWER)
    assert declined.status is ActionStatus.APPLIED and declined.verified_outcome is True
    assert [m.personality for m in done.snapshot.party] == [ME, CAUGHT]
    assert done.snapshot.party[1].nickname == "" and done.snapshot.bag[2].items[0].quantity == 2


def test_break_free_ball_commits_and_battle_continues(monkeypatch):
    before = _obs([ME], 3, "battle.action")
    broke = _obs([ME], 2, "battle.action", used=BALL)
    result = _drive(monkeypatch, before, {"item_id": BALL}, _throw(before) + [broke],
                    broke, R.THROW_BALL, R.PC_BALL)
    # PC_BALL verifies the throw commitment, never the catch (verifiers.py L10).
    assert result.status is ActionStatus.APPLIED and result.verified_outcome is True
    assert broke.snapshot.party == before.snapshot.party and broke.scene.scene_id == "battle.action"
