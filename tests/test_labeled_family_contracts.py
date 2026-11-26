"""Deferred acceptance contracts for opt-in family breakdown (#134)."""

from __future__ import annotations

from itertools import permutations

import pytest
from test_labeled_family_breakdown import observation

from addons.system_one.evaluation import EvaluationRejected, evaluate_labeled_choices


@pytest.mark.parametrize("bad", [1, 0, "true", None, []])
@pytest.mark.parametrize("enabled", [True, False])
def test_nonbool_family_breakdown_is_rejected(bad, enabled):
    with pytest.raises(EvaluationRejected):
        evaluate_labeled_choices([], enabled=enabled, family_breakdown=bad)


def test_family_rows_are_deterministic_under_permutation():
    cases = [
        observation("f2", "d1"),
        observation("f1", "d2", provider="other-provider"),
        observation("f1", "d3"),
        observation("f2", "d4", scene_id="battle.wild"),
    ]

    expected = evaluate_labeled_choices(
        cases, enabled=True, family_breakdown=True
    )["family_strata"]
    for order in permutations(cases):
        report = evaluate_labeled_choices(
            list(order), enabled=True, family_breakdown=True
        )
        assert report["family_strata"] == expected


def test_existing_case_cap_and_family_limits_are_unchanged():
    def bounded_cases():
        for index in range(1001):
            yield observation("f1", f"d{index}")

    with pytest.raises(EvaluationRejected):
        evaluate_labeled_choices(bounded_cases(), enabled=True, family_breakdown=True)
    with pytest.raises(EvaluationRejected):
        evaluate_labeled_choices(
            [observation("f" * 129, "d1")], enabled=True, family_breakdown=True
        )
