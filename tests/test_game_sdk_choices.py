"""Synthetic contract tests for the opt-in, frame-bound legal-choice SDK facade.

No ROM, app, provider, backend, executor, network or FireRed type is involved.
"""

from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError, dataclass

import pytest

from pokegent.games.observation import create_observation
from pokegent.games.plugin import GamePluginDescriptor, ReviewedPluginRegistry
from pokegent.options.models import ChoiceResult, DecisionFrame, Legality, Option
from pokegent.sdk import ChoiceOffer, ChoiceSdk, ChoiceSdkRejected

ROM_A = b"synthetic-sdk-rom-alpha" * 8
ROM_B = b"synthetic-sdk-rom-bravo" * 8


@dataclass
class _Services:
    """Stands in for plugin-owned game services; the SDK must never call them."""

    presses: int = 0

    def press(self) -> None:
        self.presses += 1


@dataclass
class _Plugin:
    descriptor: GamePluginDescriptor
    services: _Services

    def compose(self) -> _Services:
        return self.services


def _selected(game="synthetic-alpha", profile="profile-alpha", data=ROM_A):
    descriptor = GamePluginDescriptor(
        plugin_id=f"{game}:{profile}",
        game=game,
        profile_id=profile,
        contract_version=1,
        rom_sha1=hashlib.sha1(data).hexdigest(),
        capabilities=frozenset({"observation", "choice"}),
    )
    plugin = _Plugin(descriptor, _Services())
    registry = ReviewedPluginRegistry(enabled=True)
    registry.register(plugin)
    selection = registry.select(rom_bytes=data, game=game, profile_id=profile)
    return selection, plugin


def _open(game="synthetic-alpha", profile="profile-alpha", data=ROM_A):
    selection, plugin = _selected(game, profile, data)
    return ChoiceSdk(selection, enabled=True), plugin


def _observation(
    game="synthetic-alpha",
    profile="profile-alpha",
    *,
    seq=0,
    epoch=0,
    usable=True,
):
    return create_observation(
        enabled=True,
        game_id=game,
        profile_id=profile,
        snapshot_seq=seq,
        restore_epoch=epoch,
        usable=usable,
        payload={"scene": "synthetic:1"},
    )


def _option(option_id, label, legality=Legality.LEGAL):
    return Option(
        id=option_id,
        label=label,
        legality=legality,
        executor_ref="test.action" if legality.chooseable else None,
    )


def _frame(
    seq,
    *,
    decision_id="dec-1",
    fingerprint="0",
    options=(("sign", "Sign"), ("walk", "Walk")),
    diagnostics=(),
    all_options=(),
):
    return DecisionFrame(
        decision_id=decision_id,
        run_id="synthetic-sdk",
        snapshot_seq=seq,
        scene_id="field.free",
        scene_confidence=1.0,
        state_fingerprint="sha256:" + fingerprint * 64,
        options=tuple(_option(key, label) for key, label in options),
        diagnostics=tuple(diagnostics),
        all_options=tuple(all_options),
    )


def _result(option_id="sign", decision_id="dec-1", **kwargs):
    return ChoiceResult(option_id=option_id, decision_id=decision_id, **kwargs)


def test_facade_is_disabled_by_default_and_requires_exact_bool():
    selection, _ = _selected()
    disabled = ChoiceSdk(selection)
    with pytest.raises(ChoiceSdkRejected):
        disabled.observe(_observation(seq=1))
    with pytest.raises(ChoiceSdkRejected):
        disabled.offer(_frame(1))
    with pytest.raises(ChoiceSdkRejected):
        disabled.submit(_result())

    for not_a_bool in (1, 0, "true", None):
        with pytest.raises(TypeError):
            ChoiceSdk(selection, enabled=not_a_bool)


def test_constructor_requires_an_already_selected_plugin():
    with pytest.raises(TypeError):
        ChoiceSdk(object(), enabled=True)


@pytest.mark.parametrize(
    ("game", "profile", "data", "fingerprint"),
    [
        ("synthetic-alpha", "profile-alpha", ROM_A, "a"),
        ("synthetic-bravo", "profile-bravo", ROM_B, "b"),
    ],
)
def test_two_synthetic_plugins_offer_and_validate_a_choice(game, profile, data, fingerprint):
    sdk, plugin = _open(game, profile, data)
    sdk.observe(_observation(game, profile, seq=4, epoch=2))
    offer = sdk.offer(_frame(4, decision_id=f"dec-{game}", fingerprint=fingerprint))

    assert isinstance(offer, ChoiceOffer)
    assert offer.decision_id == f"dec-{game}"
    assert offer.snapshot_seq == 4
    assert offer.restore_epoch == 2
    assert tuple((option.option_id, option.label) for option in offer.options) == (
        ("sign", "Sign"),
        ("walk", "Walk"),
    )
    result = _result("walk", f"dec-{game}", probabilities={"walk": 1.0})
    assert sdk.submit(result) is result
    assert plugin.services.presses == 0


def test_offer_requires_a_current_observation():
    sdk, _ = _open()

    with pytest.raises(ChoiceSdkRejected):
        sdk.offer(_frame(0))


def test_mismatched_observation_identity_is_rejected():
    sdk, _ = _open()

    with pytest.raises(ChoiceSdkRejected):
        sdk.observe(_observation("synthetic-bravo", "profile-alpha", seq=1))
    with pytest.raises(ChoiceSdkRejected):
        sdk.observe(_observation("synthetic-alpha", "profile-bravo", seq=1))


def test_observation_sequence_must_advance_within_an_epoch():
    sdk, _ = _open()
    sdk.observe(_observation(seq=4, epoch=3))
    sdk.observe(_observation(seq=5, epoch=3))

    for stale in (5, 4):
        with pytest.raises(ChoiceSdkRejected):
            sdk.observe(_observation(seq=stale, epoch=3))
    with pytest.raises(ChoiceSdkRejected):
        sdk.observe(_observation(seq=99, epoch=2))


def test_unusable_observation_clears_pending_and_cannot_offer():
    sdk, _ = _open()
    sdk.observe(_observation(seq=1))
    sdk.offer(_frame(1))

    sdk.observe(_observation(seq=2, usable=False))

    with pytest.raises(ChoiceSdkRejected):
        sdk.offer(_frame(2))
    with pytest.raises(ChoiceSdkRejected):
        sdk.submit(_result())

    sdk.observe(_observation(seq=3))
    assert sdk.offer(_frame(3)).snapshot_seq == 3


def test_epoch_advance_clears_pending_and_sequence_reset_cannot_replay():
    sdk, _ = _open()
    sdk.observe(_observation(seq=3, epoch=1))
    sdk.offer(_frame(3, decision_id="old-frame"))
    old_result = _result(decision_id="old-frame")

    sdk.observe(_observation(seq=0, epoch=2))

    with pytest.raises(ChoiceSdkRejected):
        sdk.submit(old_result)

    fresh = sdk.offer(_frame(0, decision_id="new-frame"))
    assert fresh.restore_epoch == 2
    with pytest.raises(ChoiceSdkRejected):
        sdk.submit(old_result)
    sdk.offer(_frame(0, decision_id="new-frame"))
    assert sdk.submit(_result(decision_id="new-frame")).decision_id == "new-frame"


def test_stale_frame_and_non_plain_snapshot_sequence_are_rejected():
    sdk, _ = _open()
    sdk.observe(_observation(seq=2))

    with pytest.raises(ChoiceSdkRejected):
        sdk.offer(_frame(1))
    bool_sequence = _frame(2).model_copy(update={"snapshot_seq": True})
    with pytest.raises(ChoiceSdkRejected):
        sdk.offer(bool_sequence)


def test_offer_requires_nonempty_decision_id_and_fingerprint():
    sdk, _ = _open()
    sdk.observe(_observation(seq=2))

    with pytest.raises(ChoiceSdkRejected):
        sdk.offer(_frame(2, decision_id="   "))
    with pytest.raises(ChoiceSdkRejected):
        sdk.offer(_frame(2).model_copy(update={"state_fingerprint": " "}))


def test_offer_exposes_only_visible_chooseable_ids_and_labels():
    sdk, _ = _open()
    sdk.observe(_observation(seq=2))
    offer = sdk.offer(
        _frame(
            2,
            diagnostics=(_option("blocked", "Blocked", Legality.UNAVAILABLE),),
            all_options=(_option("filtered", "Filtered"),),
        )
    )

    assert tuple(option.option_id for option in offer.options) == ("sign", "walk")
    assert {field for field in offer.options[0].__dataclass_fields__} == {"option_id", "label"}
    for record in (offer, *offer.options):
        assert not hasattr(record, "executor_ref")
        assert not hasattr(record, "executor_args")
        assert not hasattr(record, "snapshot")
        assert not hasattr(record, "all_options")
    with pytest.raises(FrozenInstanceError):
        offer.options[0].label = "changed"


@pytest.mark.parametrize("decision_id", [None, "", "other-frame"])
def test_submission_requires_exact_pending_decision_id(decision_id):
    sdk, _ = _open()
    sdk.observe(_observation(seq=2))
    sdk.offer(_frame(2, decision_id="dec-1"))

    with pytest.raises(ChoiceSdkRejected):
        sdk.submit(_result(decision_id=decision_id))
    with pytest.raises(ChoiceSdkRejected):
        sdk.submit(_result())


def test_hidden_unavailable_and_malformed_probabilities_fail_closed():
    sdk, _ = _open()
    sdk.observe(_observation(seq=2))
    sdk.offer(
        _frame(
            2,
            diagnostics=(_option("blocked", "Blocked", Legality.UNAVAILABLE),),
            all_options=(_option("filtered", "Filtered"),),
        )
    )

    for bad in (
        _result("blocked"),
        _result("filtered"),
        _result("sign", probabilities={"filtered": 0.5}),
    ):
        with pytest.raises(ChoiceSdkRejected):
            sdk.submit(bad)
        sdk.offer(_frame(2))


def test_successful_submission_consumes_the_pending_frame():
    sdk, _ = _open()
    sdk.observe(_observation(seq=2))
    sdk.offer(_frame(2))
    accepted = _result("walk")

    assert sdk.submit(accepted) is accepted
    with pytest.raises(ChoiceSdkRejected):
        sdk.submit(accepted)
    with pytest.raises(ChoiceSdkRejected):
        sdk.submit(_result("walk"))


def test_successful_submission_consumes_the_observation_until_an_advance():
    sdk, _ = _open()
    sdk.observe(_observation(seq=2, epoch=1))
    same_frame = _frame(2)
    sdk.offer(same_frame)
    assert sdk.submit(_result("walk")).option_id == "walk"

    with pytest.raises(ChoiceSdkRejected):
        sdk.offer(same_frame)
    with pytest.raises(ChoiceSdkRejected):
        sdk.offer(_frame(2, decision_id="dec-2"))

    sdk.observe(_observation(seq=3, epoch=1))
    assert sdk.offer(_frame(3, decision_id="dec-3")).decision_id == "dec-3"
    assert sdk.submit(_result("sign", "dec-3")).decision_id == "dec-3"
    with pytest.raises(ChoiceSdkRejected):
        sdk.offer(_frame(3, decision_id="dec-4"))

    sdk.observe(_observation(seq=0, epoch=2))
    assert sdk.offer(_frame(0, decision_id="dec-5")).restore_epoch == 2
