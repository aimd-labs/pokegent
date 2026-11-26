"""Opt-in per-family diagnostics for labeled-choice evaluation (#134)."""

from __future__ import annotations

import pytest

from addons.system_one.evaluation import (
    FIELDS,
    EvaluationContext,
    EvaluationRejected,
    LabeledChoiceCase,
    evaluate_labeled_choices,
)
from pokegent.options.models import ChoiceResult, DecisionFrame, Legality, Option

_CONTEXT = {
    "game_profile": "firered-us-1.0",
    "backend_version": "mgba-0.10.3",
    "provider_protocol": "openai-compatible/1",
    "prompt_profile": "scene-v3",
    "knowledge_policy": "game-facts-only",
}
_DIMENSIONS = {"scene_id", "provider", "model", "comparison_context"}


def context(**overrides):
    return EvaluationContext(**{**_CONTEXT, **overrides})


def observation(
    family_id,
    decision_id,
    *,
    option_id="tap:a",
    labels=frozenset({"tap:a"}),
    scene_id="field.free",
    provider="system1",
    model="model-1",
    no_result=False,
    case_context=None,
):
    options = (
        Option(id="tap:a", label="A", legality=Legality.LEGAL, executor_ref="test.action"),
        Option(id="tap:b", label="B", legality=Legality.LEGAL, executor_ref="test.action"),
    )
    frame = DecisionFrame(
        decision_id=decision_id,
        run_id="run-1",
        snapshot_seq=1,
        scene_id=scene_id,
        scene_confidence=1.0,
        state_fingerprint="sha256:" + "0" * 64,
        options=options,
    )
    result = (
        None
        if no_result
        else ChoiceResult(
            option_id=option_id,
            decision_id=decision_id,
            provider_id=provider,
            model_id=model,
        )
    )
    return LabeledChoiceCase(frame, result, labels, family_id, case_context)


def test_family_breakdown_is_opt_in_and_default_output_is_unchanged():
    cases = [observation("f1", "d1"), observation("f2", "d2")]

    default = evaluate_labeled_choices(cases, enabled=True)
    explicitly_off = evaluate_labeled_choices(cases, enabled=True, family_breakdown=False)
    report = evaluate_labeled_choices(cases, enabled=True, family_breakdown=True)

    assert default == explicitly_off
    assert "family_strata" not in default
    assert report["counts"] == default["counts"]
    assert report["strata"] == default["strata"]
    assert [row["family_id"] for row in report["family_strata"]] == ["f1", "f2"]
    assert set(report["family_strata"][0]) == {"family_id", *_DIMENSIONS, *FIELDS}


def test_two_families_split_one_stratum_and_keep_shared_dimensions():
    cases = [
        observation("f1", "d1"),
        observation("f1", "d2"),
        observation("f2", "d3"),
    ]

    report = evaluate_labeled_choices(cases, enabled=True, family_breakdown=True)

    assert len(report["strata"]) == 1
    assert report["strata"][0]["supplied"] == 3
    rows = {row["family_id"]: row for row in report["family_strata"]}
    assert rows["f1"]["supplied"] == 2
    assert rows["f2"]["supplied"] == 1
    for row in report["family_strata"]:
        assert {name: row[name] for name in _DIMENSIONS} == {
            name: report["strata"][0][name] for name in _DIMENSIONS
        }
        assert "rates" not in row


def test_family_rows_keep_context_and_provider_separation():
    cases = [
        observation("f1", "d1", case_context=context()),
        observation("f1", "d2", case_context=context(prompt_profile="other")),
        observation("f1", "d3", provider="other-provider"),
        observation("f2", "d4", case_context=context()),
    ]

    report = evaluate_labeled_choices(cases, enabled=True, family_breakdown=True)

    rows = report["family_strata"]
    assert len(rows) == 4
    assert sum(row["family_id"] == "f1" for row in rows) == 3
    assert {row["provider"] for row in rows} == {"system1", "other-provider"}
    assert sum(row["comparison_context"] is None for row in rows) == 1


def test_family_rows_count_unknown_no_prediction_and_invalid_cases():
    cases = [
        observation("f1", "d1"),
        observation("f1", "d2", option_id="tap:missing"),
        observation("f1", "d3", no_result=True),
        observation("f2", "d4", labels=None),
        observation("f2", "d5", option_id="tap:b"),
    ]

    report = evaluate_labeled_choices(cases, enabled=True, family_breakdown=True)

    rows = {(row["family_id"], row["provider"]): row for row in report["family_strata"]}
    assert rows[("f1", None)]["no_prediction"] == 1
    assert rows[("f1", "system1")]["invalid_choice"] == 1
    assert rows[("f1", "system1")]["acceptable_choice"] == 1
    f2_unknown = rows[("f2", "system1")]["unknown_label"]
    assert (f2_unknown, rows[("f2", "system1")]["valid_choice"]) == (1, 2)
    for field in FIELDS:
        assert sum(row[field] for row in report["family_strata"]) == report["counts"][field]


def test_duplicate_observation_is_still_rejected_with_family_breakdown():
    first = observation("f1", "d1")

    with pytest.raises(EvaluationRejected):
        evaluate_labeled_choices(
            [first, observation("f2", "d1")], enabled=True, family_breakdown=True
        )


def test_disabled_evaluation_does_not_consume_cases_with_family_flag():
    class Poison:
        def __iter__(self):
            raise AssertionError("disabled evaluation must not consume input")

    report = evaluate_labeled_choices(Poison(), family_breakdown=True)

    assert report["evaluated"] is False
    assert "family_strata" not in report
