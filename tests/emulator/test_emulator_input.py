"""Actor input guards, adapted from AIMD Labs pokegent-archive (Nous Research MIT).

492b71b1019bc92db299a20eab6ca7b22724c5c6:tests/emulator/test_emulator_input.py
and test_emulator_actor.py; notices retained in LICENSE. Synthetic input only.
"""

from unittest.mock import Mock, call

import pytest

from pokegent.emulator import (
    GBA_EWRAM,
    InputRejected,
    normalize_buttons,
    validate_chord_whitelist,
)


@pytest.mark.parametrize("buttons", [
    "turbo", "", "a b", "up\n", None, 1, True, b"a", bytearray(b"a"), [],
    {"a": 1}, ["a", 1], [["a"]], ["a", "A"], ["l", "r"], ["up", "down"],
    ["left", "right"], ["a", "b", "start", "select"], ["a", "b", "start", "select", "l"],
])
def test_actor_rejects_buttons_before_queueing(actor, fake, monkeypatch, buttons):
    assert_rejected_without_side_effects(
        actor, fake, monkeypatch, lambda: actor.tap(buttons), InputRejected
    )


@pytest.mark.parametrize("hold,wait", [
    (0, 0), (-1, 0), (601, 0), (True, 0), (1.5, 0), ("2", 0), (None, 0),
    (1, -1), (1, 601), (1, True), (1, 1.5), (1, "2"), (1, None),
])
def test_actor_rejects_durations_before_queueing(actor, fake, monkeypatch, hold, wait):
    assert_rejected_without_side_effects(
        actor, fake, monkeypatch, lambda: actor.tap("a", hold, wait), ValueError
    )


def assert_rejected_without_side_effects(actor, fake, monkeypatch, action, error):
    before = actor.read_atomic_snapshot()
    digest = before.ram_digest
    submit = Mock(wraps=actor._clock.submit_press)
    monkeypatch.setattr(actor._clock, "submit_press", submit)
    with pytest.raises(error) as caught:
        action()
    assert type(caught.value) is error
    # The backend also validates: these spies detect an actor guard bypass even
    # when its backend would raise the same error without changing RAM.
    submit.assert_not_called()
    fake.button_down.assert_not_called()
    fake.press_buttons.assert_not_called()
    fake.tick.assert_not_called()
    after = actor.read_atomic_snapshot()
    assert dict(after.regions) == dict(before.regions)
    assert after.ram_digest == digest
    assert after.frame == before.frame == fake.frame_count == fake.frames_run == 0
    assert after.keys_held == before.keys_held == fake.pressed_keys() == frozenset()


def test_valid_tap_and_whitelisted_chord_reach_backend_and_release_keys(actor, fake):
    before = actor.read_atomic_snapshot()
    assert actor.tap("A", 3, 2) == 5
    assert actor.hold(["right", "b"], 2) == 7
    after = actor.read_atomic_snapshot()
    assert after.read_u32(GBA_EWRAM.base + 4) == 3
    assert after.read_u32(GBA_EWRAM.base) == fake.frame_count == fake.frames_run == 7
    assert after.ram_digest != before.ram_digest
    assert after.keys_held == fake.pressed_keys() == frozenset()
    assert fake.button_down.call_args_list == [call(("a",)), call(("b", "right"))]
    assert fake.tick.call_count == 7


@pytest.mark.parametrize("chord,reason", [
    (("up", "down"), "opposite"), (("left", "right", "b"), "opposite"),
    (("a", "b", "start", "select"), "soft-reset"),
    (("a", "b", "start", "select", "l"), "soft-reset"),
])
def test_dangerous_chords_refused_even_explicitly_whitelisted(chord, reason):
    with pytest.raises(ValueError, match=reason) as caught:
        validate_chord_whitelist([chord])
    assert type(caught.value) is ValueError
    with pytest.raises(InputRejected, match=reason):
        normalize_buttons(chord, chords=frozenset({frozenset(chord)}))


def test_pure_whitelist_accepts_safe_chord_and_rejects_bad_entries():
    custom = validate_chord_whitelist([("l", "r")])
    assert normalize_buttons(["r", "L"], chords=custom) == ("l", "r")
    for chord, reason in ((["a"], "at least two"), (["a", "turbo"], "unknown")):
        with pytest.raises(ValueError, match=reason):
            validate_chord_whitelist([chord])
