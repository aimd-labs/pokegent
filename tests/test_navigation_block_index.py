from types import SimpleNamespace

from pokegent.games.gen3.firered.maps.map_schema import MapObject
from pokegent.navigation.capabilities import CapabilityState
from pokegent.navigation.graph import NavigationGraph


def _object(local_id, x, y, *, hidden=0, live=None):
    obj = MapObject(
        local_id=local_id,
        graphics_id=1,
        template_graphics_id=1,
        role="npc",
        x=x,
        y=y,
        elevation=0,
        movement_type=0,
        movement_type_name="none",
        range_x=0,
        range_y=0,
        trainer_type=0,
        trainer_range=0,
        script_ptr=0x100,
        hidden_flag_id=hidden,
        hidden_flag_set=False,
        source="template",
        live=None,
        blocks_tile=True,
    )
    return obj if live is None else obj.model_copy(update={"live": live})


def test_blocking_tile_index_preserves_order_flags_previous_tiles_and_invalidates(monkeypatch):
    first = _object(1, 2, 2, hidden=49)
    overlap = _object(2, 2, 2)
    moving = _object(
        3, 4, 4, live=SimpleNamespace(prev_x=8, prev_y=8)
    )
    state = SimpleNamespace(
        identity=SimpleNamespace(group=1, number=2),
        objects=(first, overlap, moving),
    )
    graph = NavigationGraph({}, provider=object())
    builds = 0
    original = graph._build_blocking_object_index

    def count_builds(current):
        nonlocal builds
        builds += 1
        return original(current)

    monkeypatch.setattr(graph, "_build_blocking_object_index", count_builds)

    for _ in range(5):
        assert graph._blocking_object(state, 2, 2, CapabilityState()) is first
        assert graph._blocking_object(state, 4, 4, CapabilityState()) is moving
        assert graph._blocking_object(state, 8, 8, CapabilityState()) is moving
    assert graph._blocking_object(state, 2, 2, CapabilityState(flags=frozenset({49}))) is overlap
    assert builds == 1

    added = _object(4, 9, 9)
    changed = SimpleNamespace(
        identity=state.identity,
        objects=(added, first, overlap, moving),
    )
    assert graph._blocking_object(changed, 9, 9, CapabilityState()) is added
    assert graph._blocking_object(changed, 2, 2, CapabilityState()) is first
    assert builds == 2
