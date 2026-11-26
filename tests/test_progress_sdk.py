"""Synthetic contract tests for the opt-in read-only progress SDK facade.

Real ``ResolvedObjective``, ``StateSnapshot`` and ``ObjectiveTracker`` classes are used with
invented RAM facts. The canonical ``firered-us-rev0`` label is a caller assertion here, not
evidence of a ROM. No server, runtime wiring, game input or network is involved.
"""

from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError

import pytest

from pokegent import sdk_progress
from pokegent.games.gen3.firered.reader.schemas import (
    FlagSet,
    Health,
    Player,
    ReadError,
    StateSnapshot,
    VarTable,
)
from pokegent.games.plugin import GamePluginDescriptor, PluginSelection, ReviewedPluginRegistry
from pokegent.goals.models import Objective
from pokegent.goals.tracker import ObjectiveTracker
from pokegent.goals.validate import ResolvedObjective, ResolvedProbe, ResolvedTarget
from pokegent.sdk_progress import ProgressSdk, ProgressSdkRejected, ProgressView

ROM = b"synthetic-progress-sdk-rom" * 8
GAME = "firered"
PROFILE = "firered-us-rev0"
MAP_KEY = (3, 7)


class _Plugin:
    def __init__(self, descriptor):
        self.descriptor = descriptor

    def compose(self):
        return object()


def _selection(*, game=GAME, profile=PROFILE, data=ROM):
    descriptor = GamePluginDescriptor(
        plugin_id=f"{game}:{profile}",
        game=game,
        profile_id=profile,
        contract_version=1,
        rom_sha1=hashlib.sha1(data).hexdigest(),
        capabilities=frozenset({"objective_observation"}),
    )
    registry = ReviewedPluginRegistry(enabled=True)
    registry.register(_Plugin(descriptor))
    return registry.select(rom_bytes=data, game=game, profile_id=profile)


def _snapshot(*, seq=1, profile=PROFILE, flags=(), x=4, y=5, usable=True):
    if not usable:
        return StateSnapshot.create(
            rom_profile=profile,
            snapshot_seq=seq,
            health=Health(
                valid=False,
                read_errors=(
                    ReadError(subsystem="flags", code="unreadable", message="synthetic frame"),
                ),
            ),
        )
    return StateSnapshot.create(
        rom_profile=profile,
        snapshot_seq=seq,
        emulator_frame=0,
        health=Health(valid=True, read_errors=()),
        player=Player(
            name="Test",
            gender="male",
            tid=0,
            sid=0,
            map_group=MAP_KEY[0],
            map_num=MAP_KEY[1],
            map_layout_id=0,
            x=x,
            y=y,
            money=0,
            coins=0,
        ),
        flags=FlagSet(set_ids=tuple(flags)),
        vars=VarTable(),
    )


def _objective(kind="reach", objective_id="reach1"):
    return Objective.model_validate(
        {"objective_id": objective_id, "revision": 1, "kind": kind, "target": {"map": "PewterCity"}}
    )


def _tile_resolved(x=4, y=5):
    return ResolvedObjective(
        _objective(),
        ResolvedTarget(group=MAP_KEY[0], number=MAP_KEY[1]),
        (ResolvedProbe(kind="at_tile", map_key=MAP_KEY, x=x, y=y),),
    )


def _flag_resolved(expected):
    return ResolvedObjective(
        _objective(),
        ResolvedTarget(group=MAP_KEY[0], number=MAP_KEY[1]),
        (ResolvedProbe(kind="flag", flag=7, expected=expected),),
    )


def _hold_resolved():
    return ResolvedObjective(_objective("hold", "hold1"), ResolvedTarget(), ())


def _explode(*args, **kwargs):
    raise AssertionError("no evaluator or tracker may run")


def test_disabled_by_default_and_iteration_is_exact_bool(monkeypatch):
    monkeypatch.setattr(sdk_progress, "ObjectiveTracker", _explode)
    sdk = ProgressSdk(_selection())
    with pytest.raises(ProgressSdkRejected):
        sdk.evaluate(_tile_resolved(), _snapshot())
    for not_a_bool in (1, 0, "true", None):
        with pytest.raises(TypeError):
            ProgressSdk(_selection(), enabled=not_a_bool)


def test_constructor_rejects_bad_selection_foreign_game_and_foreign_profile():
    assert isinstance(_selection(), PluginSelection)
    for bad in (object(), _selection().descriptor, None):
        with pytest.raises(TypeError):
            ProgressSdk(bad, enabled=True)
    for game, profile in (("emerald", PROFILE), (GAME, "synthetic-profile")):
        with pytest.raises(ProgressSdkRejected):
            ProgressSdk(_selection(game=game, profile=profile), enabled=True)


def test_matched_current_tile_completes_and_a_different_tile_does_not():
    sdk = ProgressSdk(_selection(), enabled=True)

    view = sdk.evaluate(_tile_resolved(), _snapshot())

    assert isinstance(view, ProgressView)
    assert (view.objective_id, view.revision, view.snapshot_seq) == ("reach1", 1, 1)
    assert (view.completed, view.evaluated) == (True, True)
    assert view.evidence_kind == "snapshot_evaluation"
    assert view.durable_committed is False

    miss = sdk.evaluate(_tile_resolved(x=9), _snapshot(seq=2))
    assert (miss.completed, miss.evaluated, miss.snapshot_seq) == (False, True, 2)


@pytest.mark.parametrize(
    ("flags", "expected", "completed"),
    [((), True, False), ((7,), True, True), ((), False, True), ((7,), False, False)],
)
def test_flag_probe_keeps_existing_expected_and_current_ram_semantics(flags, expected, completed):
    view = ProgressSdk(_selection(), enabled=True).evaluate(
        _flag_resolved(expected), _snapshot(flags=flags)
    )
    assert (view.completed, view.evaluated) == (completed, True)


def test_unusable_snapshot_is_not_evaluated_and_never_completes(monkeypatch):
    monkeypatch.setattr(sdk_progress, "ObjectiveTracker", _explode)

    view = ProgressSdk(_selection(), enabled=True).evaluate(
        _tile_resolved(), _snapshot(seq=9, usable=False)
    )

    assert view.completed is None
    assert view.evaluated is False
    assert view.snapshot_seq == 9
    assert view.evidence_kind == "snapshot_evaluation"
    assert view.durable_committed is False


def test_mismatched_snapshot_profile_refuses_before_any_evaluation(monkeypatch):
    monkeypatch.setattr(sdk_progress, "ObjectiveTracker", _explode)
    sdk = ProgressSdk(_selection(), enabled=True)

    for profile in ("firered-us-rev1", "synthetic"):
        with pytest.raises(ProgressSdkRejected):
            sdk.evaluate(_tile_resolved(), _snapshot(profile=profile))


def test_evaluate_rejects_bad_inputs_before_any_evaluation(monkeypatch):
    monkeypatch.setattr(sdk_progress, "ObjectiveTracker", _explode)
    sdk = ProgressSdk(_selection(), enabled=True)

    for bad_resolved in (object(), None, _tile_resolved().objective):
        with pytest.raises(ProgressSdkRejected):
            sdk.evaluate(bad_resolved, _snapshot())
    for bad_snapshot in (object(), None, {"snapshot_seq": 1}):
        with pytest.raises(ProgressSdkRejected):
            sdk.evaluate(_tile_resolved(), bad_snapshot)


def test_hold_without_probes_is_evaluated_but_never_completed():
    view = ProgressSdk(_selection(), enabled=True).evaluate(_hold_resolved(), _snapshot())
    assert (view.completed, view.evaluated) == (False, True)


def test_view_is_frozen_and_exports_no_raw_snapshot_probes_or_tables():
    view = ProgressSdk(_selection(), enabled=True).evaluate(_tile_resolved(), _snapshot())

    with pytest.raises(FrozenInstanceError):
        view.completed = True
    for name in ("snapshot", "probes", "tables", "raw"):
        assert not hasattr(view, name)


def test_uses_one_ephemeral_storeless_sinkless_tracker_with_supplied_inputs(monkeypatch):
    seen = []

    class _Spy(ObjectiveTracker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            seen.append(self)

        def set_objective(self, resolved):
            self.switched = resolved
            return super().set_objective(resolved)

        def update(self, snapshot):
            self.fed = snapshot
            return super().update(snapshot)

    monkeypatch.setattr(sdk_progress, "ObjectiveTracker", _Spy)
    tables = object()
    resolved = _tile_resolved()
    snapshot = _snapshot()

    view = ProgressSdk(_selection(), enabled=True).evaluate(
        resolved, snapshot, tables=tables
    )

    assert view.completed is True
    assert len(seen) == 1
    tracker = seen[0]
    assert tracker.store is None
    assert tracker.sink is None
    assert tracker.tables is tables
    assert tracker.switched is resolved
    assert tracker.fed is snapshot
