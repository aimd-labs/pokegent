"""Synthetic tests for the opt-in generic observation envelope.

No ROM, app, provider, native backend or FireRed type is involved.
"""

from dataclasses import FrozenInstanceError

import pytest

from pokegent.games.observation import (
    MAX_ID_LENGTH,
    MAX_KEY_LENGTH,
    MAX_PAYLOAD_FIELDS,
    MAX_STRING_LENGTH,
    SCHEMA_VERSION,
    GameObservation,
    ObservationRejected,
    TransitionRejected,
    create_observation,
    validate_transition,
)


def make(
    game_id="synthetic-alpha",
    profile_id="profile-alpha",
    *,
    seq=0,
    epoch=0,
    usable=True,
    payload=None,
    enabled=True,
):
    return create_observation(
        enabled=enabled,
        game_id=game_id,
        profile_id=profile_id,
        snapshot_seq=seq,
        restore_epoch=epoch,
        usable=usable,
        payload={} if payload is None else payload,
    )


def test_factory_is_off_by_default_and_refuses_without_opt_in():
    with pytest.raises(ObservationRejected):
        create_observation(
            game_id="synthetic-alpha",
            profile_id="profile-alpha",
            snapshot_seq=0,
            restore_epoch=0,
            usable=True,
            payload={},
        )

    with pytest.raises(ObservationRejected):
        make(enabled=False)

    for not_a_bool in (1, 0, "true", None):
        with pytest.raises(ObservationRejected):
            make(enabled=not_a_bool)


@pytest.mark.parametrize(
    ("game_id", "profile_id"),
    [
        ("synthetic-alpha", "profile-alpha"),
        ("synthetic-beta", "profile-beta"),
    ],
)
def test_two_synthetic_games_share_the_same_valid_api(game_id, profile_id):
    observation = make(
        game_id,
        profile_id,
        seq=7,
        epoch=2,
        payload={"map": "synthetic:3", "score": 12, "active": True, "ratio": 0.5, "none": None},
    )

    assert isinstance(observation, GameObservation)
    assert observation.schema_version == SCHEMA_VERSION
    assert observation.game_id == game_id
    assert observation.profile_id == profile_id
    assert observation.snapshot_seq == 7
    assert observation.restore_epoch == 2
    assert observation.usable is True
    assert dict(observation.payload) == {
        "map": "synthetic:3",
        "score": 12,
        "active": True,
        "ratio": 0.5,
        "none": None,
    }


def test_record_and_payload_are_immutable_and_copy_caller_data():
    source = {"map": "synthetic:1", "score": 4}
    observation = make(payload=source)

    source["map"] = "synthetic:2"
    source["score"] = 99

    assert dict(observation.payload) == {"map": "synthetic:1", "score": 4}
    with pytest.raises(TypeError):
        observation.payload["score"] = 1
    with pytest.raises(FrozenInstanceError):
        observation.usable = False


@pytest.mark.parametrize("field", ["game_id", "profile_id"])
@pytest.mark.parametrize(
    "value",
    ["", "   ", b"bytes", 3, True, None, "x" * (MAX_ID_LENGTH + 1)],
)
def test_identities_are_nonempty_bounded_strings(field, value):
    kwargs = {"game_id": "synthetic-alpha", "profile_id": "profile-alpha"}
    kwargs[field] = value
    with pytest.raises(ObservationRejected):
        make(**kwargs)


@pytest.mark.parametrize("field", ["seq", "epoch"])
@pytest.mark.parametrize("value", [True, False, -1, 1.0, "1", None])
def test_counters_are_plain_non_negative_ints(field, value):
    kwargs = {"seq": 0, "epoch": 0}
    kwargs[field] = value
    with pytest.raises(ObservationRejected):
        make(**kwargs)


@pytest.mark.parametrize("value", [0, 1, "true", None, []])
def test_usable_is_an_exact_bool(value):
    with pytest.raises(ObservationRejected):
        make(usable=value)


@pytest.mark.parametrize("value", [0, True, SCHEMA_VERSION + 1, "1", None])
def test_schema_version_is_exact(value):
    with pytest.raises(ObservationRejected):
        create_observation(
            enabled=True,
            game_id="synthetic-alpha",
            profile_id="profile-alpha",
            snapshot_seq=0,
            restore_epoch=0,
            usable=True,
            payload={},
            schema_version=value,
        )


@pytest.mark.parametrize(
    "payload",
    [
        [],
        (),
        b"bytes",
        {"": 1},
        {1: 1},
        {b"key": 1},
        {True: 1},
        {"key": object()},
        {"key": b"bytes"},
        {"key": bytearray(b"bytes")},
        {"key": [1]},
        {"key": {"nested": 1}},
        {"key": (1, 2)},
        {"key": float("nan")},
        {"key": float("inf")},
        {"key": float("-inf")},
        {"k" * (MAX_KEY_LENGTH + 1): 1},
        {"key": "x" * (MAX_STRING_LENGTH + 1)},
        {f"k{index}": index for index in range(MAX_PAYLOAD_FIELDS + 1)},
    ],
)
def test_payload_rejects_non_flat_or_unbounded_values(payload):
    with pytest.raises(ObservationRejected):
        make(payload=payload)


def test_exact_bounds_are_accepted():
    observation = make(
        "g" * MAX_ID_LENGTH,
        "p" * MAX_ID_LENGTH,
        payload={("k" * MAX_KEY_LENGTH): "s" * MAX_STRING_LENGTH},
    )

    assert len(observation.game_id) == MAX_ID_LENGTH
    assert len(observation.profile_id) == MAX_ID_LENGTH
    assert len(observation.payload) == 1


def test_payload_accepts_the_maximum_field_count():
    payload = {f"k{index}": index for index in range(MAX_PAYLOAD_FIELDS)}

    assert len(make(payload=payload).payload) == MAX_PAYLOAD_FIELDS


def test_same_epoch_sequence_must_strictly_increase():
    previous = make(seq=4, epoch=3)
    newer = make(seq=5, epoch=3)

    assert validate_transition(previous, newer) == "sequence_advanced"
    for stale_seq in (4, 3):
        with pytest.raises(TransitionRejected):
            validate_transition(previous, make(seq=stale_seq, epoch=3))


def test_cross_identity_and_epoch_rollback_are_rejected():
    previous = make("synthetic-alpha", "profile-alpha", seq=4, epoch=3)

    with pytest.raises(TransitionRejected):
        validate_transition(previous, make("synthetic-beta", "profile-alpha", seq=5, epoch=3))
    with pytest.raises(TransitionRejected):
        validate_transition(previous, make("synthetic-alpha", "profile-beta", seq=5, epoch=3))
    with pytest.raises(TransitionRejected):
        validate_transition(previous, make(seq=99, epoch=2))


def test_increased_epoch_allows_sequence_reset():
    previous = make(seq=4, epoch=3)

    assert validate_transition(previous, make(seq=0, epoch=4)) == "epoch_advanced"
    assert validate_transition(previous, make(seq=4, epoch=4)) == "epoch_advanced"


def test_unusable_stays_unknown_and_never_becomes_ready():
    unknown = make(seq=9, usable=False, payload={"known": "nothing"})

    assert unknown.usable is False
    assert dict(unknown.payload) == {"known": "nothing"}
    with pytest.raises(ObservationRejected):
        unknown.require_usable()

    assert make(usable=True).require_usable() is not None
