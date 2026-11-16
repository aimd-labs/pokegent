"""Options for the evolution scene and its learn-move part
(the runtime contracts).

Strategy (let it evolve or not, learn the move or keep the known four) is never decided here: both
answers are options with facts and an overridable default. What the code hard-codes is only what the
game forces: B cancels in one state, HM moves cannot be forgotten.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from pokegent.actions.menu.system_flow import evidence_evolution as EV
from pokegent.actions.menu.system_flow import layouts as L
from pokegent.actions.menu.system_flow import moves as M
from pokegent.actions.menu.system_flow import refs as R
from pokegent.actions.menu.system_flow.evidence import Verdict, task_named
from pokegent.actions.menu.system_flow.loops import QuestionCounter
from pokegent.actions.menu.system_flow.policy import SystemFlowOptions
from pokegent.actions.menu.system_flow.symbols import EVOLUTION_TASK
from pokegent.options.models import Legality, Option, Risk, fingerprint
from pokegent.options.registry import BuildContext, FrameDraft


def _view(snapshot) -> dict[str, int]:
    task = task_named(snapshot.ui, EVOLUTION_TASK)
    assert task is not None  # the verdict was derived from it
    return EV.evo_view(task)


def _names(snapshot, d, options: SystemFlowOptions) -> tuple[str, str]:
    tables, block = options.game_tables(), M.battle_block(snapshot)
    return (
        M.species_name(d["pre"], tables=tables, block=block),
        M.species_name(d["post"], tables=tables, block=block),
    )


def _mon_name(snapshot, d, options: SystemFlowOptions) -> str:
    """What the screen calls the Pokemon: its nickname (the learn-move part comes AFTER the
    evolution, the game renames an unnicknamed Pokemon), else the species it is now."""
    member = M.party_member(snapshot, d["party_id"])
    if member is not None and member.nickname:
        return member.nickname.title()
    pre, post = _names(snapshot, d, options)
    return post if d["state"] >= L.EVOSTATE_SET_MON_EVOLVED and not d["was_stopped"] else pre


def _wait(options: SystemFlowOptions, summary: str, facts: dict[str, Any]) -> Option:
    return Option(
        id=R.OPT_WAIT,
        label="Wait for the evolution scene",
        description=f"{summary} Wait until the scene moves on. No button is pressed.",
        legality=Legality.LEGAL,
        executor_ref=R.REF_WAIT,
        executor_args={"chunk": options.wait_chunk_frames},
        risk=Risk.LOW,
        verb="WAIT",
        facts={"inputs": 0, **facts},
    )


def start(ctx: BuildContext, snapshot, verdict: Verdict, options: SystemFlowOptions) -> FrameDraft:
    d = _view(snapshot)
    frm, to = _names(snapshot, d, options)
    evo = {"from": frm, "to": to, "can_cancel": True}
    default_cancel = options.evolution == "cancel"
    allow = Option(
        id=R.OPT_ALLOW_EVOLUTION,
        label=f"Let {frm} evolve",
        description=f"Do nothing: the evolution of {frm} into {to} goes ahead. No button is pressed.",  # noqa: E501
        legality=Legality.LEGAL,
        executor_ref=R.REF_WAIT,
        executor_args={"chunk": options.wait_chunk_frames},
        risk=Risk.LOW,
        verb="ALLOW_EVOLUTION",
        semantic_prompt=f"{frm} is evolving. Allow it?",
        effect_description=f"{frm} becomes {to}.",
        facts={"evolution": evo, "default": not default_cancel, "inputs": 0},
    )
    cancel = Option(
        id=R.OPT_CANCEL_EVOLUTION,
        label=f"Cancel the evolution of {frm}",
        description=(
            "Press B once while the cancel window is open. The Pokemon stays as it is; a level-up "
            "evolution is offered again at a later level-up."
        ),
        legality=Legality.LEGAL,
        executor_ref=R.REF_CANCEL_EVOLUTION,
        postcondition_ref=R.POST_EVO_CANCELLED,
        risk=Risk.NORMAL,
        verb="CANCEL_IF_POSSIBLE",
        semantic_prompt=f"{frm} is evolving. Allow it?",
        effect_description=f"{frm} stays {frm}.",
        facts={"evolution": evo, "default": default_cancel, "inputs": 1, "escape": True},
    )
    opts = (cancel, allow) if default_cancel else (allow, cancel)
    return FrameDraft(
        options=opts,
        state_fingerprint=fingerprint(R.EVO_START, pre=d["pre"], post=d["post"], state=d["state"]),
        context={
            "summary": f"{frm} is about to evolve into {to}; the evolution can still be cancelled.",
            "evolution_default": options.evolution,
        },
        deadline_ms=10000,
    )


def animation(
    ctx: BuildContext, snapshot, verdict: Verdict, options: SystemFlowOptions
) -> FrameDraft:
    d = _view(snapshot)
    frm, to = _names(snapshot, d, options)
    phase = str(verdict.evidence.get("phase"))
    wait = _wait(options, f"{frm} is evolving into {to}.", {"evolution": {"from": frm, "to": to}})
    return FrameDraft(
        options=(wait,),
        state_fingerprint=fingerprint(R.EVO_ANIMATION, state=d["state"], post=d["post"]),
        context={"summary": f"The evolution animation of {frm} is running ({phase})."},
        deadline_ms=10000,
    )


def done(ctx: BuildContext, snapshot, verdict: Verdict, options: SystemFlowOptions) -> FrameDraft:
    d = _view(snapshot)
    frm, to = _names(snapshot, d, options)
    stopped = bool(d["was_stopped"]) or d["state"] >= L.EVOSTATE_CANCEL
    what = f"{frm} did not evolve" if stopped else f"{frm} evolved into {to}"
    ack = Option(
        id=R.OPT_ACK_EVOLUTION,
        label="Acknowledge the evolution message",
        description=f"The message ({what}) waits for A. Press A once.",
        legality=Legality.LEGAL,
        executor_ref=R.REF_PRESS_A,
        postcondition_ref=R.POST_MOVED_ON,
        risk=Risk.LOW,
        verb="ACKNOWLEDGE",
        facts={"evolution": {"from": frm, "to": to}, "inputs": 1},
    )
    return FrameDraft(
        options=(ack,),
        state_fingerprint=fingerprint(R.EVO_DONE, state=d["state"], stopped=stopped),
        context={"summary": f"The evolution scene shows a message and waits for A ({what})."},
    )


# -- learn-move part -----------------------------------------------------------------------------


def _move_context(snapshot, d, options: SystemFlowOptions):
    tables, block = options.game_tables(), M.battle_block(snapshot)
    member = M.party_member(snapshot, d["party_id"])
    known = M.known_moves(member, tables=tables, block=block) if member is not None else []
    new_id = M.new_move_id(snapshot)
    new = M.move_facts(new_id, tables=tables, block=block) if new_id else None
    return member, known, new_id, new


def move_learning(
    ctx: BuildContext,
    snapshot,
    verdict: Verdict,
    options: SystemFlowOptions,
    loops: QuestionCounter | None = None,
) -> FrameDraft:
    d = _view(snapshot)
    phase = str(verdict.evidence.get("phase"))
    frm = _mon_name(snapshot, d, options)
    member, known, new_id, new = _move_context(snapshot, d, options)
    new_name = new.name if new is not None and new.name else "the new move"
    new_fact = new.as_fact() if new is not None else None
    known_facts = [m.as_fact() for m in known]
    move_summary = (
        f"New move: {M.describe_fact(new_fact)}. Known moves: "
        + "; ".join(M.describe_fact(m) for m in known_facts)
        if new_fact is not None
        else "Move details are not readable in this snapshot."
    )
    base_facts: dict[str, Any] = {
        "known_moves": known_facts,
        "move_summary": move_summary,
        **({"new_move": new_fact} if new_fact is not None else {"new_move_unknown": True}),
    }
    if phase == "message":
        adv = Option(
            id=R.OPT_ADVANCE_TEXT,
            label="Continue the message",
            description=f"A message of {frm}'s move learning waits for A. Press A once.",
            legality=Legality.LEGAL,
            executor_ref=R.REF_PRESS_A,
            postcondition_ref=R.POST_MOVED_ON,
            risk=Risk.LOW,
            facts={"inputs": 1},
        )
        return FrameDraft(
            options=(adv,),
            state_fingerprint=fingerprint(
                R.EVO_MOVE, phase=phase, state=d["state"], ls=d["learn_state"]
            ),
            context={"summary": f"A message of {frm}'s move learning waits for A."},
        )
    if phase == "yes_no":
        question = str(verdict.evidence.get("question"))
        asked = 0
        if loops is not None:
            asked = loops.enter((d["party_id"], d["pre"], d["post"], new_id), question)
        return _yes_no(ctx, verdict, d, frm, new_name, base_facts, asked, options.learn_loop_limit)
    if phase == "choose_slot":
        return _choose_slot(snapshot, d, frm, member, known, new_id, new, base_facts, options)
    # transitional phases (busy) never reach a builder; stay defensive
    wait = _wait(options, f"{frm} is learning a move.", {})
    return FrameDraft(
        options=(wait,),
        state_fingerprint=fingerprint(R.EVO_MOVE, phase=phase, ls=d["learn_state"]),
        context={"summary": f"{frm}'s move learning is between two screens."},
    )


def _yes_no(
    ctx: BuildContext,
    verdict,
    d,
    frm: str,
    new_name: str,
    base_facts: dict[str, Any],
    asked: int = 0,
    limit: int = 0,
) -> FrameDraft:
    question = str(verdict.evidence.get("question"))
    hidden: tuple[Option, ...] = ()
    if question == "delete_a_move":
        prompt = f"Delete a move to make room for {new_name}?"
        keep = Option(
            id="keep_moves",
            label=f"Keep the known moves, skip {new_name}",
            description="Answer NO: nothing is forgotten; the game then asks whether to stop learning.",  # noqa: E501
            legality=Legality.LEGAL,
            executor_ref=R.REF_EVO_ANSWER,
            executor_args={"entry": 1},
            postcondition_ref=R.POST_EVO_ANSWERED,
            risk=Risk.LOW,
            semantic_prompt=prompt,
            effect_description=f"{new_name} is not learned unless asked again.",
            facts={**base_facts, "inputs": 2},
        )
        forget = Option(
            id="forget_a_move",
            label=f"Forget a known move to learn {new_name}",
            description="Answer YES: the summary screen opens, where the move to forget is chosen.",
            legality=Legality.LEGAL,
            executor_ref=R.REF_EVO_ANSWER,
            executor_args={"entry": 0},
            postcondition_ref=R.POST_EVO_ANSWERED,
            risk=Risk.NORMAL,
            verb="REPLACE_MOVE",
            semantic_prompt=prompt,
            effect_description="A known move will be lost once a slot is chosen.",
            facts={**base_facts, "inputs": 2},
        )
        opts = (keep, forget)
    else:
        prompt = f"Stop trying to teach {new_name}?"
        bound = bool(limit) and asked > limit
        why = (
            f"This question has been asked {asked} times in this scene. Answering NO (keep trying) "
            "only returns to the question about forgetting a move, so that answer is withdrawn: "
            "stopping is the only way on."
            if bound
            else ""
        )
        loop_facts = {"question_asked": asked, "loop_bound": limit, "reason": why} if bound else {}
        stop = Option(
            id="stop_learning",
            label=f"Stop learning {new_name}",
            description=(
                f"Answer YES: stop_learning gives up {new_name}, keeps current moves. {why}"
            ).strip(),
            legality=Legality.LEGAL,
            executor_ref=R.REF_EVO_ANSWER,
            executor_args={"entry": 0},
            postcondition_ref=R.POST_EVO_ANSWERED,
            risk=Risk.LOW,
            semantic_prompt=prompt,
            effect_description=f"stop_learning: gives up {new_name}, keeps current moves.",
            facts={
                **base_facts,
                "inputs": 2,
                "consequence": f"stop_learning: gives up {new_name}, keeps current moves",
                **loop_facts,
            },
        )
        resume = Option(
            id="keep_trying",
            label=f"Keep trying to learn {new_name}",
            description=(
                f"Not offered any more. {why}"
                if bound
                else "Answer NO: continue_learning asks again whether to replace a move."
            ),
            legality=Legality.UNAVAILABLE if bound else Legality.LEGAL,
            executor_ref=None if bound else R.REF_EVO_ANSWER,
            executor_args={} if bound else {"entry": 1},
            postcondition_ref=None if bound else R.POST_EVO_ANSWERED,
            risk=Risk.LOW,
            semantic_prompt=prompt,
            effect_description="continue_learning: asks again whether to replace a move.",
            facts={
                **base_facts,
                "inputs": 2,
                "consequence": "continue_learning: asks again whether to replace a move",
                **loop_facts,
            },
        )
        opts = (stop, resume)
        hidden = ()
    return FrameDraft(
        options=opts,
        state_fingerprint=fingerprint(
            R.EVO_MOVE, phase="yes_no", question=question, ls=d["learn_state"]
        ),
        context={"summary": f"{frm} wants to learn {new_name}: {prompt}", "question": prompt},
        deadline_ms=10000,
        all_options=hidden,
    )


def _choose_slot(snapshot, d, frm, member, known, new_id, new, base_facts, policy) -> FrameDraft:
    new_name = new.name if new is not None and new.name else "the new move"
    give_up = Option(
        id="give_up",
        label=f"Give up on {new_name}",
        description="Press B in the summary screen: no move is chosen and the game asks whether to "
        "stop learning the new move.",
        legality=Legality.LEGAL,
        executor_ref=R.REF_PRESS_B,
        postcondition_ref=R.POST_MOVED_ON,
        risk=Risk.LOW,
        semantic_prompt="Which move should be forgotten?",
        effect_description="No move is forgotten; the game asks whether to stop learning.",
        facts={**base_facts, "inputs": 1, "escape": True},
    )
    options: list[Option] = [give_up]
    slots = [
        (slot, mv)
        for slot, mv in enumerate(member.moves if member is not None else ())
        if mv.move_id
    ]
    facts_by_id = {m.move_id: m for m in known}
    tables = policy.game_tables()
    block = M.battle_block(snapshot)
    for slot, mv in slots:
        mf = facts_by_id.get(mv.move_id)
        if mf is None or not mf.name:
            rom_fact = M.move_facts(mv.move_id, tables=tables, block=block, pp=mv.pp)
            if mf is None:
                mf = rom_fact
            elif rom_fact.name:
                mf = replace(mf, name=rom_fact.name)
        name = mf.name if mf is not None and mf.name else f"move {mv.move_id}"
        is_hm = mv.move_id in M.HM_MOVE_IDS
        # CONDITIONAL, not LEGAL: the summary cursor is not readable, so the walk to the slot is
        # checked by its outcome only (the runtime contracts 7); the
        # model sees 'outcome uncertain'
        legal = Legality.UNAVAILABLE if is_hm else Legality.CONDITIONAL
        facts: dict[str, Any] = {
            "slot": slot,
            "forgets": mf.as_fact() if mf is not None else {"move_id": mv.move_id, "name": name},
            **({"learns": new.as_fact()} if new is not None else {"new_move_unknown": True}),
        }
        if is_hm:
            facts["reason"] = "HM moves cannot be forgotten"
        options.append(
            Option(
                id=f"forget_move:{slot}",
                label=f"Forget {name} (slot {slot + 1})",
                description=f"Forget {M.describe(mf) if mf is not None else name} to learn "
                f"{M.describe(new) if new is not None else new_name}."
                if not is_hm
                else f"{M.describe(mf) if mf is not None else name} is an HM move; the game refuses to forget it.",  # noqa: E501
                legality=legal,
                executor_ref=R.REF_FORGET_MOVE if not is_hm else None,
                executor_args={
                    "slot": slot,
                    "old_move": mv.move_id,
                    "party_id": d["party_id"],
                }
                if not is_hm
                else {},
                postcondition_ref=R.POST_MOVE_FORGOTTEN if not is_hm else None,
                risk=Risk.HIGH,
                verb="REPLACE_MOVE",
                semantic_prompt="Which move should be forgotten?",
                effect_description=f"{name} is lost for good and {new_name} takes its place.",
                irreversible=not is_hm,
                facts=facts,
            )
        )
    return FrameDraft(
        options=tuple(options),
        state_fingerprint=fingerprint(
            R.EVO_MOVE,
            phase="choose_slot",
            moves=[mv.move_id for _, mv in slots],
            new=new_id,
            ls=d["learn_state"],
        ),
        context={"summary": f"Choose the move {frm} forgets to learn {new_name}."},
        deadline_ms=10000,
    )
