"""Fishing (#176): the rod offer is already RAM-gated; the cast is a task-owned, input-free flow.

Source proof: pret/pokefirered @ 037335f4, ``src/item_use.c`` (CanFish/StartFishing) and
``src/field_player_avatar.c`` (Task_Fishing).  The bite path prints and then calls
``FishingWildEncounter`` without reading a button; no-bite/got-away return to free field.  These
tests are synthetic and use invented facts only.
"""

from types import SimpleNamespace

import pytest

from pokegent.actions._exec.models import WaitUntil
from pokegent.actions.items.options import ItemOptionBuilder
from pokegent.games.gen3.firered.maps.map_schema import (
    MapGrid,
    MapIdentity,
    MapLayoutInfo,
    MapState,
    PlayerPlacement,
)
from pokegent.games.gen3.firered.rom.item_catalog import (
    EffectClass,
    ItemCapability,
    ItemRecord,
    ItemTarget,
)
from pokegent.options.models import Legality


def _record() -> ItemRecord:
    return ItemRecord(
        item_id=100,
        name="old rod",
        pocket=2,
        item_type=0,
        field_use_func=1,
        battle_use_func=0,
        hold_effect=0,
        price=0,
        importance=3,
        move_id=None,
        evolutions=(),
        capability=ItemCapability(
            field=True,
            battle=False,
            target=ItemTarget.NONE,
            effects=(EffectClass.KEY_ITEM_TOGGLE,),
            consumable=False,
            field_action="rod",
        ),
        data=SimpleNamespace(pocket=2),
    )


class _Catalog:
    def item(self, item_id: int) -> ItemRecord:
        assert item_id == 100
        return _record()


def _map_state(*, facing="N", elevation=3, surfing=False, front_elevation=1,
               behavior=0x10, terrain=2):
    identity = MapIdentity(
        group=0, number=0, name="DemoPond", layout_id=1, region_section_id=0,
        map_type=0, map_type_name="town", weather=0, cave=0, floor_num=0,
        battle_type=0, music=0, biking_allowed=True, allow_escaping=True,
        allow_running=True, identity_verified=True,
    )
    layout = MapLayoutInfo(
        width=5, height=5, border_width=1, border_height=1, border=(0,),
        primary_tileset=0, secondary_tileset=0, layout_ptr=0,
    )
    player = PlayerPlacement(
        save_x=2, save_y=2, avatar_x=2, avatar_y=2, prev_x=2, prev_y=2, x=2, y=2,
        position_consistent=True, facing=facing, elevation=elevation,
        object_event_id=0, avatar_flags=0, surfing=surfing, underwater=False,
        biking=False, controllable_flag=True, forced=False, moving=False,
        tile_transition_state=0, running_state=0,
    )
    blocks = [0] * (19 * 19)
    # water one step north of the player; elevation 1 vs land elevation 3.
    blocks[(1 + 7) * 19 + (2 + 7)] = 1 | (front_elevation << 12)
    return MapState(
        identity=identity,
        layout=layout,
        grid=MapGrid(
            columns=19,
            rows=19,
            blocks=tuple(blocks),
            attributes=((0, 0), (1, behavior | (terrain << 9))),
        ),
        player=player,
        objects=(),
    )


def _snapshot(*, facing="N", elevation=3, surfing=False, front=None, map_readable=True,
              behavior=0x10, terrain=2, front_elevation=1):
    values = dict(facing=facing, elevation=elevation, surfing=surfing,
                  behavior=behavior, terrain=terrain, front_elevation=front_elevation)
    values.update(front or {})
    state = _map_state(**values) if map_readable else None
    item = SimpleNamespace(item_id=100, quantity=1)
    return SimpleNamespace(map=state, bag=(SimpleNamespace(items=(item,)),), party=())


def _verdict(snapshot):
    return ItemOptionBuilder(_Catalog())._without_target(snapshot, _record(), "field", None)


def test_land_cast_needs_water_terrain_and_player_elevation_three():
    assert _verdict(_snapshot()).legality is Legality.LEGAL
    # CanFish rejects waterfalls even though the tile is surfable.
    assert _verdict(_snapshot(behavior=0x13)).legality is Legality.UNAVAILABLE
    # GetCollisionAtCoords must report the elevation mismatch: the player must stand at 3.
    assert _verdict(_snapshot(elevation=2)).legality is Legality.UNAVAILABLE
    # A water terrain attribute, not just a surfable behavior.
    assert _verdict(_snapshot(terrain=0)).legality is Legality.UNAVAILABLE


def test_surfing_cast_does_not_use_the_land_elevation_rule():
    # CanFish while surfing: surfable behavior with collision 0, no land elevation check.
    assert _verdict(_snapshot(surfing=True, elevation=0)).legality is Legality.LEGAL
    assert (
        _verdict(_snapshot(surfing=True, elevation=0, behavior=0)).legality
        is Legality.UNAVAILABLE
    )


def test_unknown_map_stays_unknown():
    assert _verdict(_snapshot(map_readable=False)).legality is Legality.UNKNOWN


def test_rod_option_targets_the_dedicated_fishing_executor():
    from pokegent.actions.field.macros import executor_specs

    option = ItemOptionBuilder(_Catalog()).build(_snapshot(), context="field")[0]
    assert option.executor_ref == "field.fish"
    assert option.postcondition_ref == "field.fishing_resolved"
    spec = {item.ref: item for item in executor_specs()}["field.fish"]
    assert spec.check_args(option.executor_args) is None


def test_cast_is_only_observed_on_the_task_fact_not_on_a_dialog_scene():
    from pokegent.actions.field import fishing

    assert fishing.cast_started(_obs("scene.unknown", prevent_step=True))
    assert not fishing.cast_started(_obs("field.free", prevent_step=False))
    assert not fishing.cast_started(_obs("dialog.advance", prevent_step=False))
    # Missing/unreadable prevent_step is never a cast.
    assert not fishing.cast_started(_obs("scene.unknown", prevent_step=None))


def test_unreadable_prevent_step_never_passes_the_cast_gate():
    from pokegent.actions.field import fishing

    gen = fishing.drive(_obs("field.free", prevent_step=None))
    step = next(gen)
    assert isinstance(step, WaitUntil)
    assert step.max_frames == fishing.CAST_FRAMES
    gen.close()


def test_flow_sends_no_input_after_the_bag_use():
    from pokegent.actions.field import fishing

    steps = []
    gen = fishing.drive(_obs("scene.unknown", prevent_step=False))
    steps.append(next(gen))
    steps.append(gen.send(_obs("scene.unknown", prevent_step=True)))
    assert steps[-1].max_frames == fishing.TERMINAL_FRAMES
    with pytest.raises(StopIteration) as stop:
        gen.send(_obs("battle.entering", prevent_step=False))
    assert stop.value.value == "battle"
    assert all(isinstance(step, WaitUntil) for step in steps)


def test_no_battle_needs_a_settled_free_field_after_the_cast():
    from pokegent.actions.field import fishing

    tracker = fishing.TerminalTracker()
    free = _obs("field.free", prevent_step=False)
    for _ in range(fishing.FREE_SETTLE_CHECKS - 1):
        assert not tracker(free)
    assert tracker(free)
    assert tracker.outcome == "no_battle"

    # A bite unlock is only one or two free frames: a dialog frame resets the settle window.
    tracker = fishing.TerminalTracker()
    for _ in range(fishing.FREE_SETTLE_CHECKS - 1):
        assert not tracker(free)
    assert not tracker(_obs("dialog.printing", prevent_step=False))
    assert not tracker(free)
    assert tracker.outcome is None

    # The battle terminal is immediate and never needs the free settle, including the one-frame
    # unlock that precedes a bite encounter.
    tracker = fishing.TerminalTracker()
    assert not tracker(free)
    assert tracker(_obs("battle.entering", prevent_step=False))
    assert tracker.outcome == "battle"


def test_verifier_rejects_an_unrelated_free_snapshot():
    from pokegent.actions.field import fishing

    free = _obs("field.free", prevent_step=False)
    ctx = SimpleNamespace(
        scene=free.scene,
        before=SimpleNamespace(emulator_frame=100),
        after=SimpleNamespace(emulator_frame=100, ui=free.snapshot.ui),
    )
    assert not fishing.resolved(ctx)
    ctx.after = SimpleNamespace(emulator_frame=220, ui=free.snapshot.ui)
    assert fishing.resolved(ctx)
    battle = _obs("battle.entering", prevent_step=False)
    assert fishing.resolved(
        SimpleNamespace(scene=battle.scene, before=ctx.before, after=ctx.after)
    )


def _obs(scene="field.free", *, prevent_step=False, controls_locked=False, text_waiting=False):
    ui = SimpleNamespace(
        valid=True,
        main=SimpleNamespace(in_battle=scene.startswith("battle.")),
        field=SimpleNamespace(valid=True, prevent_step=prevent_step),
        text=SimpleNamespace(valid=True, any_active=text_waiting, waiting_for_button=text_waiting),
        script=SimpleNamespace(valid=True, controls_locked=controls_locked),
    )
    return SimpleNamespace(scene=SimpleNamespace(scene_id=scene), snapshot=SimpleNamespace(ui=ui))
