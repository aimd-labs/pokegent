"""Generic, bounded cursor execution for RAM-decoded menu models.

The executor accepts only a stable entry key. It re-decodes the model after every input and
confirms only after RAM reports that exact key as selected. Callers supply the concrete ``Tap``
factory so this module does not own a family's timing policy.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from pokegent.actions._exec.contracts import MenuEntry, MenuModel, PromptModel
from pokegent.actions._exec.models import InputStep, Tap

CursorFailureCode = Literal[
    "model_unreadable",
    "target_missing",
    "target_ambiguous",
    "target_disabled",
    "cursor_unreadable",
    "cursor_key_missing",
    "cursor_did_not_move",
    "cursor_moved_away",
    "move_budget_exhausted",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CursorFailure(_Frozen):
    code: CursorFailureCode
    reason: str
    stable_key: str
    surface: str | None = None


class CursorExecutionError(RuntimeError):
    """A typed safe stop. The confirmation input has not been emitted for this failure."""

    def __init__(self, failure: CursorFailure) -> None:
        self.failure = failure
        self.failure_reason = failure.code
        super().__init__(f"{failure.code}: {failure.reason}")


class CursorPlan(_Frozen):
    surface: str
    stable_key: str
    start_key: str
    target_index: int
    buttons: tuple[str, ...]


class ShadowParity(_Frozen):
    status: Literal["match", "mismatch"]
    reason: str
    surface: str
    stable_key: str
    predicted_buttons: tuple[str, ...] = ()
    legacy_buttons: tuple[str, ...] = ()
    failure: CursorFailure | None = None


def _fail(code: CursorFailureCode, reason: str, key: str, model: MenuModel | None = None):
    raise CursorExecutionError(
        CursorFailure(
            code=code,
            reason=reason,
            stable_key=key,
            surface=model.surface if model is not None else None,
        )
    )


def menu_entry_index(model: MenuModel, stable_key: str) -> int:
    """Resolve a game-identity key in one fresh menu model, never by row arithmetic."""
    matches = [index for index, entry in enumerate(model.entries) if entry.stable_key == stable_key]
    if not matches:
        _fail("target_missing", f"menu entry {stable_key!r} is absent", stable_key, model)
    if len(matches) != 1:
        _fail("target_ambiguous", f"menu entry {stable_key!r} is duplicated", stable_key, model)
    if not model.entries[matches[0]].enabled:
        _fail("target_disabled", f"menu entry {stable_key!r} is disabled", stable_key, model)
    return matches[0]


def _cursor_index(model: MenuModel, stable_key: str) -> int:
    if model.selected_key is None:
        _fail("cursor_unreadable", "RAM did not report a selected entry", stable_key, model)
    matches = [
        index for index, entry in enumerate(model.entries) if entry.stable_key == model.selected_key
    ]
    if len(matches) != 1:
        _fail(
            "cursor_key_missing",
            f"selected key {model.selected_key!r} is not unique in the decoded entries",
            stable_key,
            model,
        )
    return matches[0]


def cursor_button(current: int, target: int) -> str | None:
    if current == target:
        return None
    return "down" if target > current else "up"


def grid_cursor_button(current: int, target: int, *, columns: int) -> str | None:
    """Return one deterministic grid move, preferring rows before columns."""
    if columns <= 0:
        raise ValueError("columns must be positive")
    current_row, current_column = divmod(current, columns)
    target_row, target_column = divmod(target, columns)
    if current_row != target_row:
        return "down" if target_row > current_row else "up"
    if current_column != target_column:
        return "right" if target_column > current_column else "left"
    return None


def plan_menu_entry(model: MenuModel, stable_key: str, *, max_moves: int = 128) -> CursorPlan:
    """Predict the bounded button sequence from one RAM model (directions, then confirmation)."""
    target = menu_entry_index(model, stable_key)
    current = _cursor_index(model, stable_key)
    moves = abs(target - current)
    if moves > max_moves:
        _fail(
            "move_budget_exhausted",
            f"selection needs {moves} moves but the bound is {max_moves}",
            stable_key,
            model,
        )
    direction = cursor_button(current, target)
    buttons = (() if direction is None else (direction,) * moves) + ("a",)
    assert model.selected_key is not None
    return CursorPlan(
        surface=model.surface,
        stable_key=stable_key,
        start_key=model.selected_key,
        target_index=target,
        buttons=buttons,
    )


def select_menu_entry(
    observation: Any,
    stable_key: str,
    decode: Callable[[Any], MenuModel | None],
    *,
    tap: Callable[[str], Tap] = Tap,
    max_moves: int = 128,
    readback_retries: int = 0,
    confirm_retries: int = 0,
    next_button: Callable[[int, int], str | None] = cursor_button,
) -> Generator[Tap, Any, Any]:
    """Move to *stable_key* from fresh RAM models and confirm it, within ``max_moves``.

    ``observation`` and each value sent back by the authoritative Executor expose ``.snapshot``.
    The wait after every input is bounded by the supplied ``Tap`` and by the surrounding
    ``ExecutorSpec`` frame/wall budgets. This generator adds an independent input bound. A
    confirmation is retried only while a fresh decode proves that the same target remains selected.
    """
    if confirm_retries < 0:
        raise ValueError("confirm_retries must be non-negative")
    obs = observation
    if readback_retries < 0:
        raise ValueError("readback_retries must be non-negative")
    moves = 0
    retries = 0
    while True:
        model = decode(obs.snapshot)
        if model is None:
            _fail("model_unreadable", "menu model could not be decoded", stable_key)
        target = menu_entry_index(model, stable_key)
        current = _cursor_index(model, stable_key)
        button = next_button(current, target)
        if button is None:
            obs = yield tap("a")
            for _ in range(confirm_retries):
                updated = decode(obs.snapshot)
                if updated is None or updated.surface != model.surface:
                    return obs
                updated_target = menu_entry_index(updated, stable_key)
                updated_current = _cursor_index(updated, stable_key)
                if updated_current != updated_target:
                    _fail(
                        "cursor_moved_away",
                        "menu remained open but no longer selected the confirmation target",
                        stable_key,
                        updated,
                    )
                obs = yield tap("a")
            return obs
        if moves >= max_moves + readback_retries:
            _fail(
                "move_budget_exhausted",
                f"cursor did not reach the target in {max_moves} moves and "
                f"{readback_retries} retries",
                stable_key,
                model,
            )
        obs = yield tap(button)
        moves += 1
        updated = decode(obs.snapshot)
        if updated is None:
            _fail("model_unreadable", "menu disappeared after a cursor input", stable_key, model)
        new_target = menu_entry_index(updated, stable_key)
        new_current = _cursor_index(updated, stable_key)
        did_not_move = updated.selected_key == model.selected_key
        moved_away = next_button is cursor_button and abs(new_target - new_current) >= abs(
            target - current
        )
        if (did_not_move or moved_away) and retries < readback_retries:
            retries += 1
            continue
        if did_not_move:
            _fail(
                "cursor_did_not_move",
                f"{button} did not change the RAM cursor",
                stable_key,
                updated,
            )
        if moved_away:
            _fail(
                "cursor_moved_away",
                f"{button} did not move the RAM cursor toward the target",
                stable_key,
                updated,
            )


def input_buttons(trace: Iterable[InputStep]) -> tuple[str, ...]:
    """Flatten only single-button tap steps from an ActionResult trace."""
    return tuple(step.buttons[0] for step in trace if step.kind == "tap" and len(step.buttons) == 1)


def compare_shadow(
    plan: CursorPlan | CursorFailure,
    legacy_trace: Iterable[InputStep],
) -> ShadowParity:
    legacy = input_buttons(legacy_trace)
    if isinstance(plan, CursorFailure):
        return ShadowParity(
            status="mismatch",
            reason=plan.code,
            surface=plan.surface or "unknown",
            stable_key=plan.stable_key,
            legacy_buttons=legacy,
            failure=plan,
        )
    matched = plan.buttons == legacy
    return ShadowParity(
        status="match" if matched else "mismatch",
        reason="button_sequence_match" if matched else "button_sequence_mismatch",
        surface=plan.surface,
        stable_key=plan.stable_key,
        predicted_buttons=plan.buttons,
        legacy_buttons=legacy,
    )


def shadow_plan(
    model: MenuModel | PromptModel | None,
    stable_key: str,
    *,
    surface: str = "prompt",
) -> CursorPlan | CursorFailure:
    """Non-throwing planning entry point used by shadow telemetry."""
    try:
        if model is None:
            return CursorFailure(
                code="model_unreadable",
                reason="no decoded model accompanied the decision frame",
                stable_key=stable_key,
            )
        if isinstance(model, PromptModel):
            model = MenuModel(
                surface=surface,
                entries=tuple(
                    MenuEntry(
                        stable_key=choice.stable_key,
                        label=choice.stable_key,
                        effect=choice.effect,
                    )
                    for choice in model.choices
                ),
                selected_key=None if model.cursor_key == "cursor_unknown" else model.cursor_key,
                cancel=True,
            )
        return plan_menu_entry(model, stable_key)
    except CursorExecutionError as exc:
        return exc.failure
    except Exception as exc:
        return CursorFailure(
            code="model_unreadable",
            reason=f"{type(exc).__name__}: {exc}"[:240],
            stable_key=stable_key,
            surface=surface,
        )
