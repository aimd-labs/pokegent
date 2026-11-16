"""PROMPT adapter for the dialog family.

This module is deliberately independent of option execution.  Text and script identifiers are
looked up in a small, auditable registry; a miss produces ``unknown`` effects rather than a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pokegent.actions._exec.contracts import PromptChoice, PromptModel
from pokegent.actions.menu.dialog.text import DialogText
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot

PromptKind = Literal["yes_no", "multichoice", "confirm"]


@dataclass(frozen=True)
class PromptSemantic:
    kind: PromptKind
    yes: str
    no: str
    reversible: bool = True
    requires_authorization: bool = False


@dataclass(frozen=True)
class _Rule:
    pattern: re.Pattern[str] | None
    script_ids: frozenset[str]
    semantic: PromptSemantic


_UNKNOWN = PromptSemantic("yes_no", "unknown", "unknown")
_RULES: tuple[_Rule, ...] = (
    _Rule(
        re.compile(r"would you like to use cut", re.I),
        frozenset(),
        PromptSemantic("yes_no", "uses CUT", "declines CUT"),
    ),
    _Rule(
        re.compile(r"want to use the poke fl[u]?te", re.I),
        frozenset(),
        PromptSemantic("yes_no", "uses the POKE FLUTE", "does not use the POKE FLUTE"),
    ),
    _Rule(
        re.compile(r"would you like to save|save the game|save your game", re.I),
        frozenset(),
        PromptSemantic("confirm", "saves the game", "does not save the game"),
    ),
    _Rule(
        re.compile(r"would you like to buy|buy this|purchase", re.I),
        frozenset(),
        PromptSemantic(
            "confirm",
            "confirms the shop transaction",
            "declines the shop transaction",
            reversible=False,
            requires_authorization=True,
        ),
    ),
)


def _script_id(snapshot: StateSnapshot) -> str | None:
    ui = snapshot.ui
    native = ui.script.native if ui is not None and ui.valid else None
    return getattr(native, "name", None) or getattr(native, "symbol", None)


def semantic_for(question: str | None, *, scene_id: str, script_id: str | None) -> PromptSemantic:
    """Resolve known text/script identities; misses are intentionally not classified."""
    for rule in _RULES:
        if (rule.pattern is not None and question and rule.pattern.search(question)) or (
            script_id is not None and script_id in rule.script_ids
        ):
            return rule.semantic
    return _UNKNOWN


def _cursor_key(snapshot: StateSnapshot, choices: tuple[PromptChoice, ...]) -> str:
    ui = snapshot.ui
    menu = ui.menu.menu if ui is not None and ui.valid else None
    cursor = getattr(menu, "cursor_pos", None)
    if (
        getattr(menu, "window_in_use", False)
        and isinstance(cursor, int)
        and 0 <= cursor < len(choices)
    ):
        return choices[cursor].stable_key
    return "cursor_unknown"


def build_prompt_model(
    snapshot: StateSnapshot,
    text: DialogText | None,
    *,
    scene_id: str,
    menu_entries=None,
) -> PromptModel | None:
    """Build a prompt beside the existing dialog builder, using only RAM/UI evidence."""
    ui = snapshot.ui
    if ui is None or not ui.valid or ui.menu_type not in {"yes_no", "multichoice"}:
        return None
    question = (
        text.last_page if text is not None and text.last_page else "question text unavailable"
    )
    semantic = semantic_for(
        question if text is not None else None, scene_id=scene_id, script_id=_script_id(snapshot)
    )
    if ui.menu_type == "multichoice":
        menu = ui.menu.multichoice
        raw_count = getattr(ui.menu.menu, "max_cursor_pos", -1) + 1
        if menu is None or not 0 < raw_count <= 128:
            return None
        resolved = (
            menu_entries(snapshot, menu.multichoice_id, raw_count)
            if menu_entries is not None
            else None
        )
        pc_entries = (
            tuple(resolved)
            if resolved is not None and all(key.startswith("pc:") for key, _ in resolved)
            else None
        )
        entries = tuple(resolved) if resolved is not None else None
        if entries is None:
            entries = tuple((f"choice:{i}", f"Entry {i + 1}") for i in range(raw_count))
        choices = tuple(
            PromptChoice(
                stable_key=key,
                effect=f"select {label}" if pc_entries is not None else "unknown",
            )
            for key, label in entries
        )
        kind: PromptKind = "multichoice"
    else:
        choices = (
            PromptChoice(stable_key="yes", effect=semantic.yes),
            PromptChoice(stable_key="no", effect=semantic.no),
        )
        kind = semantic.kind
    return PromptModel(
        question=question,
        kind=kind,
        choices=choices,
        cursor_key=_cursor_key(snapshot, choices),
        reversible=semantic.reversible,
        requires_authorization=semantic.requires_authorization,
    )
