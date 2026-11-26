"""Deterministic concurrency regressions for the local choice SDK.

Synthetic observations/frames only; only ``check_choice`` is monkeypatched to hold validation.
"""

from __future__ import annotations

import hashlib
import threading

import pytest

import pokegent.sdk as sdk_module
from pokegent.games.observation import create_observation
from pokegent.games.plugin import GamePluginDescriptor, ReviewedPluginRegistry
from pokegent.options.models import ChoiceResult, DecisionFrame, Legality, Option
from pokegent.sdk import ChoiceSdk, ChoiceSdkRejected

GAME, PROFILE = "synthetic-alpha", "profile-alpha"
ROM = b"synthetic-sdk-concurrency-rom" * 8


class _Plugin:
    def __init__(self, descriptor):
        self.descriptor = descriptor

    def compose(self):
        return None


def _sdk():
    descriptor = GamePluginDescriptor(
        plugin_id=f"{GAME}:{PROFILE}", game=GAME, profile_id=PROFILE, contract_version=1,
        rom_sha1=hashlib.sha1(ROM).hexdigest(),
        capabilities=frozenset({"observation", "choice"}),
    )
    registry = ReviewedPluginRegistry(enabled=True)
    registry.register(_Plugin(descriptor))
    return ChoiceSdk(registry.select(rom_bytes=ROM, game=GAME, profile_id=PROFILE), enabled=True)


def _obs(seq):
    return create_observation(
        enabled=True, game_id=GAME, profile_id=PROFILE, snapshot_seq=seq,
        restore_epoch=0, usable=True, payload={"scene": "synthetic:1"},
    )


def _frame(seq, decision_id="dec-1"):
    return DecisionFrame(
        decision_id=decision_id, run_id="synthetic-sdk", snapshot_seq=seq,
        scene_id="field.free", scene_confidence=1.0,
        state_fingerprint="sha256:" + "0" * 64,
        options=(
            Option(id="walk", label="Walk", legality=Legality.LEGAL, executor_ref="test.action"),
        ),
    )


def _result(decision_id):
    return ChoiceResult(option_id="walk", decision_id=decision_id)


def _join(*threads):
    for thread in threads:
        if thread.ident is not None:
            thread.join(2.0)
    assert all(not thread.is_alive() for thread in threads)


def _assert_fresh_choice(sdk):
    assert sdk.offer(_frame(2, "dec-new")).decision_id == "dec-new"
    assert sdk.submit(_result("dec-new")).decision_id == "dec-new"
    with pytest.raises(ChoiceSdkRejected):
        sdk.submit(_result("dec-new"))


def test_submit_validation_and_newer_observe_linearize(monkeypatch):
    sdk = _sdk()
    sdk.observe(_obs(1))
    sdk.offer(_frame(1, "dec-old"))
    entered, release, done = (threading.Event() for _ in range(3))
    seen, real = {}, sdk_module.check_choice

    def delayed(frame, result, *, allow_all):
        entered.set()
        assert release.wait(2.0), "validation was never released"
        return real(frame, result, allow_all=allow_all)

    monkeypatch.setattr(sdk_module, "check_choice", delayed)

    def submit_old():
        seen["old"] = sdk.submit(_result("dec-old"))

    def observe_new():
        try:
            sdk.observe(_obs(2))
        finally:
            done.set()

    submitter = threading.Thread(target=submit_old, daemon=True)
    observer = threading.Thread(target=observe_new, daemon=True)
    try:
        submitter.start()
        assert entered.wait(2.0)
        observer.start()
        early = done.wait(1.0)
    finally:
        release.set()
        _join(submitter, observer)
    assert seen.get("old") is not None
    if early:
        with pytest.raises(ChoiceSdkRejected):
            sdk.offer(_frame(2, "dec-new"))
        pytest.fail("observe completed during submit validation and consumed the new observation")
    _assert_fresh_choice(sdk)


def test_concurrent_duplicate_submit_validates_exactly_once():
    sdk = _sdk()
    sdk.observe(_obs(1))
    sdk.offer(_frame(1))
    start, seen = threading.Barrier(2, timeout=2.0), []

    def attempt():
        start.wait()
        try:
            sdk.submit(_result("dec-1"))
            seen.append("validated")
        except ChoiceSdkRejected:
            seen.append("rejected")

    threads = [threading.Thread(target=attempt, daemon=True) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
    finally:
        _join(*threads)
    assert sorted(seen) == ["rejected", "validated"]


def test_concurrent_offer_and_reobserve_leaves_no_stale_pending():
    sdk = _sdk()
    sdk.observe(_obs(1))
    start = threading.Barrier(2, timeout=2.0)

    def offer_old():
        start.wait()
        try:
            sdk.offer(_frame(1, "dec-old"))
        except ChoiceSdkRejected:
            pass

    def observe_new():
        start.wait()
        sdk.observe(_obs(2))

    threads = [
        threading.Thread(target=offer_old, daemon=True),
        threading.Thread(target=observe_new, daemon=True),
    ]
    try:
        for thread in threads:
            thread.start()
    finally:
        _join(*threads)
    with pytest.raises(ChoiceSdkRejected):
        sdk.submit(_result("dec-old"))
    assert sdk.offer(_frame(2, "dec-new")).decision_id == "dec-new"
