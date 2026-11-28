"""A train objective's level goal reaches the party menu as named training targets."""

import pytest
from test_party_give_item_args import R, _mon, _party_snapshot, _registries

from pokegent.options.registry import BuildContext


def _context(levels, party_levels=(42, 46, 55)):
    snapshot = _party_snapshot()
    members = []
    for slot, level in enumerate(party_levels):
        mon = _mon(slot, 0x100 + slot)
        mon.level = level
        members.append(mon)
    snapshot.party = tuple(members)
    with pytest.MonkeyPatch.context() as monkeypatch:
        builder = _registries(monkeypatch).builders.for_scene(R.PARTY_HELD_ITEM)
    ctx = BuildContext(
        snapshot=snapshot,
        scene=type("S", (), {"scene_id": R.PARTY_HELD_ITEM})(),
        objective_levels=levels,
    )
    return builder.build(ctx).context


def test_party_level_goal_lists_every_member_below_it():
    targets = _context(((None, None, 55),))["training_targets"]
    assert [(t["slot"], t["level"], t["goal_level"]) for t in targets] == [(0, 42, 55), (1, 46, 55)]


def test_mon_level_goal_lists_only_that_member():
    targets = _context(((0x101, None, 55),))["training_targets"]
    assert [(t["slot"], t["level"]) for t in targets] == [(1, 46)]


def test_no_level_goal_or_goal_met_adds_nothing():
    assert "training_targets" not in _context(())
    assert "training_targets" not in _context(((None, None, 40),))
