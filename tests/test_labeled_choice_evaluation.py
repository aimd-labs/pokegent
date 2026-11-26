"""Default-off labeled-choice evaluation API (#134): synthetic frames only, no provider."""

from __future__ import annotations

import json

import pytest

from addons.system_one.evaluation import (
    EvaluationContext,
    EvaluationRejected,
    LabeledChoiceCase,
    evaluate_labeled_choices,
)
from pokegent.options.models import ChoiceResult, DecisionFrame, Legality, Option

_NO_RESULT = object()
_CONTEXT = {
    "game_profile": "firered-us-1.0",
    "backend_version": "mgba-0.10.3",
    "provider_protocol": "openai-compatible/1",
    "prompt_profile": "scene-v3",
    "knowledge_policy": "game-facts-only",
}


def context(**overrides):
    return EvaluationContext(**{**_CONTEXT, **overrides})


def make_frame(decision_id="dec-1", *, run_id="run-1", scene_id="field.free", hidden=False):
    options = (
        Option(id="tap:a", label="A", legality=Legality.LEGAL, executor_ref="test.action"),
        Option(id="tap:b", label="B", legality=Legality.LEGAL, executor_ref="test.action"),
    )
    extra = {}
    if hidden:
        extra["all_options"] = (
            Option(
                id="tap:hidden",
                label="Hidden",
                legality=Legality.LEGAL,
                executor_ref="test.action",
            ),
        )
    return DecisionFrame(
        decision_id=decision_id,
        run_id=run_id,
        snapshot_seq=1,
        scene_id=scene_id,
        scene_confidence=1.0,
        state_fingerprint="sha256:" + "0" * 64,
        options=options,
        **extra,
    )


def make_result(option_id="tap:a", *, decision_id="dec-1", provider="system1", model="model-1"):
    return ChoiceResult(
        option_id=option_id,
        decision_id=decision_id,
        provider_id=provider,
        model_id=model,
    )


def case(
    decision_id="dec-1",
    *,
    option_id="tap:a",
    labels=frozenset({"tap:a"}),
    family="fam-1",
    run_id="run-1",
    scene_id="field.free",
    provider="system1",
    model="model-1",
    result_decision_id=_NO_RESULT,
    no_result=False,
    hidden=False,
    context=None,
):
    frame = make_frame(decision_id, run_id=run_id, scene_id=scene_id, hidden=hidden)
    if no_result:
        result = None
    else:
        bound_id = decision_id if result_decision_id is _NO_RESULT else result_decision_id
        result = make_result(
            option_id, decision_id=bound_id, provider=provider, model=model
        )
    return LabeledChoiceCase(
        frame=frame,
        result=result,
        acceptable_option_ids=labels,
        family_id=family,
        context=context,
    )


def test_balanced_denominators_and_unknown_labels_do_not_change_rates():
    cases = [
        case("d1", option_id="tap:a", labels=frozenset({"tap:a", "tap:b"}), family="f1"),
        case("d2", option_id="tap:b", labels=frozenset({"tap:a"}), family="f1"),
        case("d3", option_id="tap:missing", labels=frozenset({"tap:a"}), family="f2"),
        case("d4", no_result=True, labels=frozenset({"tap:a"}), family="f2"),
        case("d5", option_id="tap:a", labels=None, family="f3"),
        case("d6", option_id="tap:b", labels=None, family="f3"),
    ]

    report = evaluate_labeled_choices(cases, enabled=True)

    counts = report["counts"]
    assert counts == {
        "supplied": 6,
        "labeled": 4,
        "unknown_label": 2,
        "no_prediction": 1,
        "invalid_choice": 1,
        "valid_choice": 4,
        "labeled_valid_choice": 2,
        "acceptable_choice": 1,
    }
    assert report["rates"]["acceptable_choice_rate"] == 0.5
    assert report["rates"]["labeled_case_success_rate"] == 0.25
    assert "labeled_valid_choice" in report["rates"]["acceptable_choice_rate_denominator"]
    assert "labeled_cases" in report["rates"]["labeled_case_success_rate_denominator"]
    assert report["supplied_family_count"] == 3
    assert report["labeled_family_count"] == 2


def test_wrong_legal_result_is_valid_but_illegal_or_hidden_is_invalid():
    wrong = evaluate_labeled_choices(
        [case("d1", option_id="tap:b", labels=frozenset({"tap:a"}))], enabled=True
    )
    assert wrong["counts"]["valid_choice"] == 1
    assert wrong["counts"]["invalid_choice"] == 0
    assert wrong["counts"]["acceptable_choice"] == 0
    assert wrong["rates"]["acceptable_choice_rate"] == 0.0

    illegal = evaluate_labeled_choices(
        [case("d2", option_id="tap:missing", labels=frozenset({"tap:a"}))], enabled=True
    )
    assert illegal["counts"]["valid_choice"] == 0
    assert illegal["counts"]["invalid_choice"] == 1
    assert illegal["rates"]["acceptable_choice_rate"] is None

    hidden = evaluate_labeled_choices(
        [case("d3", option_id="tap:hidden", labels=frozenset({"tap:a"}), hidden=True)],
        enabled=True,
    )
    assert hidden["counts"]["invalid_choice"] == 1
    assert hidden["counts"]["valid_choice"] == 0


def test_stale_missing_and_no_prediction_are_distinct():
    cases = [
        case("d1", result_decision_id="dec-old", labels=frozenset({"tap:a"})),
        case("d2", result_decision_id=None, labels=frozenset({"tap:a"})),
        case("d3", no_result=True, labels=frozenset({"tap:a"})),
    ]

    report = evaluate_labeled_choices(cases, enabled=True)

    assert report["counts"]["invalid_choice"] == 2
    assert report["counts"]["no_prediction"] == 1
    assert report["counts"]["valid_choice"] == 0
    assert report["rates"]["acceptable_choice_rate"] is None
    assert report["rates"]["labeled_case_success_rate"] == 0.0


def test_hidden_probability_key_is_invalid():
    result = ChoiceResult(
        option_id="tap:a",
        probabilities={"tap:a": 0.8, "tap:hidden": 0.2},
        decision_id="d1",
    )
    case_item = LabeledChoiceCase(make_frame("d1"), result, frozenset({"tap:a"}), "f1")

    report = evaluate_labeled_choices([case_item], enabled=True)

    assert report["counts"]["invalid_choice"] == 1
    assert report["counts"]["valid_choice"] == 0


@pytest.mark.parametrize(
    "labels",
    [
        frozenset({"tap:unknown"}),
        frozenset(),
        {"tap:a"},
        frozenset(f"tap:{index}" for index in range(257)),
    ],
)
def test_bad_label_containers_are_rejected(labels):
    with pytest.raises(EvaluationRejected):
        evaluate_labeled_choices([case("d1", labels=labels)], enabled=True)


def test_hidden_label_id_is_rejected_not_scored_wrong():
    with pytest.raises(EvaluationRejected):
        evaluate_labeled_choices(
            [case("d1", labels=frozenset({"tap:hidden"}), hidden=True)], enabled=True
        )


def test_repeated_family_is_counted_once_while_cases_remain():
    cases = [
        case("d1", family="same-family"),
        case("d2", family="same-family"),
        case("d3", family="other-family", labels=None),
    ]

    report = evaluate_labeled_choices(cases, enabled=True)

    assert report["counts"]["supplied"] == 3
    assert report["counts"]["labeled"] == 2
    assert report["supplied_family_count"] == 2
    assert report["labeled_family_count"] == 1


def test_duplicate_observation_rejected_and_another_model_grouped_separately():
    first = case("d1", model="model-1")
    with pytest.raises(EvaluationRejected):
        evaluate_labeled_choices([first, first], enabled=True)

    report = evaluate_labeled_choices(
        [first, case("d1", model="model-2")], enabled=True
    )
    assert report["counts"]["supplied"] == 2
    assert len(report["strata"]) == 2
    assert {row["model"] for row in report["strata"]} == {"model-1", "model-2"}


def test_zero_denominators_are_none():
    report = evaluate_labeled_choices([case("d1", labels=None)], enabled=True)

    assert report["counts"]["labeled"] == 0
    assert report["rates"]["acceptable_choice_rate"] is None
    assert report["rates"]["labeled_case_success_rate"] is None


def test_default_off_is_inert_and_does_not_consume_cases():
    class NotIterable:
        def __iter__(self):
            raise AssertionError("disabled evaluation must not consume input")

    report = evaluate_labeled_choices(NotIterable())

    assert report["enabled"] is False
    assert report["evaluated"] is False
    assert report["eligibility"] == "not_evaluated"
    assert report["authority_granted"] is False


@pytest.mark.parametrize("bad", [1, 0, "true", None])
def test_nonbool_opt_in_is_rejected(bad):
    with pytest.raises(EvaluationRejected):
        evaluate_labeled_choices([], enabled=bad)


def test_case_cap_is_bounded_before_materialization():
    consumed = 0

    def bounded_cases():
        nonlocal consumed
        for index in range(2000):
            consumed += 1
            yield case(f"d{index}")

    with pytest.raises(EvaluationRejected):
        evaluate_labeled_choices(bounded_cases(), enabled=True)

    assert consumed == 1001


def test_output_is_plain_jsonable_and_omits_case_identity_and_payloads():
    report = evaluate_labeled_choices(
        [case("dec-secret", run_id="run-secret", family="family-secret")], enabled=True
    )

    raw = json.dumps(report, sort_keys=True, allow_nan=False)
    for secret in ("dec-secret", "run-secret", "family-secret", "tap:a", "executor"):
        assert secret not in raw
    for field in ("decision_id", "run_id", "option_id", "raw_response_ref"):
        assert field not in report
        assert field not in raw
    assert report["eligibility"] == "not_evaluated"
    assert report["authority_granted"] is False
    assert report["limits"]["labels"] == "caller_supplied_not_independent_certification"
    assert report["limits"]["family_grouping"] == "supplied_not_independent_certification"
    assert report["limits"]["no_prediction_identity"] == "unknown_provider_and_model"


def test_exact_string_identity_is_required():
    class StrSubclass(str):
        pass

    with pytest.raises(EvaluationRejected):
        evaluate_labeled_choices(
            [case("d1", family=StrSubclass("family"))], enabled=True
        )
    with pytest.raises(EvaluationRejected):
        evaluate_labeled_choices(
            [case("d1", labels=frozenset({StrSubclass("tap:a")}))], enabled=True
        )


def test_provider_and_model_identity_is_bounded_and_empty_is_unknown():
    report = evaluate_labeled_choices([case("d1", provider="", model="")], enabled=True)
    row = report["strata"][0]
    assert row["provider"] is None
    assert row["model"] is None

    with pytest.raises(EvaluationRejected):
        evaluate_labeled_choices([case("d2", provider="p" * 129)], enabled=True)
    with pytest.raises(EvaluationRejected):
        evaluate_labeled_choices([case("d3", model="m" * 129)], enabled=True)

    class StrSubclass(str):
        pass

    exact = make_result(decision_id="d4").model_copy(
        update={"provider_id": StrSubclass("system1")}
    )
    with pytest.raises(EvaluationRejected):
        evaluate_labeled_choices(
            [LabeledChoiceCase(make_frame("d4"), exact, frozenset({"tap:a"}), "f1")],
            enabled=True,
        )

    empty_subclass = make_result(decision_id="d5").model_copy(
        update={"provider_id": StrSubclass("")}
    )
    with pytest.raises(EvaluationRejected):
        evaluate_labeled_choices(
            [LabeledChoiceCase(make_frame("d5"), empty_subclass, frozenset({"tap:a"}), "f1")],
            enabled=True,
        )

    with pytest.raises(EvaluationRejected):
        evaluate_labeled_choices([case("d6", provider="   ")], enabled=True)
    with pytest.raises(EvaluationRejected):
        evaluate_labeled_choices([case("d7", model="\t")], enabled=True)


def test_malformed_case_and_identity_are_rejected():
    with pytest.raises(EvaluationRejected):
        evaluate_labeled_choices([object()], enabled=True)
    with pytest.raises(EvaluationRejected):
        evaluate_labeled_choices([case("d1", family="  ")], enabled=True)
    with pytest.raises(EvaluationRejected):
        evaluate_labeled_choices([case("d1", family="f" * 129)], enabled=True)
    with pytest.raises(EvaluationRejected):
        evaluate_labeled_choices([case("d1", run_id="")], enabled=True)


@pytest.mark.parametrize("tag", sorted(_CONTEXT))
def test_differing_single_context_tag_forms_separate_strata(tag):
    report = evaluate_labeled_choices(
        [case("d1", context=context()), case("d2", context=context(**{tag: "other"}))],
        enabled=True,
    )

    assert report["counts"]["supplied"] == 2
    assert len(report["strata"]) == 2
    contexts = [row["comparison_context"] for row in report["strata"]]
    assert all(isinstance(item, dict) for item in contexts)
    assert {item[tag] for item in contexts} == {_CONTEXT[tag], "other"}
    assert all(row["supplied"] == 1 for row in report["strata"])


def test_same_context_shares_one_stratum_with_exact_counts():
    report = evaluate_labeled_choices(
        [case("d1", context=context()), case("d2", context=context())], enabled=True
    )

    assert len(report["strata"]) == 1
    row = report["strata"][0]
    assert row["supplied"] == 2
    assert row["labeled"] == 2
    assert row["comparison_context"] == _CONTEXT
    assert report["counts"]["supplied"] == 2


def test_unknown_context_is_distinct_from_known_context():
    report = evaluate_labeled_choices(
        [case("d1"), case("d2", context=context())], enabled=True
    )

    assert len(report["strata"]) == 2
    by_unknown = {row["comparison_context"] is None: row for row in report["strata"]}
    assert by_unknown[True]["supplied"] == 1
    assert by_unknown[False]["comparison_context"] == _CONTEXT


def test_absent_context_keeps_positional_signature_and_none_bucket():
    item = LabeledChoiceCase(
        make_frame("d1"), make_result(), frozenset({"tap:a"}), "f1"
    )

    report = evaluate_labeled_choices([item], enabled=True)

    row = report["strata"][0]
    assert row["scene_id"] == "field.free"
    assert row["provider"] == "system1"
    assert row["model"] == "model-1"
    assert row["comparison_context"] is None
    assert report["counts"]["supplied"] == 1


def test_duplicate_observation_rejected_even_when_context_differs():
    first = case("d1", context=context())
    relabeled = case("d1", context=context(prompt_profile="other"))

    with pytest.raises(EvaluationRejected):
        evaluate_labeled_choices([first, relabeled], enabled=True)


@pytest.mark.parametrize("tag", sorted(_CONTEXT))
@pytest.mark.parametrize("bad", [True, "", "   ", "x" * 129])
def test_malformed_context_tags_rejected_without_echo(tag, bad):
    with pytest.raises(EvaluationRejected) as excinfo:
        context(**{tag: bad})

    message = str(excinfo.value)
    assert "context." + tag in message
    if str(bad):
        assert str(bad) not in message


@pytest.mark.parametrize("bad", ["not-a-context", {}, object(), 3])
def test_non_context_value_is_rejected(bad):
    item = LabeledChoiceCase(
        make_frame("d1"), make_result(), frozenset({"tap:a"}), "f1", bad
    )

    with pytest.raises(EvaluationRejected):
        evaluate_labeled_choices([item], enabled=True)


def test_context_rows_are_plain_jsonable_and_limits_are_explicit():
    report = evaluate_labeled_choices(
        [case("dec-secret", context=context())], enabled=True
    )

    raw = json.dumps(report, sort_keys=True, allow_nan=False)
    assert "dec-secret" not in raw
    assert report["strata"][0]["comparison_context"] == _CONTEXT
    assert report["limits"]["comparison_context"] == "caller_supplied_not_runtime_verified"
    assert report["limits"]["unknown_context"] == "not_inferred_from_model_or_frame"
    assert report["eligibility"] == "not_evaluated"
    assert report["authority_granted"] is False

