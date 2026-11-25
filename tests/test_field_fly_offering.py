"""Fly exposure from a ready outdoor scene without a planner-selected Fly edge."""

from types import SimpleNamespace

import pytest
from test_field_targets_hidden_flags import _state
from test_probe_primitives import snapshot

from pokegent.actions.field import fly
from pokegent.actions.field.builder import FieldBuilder
from pokegent.games.gen3.firered.reader.schemas import FlagSet
from pokegent.options.registry import BuildContext
from pokegent.state.scenes import SceneResult

CURRENT = fly.FlyDestination(
    mapsec=0,
    name="DemoMap",
    map_key=(0, 0),
    landing=(2, 2),
    visited_flag=19,
    cell=(0, 0),
)
TARGET = fly.FlyDestination(
    mapsec=1,
    name="DemoExit",
    map_key=(0, 1),
    landing=(1, 1),
    visited_flag=20,
    cell=(1, 1),
)
OTHER = fly.FlyDestination(
    mapsec=2,
    name="DemoLake",
    map_key=(0, 2),
    landing=(3, 3),
    visited_flag=21,
    cell=(2, 2),
)


@pytest.fixture
def destinations(monkeypatch):
    rows = (CURRENT, TARGET, OTHER)
    monkeypatch.setattr(fly, "DESTINATIONS", rows)
    monkeypatch.setattr(fly, "DESTINATIONS_BY_NAME", {row.name: row for row in rows})
    monkeypatch.setattr(fly, "DESTINATIONS_BY_KEY", {row.map_key: row for row in rows})
    return rows


def _outdoor_state():
    state = _state(())
    identity = state.identity.model_copy(update={"map_type": 1, "map_type_name": "town"})
    return state.model_copy(update={"identity": identity})


def _snapshot(state, *, badges=(fly.THUNDER_BADGE,), moves=(fly.FLY_MOVE_ID,), flags=(19, 20, 21)):
    base = snapshot()
    player = base.player.model_copy(update={"badges": badges})
    party = tuple(
        SimpleNamespace(
            slot=index,
            species=index + 1,
            moves=tuple(SimpleNamespace(move_id=move) for move in moves),
        )
        for index, _ in enumerate(moves)
    )
    return base.model_copy(
        update={
            "map": state,
            "player": player,
            "party": party,
            "flags": FlagSet(set_ids=tuple(sorted(flags))),
        }
    )


def _draft(snap):
    ctx = BuildContext(
        snapshot=snap,
        scene=SceneResult(scene_id="field.free", confidence=1, detector="invented"),
    )
    return FieldBuilder(field_menu=False).build(ctx)


def test_ready_outdoor_scene_offers_visited_fly_destinations_without_goal(destinations):
    draft = _draft(_snapshot(_outdoor_state()))
    flies = [option for option in draft.options if option.id.startswith("fly:")]
    assert [option.id for option in flies] == ["fly:DemoExit", "fly:DemoLake"]
    assert all(option.chooseable for option in flies)
    assert all(option.facts["serves_goal"] is False for option in flies)
    assert all(option.facts["visited"] is True for option in flies)


def test_unvisited_destination_is_not_offered(destinations):
    draft = _draft(_snapshot(_outdoor_state(), flags=(19, 21)))
    assert [option.id for option in draft.options if option.id.startswith("fly:")] == [
        "fly:DemoLake"
    ]


def test_general_destinations_are_bounded_and_stable(destinations, monkeypatch):
    limit = 3
    rows = tuple(
        fly.FlyDestination(
            mapsec=index + 1,
            name=f"DemoStop{index}",
            map_key=(0, index + 1),
            landing=(1, 1),
            visited_flag=30 + index,
            cell=(index, index),
        )
        for index in range(limit + 2)
    )
    monkeypatch.setattr(fly, "DESTINATIONS", (CURRENT, *rows))
    monkeypatch.setattr(
        fly, "DESTINATIONS_BY_KEY", {CURRENT.map_key: CURRENT, **{row.map_key: row for row in rows}}
    )
    flags = (19, *(30 + index for index in range(len(rows))))
    for _ in range(2):
        draft = _draft(_snapshot(_outdoor_state(), flags=flags))
        assert [option.id for option in draft.options if option.id.startswith("fly:")] == [
            f"fly:DemoStop{index}" for index in range(limit)
        ]


def test_missing_badge_or_carrier_never_offers_fly(destinations):
    assert not [
        option.id
        for option in _draft(_snapshot(_outdoor_state(), badges=())).options
        if option.id.startswith("fly:")
    ]
    assert not [
        option.id
        for option in _draft(_snapshot(_outdoor_state(), moves=(1,))).options
        if option.id.startswith("fly:")
    ]


def test_indoor_or_unproved_launch_map_refuses_fly(destinations):
    state = _outdoor_state()
    indoor = state.model_copy(
        update={"identity": state.identity.model_copy(update={"map_type": 0})}
    )
    assert not [
        option.id for option in _draft(_snapshot(indoor)).options if option.id.startswith("fly:")
    ]
    elsewhere = state.model_copy(
        update={"identity": state.identity.model_copy(update={"group": 3, "number": 9})}
    )
    assert fly.can_launch(elsewhere) is False
    assert not [
        option.id for option in _draft(_snapshot(elsewhere)).options if option.id.startswith("fly:")
    ]
