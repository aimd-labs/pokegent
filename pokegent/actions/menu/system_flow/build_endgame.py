"""Options for the Hall of Fame, credits, boot/intro, title screen and main menu."""

from __future__ import annotations

from pokegent.actions.menu.system_flow import evidence_endgame as EG
from pokegent.actions.menu.system_flow import refs as R
from pokegent.actions.menu.system_flow.evidence import Verdict, game_clear
from pokegent.actions.menu.system_flow.policy import SystemFlowOptions
from pokegent.options.models import Legality, Option, Risk, fingerprint
from pokegent.options.registry import BuildContext, FrameDraft


def _wait_option(
    scene: str, label: str, description: str, chunk: int, *, verb: str | None, facts: dict
) -> Option:
    return Option(
        id=R.OPT_WAIT,
        label=label,
        description=description,
        legality=Legality.LEGAL,
        executor_ref=R.REF_WAIT,
        executor_args={"chunk": chunk},
        risk=Risk.LOW,
        verb=verb,
        facts={"inputs": 0, **facts},
    )


def hall_of_fame(
    ctx: BuildContext, snapshot, verdict: Verdict, options: SystemFlowOptions
) -> FrameDraft:
    phase = str(verdict.evidence.get("phase"))
    clear = game_clear(snapshot)
    if phase == "exit_ready":
        ack = Option(
            id=R.OPT_ACK_CLEAR,
            label="Acknowledge the Hall of Fame",
            description=(
                "The Hall of Fame screen waits for A, the only button it reads. Press A once: the "
                "screen fades out and the game walks on to the credits. FLAG_SYS_GAME_CLEAR is "
                "already set; nothing is reset."
            ),
            legality=Legality.LEGAL,
            executor_ref=R.REF_ACK_CLEAR,
            postcondition_ref=R.POST_MOVED_ON,
            risk=Risk.LOW,
            verb="ACKNOWLEDGE_CLEAR",
            facts={"phase": phase, "inputs": 1, "game_clear": clear},
        )
        opts = (ack,)
        summary = "The Hall of Fame screen is waiting for A."
    else:
        wait = _wait_option(
            R.HALL_OF_FAME,
            "Wait for the Hall of Fame",
            "The Hall of Fame screen is still showing the team; it reads no button yet. Wait until "
            "it asks for A. No button is pressed.",
            options.wait_chunk_frames,
            verb=None,
            facts={"phase": phase, "game_clear": clear},
        )
        opts = (wait,)
        summary = "The Hall of Fame screen is running (no button is read yet)."
    return FrameDraft(
        options=opts,
        state_fingerprint=fingerprint(R.HALL_OF_FAME, phase=phase),
        context={"summary": summary},
        deadline_ms=10000,
    )


def credits(
    ctx: BuildContext, snapshot, verdict: Verdict, options: SystemFlowOptions
) -> FrameDraft:
    wait = _wait_option(
        R.CREDITS,
        "Wait for the credits",
        "The credits are rolling (about 15,000 frames). They read A only at THE END and end by "
        "themselves; wait without pressing anything.",
        options.credits_chunk_frames,
        verb="WAIT",
        facts={
            "phase": "rolling",
            "end_prompt_observable": False,
            "game_clear": game_clear(snapshot),
        },
    )
    advance = Option(
        id=R.OPT_ADVANCE_CREDITS,
        label="Press A at THE END",
        description="The THE END prompt cannot be observed on this ROM profile.",
        legality=Legality.UNSUPPORTED,
        verb="ADVANCE_IF_PROMPTED",
        facts={
            "reason": "the end prompt lives in a heap struct no profile symbol reads; the screen "
            "times out by itself after 600 frames"
        },
    )
    return FrameDraft(
        options=(wait, advance),
        state_fingerprint=fingerprint(R.CREDITS, phase="rolling"),
        context={"summary": "The credits are rolling."},
        deadline_ms=10000,
    )


def logo(ctx: BuildContext, snapshot, verdict: Verdict, options: SystemFlowOptions) -> FrameDraft:
    skip = Option(
        id=R.OPT_SKIP_INTRO,
        label="Skip the intro movie",
        description="The intro movie plays after the reset; START skips it and goes to the title "
        "screen. Nothing is chosen or lost.",
        legality=Legality.LEGAL,
        executor_ref=R.REF_PRESS_START,
        postcondition_ref=R.POST_MOVED_ON,
        risk=Risk.LOW,
        verb="ADVANCE_WHEN_READY",
        facts={"phase": "intro", "inputs": 1},
    )
    return FrameDraft(
        options=(skip,),
        state_fingerprint=fingerprint(R.LOGO, phase="intro"),
        context={"summary": "The intro movie is playing."},
    )


def title(ctx: BuildContext, snapshot, verdict: Verdict, options: SystemFlowOptions) -> FrameDraft:
    press = Option(
        id=R.OPT_PRESS_START,
        label="Press START at the title screen",
        description="The title screen waits for START (or A). Press START once; the main menu "
        "follows. This does not start a new game.",
        legality=Legality.LEGAL,
        executor_ref=R.REF_PRESS_START,
        postcondition_ref=R.POST_MOVED_ON,
        risk=Risk.LOW,
        verb="PRESS_START",
        facts={
            "phase": "run",
            "inputs": 1,
            "new_game_forbidden": True,
            "new_game_mode": options.new_game.enabled,
        },
    )
    return FrameDraft(
        options=(press,),
        state_fingerprint=fingerprint(R.TITLE_PRESS_START, phase="run"),
        context={"summary": "The title screen is waiting for START."},
    )


def main_menu(
    ctx: BuildContext, snapshot, verdict: Verdict, options: SystemFlowOptions
) -> FrameDraft:
    scene = verdict.scene_id
    view = EG.main_menu_view(snapshot.ui)
    if view is None:
        return FrameDraft(
            options=(
                Option(
                    id="pause_and_escalate",
                    label="Pause and escalate",
                    description="The main menu is up but its type and cursor cannot be read.",
                    legality=Legality.UNKNOWN,
                    facts={"reason": "main menu task data unreadable"},
                ),
            ),
            state_fingerprint=fingerprint(scene, missing="menu_view"),
            context={"summary": "A main menu is open (unreadable)."},
        )
    entries = list(view["entries"])  # type: ignore[arg-type]
    cursor = int(view["cursor"])  # type: ignore[arg-type]
    has_save = bool(view["has_save"])
    new_game_enabled = options.new_game.enabled
    forbidden = Option(
        id=R.OPT_NEW_GAME,
        label="Start a NEW GAME",
        description="Start a fresh game. This replaces the currently selected save.",
        legality=Legality.LEGAL if new_game_enabled else Legality.UNAVAILABLE,
        executor_ref=R.REF_NEW_GAME if new_game_enabled else None,
        postcondition_ref=R.POST_MOVED_ON if new_game_enabled else None,
        verb="NEW_GAME" if scene == R.TITLE_MAIN else None,  # title.continue has no such verb
        facts={
            "reason": "explicit --new-game run"
            if new_game_enabled
            else "never chosen: it would erase the run",
            "new_game_forbidden": not new_game_enabled,
            "new_game_mode": new_game_enabled,
        },
    )
    if has_save:
        cont = Option(
            id=R.OPT_CONTINUE,
            label="CONTINUE the saved game",
            description="Load the saved game: the cursor goes to CONTINUE (the first entry) and A is "  # noqa: E501
            "pressed once. The game resumes where it was saved.",
            legality=Legality.LEGAL,
            executor_ref=R.REF_CONTINUE,
            postcondition_ref=R.POST_MOVED_ON,
            risk=Risk.LOW,
            verb="CONTINUE_GAME" if scene == R.TITLE_CONTINUE else "CONTINUE",
            facts={"inputs": 1 + cursor, "new_game_forbidden": True},
        )
        opts: tuple[Option, ...] = (forbidden, cont) if new_game_enabled else (cont, forbidden)
        summary = (
            "The main menu offers CONTINUE and NEW GAME; explicit new-game mode selects NEW GAME."
        )
    elif new_game_enabled:
        opts = (forbidden,)
        summary = "The new-game run is ready to start a new game."
    else:
        opts = (forbidden,)
        summary = "The main menu has no save to continue; only NEW GAME is listed (never chosen)."
    return FrameDraft(
        options=opts,
        state_fingerprint=fingerprint(
            scene, menu=view["menu_type_name"], cursor=cursor, entries=entries
        ),
        context={"summary": summary, "menu_entries": entries, "cursor": cursor},
        deadline_ms=10000,
    )


def naming(ctx, snapshot, verdict, options: SystemFlowOptions) -> FrameDraft:
    phase = str(verdict.evidence.get("phase"))
    if phase == "confirm":
        opt = Option(
            id="confirm_preset_name",
            label="Confirm the preset name",
            description="Confirm the game-provided preset name with YES.",
            legality=Legality.LEGAL,
            executor_ref=R.REF_CONFIRM_NAME,
            executor_args={"entry": 0},
            postcondition_ref=R.POST_MOVED_ON,
            risk=Risk.LOW,
            verb="CONFIRM_NAME",
            facts={
                "preset": True,
                "new_game_mode": options.new_game.enabled,
                "default_answer": "yes",
            },
        )
        return FrameDraft(
            options=(opt,),
            state_fingerprint=fingerprint(R.NAMING, phase=phase),
            context={"summary": "The preset name is waiting for confirmation."},
        )
    opt = Option(
        id=R.OPT_SELECT_PRESET,
        label="Accept the game preset name",
        description="Accept the game-provided preset already in the name field; no custom keyboard entry is used.",  # noqa: E501
        legality=Legality.LEGAL,
        executor_ref=R.REF_ACCEPT_PRESET,
        postcondition_ref=R.POST_NAME_ACCEPTED,
        risk=Risk.LOW,
        verb="CONFIRM_NAME",
        facts={"preset": True, "new_game_mode": options.new_game.enabled, "default_answer": "yes"},
    )
    return FrameDraft(
        options=(opt,),
        state_fingerprint=fingerprint(R.NAMING, phase=phase),
        context={"summary": "The naming screen contains a game-provided preset."},
    )


def professor_text(ctx, snapshot, verdict, options: SystemFlowOptions) -> FrameDraft:
    phase = str(verdict.evidence.get("phase"))
    opt = Option(
        id=R.OPT_ADVANCE_TEXT,
        label="Advance the introduction",
        description="The detected introduction input point is waiting for A.",
        legality=Legality.LEGAL,
        executor_ref=R.REF_ADVANCE_INTRO,
        postcondition_ref=R.POST_INPUT_PROCESSED,
        risk=Risk.LOW,
        verb="ADVANCE_TEXT",
        facts={"phase": phase, "inputs": 1},
    )
    return FrameDraft(
        options=(opt,),
        state_fingerprint=fingerprint(
            R.PROFESSOR_TEXT,
            phase=phase,
            state=verdict.evidence.get("state"),
            page=verdict.evidence.get("page"),
            text_ptrs=verdict.evidence.get("text_ptrs"),
        ),
        context={"summary": "The introduction is waiting for A."},
    )


def gender(ctx, snapshot, verdict, options: SystemFlowOptions) -> FrameDraft:
    entry = 0 if options.new_game.gender == "male" else 1
    verb = "CHOOSE_BOY" if entry == 0 else "CHOOSE_GIRL"
    opt = Option(
        id="choose_boy" if entry == 0 else "choose_girl",
        label="Choose BOY" if entry == 0 else "Choose GIRL",
        description="Choose the configured player gender from the detected gender menu.",
        legality=Legality.LEGAL,
        executor_ref=R.REF_CHOOSE_GENDER,
        executor_args={"entry": entry},
        postcondition_ref=R.POST_MOVED_ON,
        risk=Risk.LOW,
        verb=verb,
        facts={"entry": entry, "configured_gender": options.new_game.gender},
    )
    return FrameDraft(
        options=(opt,),
        state_fingerprint=fingerprint(R.GENDER, phase="gender"),
        context={"summary": "The detected gender menu is ready."},
    )


def rival_name(ctx, snapshot, verdict, options: SystemFlowOptions) -> FrameDraft:
    opt = Option(
        id=R.OPT_SELECT_PRESET,
        label="Choose the first rival preset",
        description="Move from NEW NAME to the game's first rival preset and confirm it.",
        legality=Legality.LEGAL if options.new_game.enabled else Legality.UNAVAILABLE,
        executor_ref=R.REF_SELECT_PRESET if options.new_game.enabled else None,
        executor_args={"index": 0},
        postcondition_ref=R.POST_MOVED_ON if options.new_game.enabled else None,
        risk=Risk.LOW,
        verb="SET_RIVAL_NAME",
        facts={"preset_index": 0, "new_game_mode": options.new_game.enabled},
    )
    return FrameDraft(
        options=(opt,),
        state_fingerprint=fingerprint(R.RIVAL_NAME, phase="preset_choices"),
        context={"summary": "The rival preset list is ready."},
    )


def script_movement(ctx, snapshot, verdict, options: SystemFlowOptions) -> FrameDraft:
    return FrameDraft(
        options=(),
        state_fingerprint=fingerprint(R.SCRIPT_MOVEMENT, phase=verdict.evidence.get("phase")),
        context={"summary": "The introduction is transitioning without player input."},
    )


#: gQuestLogState while a recap plays: 2 playback, 3 last playback (src/quest_log.c)
QUEST_LOG_PLAYING = (2, 3)


def quest_log(
    ctx: BuildContext, snapshot, verdict: Verdict | None, options: SystemFlowOptions
) -> FrameDraft:
    """The recap that follows CONTINUE (``TryStartQuestLogPlayback``, only if the save
    recorded one).

    The scene is DETECTED by the dialog family from ``gQuestLogState``; this family only builds its
    options. Two ways on, no strategy in either: wait (the recap ends by itself) and ONE B,
    which the
    decomp reads as "skip the recap" (``src/quest_log.c``, HandleQuestLogInput; written from the
    decompile and the b35c notes, never seen on this ROM). The skip is therefore CONDITIONAL,
    one tap per
    decision, and its postcondition (``gQuestLogState`` back to 0 or 1) tells whether the game
    took it.
    """
    state = snapshot.ui.menu.quest_log_state if snapshot.ui is not None else None
    skip = Option(
        id=R.OPT_SKIP_QUEST_LOG,
        label="Skip the quest log recap",
        description=(
            "The game replays what happened while the save was recorded. B skips it; the input is "
            "written from the decompile and has not been seen on this ROM, so the outcome is checked "  # noqa: E501
            "(the recap state must end). One B, never repeated within an action. Nothing is chosen "
            "or lost."
        ),
        legality=Legality.CONDITIONAL,
        executor_ref=R.REF_SKIP_QUEST_LOG,
        postcondition_ref=R.POST_QUEST_LOG_LEFT,
        risk=Risk.LOW,
        verb="SKIP_OR_EXIT",
        facts={
            "inputs": 1,
            "input_verified": False,
            "reason": "B ends the quest log playback in the decompile; not yet observed on this ROM",  # noqa: E501
            "quest_log_state": state,
        },
    )
    wait = _wait_option(
        R.QUEST_LOG,
        "Wait for the quest log recap",
        "The recap plays and ends by itself; wait without pressing anything.",
        options.wait_chunk_frames,
        verb="WAIT",
        facts={"quest_log_state": state},
    )
    return FrameDraft(
        options=(skip, wait),
        state_fingerprint=fingerprint(R.QUEST_LOG, state=state),
        context={"summary": "The quest log recap is playing."},
        deadline_ms=10000,
    )
