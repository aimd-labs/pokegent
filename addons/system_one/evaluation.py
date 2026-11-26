"""Default-off evaluation of caller-labeled choices (#134): counts supplied labels only.

No label certification, authority, eligibility, provider/executor call or file access.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import islice
from typing import Any

from pokegent.options.models import ChoiceResult, DecisionFrame, check_choice

MAX_CASES, MAX_LABELS, MAX_IDENTITY = 1000, 256, 128
CONTEXT_FIELDS = ("game_profile", "backend_version", "provider_protocol",
                  "prompt_profile", "knowledge_policy")
FIELDS = ("supplied", "labeled", "unknown_label", "no_prediction", "invalid_choice",
          "valid_choice", "labeled_valid_choice", "acceptable_choice")


class EvaluationRejected(ValueError):
    """Malformed labeled-evaluation request; case details are never echoed."""


def _identity(value: Any, name: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > MAX_IDENTITY:
        raise EvaluationRejected(f"{name} must be nonblank and at most 128 characters")
    return value


@dataclass(frozen=True)
class EvaluationContext:
    """Caller-supplied comparison tags; not runtime admission or proof."""

    game_profile: str
    backend_version: str
    provider_protocol: str
    prompt_profile: str
    knowledge_policy: str

    def __post_init__(self) -> None:
        for name in CONTEXT_FIELDS:
            _identity(getattr(self, name), f"context.{name}")


@dataclass(frozen=True)
class LabeledChoiceCase:
    frame: DecisionFrame
    result: ChoiceResult | None
    acceptable_option_ids: frozenset[str] | None
    family_id: str
    context: EvaluationContext | None = None


def _peer_identity(value: Any, name: str) -> str | None:
    if type(value) is str and value == "":
        return None
    return _identity(value, name)


def _labels(case: LabeledChoiceCase, chooseable: set[str]) -> frozenset[str] | None:
    labels = case.acceptable_option_ids
    if labels is None:
        return None
    if type(labels) is not frozenset or not labels or len(labels) > MAX_LABELS:
        raise EvaluationRejected("labels must be a nonempty frozenset of at most 256 ids")
    if not all(type(label) is str and label in chooseable for label in labels):
        raise EvaluationRejected("labels must name visible options of the frame")
    return labels


def _collect(cases: Iterable[LabeledChoiceCase]) -> list[LabeledChoiceCase]:
    if isinstance(cases, (str, bytes, bytearray)):
        raise EvaluationRejected("cases must be an iterable of LabeledChoiceCase")
    try:
        materialized = list(islice(iter(cases), MAX_CASES + 1))
    except TypeError:
        raise EvaluationRejected("cases must be an iterable of LabeledChoiceCase") from None
    if len(materialized) > MAX_CASES:
        raise EvaluationRejected(f"cases exceeds {MAX_CASES}")
    return materialized


def evaluate_labeled_choices(
    cases: Iterable[LabeledChoiceCase], *, enabled: bool = False,
    family_breakdown: bool = False,
) -> dict[str, Any]:
    """Evaluate supplied labels only when ``enabled is True``; otherwise inert."""
    if type(enabled) is not bool:
        raise EvaluationRejected("enabled must be a bool")
    if type(family_breakdown) is not bool:
        raise EvaluationRejected("family_breakdown must be a bool")
    if not enabled:
        return {"kind": "labeled_choice_evaluation/1", "enabled": False, "evaluated": False,
                "eligibility": "not_evaluated", "authority_granted": False}
    counts = dict.fromkeys(FIELDS, 0)
    rows: dict[
        tuple[str, str | None, str | None, tuple[str, ...] | None], dict[str, int]
    ] = {}
    family_rows: dict[
        tuple[str, str, str | None, str | None, tuple[str, ...] | None], dict[str, int]
    ] = {}
    supplied_families: set[str] = set()
    labeled_families: set[str] = set()
    seen: set[tuple[str, str, str | None, str | None]] = set()
    for case in _collect(cases):
        if not isinstance(case, LabeledChoiceCase):
            raise EvaluationRejected("cases must contain LabeledChoiceCase values")
        context = case.context
        if context is not None and not isinstance(context, EvaluationContext):
            raise EvaluationRejected("context must be an EvaluationContext or None")
        context_key = (None if context is None
                       else tuple(getattr(context, name) for name in CONTEXT_FIELDS))
        frame, result = case.frame, case.result
        if not isinstance(frame, DecisionFrame) or not (
            result is None or isinstance(result, ChoiceResult)
        ):
            raise EvaluationRejected("frame/result must be real decision objects")
        for value, name in ((case.family_id, "family_id"), (frame.run_id, "run_id"),
                            (frame.decision_id, "decision_id"), (frame.scene_id, "scene_id")):
            _identity(value, name)
        labels = _labels(case, set(frame.chooseable_ids()))
        provider = _peer_identity(result.provider_id, "provider_id") if result is not None else None
        model = _peer_identity(result.model_id, "model_id") if result is not None else None
        duplicate = (frame.run_id, frame.decision_id, provider, model)
        if duplicate in seen:
            raise EvaluationRejected("duplicate (run_id, decision_id, provider, model) case")
        seen.add(duplicate)
        supplied_families.add(case.family_id)
        if labels is not None:
            labeled_families.add(case.family_id)
        row = rows.setdefault(
            (frame.scene_id, provider, model, context_key), dict.fromkeys(FIELDS, 0)
        )
        valid = (
            result is not None and result.decision_id == frame.decision_id
            and check_choice(frame, result, allow_all=False) is None
        )
        acceptable = valid and labels is not None and result.option_id in labels
        flags = {"supplied": True, "labeled": labels is not None,
                 "unknown_label": labels is None, "no_prediction": result is None,
                 "invalid_choice": result is not None and not valid, "valid_choice": valid,
                 "labeled_valid_choice": valid and labels is not None,
                 "acceptable_choice": acceptable}
        if family_breakdown:
            family_row = family_rows.setdefault(
                (case.family_id, frame.scene_id, provider, model, context_key),
                dict.fromkeys(FIELDS, 0),
            )
            for name, flag in flags.items():
                family_row[name] += 1 if flag else 0
        for name, flag in flags.items():
            increment = 1 if flag else 0
            counts[name] += increment
            row[name] += increment
    strata = [
        {"scene_id": key[0], "provider": key[1], "model": key[2],
         "comparison_context": (None if key[3] is None
                                else dict(zip(CONTEXT_FIELDS, key[3]))),
         **rows[key]}
        for key in sorted(rows, key=lambda key: (
            key[0], key[1] is not None, key[1] or "", key[2] is not None, key[2] or "",
            key[3] is not None, key[3] or (),
        ))
    ]
    acceptable = counts["acceptable_choice"]
    report = {
        "kind": "labeled_choice_evaluation/1", "enabled": True, "evaluated": True,
        "counts": counts,
        "rates": {
            "acceptable_choice_rate": (None if not counts["labeled_valid_choice"]
                                       else acceptable / counts["labeled_valid_choice"]),
            "acceptable_choice_rate_denominator": "acceptable_choice/labeled_valid_choice",
            "labeled_case_success_rate": (None if not counts["labeled"]
                                          else acceptable / counts["labeled"]),
            "labeled_case_success_rate_denominator": "acceptable_choice/labeled_cases",
        },
        "supplied_family_count": len(supplied_families),
        "labeled_family_count": len(labeled_families),
        "strata": strata,
        "limits": {"labels": "caller_supplied_not_independent_certification",
                   "accuracy": "conditional_on_supplied_labels",
                   "family_grouping": "supplied_not_independent_certification",
                   "no_prediction_identity": "unknown_provider_and_model",
                   "comparison_context": "caller_supplied_not_runtime_verified",
                   "unknown_context": "not_inferred_from_model_or_frame",
                   "case_cap": MAX_CASES, "label_cap": MAX_LABELS},
        "eligibility": "not_evaluated", "authority_granted": False,
    }
    if family_breakdown:
        report["family_strata"] = [
            {"family_id": key[0], "scene_id": key[1], "provider": key[2], "model": key[3],
             "comparison_context": (None if key[4] is None
                                    else dict(zip(CONTEXT_FIELDS, key[4]))),
             **family_rows[key]}
            for key in sorted(family_rows, key=lambda key: (
                key[0], key[1], key[2] is not None, key[2] or "",
                key[3] is not None, key[3] or "", key[4] is not None, key[4] or (),
            ))
        ]
    return report
