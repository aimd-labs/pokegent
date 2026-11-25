"""Readable diagnostic reasons: one shared projection for the served frame and the pause.

The bodies below were moved unchanged from :mod:`pokegent.server` (PR214) so the HTTP
diagnostics view and the Runner's NO_OPTIONS pause share exactly one sanitizer, id redactor
and leak guard. Nothing here infers a cause: only a producer-written ``facts["reason"]`` is
projected, and a refused or missing reason stays absent.
"""

from __future__ import annotations

from typing import Any

from pokegent.options.models import (
    DecisionFrame,
    PayloadLeakError,
    assert_payload_clean,
    sanitize_internal_text,
)

MAX_SERVED_DIAGNOSTICS = 32
MAX_SERVED_DIAGNOSTIC_REASON_CHARS = 240
MAX_PAUSE_REASONS = 3
MAX_PAUSE_REASON_CHARS = 240


def redact_diagnostic_ids(text: str, frame: DecisionFrame) -> str:
    """Redact diagnostic ids locally; the shared sanitizer and guard do not cover option ids."""
    for option_id in sorted(
        (option.id for option in frame.diagnostics), key=len, reverse=True
    ):
        text = text.replace(option_id, "[non-selectable option]")
    return text


def served_diagnostics(frame: DecisionFrame) -> dict[str, Any]:
    """Bounded readable rows for diagnostics; explanation only, never selectable identity.

    Reasons are projected through the existing sanitizer and leak guard against the owning
    frame, so an unrenderable reason stays ``None`` instead of publishing hidden metadata.
    Diagnostic ids are redacted locally because neither helper covers option identity.
    """
    items: list[dict[str, Any]] = []
    for option in frame.diagnostics[:MAX_SERVED_DIAGNOSTICS]:
        reason = option.facts.get("reason")
        text: str | None = None
        if isinstance(reason, str) and reason.strip():
            text = redact_diagnostic_ids(sanitize_internal_text(reason, frame), frame)
            if len(text) > MAX_SERVED_DIAGNOSTIC_REASON_CHARS:
                text = text[: MAX_SERVED_DIAGNOSTIC_REASON_CHARS - 3].rstrip() + "..."
            try:
                assert_payload_clean(
                    {"state": {"diagnostic": text}, "instructions": "", "criteria": {}},
                    frame,
                )
            except PayloadLeakError:
                text = None
        items.append({"legality": option.legality.value, "reason": text})
    return {"total": len(frame.diagnostics), "items": items}


def bounded_pause_reasons(frame: DecisionFrame) -> tuple[str, ...]:
    """The producer-written reasons a NO_OPTIONS pause may show, in diagnostic order.

    This is a consumer projection, not a second sanitizer: it reuses
    :func:`served_diagnostics` and never reads route needs, blocked-route facts or legality
    counts. Reasons are deduped by their exact text and the whole line stays inside the
    3-reason / 240-character pause bound; a reason is never sliced here.
    """
    selected: list[str] = []
    for item in served_diagnostics(frame)["items"]:
        reason = item["reason"]
        if not isinstance(reason, str) or not reason or reason in selected:
            continue
        joined = len("; ".join((*selected, reason)))
        if joined > MAX_PAUSE_REASON_CHARS:
            break
        selected.append(reason)
        if len(selected) == MAX_PAUSE_REASONS:
            break
    return tuple(selected)
