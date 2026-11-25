"""Regression: the boulder plan cache must key the same static geometry as the world cache.

``boulder_options`` offers a plan from ``BoulderPlans.plans`` (cached), while
``BoulderMacro._replan`` solves a fresh ``BoulderPlans.world``. A collision or fixed-object change
that leaves the boulder positions, the opened-switch mask, the cheapest region distance and the
exits identical must therefore invalidate the cached plan: otherwise the offer advertises a legal
``boulder_plan`` whose executor immediately solves ``NoPlan`` and sends no input.

Only invented map data: no ROM layout, save, capture or generated game data is read.
"""

from types import SimpleNamespace

import pytest

from pokegent.actions._exec.models import Tap
from pokegent.actions._exec.registry import StepContext
from pokegent.actions.field.attempts import AttemptLedger
from pokegent.actions.field.boulder_macro import BoulderMacro
from pokegent.actions.field.boulder_options import BoulderPlans
from pokegent.actions.field.boulders import Goal, NoPlan, Plan
from pokegent.actions.field.macros import FieldMacros
from pokegent.games.gen3.firered.maps.map_behaviors import MB
from pokegent.games.gen3.firered.maps.map_schema import (
    LiveObject,
    MapGrid,
    MapIdentity,
    MapLayoutInfo,
    MapObject,
    MapState,
    PlayerPlacement,
)
from pokegent.options.models import Option
from pokegent.state.scenes import SceneResult

W = H = 16
FLOOR, BUTTON = 1, 2
TILES = {FLOOR: MB["MB_NORMAL"], BUTTON: MB["MB_STRENGTH_BUTTON"]}
SWITCH = (6, 10)
GOAL = Goal("switch", SWITCH)


def _object(local_id, x, y, role="boulder"):
    live = LiveObject(
        slot=local_id, local_id=local_id, x=x, y=y, prev_x=x, prev_y=y, initial_x=x, initial_y=y,
        facing=None, elevation=3, invisible=False, off_screen=False, moving=False, range_x=0,
        range_y=0, map_group=91, map_number=7, graphics_id=97, movement_type=0,
    )  # fmt: skip
    return MapObject(
        local_id=local_id, graphics_id=97, role=role, x=x, y=y, elevation=3, movement_type=0,
        movement_type_name="none", range_x=0, range_y=0, trainer_type=0, trainer_range=0,
        script_ptr=None, hidden_flag_id=0, hidden_flag_set=None, source="template+live",
        live=live, blocks_tile=True,
    )  # fmt: skip


def _state(*, walls=(), extra_objects=(), player=(2, 10), facing="E"):
    """Invented cave: one boulder at (4, 10), the floor switch at (6, 10)."""
    blocks = [0x3FF] * ((W + 14) * (H + 14))

    def put(x, y, metatile, collision=0):
        blocks[(y + 7) * (W + 14) + x + 7] = metatile | (collision << 10) | (3 << 12)

    for y in range(H):
        for x in range(W):
            put(x, y, FLOOR)
    put(*SWITCH, BUTTON)
    for x, y in walls:
        put(x, y, FLOOR, collision=1)
    return MapState(
        identity=MapIdentity(
            group=91, number=7, name="InventedBoulder2F", layout_id=1, region_section_id=0,
            map_type=0, map_type_name="cave", weather=0, cave=0, floor_num=0, battle_type=0,
            music=0, biking_allowed=False, allow_escaping=False, allow_running=True,
            identity_verified=True,
        ),  # fmt: skip
        layout=MapLayoutInfo(
            width=W, height=H, border_width=1, border_height=1, border=(0,),
            primary_tileset=0, secondary_tileset=0, layout_ptr=0,
        ),  # fmt: skip
        grid=MapGrid(
            columns=W + 14, rows=H + 14, blocks=tuple(blocks), attributes=tuple(TILES.items())
        ),  # fmt: skip
        player=PlayerPlacement(
            save_x=player[0], save_y=player[1], avatar_x=player[0], avatar_y=player[1],
            prev_x=player[0], prev_y=player[1], x=player[0], y=player[1],
            position_consistent=True, facing=facing, elevation=3, object_event_id=0,
            avatar_flags=0, surfing=False, underwater=False, biking=False, controllable_flag=True,
            forced=False, moving=False, tile_transition_state=0, running_state=0,
        ),  # fmt: skip
        objects=(_object(1, 4, 10), *extra_objects),
        strength_active=False,
    )


def _cache_key_parts(plans, state):
    """The parts ``BoulderPlans.plans`` uses today: they must stay equal between both states."""
    world = plans.world(state)
    positions, mask = world.initial()
    region = world.region(mask, positions, world.start_node())
    return positions, mask, min(region.dist), world.start_node()


def _first_step(state, plans):
    option = Option(
        id="boulder_plan:InventedBoulder2F:switch:6:10", label="Solve the boulder puzzle",
        description="invented", legality="LEGAL", facts={}, executor_ref="field.push_boulders",
        executor_args={"goal_kind": 0, "x": SWITCH[0], "y": SWITCH[1]},
    )  # fmt: skip
    snapshot = SimpleNamespace(map=state, player=None, party=(), flags=None)
    scene = SceneResult(scene_id="field.free", confidence=1.0)
    ctx = StepContext(
        option=option, args=dict(option.executor_args), frame=None, snapshot=snapshot, scene=scene
    )
    ledger = AttemptLedger()
    generator = BoulderMacro(FieldMacros(ledger), plans).push_boulders(ctx)
    try:
        return next(generator), ledger
    except StopIteration:
        return None, ledger


AROUND = ((3, 10), (5, 10), (4, 9), (4, 11))


def _changed(name):
    """Same map, boulder positions, mask, region distance and exits; different static geometry.

    All four landing tiles around the boulder become solid, so no push exists at all.
    """
    if name == "fixed_object":
        return _state(
            extra_objects=tuple(
                _object(2 + i, x, y, role="other") for i, (x, y) in enumerate(AROUND)
            )
        )
    assert name == "collision"
    return _state(walls=AROUND)


@pytest.mark.parametrize("geometry", ["fixed_object", "collision"])
def test_plan_cache_agrees_with_fresh_solver_after_geometry_change(geometry):
    """A cached offer must never survive a geometry change the fresh solver rejects."""
    plans = BoulderPlans()
    solvable = _state()
    assert isinstance(plans.world(solvable).solve(GOAL), Plan)
    cached = plans.plans(solvable, plans.world(solvable), ())[GOAL]
    assert isinstance(cached, Plan)
    baseline_key = _cache_key_parts(plans, solvable)

    changed = _changed(geometry)
    fresh = BoulderPlans().world(changed).solve(GOAL)
    assert isinstance(fresh, NoPlan)
    # Causal precondition: the cache-key parts plans() sees are unchanged.
    assert _cache_key_parts(plans, changed) == baseline_key

    offered = plans.plans(changed, plans.world(changed), ())[GOAL]
    assert type(offered) is type(fresh), (
        f"{geometry}: cached offer {type(offered).__name__} disagrees with fresh "
        f"solve {type(fresh).__name__} ({getattr(fresh, 'reason', '')})"
    )


@pytest.mark.parametrize("geometry", ["fixed_object", "collision"])
def test_stale_offer_never_becomes_a_zero_input_macro_exit(geometry):
    """The real executor re-solves fresh, so a stale offer leaves the option with no input."""
    plans = BoulderPlans()
    solvable = _state()
    assert isinstance(plans.plans(solvable, plans.world(solvable), ())[GOAL], Plan)

    changed = _changed(geometry)
    assert isinstance(BoulderPlans().world(changed).solve(GOAL), NoPlan)
    step, ledger = _first_step(changed, plans)
    assert step is None, step
    assert {attempt.last_reason for attempt in ledger.entries.values()} == {"no_plan:unsolvable"}


def test_unchanged_world_is_reused_and_still_yields_a_push_step():
    """Control: without a geometry change the cache still reuses the plan and inputs follow."""
    plans = BoulderPlans()
    state = _state()
    world = plans.world(state)
    first = plans.plans(state, world, ())[GOAL]
    assert isinstance(first, Plan)
    assert plans.plans(state, plans.world(state), ())[GOAL] is first
    step, _ledger = _first_step(state, plans)
    assert isinstance(step, Tap)
