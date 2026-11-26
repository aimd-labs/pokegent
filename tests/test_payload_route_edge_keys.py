"""#178: a route edge that is not an exposed option must not trip the payload-leak guard."""

import pytest

from pokegent.options.models import (
    DecisionFrame,
    Legality,
    Option,
    PayloadLeakError,
    assert_payload_clean,
    fingerprint,
    to_system_one_payload,
)


def _option(option_id: str, **extra) -> Option:
    return Option(
        id=option_id,
        label="Travel to the connected map",
        description="Travel to the connected map.",
        legality=Legality.LEGAL,
        executor_ref="field.edge",
        executor_args={"edge": option_id, **extra},
        postcondition_ref="pc.edge",
    )


def _frame(route: list[str], options: tuple[Option, ...] | None = None) -> DecisionFrame:
    if options is None:
        options = (_option("edge:E:3:25"), _option("edge:W:3:34"))
    return DecisionFrame(
        run_id="synthetic",
        decision_id="dec-178",
        snapshot_seq=1,
        scene_id="field.free",
        scene_confidence=1.0,
        state_fingerprint=fingerprint("field.free"),
        options=options,
        context={"navigation": {"route": route}},
    )


def test_route_edge_not_in_options_is_relabeled_not_leaked():
    frame = _frame(["walk east", "edge:E:3:6"])
    payload = to_system_one_payload(frame)
    assert_payload_clean(payload, frame)
    assert "edge:E:3:6" not in str(payload)
    assert set(payload["criteria"]) == {"edge:E:3:25", "edge:W:3:34"}


def test_route_edge_that_is_an_option_is_kept():
    frame = _frame(["edge:E:3:25"])
    assert to_system_one_payload(frame)["state"]["navigation"]["route"] == ["edge:E:3:25"]


def test_guard_still_rejects_unsanitized_edge_key():
    frame = _frame(["edge:E:3:6"])
    payload = to_system_one_payload(_frame(["walk east"]))
    payload["state"]["navigation"] = {"route": ["edge:E:3:6"]}
    with pytest.raises(PayloadLeakError):
        assert_payload_clean(payload, frame)


def test_route_warp_not_in_options_is_relabeled_not_leaked():
    frame = _frame(["walk east", "warp:3:6:1"])
    payload = to_system_one_payload(frame)
    assert_payload_clean(payload, frame)
    assert "warp:3:6:1" not in str(payload)
    assert set(payload["criteria"]) == {"edge:E:3:25", "edge:W:3:34"}


def test_route_warp_that_is_an_option_is_kept():
    frame = _frame(["warp:3:6:1"], options=(_option("warp:3:6:1"),))
    assert to_system_one_payload(frame)["state"]["navigation"]["route"] == ["warp:3:6:1"]


def test_guard_still_rejects_unsanitized_warp_key():
    frame = _frame(["warp:3:6:1"])
    payload = to_system_one_payload(_frame(["walk east"]))
    payload["state"]["navigation"] = {"route": ["warp:3:6:1"]}
    with pytest.raises(PayloadLeakError):
        assert_payload_clean(payload, frame)
