"""Option builder of the dialog family (spec 6.C, 6.K): every legal option, nothing guessed.

One builder for the ``dialog`` family plus the two ``system`` scenes of this package (whiteout,
quest log). It reads ``snapshot.ui`` (counts and cursors come from RAM, never from a constant) and
the proven-current message text from the sidecar. It never presses, never calls a model and never
assigns frame identity.

Rules this file enforces (each has a test):

* ``dialog.printing`` offers WAIT and nothing else: no A/B while a printer runs (6.K.5);
* A is offered only where the game is provably blocked on it (printer waiting, native A/B wait);
* every yes/no carries the six 6.C fields; there is no generic YES: the effect text comes from the
  classified question, an unknown question is escalated (default) or offered as CONDITIONAL;
* a DESTRUCTIVE question never offers YES without an explicit authorization reason (hard invariant);
* one option per REAL entry (count from ``sMenu``), ids from the position, labels only when a label
  provider knows them: the entry text is not in the state, so an unlabelled menu is UNKNOWN by
  default and the frame says so.
"""

from __future__ import annotations

from pokegent.actions._exec.contracts import ActionContract, ExpectedState
from pokegent.actions.menu.dialog import evidence as E
from pokegent.actions.menu.dialog.prompts import (
    PromptInfo,
    PromptKind,
    PromptPolicy,
    classify_prompt,
    prompt_consequences,
)
from pokegent.actions.menu.dialog.text import DialogText, TextSidecar
from pokegent.games.gen3.firered.reader.menu_facts import menu_entry_facts
from pokegent.options.models import Legality, Option, Risk, fingerprint
from pokegent.options.registry import BuildContext, FrameDraft

MAX_ENTRIES = 16  # a menu with more entries than this is not read blindly

# refs (resolved in executor/verifier registries of this package)
REF_WAIT_TEXT = "dialog.wait_for_text"
REF_ADVANCE = "dialog.advance_text"
REF_CLOSE = "dialog.close_dialog"
REF_ACK_ITEM = "dialog.acknowledge_item"
REF_ACK_WHITEOUT = "dialog.acknowledge_whiteout"
REF_WAIT_RECOVERY = "dialog.wait_for_recovery"
REF_ANSWER = "dialog.answer_yes_no"
REF_SELECT_CHOICE = "dialog.select_choice"
REF_SELECT_PC = "dialog.select_pc_choice"
REF_SELECT_ROW = "dialog.select_row"
REF_CANCEL_LIST = "dialog.cancel_list"
REF_CANCEL_CHOICE = "dialog.cancel_choice"
REF_SCROLL_LIST = "dialog.scroll_list"
REF_PC_STORAGE_ERROR = "dialog.dismiss_pc_storage_error"

POST_TEXT_SETTLED = "dialog.text_settled"
POST_BOX_MOVED_ON = "dialog.box_moved_on"
POST_FALLBACK_PROGRESS = "dialog.fallback_progressed"
POST_PROMPT_CLOSED = "dialog.prompt_closed"
WATERFALL_POSTCONDITION = "field.waterfall_done"
POST_PC_DESTINATION = "dialog.pc_destination"
POST_WHITEOUT_DONE = "dialog.whiteout_done"
POST_LIST_CLOSED = "dialog.list_closed"
POST_LIST_MOVED = "dialog.list_moved"

TASK_YES_NO = "Task_YesNoMenu_HandleInput"
TASK_MULTICHOICE = "Task_MultichoiceMenu_HandleInput"


def _ctx_text(text: DialogText | None) -> dict[str, str]:
    return {"dialog_text": text.text} if text is not None else {}


class DialogBuilder:
    def __init__(self, sidecar: TextSidecar | None = None, policy: PromptPolicy | None = None):
        self._sidecar = sidecar
        self.policy = policy or PromptPolicy()

    # -- entry ---------------------------------------------------------------------------------

    def build(self, ctx: BuildContext) -> FrameDraft:
        snapshot, scene = ctx.snapshot, ctx.scene.scene_id
        ui = snapshot.ui
        text = self._sidecar.get(snapshot) if self._sidecar is not None else None
        if ui is None or not ui.valid:
            return self._missing(scene, "snapshot carries no UI evidence")
        handler = {
            E.PRINTING: self._printing,
            E.ADVANCE: self._press_a,
            E.END: self._press_a,
            E.ITEM_RECEIVE: self._press_a,
            E.YES_NO: self._yes_no,
            E.HEAL_OFFER: self._yes_no,
            E.MULTICHOICE: self._multichoice,
            E.GRID_CHOICE: self._grid,
            E.LIST: self._list,
            E.WHITEOUT: self._whiteout,
            E.QUEST_LOG: self._quest_log,
            E.UNKNOWN_DIALOG: self._unknown,
        }.get(scene)
        if handler is None:
            return self._missing(scene, f"the dialog family has no builder for {scene}")
        return handler(ctx, snapshot, ui, text)

    @staticmethod
    def _missing(scene: str, reason: str) -> FrameDraft:
        option = Option(
            id="pause_and_escalate",
            label="Pause and escalate",
            description=f"Nothing can be offered: {reason}.",
            legality=Legality.UNKNOWN,
            facts={"reason": reason},
        )
        return FrameDraft(
            options=(option,),
            state_fingerprint=fingerprint(scene, missing=reason),
            context={"summary": "The screen state could not be established."},
        )

    # -- dialog.printing ---------------------------------------------------------------------------

    def _printing(self, ctx, snapshot, ui, text) -> FrameDraft:
        wait = Option(
            id="wait_for_text",
            label="Wait for the text",
            description="The message is still being printed. Wait until it is complete; "
            "no button is pressed.",
            legality=Legality.LEGAL,
            executor_ref=REF_WAIT_TEXT,
            postcondition_ref=POST_TEXT_SETTLED,
            risk=Risk.LOW,
            verb="WAIT_FOR_TEXT",
            facts={"inputs": 0},
        )
        return FrameDraft(
            options=(wait,),
            state_fingerprint=fingerprint(
                E.PRINTING,
                box="field_message",
                page=text.page_index if text is not None else None,
                prompt=text.prompt_index if text is not None else None,
                text=text.digest if text is not None else None,
            ),
            context={"summary": "A text box is printing.", **_ctx_text(text)},
        )

    # -- dialog.advance / dialog.end / dialog.item_receive -----------------------------------------

    def _press_a(self, ctx, snapshot, ui, text) -> FrameDraft:
        scene = ctx.scene.scene_id
        fallback = bool(ctx.scene.evidence.get("fallback"))
        waiting = sorted(
            (p.window_id, p.state_name, p.current_char_ptr)
            for p in ui.text.printers
            if p.waiting_for_button
        )
        native = E.native_name(ui) if E.native_name(ui) == E.NATIVE_WAIT_A_B else None
        if not waiting and native is None and not fallback:
            return self._missing(scene, "no printer or script wait is blocked on A/B")
        info = classify_prompt(text)
        if info.kind is PromptKind.PC_STORAGE_ERROR and E.callback_name(ui) == "CB2_PokeStorage":
            option = Option(
                id="dismiss_pc_storage_error",
                label="Dismiss the PC storage error",
                description="Acknowledge the PC error and return to the current storage screen.",
                legality=Legality.LEGAL,
                executor_ref=REF_PC_STORAGE_ERROR,
                postcondition_ref=POST_BOX_MOVED_ON,
                risk=Risk.LOW,
                verb="CANCEL",
                facts={"pc_error": info.reason, "escape": True, "dialog_text": info.full_text},
                semantic_prompt=info.full_text or "PC storage error",
                effect_description="Dismisses the error without changing party or box contents.",
                irreversible=False,
            )
            return FrameDraft(
                options=(option,),
                state_fingerprint=fingerprint(scene, pc_error=info.digest),
                context={
                    "summary": "The PC refused the storage operation.",
                    "pc_error": info.full_text,
                },
                deadline_ms=10000,
            )
        if fallback:
            opt = ("advance_unknown_text", "Advance / close text", REF_ADVANCE, "ADVANCE_TEXT")
            desc = (
                "The overworld message box has no live owner signature; press A once to advance "
                "or close it, then verify RAM progress."
            )
            summary = "An otherwise-unowned overworld message box is waiting for generic advance."
            postcondition = POST_FALLBACK_PROGRESS
        elif scene == E.ADVANCE:
            opt = ("advance_text", "Continue the text", REF_ADVANCE, "ADVANCE_TEXT")
            desc = "Press A to show the next part of the message."
            summary = "A text box is waiting for A to continue."
            postcondition = POST_BOX_MOVED_ON
        elif scene == E.END:
            opt = ("close_dialog", "Close the text box", REF_CLOSE, "CLOSE_DIALOG")
            desc = "The message is complete; press A to close the text box."
            summary = "The text is finished and the box waits for A to close."
            postcondition = POST_BOX_MOVED_ON
        else:
            opt = ("acknowledge_item", "Acknowledge the item", REF_ACK_ITEM, "ACKNOWLEDGE_ITEM")
            desc = "An item message is shown; press A to acknowledge it."
            summary = "An item message waits for A."
            postcondition = POST_BOX_MOVED_ON
        option = Option(
            id=opt[0],
            label=opt[1],
            description=desc,
            legality=Legality.LEGAL,
            executor_ref=opt[2],
            postcondition_ref=postcondition,
            risk=Risk.LOW,
            verb=opt[3],
            facts={
                "blocked_on": "printer" if waiting else "script_wait",
                "inputs": 1,
                **({"fallback": True, "postcondition_failure_limit": 2} if fallback else {}),
            },
            action_contract=(
                ActionContract(
                    expected_outcome_scenes=(
                        "battle.entering",
                        "battle.animation",
                        "battle.message",
                        "battle.action",
                    )
                )
                if opt[0] == "close_dialog"
                else None
            ),
        )
        options = [option]
        info = classify_prompt(text) if scene == E.ITEM_RECEIVE else None
        if info is not None and info.bag_full:
            options.append(
                Option(
                    id="bag_full_replan",
                    label="Replan: the bag is full",
                    description="The message says the bag is full; this needs a plan change, "
                    "not a button.",
                    legality=Legality.UNSUPPORTED,
                    verb="BAG_FULL_REPLAN",
                    facts={"reason": "planner action, not an input"},
                )
            )
        fp = fingerprint(
            scene,
            waiting=[list(w) for w in waiting],
            native=native,
            text=text.digest if text is not None else None,
        )
        context = {"summary": summary, **_ctx_text(text)}
        if info is not None:
            context["bag_full"] = info.bag_full
        return FrameDraft(options=tuple(options), state_fingerprint=fp, context=context)

    # -- yes / no and the heal offer -------------------------------------------------------------

    def _yes_no(self, ctx, snapshot, ui, text) -> FrameDraft:
        scene = ctx.scene.scene_id
        mechanism = (
            "party_exit_yes_no"
            if E.party_exit_yes_no_open(ui)
            else "pc_storage_yes_no"
            if E.pc_storage_yes_no_open(ui)
            else "multichoice_yes_no"
            if E.yes_no_multichoice(ui, snapshot.rom_profile)
            else "script"
        )
        if mechanism == "script" and not (E.yes_no_open(ui) or E.yes_no_task_menu_open(ui)):
            return self._missing(scene, "no corroborated yes/no prompt")
        sm = ui.menu.menu
        assert sm is not None
        if mechanism == "pc_storage_yes_no":
            text = DialogText(("Continue BOX operations?",), source="pc_storage")
        if mechanism == "party_exit_yes_no":
            # The verified owning task proves the effect. gStringVar4 may be stale.
            text = DialogText(
                ("Use the selected field move to leave this area?",), source="party_exit_task"
            )
        info = classify_prompt(text)
        heal = scene == E.HEAL_OFFER
        if heal and info.kind is not PromptKind.HEAL:
            return self._missing(scene, "heal offer without the heal question text")
        source_task = (
            E.PARTY_EXIT_TASK
            if mechanism == "party_exit_yes_no"
            else TASK_MULTICHOICE
            if mechanism == "multichoice_yes_no"
            else "Task_OnBPressed"
            if mechanism == "pc_storage_yes_no"
            else TASK_YES_NO
        )

        question = info.question or (
            "A yes/no question is open; its text could not be read from the game."
        )
        yes_leg, no_leg, yes_effect, no_effect, yes_facts = self._yes_no_policy(
            info, ctx.goal_id, ctx.objective_kind
        )
        if mechanism == "party_exit_yes_no":
            yes_leg = no_leg = Legality.LEGAL
            yes_effect = "Uses the already selected Dig or Teleport field move to leave this area."
            no_effect = "Cancels leaving this area and returns to choosing a party member."
            yes_facts = {"prompt_kind": "party_exit", "effect_known": True}
        # Every yes/no adapter option participates in the common affirmative-first
        # ordering contract, including prompts that are not START-menu prompts.
        yes_facts = {**yes_facts, "affirmative": True}
        irreversible_yes = (
            info.kind is PromptKind.DESTRUCTIVE or info.irreversible
        ) and yes_leg.chooseable
        no_facts = {
            k: v
            for k, v in yes_facts.items()
            if k
            in (
                "prompt_kind",
                "question_kind",
                "effect_known",
                "field_move",
                "wrong_answer_cost",
                "door",
                "door_flag",
            )
        }
        # Keep the prompt's consequences on both answer options.  The negative answer is
        # also the structural escape from the confirmation, irrespective of the wording of
        # the question (PC confirmations use the same generic adapter).
        yes_facts["yes_effect"] = yes_effect
        no_facts["no_effect"] = no_effect
        normalized_question = (question or "").lower()
        consequential = (
            info.kind is PromptKind.DESTRUCTIVE
            or info.irreversible
            or "starter" in normalized_question
            or "nickname" in normalized_question
            or "name" in normalized_question
        )
        if not consequential:
            no_facts["escape"] = True
        if "continue box" in normalized_question:
            yes_facts["return_phase"] = "storage"
            no_facts["return_phase"] = "pc_menu"
        if isinstance(yes_facts.get("serves_goal"), bool):
            # Declining a field move never advances the objective.  Do not derive this by
            # complementing YES: a non-route YES is false, but NO is not thereby serving.
            no_facts["serves_goal"] = False
            no_facts["default_answer"] = "no"
        no_facts["affirmative"] = False

        ids = ("heal_team", "decline_heal") if heal else ("yes", "no")
        verbs = ("HEAL_TEAM", "DECLINE_HEAL") if heal else ("YES", "NO")
        labels = ("Heal the team", "Decline") if heal else ("YES", "NO")
        base_post = "The prompt closes and the script continues with the "
        options = (
            Option(
                id=ids[0],
                label=labels[0],
                description=yes_effect,
                legality=yes_leg,
                executor_ref=REF_ANSWER if yes_leg.chooseable else None,
                executor_args={"entry": 0} if yes_leg.chooseable else {},
                postcondition_ref=(
                    WATERFALL_POSTCONDITION
                    if yes_leg.chooseable and yes_facts.get("waterfall_destination") is not None
                    else POST_PROMPT_CLOSED
                    if yes_leg.chooseable
                    else None
                ),
                risk=Risk.HIGH if irreversible_yes else Risk.LOW if heal else Risk.NORMAL,
                verb=verbs[0],
                facts=yes_facts,
                semantic_prompt=question,
                effect_description=yes_effect,
                irreversible=irreversible_yes,
                source_task=source_task,
                expected_postcondition=base_post + "YES answer.",
            ),
            Option(
                id=ids[1],
                label=labels[1],
                description=no_effect,
                legality=no_leg,
                executor_ref=REF_ANSWER if no_leg.chooseable else None,
                executor_args={"entry": 1} if no_leg.chooseable else {},
                postcondition_ref=POST_PROMPT_CLOSED if no_leg.chooseable else None,
                risk=Risk.LOW,
                verb=verbs[1],
                facts=no_facts,
                semantic_prompt=question,
                effect_description=no_effect,
                irreversible=False,
                source_task=source_task,
                expected_postcondition=base_post + "NO answer.",
            ),
        )
        fp = fingerprint(
            scene,
            mechanism=mechanism,
            window=sm.window_id,
            kind=info.kind.value,
            prompt=info.digest or "unreadable",
        )
        context: dict[str, object] = {
            "summary": "A yes/no question is open."
            if not heal
            else "A Pokemon Center nurse offers to heal the team.",
            "question": question,
            "prompt_kind": info.kind.value,
            "dialog_text": info.full_text or question,
        }
        return FrameDraft(options=options, state_fingerprint=fp, context=context, deadline_ms=10000)

    def _yes_no_policy(
        self, info: PromptInfo, goal_id: str | None, objective_kind: str | None = None
    ) -> tuple[Legality, Legality, str, str, dict[str, object]]:
        """(yes legality, no legality, yes effect, no effect, yes facts) for a classified prompt."""
        kind = info.kind
        facts: dict[str, object] = {
            "prompt_kind": kind.value,
            "question_kind": info.rule_kind or kind.value,
            **dict(info.metadata),
        }
        if kind is PromptKind.HEAL:
            # Healing is the action that satisfies a live heal objective's
            # party_healthy probe.  Keep the same facts contract as navigation
            # options so every decider prefers the nurse over exits or decline.
            if goal_id is not None and objective_kind == "heal":
                facts.update(
                    {
                        "serves_goal": True,
                        "success_probe": "party_healthy",
                    }
                )
            return (
                Legality.LEGAL,
                Legality.LEGAL,
                "Heals the whole party to full health (HP, PP, status).",
                "Declines the offer; the party stays as it is.",
                facts,
            )
        if kind in (PromptKind.FIELD_MOVE, PromptKind.POKE_FLUTE, PromptKind.SECRET_SWITCH):
            move = (info.field_move or "the field move").upper()
            serves_goal = self.policy.serves_goal_for(info, goal_id)
            if serves_goal is not None:
                facts.update({"serves_goal": serves_goal, "default_answer": "yes"})
            if self.policy.field_move_is_next_route_step(info, goal_id):
                facts["next_route_step"] = True
            if kind is PromptKind.POKE_FLUTE:
                yes_effect = "Uses the POKE FLUTE here and clears the scripted blocker."
                no_effect = "Does not use the POKE FLUTE; the scripted blocker remains."
            elif kind is PromptKind.SECRET_SWITCH:
                yes_effect = "Presses the secret switch and changes the Mansion gates."
                no_effect = "Does not press the secret switch; the Mansion gates stay unchanged."
            else:
                yes_effect = f"Uses {move} here (a field move with a lasting effect on the map)."
                no_effect = f"Does not use {move}."
            if (
                move == "WATERFALL"
                and self.policy.field_move_trigger.expected_destination is not None
            ):
                facts["waterfall_destination"] = list(
                    self.policy.field_move_trigger.expected_destination
                )
            if facts.get("next_route_step") is True:
                yes_effect += " This is the next step of the route; the default answer is YES."
                no_effect += " The next step of the route remains blocked."
            no_leg = (
                Legality.UNAVAILABLE
                if facts.get("next_route_step") is True and serves_goal is True
                else Legality.LEGAL
            )
            return (
                Legality.LEGAL,
                no_leg,
                yes_effect,
                no_effect,
                {**facts, "field_move": info.field_move or ""},
            )
        if kind is PromptKind.FOSSIL_CHOICE:
            facts.update({"irreversible": True, "other_fossil_gone": True})
            return (
                Legality.LEGAL,
                Legality.LEGAL,
                "Chooses this fossil; the other fossil is gone after this choice.",
                "Declines this fossil choice; no fossil is taken here.",
                facts,
            )
        if kind is PromptKind.QUIZ:
            serves_goal = self.policy.serves_goal_for(info, goal_id)
            if serves_goal is not None:
                facts.update(
                    {
                        "serves_goal": serves_goal,
                        "default_answer": "yes" if serves_goal else "no",
                    }
                )
            if info.wrong_answer_cost:
                facts["wrong_answer_cost"] = info.wrong_answer_cost
            correct = "correct" if serves_goal else "wrong"
            return (
                Legality.LEGAL,
                Legality.LEGAL,
                f"Answers YES ({correct}); a wrong answer costs a trainer battle.",
                "Answers NO ({}); a wrong answer costs a trainer battle.".format(
                    "wrong" if serves_goal else "correct"
                ),
                facts,
            )
        if kind is PromptKind.DESTRUCTIVE:
            reason = self.policy.destructive_reason(info, goal_id)
            no_effect = "Declines; nothing is lost."
            if reason:
                return (
                    Legality.LEGAL,
                    Legality.LEGAL,
                    "Confirms an irreversible action (release, toss, overwrite or similar); "
                    "it cannot be undone.",
                    no_effect,
                    {**facts, "authorized_by": reason},
                )
            return (
                Legality.UNAVAILABLE,
                Legality.LEGAL,
                "Would confirm an irreversible action; blocked without an explicit reason.",
                no_effect,
                {**facts, "blocked": "destructive prompt without explicit authorization"},
            )
        consequences = prompt_consequences(
            info,
            tables=self.policy.tables,
            item_offer=info.kind is PromptKind.UNKNOWN and self.policy.unknown_prompts == "offer",
        )
        if consequences is not None:
            yes_effect, no_effect = consequences
            return (
                Legality.LEGAL,
                Legality.LEGAL,
                yes_effect,
                no_effect,
                {**facts, "effect_known": True},
            )
        # Every yes/no box is an answerable boolean connector.  The game text is the
        # evidence for the question; the registry only enriches known effects and
        # reversibility.  In particular, an unknown prompt must never create a no-choice
        # frame and pause the run.
        return (
            Legality.LEGAL,
            Legality.LEGAL,
            "Answers YES; the specific consequence is not readable from the game text.",
            "Answers NO; the specific consequence is not readable from the game text.",
            {
                **facts,
                "effect_known": False,
                "prompt_text": info.question or "question text unavailable",
            },
        )

    # -- multichoice -----------------------------------------------------------------------------

    def _menu_legality(self, labelled: bool) -> Legality:
        if labelled:
            return Legality.LEGAL
        return Legality.CONDITIONAL if self.policy.unknown_prompts == "offer" else Legality.UNKNOWN

    def _multichoice(self, ctx, snapshot, ui, text) -> FrameDraft:
        if not E.multichoice_open(ui):
            return self._missing(ctx.scene.scene_id, "no corroborated multichoice menu")
        mc, sm = ui.menu.multichoice, ui.menu.menu
        assert mc is not None and sm is not None
        count = sm.max_cursor_pos + 1
        if count > MAX_ENTRIES:
            return self._missing(ctx.scene.scene_id, f"{count} entries are too many to read")
        labels = self.policy.labels(mc.multichoice_id, count) if self.policy.labels else None
        resolved = (
            self.policy.menu_entries(snapshot, mc.multichoice_id, count)
            if self.policy.menu_entries is not None
            else None
        )
        pc_entries = (
            tuple(resolved)
            if resolved is not None and all(key.startswith("pc:") for key, _ in resolved)
            else None
        )
        resolved_entries = tuple(resolved) if resolved is not None else None
        entries = resolved_entries or tuple((f"choice:{i}", f"Entry {i + 1}") for i in range(count))
        labelled = labels is not None and len(labels) == count
        if resolved_entries is not None and not labelled:
            entries = resolved_entries
            labelled = True
        info = classify_prompt(text)
        question = info.question or "A menu is open; the question could not be read."
        legality = self._menu_legality(labelled)
        options = []
        for i in range(count):
            key, pc_label = entries[i]
            label = labels[i] if labels is not None and len(labels) == count else pc_label
            is_pc = pc_entries is not None
            menu_facts = menu_entry_facts(key)
            ferry_route_step = key.startswith("route:ferry:")
            if ferry_route_step:
                menu_facts.update(
                    {
                        "route_step": True,
                        "next_route_step": True,
                        "on_route": True,
                        "serves_goal": True,
                        "route_relation": "next route step",
                    }
                )
            options.append(
                Option(
                    id=key,
                    label=label,
                    description=(
                        f"Menu entry {i + 1} of {count}"
                        + ("" if labelled else "; its text is not readable from the game state")
                        + "."
                    ),
                    legality=legality,
                    executor_ref=(REF_SELECT_PC if is_pc else REF_SELECT_CHOICE)
                    if legality.chooseable
                    else None,
                    executor_args={"index": i} if legality.chooseable else {},
                    postcondition_ref=(POST_PC_DESTINATION if is_pc else POST_PROMPT_CLOSED)
                    if legality.chooseable
                    else None,
                    risk=Risk.NORMAL,
                    verb="SELECT_CHOICE",
                    facts={
                        "index": i,
                        "entries": count,
                        "label_known": labelled,
                        "stable_key": key,
                        "menu_choice": True,
                        "menu_cost": "one menu action",
                        "menu_prerequisites": "enabled",
                        "menu_novelty": "not previously selected",
                        "menu_outcome": f"selects '{label}'",
                        "menu_risk": "none",
                        "menu_reversible": True,
                        **menu_facts,
                    },
                    semantic_prompt=question,
                    effect_description=(
                        f"Selects '{label}'." if labelled else f"Selects menu entry {i + 1}."
                    ),
                    source_task=TASK_MULTICHOICE,
                    expected_postcondition=(
                        "The selected PC destination scene is reached."
                        if is_pc
                        else "The menu closes and the script continues."
                    ),
                    action_contract=ActionContract(
                        cursor_key=key,
                        expected=(ExpectedState(kind="menu_closed", surface=E.MULTICHOICE),),
                    ),
                )
            )
        if pc_entries is None and mc.multichoice_id == 255:
            # 255 is also the game's generic/no-table menu id.  Keep its entries UNKNOWN, but
            # always expose the safe B escape so an unreadable menu cannot trap the runner.
            options.append(
                Option(
                    id="cancel_unknown_menu",
                    label="Cancel unknown menu (back out)",
                    description=(
                        "Press B to leave this unrecognised menu; its entries remain escalated "
                        "as unknown."
                    ),
                    legality=Legality.LEGAL,
                    executor_ref=REF_CANCEL_CHOICE,
                    postcondition_ref=POST_PROMPT_CLOSED,
                    risk=Risk.LOW,
                    verb="SELECT_CHOICE",
                    facts={
                        "reason": "safe B exit",
                        "menu_id": 255,
                        "escape": True,
                        "unknown_menu": True,
                        "menu_choice": True,
                        "menu_cost": "one menu action",
                        "menu_prerequisites": "enabled",
                        "menu_novelty": "not previously selected",
                        "menu_objective": "leave the current menu",
                        "menu_outcome": "leaves the unknown menu",
                        "menu_risk": "none",
                        "menu_reversible": True,
                    },
                    source_task=TASK_MULTICHOICE,
                    expected_postcondition="The unknown menu closes after a B press.",
                )
            )
        fp = fingerprint(
            ctx.scene.scene_id,
            menu=mc.multichoice_id,
            entries=count,
            window=mc.window_id,
            prompt=info.digest or "unreadable",
        )
        context = {
            "summary": f"A menu with {count} entries is open.",
            "question": question,
            "entries_labelled": labelled,
            "menu_variant": mc.multichoice_id,
        }
        if pc_entries is not None:
            context["repetition_scope"] = "pc_choice"
        return FrameDraft(
            options=tuple(options), state_fingerprint=fp, context=context, deadline_ms=10000
        )

    def _grid(self, ctx, snapshot, ui, text) -> FrameDraft:
        sm = ui.menu.menu
        cols = sm.columns if sm else 0
        option = Option(
            id="select_grid_choice",
            label="Select a grid entry",
            description="A multi-column menu is open; grid navigation is not implemented.",
            legality=Legality.UNSUPPORTED,
            verb="SELECT_CHOICE",
            facts={"columns": cols, "reason": "grid_choice navigation is not implemented"},
        )
        return FrameDraft(
            options=(option,),
            state_fingerprint=fingerprint(ctx.scene.scene_id, columns=cols),
            context={"summary": "A multi-column menu is open (unsupported)."},
        )

    # -- list ------------------------------------------------------------------------------------

    def _list(self, ctx, snapshot, ui, text) -> FrameDraft:
        lm = ui.menu.list_menu
        if lm is None or not E.list_open(ui):
            return self._missing(ctx.scene.scene_id, "no corroborated list menu")
        absolute = lm.scroll_offset + lm.cursor_row
        shown = max(0, min(lm.max_showed, lm.total_items - lm.scroll_offset))
        labels = self.policy.labels(-1, lm.total_items) if self.policy.labels else None
        labelled = labels is not None and len(labels) == lm.total_items
        legality = self._menu_legality(labelled)
        question = (classify_prompt(text).question) or "A list is open."
        options: list[Option] = []
        for row in range(lm.scroll_offset, lm.scroll_offset + shown):
            label = labels[row] if labelled else f"Row {row + 1}"
            options.append(
                Option(
                    id=f"row:{row}",
                    label=label,
                    description=f"List row {row + 1} of {lm.total_items}"
                    + ("" if labelled else "; its text is not readable from the game state")
                    + ".",
                    legality=legality,
                    executor_ref=REF_SELECT_ROW if legality.chooseable else None,
                    executor_args={"row": row} if legality.chooseable else {},
                    postcondition_ref=POST_LIST_CLOSED if legality.chooseable else None,
                    verb="SELECT_ROW",
                    facts={"row": row, "total": lm.total_items, "label_known": labelled},
                    semantic_prompt=question,
                    effect_description=f"Selects '{label}'.",
                    source_task="ListMenuDummyTask",
                    expected_postcondition="The list closes and the script continues.",
                )
            )
        scroll_ok = labelled
        for direction, verb, possible in (
            ("up", "SCROLL_UP", absolute > 0),
            ("down", "SCROLL_DOWN", absolute < lm.total_items - 1),
        ):
            legal = Legality.LEGAL if (scroll_ok and possible) else Legality.UNAVAILABLE
            options.append(
                Option(
                    id=f"scroll_{direction}",
                    label=f"Scroll {direction}",
                    description=f"Move the list cursor one row {direction}.",
                    legality=legal,
                    executor_ref=REF_SCROLL_LIST if legal.chooseable else None,
                    executor_args={"delta": 1 if direction == "down" else -1}
                    if legal.chooseable
                    else {},
                    postcondition_ref=POST_LIST_MOVED if legal.chooseable else None,
                    risk=Risk.LOW,
                    verb=verb,
                    facts={"possible": possible, "rows_readable": labelled},
                )
            )
        options.append(
            Option(
                id="cancel",
                label="Close the list",
                description="Press B to leave the list without choosing a row.",
                legality=Legality.LEGAL,
                executor_ref=REF_CANCEL_LIST,
                postcondition_ref=POST_LIST_CLOSED,
                risk=Risk.LOW,
                verb="CANCEL",
                facts={"escape": True},
            )
        )
        fp = fingerprint(
            ctx.scene.scene_id,
            total=lm.total_items,
            shown=lm.max_showed,
            window=lm.window_id,
            prompt=classify_prompt(text).digest or "unreadable",
        )
        return FrameDraft(
            options=tuple(options),
            state_fingerprint=fp,
            context={
                "summary": f"A list with {lm.total_items} rows is open.",
                "entries_labelled": labelled,
            },
            deadline_ms=10000,
        )

    # -- system.whiteout --------------------------------------------------------------------------

    def _whiteout(self, ctx, snapshot, ui, text) -> FrameDraft:
        waiting = sorted(
            (p.window_id, p.state_name, p.current_char_ptr)
            for p in ui.text.printers
            if p.waiting_for_button
        )
        if waiting:
            option = Option(
                id="acknowledge_whiteout",
                label="Acknowledge the whiteout",
                description="All POKeMON fainted; the game moved the player to a POKeMON Center. "
                "Press A to continue the message.",
                legality=Legality.LEGAL,
                executor_ref=REF_ACK_WHITEOUT,
                postcondition_ref=POST_BOX_MOVED_ON,
                risk=Risk.LOW,
                verb="ACKNOWLEDGE",
                facts={"inputs": 1},
            )
            fp = fingerprint(E.WHITEOUT, phase="acknowledge", waiting=[list(w) for w in waiting])
            summary = "The party whited out; a message waits for A."
        else:
            option = Option(
                id="wait_for_recovery",
                label="Wait for the recovery",
                description="The whiteout sequence is running; wait until the game returns "
                "control. No button is pressed.",
                legality=Legality.LEGAL,
                executor_ref=REF_WAIT_RECOVERY,
                postcondition_ref=POST_WHITEOUT_DONE,
                risk=Risk.LOW,
                verb="WAIT_FOR_RECOVERY",
                facts={"inputs": 0},
            )
            fp = fingerprint(E.WHITEOUT, phase="wait")
            summary = "The whiteout sequence is running."
        return FrameDraft(
            options=(option,),
            state_fingerprint=fp,
            context={"summary": summary, **_ctx_text(text)},
        )

    # -- system.quest_log_playback: recognised, deliberately unsupported -------------------------

    def _quest_log(self, ctx, snapshot, ui, text) -> FrameDraft:
        option = Option(
            id="skip_or_exit",
            label="Skip or exit the quest log",
            description="The quest log replay is running; the exit input is not verified.",
            legality=Legality.UNSUPPORTED,
            verb="SKIP_OR_EXIT",
            facts={
                "reason": "gQuestLogState playback was never observed on this ROM, so the "
                "exit input is not known (spec 6.A: only with a known allowed input)"
            },
        )
        return FrameDraft(
            options=(option,),
            state_fingerprint=fingerprint(E.QUEST_LOG, state=ui.menu.quest_log_state),
            context={"summary": "The quest log replay is running (unsupported)."},
        )

    # -- dialog.unknown --------------------------------------------------------------------------

    def _unknown(self, ctx, snapshot, ui, text) -> FrameDraft:
        printers = [(p.window_id, p.state_name) for p in ui.text.printers]
        option = Option(
            id="pause_and_escalate",
            label="Pause and escalate",
            description="A dialog-like screen is up that no known scene explains; pressing a "
            "button without knowing the prompt source is not allowed.",
            legality=Legality.UNKNOWN,
            verb="PAUSE_AND_ESCALATE",
            facts={"printers": [list(p) for p in printers]},
        )
        return FrameDraft(
            options=(option,),
            state_fingerprint=fingerprint(
                E.UNKNOWN_DIALOG,
                printers=[list(p) for p in printers],
                script=ui.script.status_name,
            ),
            context={"summary": "An unrecognised dialog is on the screen.", **_ctx_text(text)},
        )
