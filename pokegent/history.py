"""Bounded neutral recent choices; adapted from the MIT-licensed fork (see notices)."""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Hashable, Mapping
from dataclasses import dataclass
from typing import Any

from pokegent.actions._exec.models import ActionResult, ActionStatus
from pokegent.frozen import freeze
from pokegent.options.models import DecisionFrame

#: the context key the model sees
HISTORY_KEY = "recent_choices"
DEFAULT_KEEP = 3
MAX_KEEP = 8
#: characters of one old/new value in a diff, and of an option label
VALUE_CHARS = 60
LABEL_CHARS = 60
#: changed keys listed per entry; the rest is counted ("+N more changed")
MAX_DIFF_KEYS = 3
#: a changed value of these keys is a counter: reported in brackets only if NOTHING else changed
COUNTER_KEYS = ("turns_played",)
#: context keys that describe the screen, the goal or the history itself, not the situation the
#: choice acted on; a screen change shows in the options already, a goal change resets the history
SKIP_KEYS = frozenset(
    {
        "scene",
        "goal",
        "objective",
        "prompt",
        "question",
        "summary",
        "menu",
        "menu_variant",
        "party_phase",
        "entries_labelled",
        "dialog_text",
        "situation",
        "story_facts",
        HISTORY_KEY,
    }
)
#: Smallest number of leading words old and new value must share before the new value drops them
#: (``TestMon Lv5 HP 100% -> HP 62%``): the name and the level are not repeated, the last shared
#: word stays as the anchor.
MIN_SHARED_WORDS = 2

_MISSING = object()


def scene_family(scene_id: str) -> str:
    return scene_id.split(".", 1)[0]


def _cap(text: str, limit: int = VALUE_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: max(limit - 3, 1)]
    if " " in cut[limit // 2 :]:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.") + "..."


def render_value(value: Any) -> str:
    """A context value as short text; a missing key renders as ``none``."""
    if value is _MISSING or value is None:
        return "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, Mapping):
        return ", ".join(f"{k} {render_value(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return ", ".join(render_value(v) for v in value)
    return str(value)


def _same(a: Any, b: Any) -> bool:
    """Equality that does not care whether a list came as a tuple or a list."""
    if a is _MISSING or b is _MISSING:
        return a is b
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def _elide_shared_words(old: str, new: str) -> str:
    """Drop the leading words ``new`` shares with ``old``, except the last shared one (an anchor):
    ``TestMon Lv5 HP 100%`` / ``TestMon Lv5 HP 62%`` gives ``HP 62%``. Needs at least
    ``MIN_SHARED_WORDS`` shared words and never empties ``new``."""
    a, b = old.split(), new.split()
    shared = 0
    while shared < min(len(a), len(b)) and a[shared] == b[shared]:
        shared += 1
    if shared >= MIN_SHARED_WORDS and shared < len(b):
        return " ".join(b[shared - 1 :])
    return new


def change_text(key: str, before: Any, after: Any) -> str:
    """``key: old -> new``, each value capped; ``new`` drops the words it shares with ``old``."""
    old, new = render_value(before), render_value(after)
    return f"{key}: {_cap(old)} -> {_cap(_elide_shared_words(old, new))}"


def _changed_keys(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """Keys whose value differs: the order of ``after`` first, then keys only ``before`` had."""
    ordered = [k for k in after if k not in SKIP_KEYS and not k.startswith("recent_")]
    ordered += sorted(
        k for k in before if k not in after and k not in SKIP_KEYS and not k.startswith("recent_")
    )
    return [k for k in ordered if not _same(before.get(k, _MISSING), after.get(k, _MISSING))]


def context_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
    """The mechanical diff of two frame contexts, as one short string.

    ``key: old -> new`` for at most ``MAX_DIFF_KEYS`` changed top-level keys (then ``+N more
    changed``); counter keys are left out while anything else changed. If only counters (or nothing)
    changed: ``nothing else changed`` plus the counters in brackets. No interpretation.
    """
    changed = _changed_keys(before, after)
    shown = [k for k in changed if k not in COUNTER_KEYS]
    if not shown:
        text = "nothing else changed"
        counters = [k for k in changed if k in COUNTER_KEYS]
        if counters:
            moves = (
                f"{k} {_cap(render_value(before.get(k, _MISSING)))} -> "
                f"{_cap(render_value(after.get(k, _MISSING)))}"
                for k in counters
            )
            text += f" ({'; '.join(moves)})"
        return text
    parts = [
        change_text(k, before.get(k, _MISSING), after.get(k, _MISSING))
        for k in shown[:MAX_DIFF_KEYS]
    ]
    if len(shown) > MAX_DIFF_KEYS:
        parts.append(f"+{len(shown) - MAX_DIFF_KEYS} more changed")
    return "; ".join(parts)


def outcome_text(result: ActionResult) -> str:
    """The status of an executed action: ``done`` or ``<failed|rejected|interrupted> (E_CODE)``."""
    status = result.status
    if status is ActionStatus.APPLIED:
        return "done"
    word = {
        ActionStatus.REJECTED: "rejected",
        ActionStatus.INTERRUPTED: "interrupted",
    }.get(status, "failed")  # timed_out and uncertain: inputs ran, the outcome is not established
    code = result.error_code.value if result.error_code is not None else "no error code"
    return f"{word} ({code})"


@dataclass
class _Entry:
    head: str  # "<label>: <outcome>"
    family: str
    goal_key: Hashable
    #: context of the frame the choice was made on; ``None`` once the diff is attached (or never
    #: needed: a rejected action pressed nothing)
    context: Mapping[str, Any] | None
    diff: str | None = None

    def text(self) -> str:
        return f"{self.head}; {self.diff}" if self.diff else self.head


class DecisionHistory:
    """Last decider choices; zero disables this optional context."""

    def __init__(self, keep: int = DEFAULT_KEEP) -> None:
        if isinstance(keep, bool) or not isinstance(keep, int) or not 0 <= keep <= MAX_KEEP:
            raise ValueError(f"decision_history must be an integer from 0 to {MAX_KEEP}")
        self.keep = keep
        self._entries: deque[_Entry] = deque(maxlen=keep or 1)
        self._last_turns: int | None = None

    def reset(self) -> None:
        self._entries.clear()
        self._last_turns = None

    def observe(self, frame: DecisionFrame, goal_key: Hashable = None) -> None:
        if not self.keep:
            return
        if self._entries:
            last = self._entries[-1]
            if (scene_family(frame.scene_id) != last.family or goal_key != last.goal_key
                    or _counter_went_back({"turns_played": self._last_turns}, frame.context)):
                self.reset()
        turns = frame.context.get("turns_played")
        self._last_turns = turns if isinstance(turns, int) and not isinstance(turns, bool) else None

    def prepare(self, frame: DecisionFrame, goal_key: Hashable = None) -> DecisionFrame:
        self.observe(frame, goal_key)
        if not self.keep or not self._entries:
            return frame
        last = self._entries[-1]
        if last.context is not None:
            before = last.context
            last.diff = context_diff(before, frame.context)
            last.context = None
        if HISTORY_KEY in frame.context:
            return frame
        context = dict(frame.context)
        context[HISTORY_KEY] = [entry.text() for entry in self._entries]
        return frame.model_copy(update={"context": freeze(context)})

    def record(self, frame: DecisionFrame, option_id: str, result: ActionResult,
               goal_key: Hashable = None) -> None:
        if not self.keep:
            return
        self.observe(frame, goal_key)
        option = frame.get(option_id)
        label = _cap(option.label if option is not None else option_id, LABEL_CHARS)
        self._entries.append(_Entry(
            head=f"{label}: {outcome_text(result)}", family=scene_family(frame.scene_id),
            goal_key=goal_key,
            context=None if result.status is ActionStatus.REJECTED else dict(frame.context),
        ))


def _counter_went_back(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    for key in COUNTER_KEYS:
        old, new = before.get(key), after.get(key)
        if (
            isinstance(old, int)
            and isinstance(new, int)
            and not isinstance(old, bool)
            and not isinstance(new, bool)
            and new < old
        ):
            return True
    return False
