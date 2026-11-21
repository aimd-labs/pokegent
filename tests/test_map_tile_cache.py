from pokegent.games.gen3.firered.maps import map_constants as K
from pokegent.games.gen3.firered.maps.map_schema import (
    MapGrid,
    MapIdentity,
    MapLayoutInfo,
    MapState,
)


def _state():
    identity = MapIdentity(group=0, number=0, name="Cache", layout_id=1,
        region_section_id=0, map_type=0, map_type_name="indoor", weather=0,
        cave=0, floor_num=0, battle_type=0, music=0, biking_allowed=False,
        allow_escaping=False, allow_running=True, identity_verified=True)
    layout = MapLayoutInfo(width=1, height=1, border_width=1, border_height=1,
        border=(5,), primary_tileset=0, secondary_tileset=0, layout_ptr=0)
    blocks = [K.MAPGRID_UNDEFINED] * 225
    blocks[7 * 15 + 7] = 2
    return MapState(identity=identity, layout=layout,
        grid=MapGrid(columns=15, rows=15, blocks=tuple(blocks), attributes=((2, 0x12345678),)))


def test_tile_cache_reuses_values_and_invalidates_copies(monkeypatch):
    state = _state()
    original_raw_block = MapState.raw_block
    raw_block_calls = []

    def count_raw_block_calls(self, x, y):
        raw_block_calls.append((x, y))
        return original_raw_block(self, x, y)

    monkeypatch.setattr(MapState, "raw_block", count_raw_block_calls)
    before = state.model_dump_json()
    for _ in range(20):
        assert state.attributes(0, 0) == 0x12345678
        assert state.behavior(0, 0) == (0x12345678 & K.ATTR_BEHAVIOR_MASK)
        assert state.attributes(-8, 0) == 0
        assert state.behavior(-8, 0) == (0 & K.ATTR_BEHAVIOR_MASK)
    assert len(state._tile_cache) == 2
    assert raw_block_calls == [(0, 0), (-8, 0)]
    assert state.raw_block(0, 0) == 2
    assert state.collision(0, 0) == 0
    assert state.elevation(0, 0) == 0
    assert state.raw_block(-8, 0) == (5 | K.MAPGRID_COLLISION_MASK)
    assert state.model_dump_json() == before

    changed_grid = state.grid.model_copy(update={
        "blocks": tuple([3 if i == 112 else b for i, b in enumerate(state.grid.blocks)]),
        "attributes": ((3, 0x87654321),),
    })
    changed = state.model_copy(update={"grid": changed_grid})
    assert changed.raw_block(0, 0) == 3
    assert changed.attributes(0, 0) == 0x87654321
    assert state.raw_block(0, 0) == 2

    changed_layout = state.model_copy(
        update={"layout": state.layout.model_copy(update={"border": (9,)})}
    )
    assert changed_layout.raw_block(-8, 0) == (9 | K.MAPGRID_COLLISION_MASK)

    unknown_blocks = tuple([K.MAPGRID_UNDEFINED] + list(state.grid.blocks[1:]))
    unknown = state.model_copy(
        update={"grid": state.grid.model_copy(update={"blocks": unknown_blocks})}
    )
    assert unknown.attributes(-7, -7) == 0
