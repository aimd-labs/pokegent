"""One decision as the viewer shows it: question, options with probabilities, the pick, the fact.

A record is a plain JSON-able dict built from a :class:`~pokegent.options.models.DecisionFrame`
and the decider's :class:`~pokegent.options.models.ChoiceResult`. It carries what the MODEL
saw (option ids, labels, and the criterion sentence of each option) and what it
answered; it never carries what the model must not see: no ``executor_ref`` / ``executor_args``,
no ``postcondition_ref``, no fingerprint, no snapshot hash, no error message text (an exception is
reduced to its class name and its ``E_*`` code).

:class:`ObservedDecider` wraps a decider and reports every call to a recorder. It returns the
inner result unchanged (same object) and re-raises the inner exception unchanged: it can neither
change an answer nor swallow a failure, and a recorder that raises is ignored.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from pokegent.options.models import (
    ChoiceResult,
    DecisionFrame,
    Option,
    criterion_text,
    to_system_one_payload,
)

#: option rows a record keeps (a frame with more is cut after the pick is secured)
MAX_OPTIONS = 24
TEXT_CHARS = 420
#: the state the model was sent is kept whole up to this many characters of JSON
MODEL_STATE_CHARS = 6000
LABEL_CHARS = 80

_QUESTIONS: Mapping[str, str] = {
    "field.free": "Where should the player go next?",
    "field.trainer_vision": "A trainer has spotted us. What now?",
    "dialog.printing": "The text is still printing. Wait or continue?",
    "dialog.advance": "How should the dialog go on?",
    "dialog.end": "How should the dialog end?",
    "dialog.yes_no": "Yes or no?",
    "dialog.multichoice": "Which entry of the menu?",
    "dialog.list": "Which entry of the list?",
    "dialog.heal_offer": "Heal the party?",
    "dialog.item_receive": "An item was received. Continue?",
    "system.whiteout": "The party whited out. How to go on?",
    "battle.action": "What should we do this turn?",
    "battle.moves_ui": "Which move?",
    "battle.choose_target": "Which target?",
    "battle.party": "Which Pokemon?",
    "battle.forced_switch": "Which Pokemon do we send out?",
    "battle.switch_offer": "Switch Pokemon before the next opponent?",
    "battle.bag": "Which item in battle?",
    "battle.item_target": "Use the item on which Pokemon?",
    "battle.message": "Continue the battle text?",
    "battle.faint": "A Pokemon fainted. How to go on?",
    "battle.level_up": "Level up. Continue?",
    "battle.learn_move": "Learn the new move?",
    "battle.ended": "The battle is over. Continue?",
}
_FAMILY_LABELS = {
    "field": "FIELD",
    "battle": "BATTLE",
    "dialog": "DIALOG",
    "system": "SYSTEM",
}


def model_view(frame: DecisionFrame) -> dict[str, Any] | None:
    """What the model was sent besides the criteria: the instruction text and the ``state``
    (scene, goal line, context). The payload guard has already checked that it
    carries no executor reference, fingerprint or id; ``None`` if it would not pass it."""
    try:
        payload = to_system_one_payload(frame)
    except Exception:  # noqa: BLE001 - a leaky frame is shown without its payload, never raised
        return None
    state = payload["state"]
    text = json.dumps(state, ensure_ascii=False)
    if len(text) > MODEL_STATE_CHARS:
        state = {"_truncated": True, "chars": len(text), "keys": sorted(state)}
    return {"instructions": payload["instructions"], "state": state}


def cap(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def family_of(scene_id: str) -> str:
    return scene_id.split(".", 1)[0]


def question_for(frame: DecisionFrame) -> str:
    """The question in plain English: the game's own prompt when an option carries one, else a
    sentence per scene (the model itself only gets the constant instruction and the criteria)."""
    for option in frame.chooseable():
        if option.semantic_prompt:
            return cap(option.semantic_prompt, TEXT_CHARS)
    return _QUESTIONS.get(frame.scene_id, f"Choose an option ({frame.scene_id}).")


def first_sentence(text: str) -> str:
    for i, ch in enumerate(text):
        if ch in "?!." and (i + 1 == len(text) or text[i + 1] == " "):
            return text[: i + 1]
    return text


def _option_row(option: Option, probability: float | None, selected: bool) -> dict[str, Any]:
    return {
        "key": option.id,
        "label": cap(option.label, LABEL_CHARS),
        "text": cap(criterion_text(option), TEXT_CHARS),
        "probability": probability,
        "selected": selected,
        "legality": option.legality.value,
        "risk": option.risk.value,
        "irreversible": option.irreversible,
    }


def _rows(
    frame: DecisionFrame, probabilities: Mapping[str, float], selected: str | None
) -> tuple[list[dict[str, Any]], int]:
    options = frame.chooseable()
    rows = [_option_row(o, probabilities.get(o.id), o.id == selected) for o in options]
    total = len(rows)
    if len(rows) > MAX_OPTIONS:  # keep the best by probability, the pick always among them
        keep = sorted(
            range(len(rows)),
            key=lambda i: (-(rows[i]["probability"] or 0.0), i),
        )
        chosen = set(keep[:MAX_OPTIONS])
        chosen.update(i for i, r in enumerate(rows) if r["selected"])
        rows = [r for i, r in enumerate(rows) if i in chosen]
    return rows, total


def decision_record(
    frame: DecisionFrame,
    *,
    result: ChoiceResult | None,
    source: str,
    elapsed_ms: float | None,
    now: float,
    status: str = "executing",
    error: str | None = None,
    goal_summary: str | None = None,
    model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The record of one decision. ``result`` is ``None`` for a failed decider call; ``model`` is
    :func:`model_view` of the frame (operator surface only)."""
    selected = result.option_id if result is not None else None
    probabilities = dict(result.probabilities) if result is not None else {}
    rows, total = _rows(frame, probabilities, selected)
    pick = frame.get(selected) if selected else None
    fact = cap(criterion_text(pick), TEXT_CHARS) if pick is not None else None
    question = question_for(frame)
    return {
        "id": frame.decision_id,
        "ts": round(now, 3),
        "scene": frame.scene_id,
        "family": family_of(frame.scene_id),
        "family_label": _FAMILY_LABELS.get(
            family_of(frame.scene_id), family_of(frame.scene_id).upper()
        ),
        "question": question,
        "question_short": first_sentence(question),
        "goal": frame.goal_id,
        "goal_summary": goal_summary,
        "options": rows,
        "options_total": total,
        "probabilities": {k: round(v, 4) for k, v in probabilities.items()},
        "selected": selected,
        "selected_label": cap(pick.label, LABEL_CHARS) if pick is not None else None,
        "fact_sentence": fact,
        "source": source,
        "decided_by": None,
        "awaiting_agent": False,
        "provider": (result.provider_id or None) if result is not None else None,
        "model": (result.model_id or None) if result is not None else None,
        "latency_ms": None if elapsed_ms is None else round(elapsed_ms, 1),
        "confidence": result.confidence if result is not None else None,
        "truncated": result.truncated if result is not None else None,
        "model_view": model,
        "status": status,
        "error": error,
        "result": None,
        "forced": source == "forced",
    }


def forced_record(
    frame: DecisionFrame, option_id: str, *, now: float, goal_summary: str | None = None
) -> dict[str, Any]:
    """A frame with exactly one chooseable option: no decider was asked, the runner just acted."""
    result = ChoiceResult(option_id=option_id, provider_id="runner", model_id="forced")
    return decision_record(
        frame, result=result, source="forced", elapsed_ms=None, now=now, goal_summary=goal_summary
    )


def error_label(exc: BaseException) -> str:
    """An exception reduced to what is safe to show: its class and its ``E_*`` code, no message."""
    code = getattr(getattr(exc, "code", None), "value", None)
    return f"{type(exc).__name__} ({code})" if code else type(exc).__name__


class ObservedDecider:
    """A decider that reports each call to ``on_call(frame, result, error, elapsed_ms)`` (and the
    start of it to ``on_start(frame)``, so a slow model shows as "thinking").

    ``result`` is the inner result object itself (``None`` on an exception, which is re-raised
    unchanged). Every other attribute (``manifest``, ``close``, ...) is the inner decider's.
    """

    def __init__(
        self,
        inner: Any,
        on_call: Callable[[DecisionFrame, ChoiceResult | None, BaseException | None, float], None],
        *,
        on_start: Callable[[DecisionFrame], None] | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._inner = inner
        self._on_call = on_call
        self._on_start = on_start
        self._clock = clock

    def decide(self, frame: DecisionFrame) -> ChoiceResult:
        if self._on_start is not None:
            try:
                self._on_start(frame)
            except Exception:  # noqa: BLE001 - a broken viewer never touches a decision
                pass
        started = self._clock()
        try:
            result = self._inner.decide(frame)
        except BaseException as exc:
            self._report(frame, None, exc, started)
            raise
        self._report(frame, result, None, started)
        return result

    def _report(
        self,
        frame: DecisionFrame,
        result: ChoiceResult | None,
        error: BaseException | None,
        started: float,
    ) -> None:
        try:
            self._on_call(frame, result, error, (self._clock() - started) * 1000.0)
        except Exception:  # noqa: BLE001 - a broken viewer never touches a decision
            pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
