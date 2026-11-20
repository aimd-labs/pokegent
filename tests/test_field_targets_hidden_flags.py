"""Field target filtering: required NPCs and item balls under hidden-flag quirks.

Regression coverage for the live Fuchsia/Vermilion adapter fixes:
- a scripted objective's required NPC must stay targetable even when its
  hidden flag is not provably clear (``hidden_flag_set=None``), because the
  executor's postcondition verifies the talk actually happened;
- an item ball's flag records "already picked up", so a ball whose flag is
  set must be skipped, while a ball with an unknown or cleared flag remains
  a target.
"""
import pokegent.actions.field.nav as N
from pokegent.actions.field.targets import object_targets
from pokegent.games.gen3.firered.maps.map_schema import (
    MapGrid,
    MapIdentity,
    MapLayoutInfo,
    MapObject,
    MapState,
    PlayerPlacement,
)


def _nav(_state: MapState) -> N.Navigator:
    class _StubNav:
        def interaction_approaches(self, tile, *, face_only=None, counter_ok=True):
            x, y = tile
            return [((x - 1, y), "E"), ((x + 1, y), "W")]

    return _StubNav()  # type: ignore[return-value]


def _state(objects: tuple[MapObject, ...]) -> MapState:
    identity = MapIdentity(
        group=0,
        number=0,
        name="DemoMap",
        layout_id=1,
        region_section_id=0,
        map_type=0,
        map_type_name="indoor",
        weather=0,
        cave=0,
        floor_num=0,
        battle_type=0,
        music=0,
        biking_allowed=False,
        allow_escaping=False,
        allow_running=True,
        identity_verified=True,
    )
    layout = MapLayoutInfo(
        width=5,
        height=5,
        border_width=1,
        border_height=1,
        border=(0,),
        primary_tileset=0,
        secondary_tileset=0,
        layout_ptr=0,
    )
    player = PlayerPlacement(
        save_x=2,
        save_y=2,
        avatar_x=2,
        avatar_y=2,
        prev_x=2,
        prev_y=2,
        x=2,
        y=2,
        position_consistent=True,
        facing="N",
        elevation=0,
        object_event_id=0,
        avatar_flags=0,
        surfing=False,
        underwater=False,
        biking=False,
        controllable_flag=True,
        forced=False,
        moving=False,
        tile_transition_state=0,
        running_state=0,
    )
    blocks = [0x3FF] * (19 * 19)
    for y in range(5):
        for x in range(5):
            blocks[(y + 7) * 19 + x + 7] = 0
    return MapState(
        identity=identity,
        layout=layout,
        grid=MapGrid(columns=19, rows=19, blocks=tuple(blocks), attributes=((0, 0),)),
        player=player,
        objects=objects,
    )


def _object(
    local_id: int,
    *,
    role: str,
    x: int,
    y: int,
    hidden_flag_id: int = 0,
    hidden_flag_set: bool | None = None,
    script_ptr: int = 0x100,
) -> MapObject:
    return MapObject(
        local_id=local_id,
        graphics_id=1,
        template_graphics_id=1,
        role=role,  # type: ignore[arg-type]
        x=x,
        y=y,
        elevation=0,
        movement_type=0,
        movement_type_name="none",
        range_x=0,
        range_y=0,
        trainer_type=0,
        trainer_range=0,
        script_ptr=script_ptr,
        hidden_flag_id=hidden_flag_id,
        hidden_flag_set=hidden_flag_set,
        source="template",
        live=None,
        blocks_tile=True,
    )


def test_required_npc_survives_unreadable_hide_flag_for_fight_objective():
    npc = _object(
        7,
        role="npc",
        x=2,
        y=4,
        hidden_flag_id=49,
        hidden_flag_set=None,  # flags unreadable: objective still names this NPC
    )
    state = _state((npc,))
    # Fight objectives (gym leaders, grunts) opt into the exemption: the
    # objective names the NPC and the executor verifies the talk happened.
    targets = object_targets(
        state, _nav(state), required_npc=7, exempt_required_npc_hidden=True
    )
    ids = [t.option_id for t in targets]
    assert "talk:7" in ids
    info = next(t.info for t in targets if t.option_id == "talk:7")
    assert info["objective_target"] is True


def test_required_npc_unknown_hidden_still_refused_for_talk_objective():
    npc = _object(
        7,
        role="npc",
        x=2,
        y=4,
        hidden_flag_id=49,
        hidden_flag_set=None,  # unreadable
    )
    state = _state((npc,))
    # Talk objectives keep the story-port safety: never invent a reachable
    # target whose hide flag is set or unreadable.
    targets = object_targets(state, _nav(state), required_npc=7)
    assert all(t.option_id != "talk:7" for t in targets)
    targets = object_targets(
        state, _nav(state), required_npc=7, exempt_required_npc_hidden=False
    )
    assert all(t.option_id != "talk:7" for t in targets)


def test_taken_item_ball_is_skipped():
    ball = _object(
        2,
        role="item_ball",
        x=4,
        y=2,
        hidden_flag_id=55,
        hidden_flag_set=True,  # already picked up
    )
    state = _state((ball,))
    targets = object_targets(state, _nav(state), required_npc=None)
    assert all(t.option_id != "item:2" for t in targets)


def test_unpicked_item_ball_remains_targetable():
    ball = _object(
        2,
        role="item_ball",
        x=4,
        y=2,
        hidden_flag_id=0,
        hidden_flag_set=False,
    )
    state = _state((ball,))
    targets = object_targets(state, _nav(state), required_npc=None)
    assert "item:2" in [t.option_id for t in targets]


def test_unreadable_flag_ball_still_targetable():
    ball = _object(
        2,
        role="item_ball",
        x=4,
        y=2,
        hidden_flag_id=55,
        hidden_flag_set=None,  # unknown flag: still findable
    )
    state = _state((ball,))
    targets = object_targets(state, _nav(state), required_npc=None)
    assert "item:2" in [t.option_id for t in targets]
