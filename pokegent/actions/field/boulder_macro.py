"""Executor of ``boulder_plan:*``: arm Strength if needed, then push, verifying every single push.

The macro never pushes blind:

1. **Arming.** If Strength is not active on this map load (``FLAG_SYS_USE_STRENGTH`` clear) the
macro
   does what the ``strength:<id>`` option does (walk to the boulder it will push first, face it,
   press A)
   and ends there; the YES/NO prompt belongs to the dialog family. The runner then offers the
   plan again,
   now armed.
2. **Pushes.** For each push of the plan: the boulder must stand where the plan says (else
re-plan from
   the live state), walk to the stand tile on the planner's own path (arrow mats are walked; the
   walker of
   ``targets.py`` treats every warp tile as terminal), face the boulder, check with
   ``predict_step`` that
   the press is a push of THAT boulder and with ``simulate_push`` that it lands where the plan
   says, tap,
   wait for the controls, and read the boulder back: it must have moved by exactly the expected
   delta (a
   fall: gone). A boulder that did not move is a refused push (at most 2 per push); anywhere
   else is a
   mismatch and re-plans (at most 3 times). Everything stops at a foreign scene (a trainer, a
   dialog).
3. **Done** when the goal holds (a boulder on the switch, in the hole, or the exit reachable); the
   attempt ledger records the failure reason otherwise (``no_plan:*``, ``push_refused``,
   ``mismatch``).
"""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import replace

from pokegent.actions._exec.models import ExecutionKind, Step, Tap
from pokegent.actions._exec.registry import Arg, ExecutorSpec, StepContext
from pokegent.actions.field import evidence as E
from pokegent.actions.field import nav as N
from pokegent.actions.field import targets as T
from pokegent.actions.field.attempts import capability_signature
from pokegent.actions.field.boulder_options import PUSH_BOULDERS, BoulderPlans, goal_from_args
from pokegent.actions.field.boulders import Goal, Plan, Push
from pokegent.actions.field.macros import (
    BUTTON,
    STEP_HOLD,
    STEP_WAIT,
    TURN_HOLD,
    TURN_WAIT,
    FieldMacros,
    _find,
    _map_key,
    _Walk,
)
from pokegent.games.gen3.firered.maps.map_boulders import armed, boulders, simulate_push
from pokegent.games.gen3.firered.maps.map_motion import predict_step
from pokegent.games.gen3.firered.maps.map_schema import MapState
from pokegent.pipeline import Observation

MAX_REPLANS = 3
MAX_REFUSALS = 2


class BoulderMacro:
    def __init__(self, macros: FieldMacros, plans: BoulderPlans) -> None:
        self.macros = macros
        self.plans = plans

    def spec(self) -> ExecutorSpec:
        return ExecutorSpec(
            PUSH_BOULDERS,
            ExecutionKind.INTENT,
            self.push_boulders,
            args={
                "goal_kind": Arg(int, min=0, max=3),
                "x": Arg(int, min=0, max=1023),
                "y": Arg(int, min=0, max=1023),
            },
            allowed_scenes=frozenset({E.FREE, "scene.unknown"}),
            max_inputs=900,
            max_frames=30000,
            settle_frames=900,
        )

    # -- helpers
    # -------------------------------------------------------------------------------------

    @staticmethod
    def _boulder(state: MapState, local_id: int):
        return next((b for b in boulders(state) if b.local_id == local_id), None)

    @staticmethod
    def _arming_target(state: MapState, nav: N.Navigator, ids: list[int]):
        for local_id in dict.fromkeys(ids):
            target = _find(state, nav, f"strength:{local_id}")
            if target is not None and T.plan_target(nav, nav.search(), target) is not None:
                return target
        for target in T.enumerate_targets(state, nav):
            if target.option_id.startswith("strength:") and T.plan_target(
                nav, nav.search(), target
            ):
                return target
        return None

    def _replan(self, state: MapState, goal: Goal) -> Plan | str:
        result = self.plans.world(state).solve(goal)
        return result if isinstance(result, Plan) else f"no_plan:{result.reason}"

    # -- the macro
    # -----------------------------------------------------------------------------------

    def push_boulders(self, ctx: StepContext) -> Generator[Step, Observation, None]:
        m = self.macros
        state0 = ctx.snapshot.map
        assert state0 is not None
        goal = goal_from_args(ctx.args)
        start_map = _map_key(state0)
        key = m.ledger.key(ctx.option.id, start_map, capability_signature(ctx.snapshot))
        walk = _Walk(state0, Observation(ctx.snapshot, ctx.scene))
        reason = ""
        progressed = False
        try:
            plan = self._replan(state0, goal)
            if isinstance(plan, str):
                reason = plan
                return
            if armed(state0) is not True:
                ids = [p.boulder for p in plan.pushes]
                yield from m._run(
                    ctx, lambda st, nav: self._arming_target(st, nav, ids), press_a=True
                )
                return
            pushes = list(plan.pushes)
            n = replans = 0
            refusals: dict[tuple, int] = {}
            while n < len(pushes):
                push = pushes[n]
                outcome = yield from self._one_push(walk, push, start_map)
                if outcome == "pushed":
                    n += 1
                    progressed = True
                    continue
                if outcome == "refused":
                    k = (push.boulder, push.direction, push.src)
                    refusals[k] = refusals.get(k, 0) + 1
                    if refusals[k] >= MAX_REFUSALS:
                        reason = "push_refused"
                        return
                    continue
                if outcome == "mismatch":
                    replans += 1
                    if replans > MAX_REPLANS:
                        reason = "mismatch"
                        return
                    new = self._replan(walk.state, goal)
                    if isinstance(new, str):
                        reason = new
                        return
                    pushes, n = list(new.pushes), 0
                    continue
                reason = outcome  # left_map / interrupted / failed
                return
            m.ledger.record_success(key)
        finally:
            if reason and reason != "left_map":
                m.ledger.record_failure(key, reason, progressed=progressed)

    def _one_push(
        self, walk: _Walk, push: Push, start_map: tuple[int, int]
    ) -> Generator[Step, Observation, str]:
        """pushed | refused | mismatch | left_map | failed"""
        m = self.macros
        state = walk.state
        boulder = self._boulder(state, push.boulder)
        if boulder is None or boulder.hidden or boulder.xy != push.src:
            return "mismatch"
        # -- walk to the stand tile on the planner's own path
        world = self.plans.world(state)
        positions, mask = world.initial()
        region = world.region(mask, positions, world.start_node())
        node = world._stand_node(region, push.stand, boulder.elevation)
        if node is None:
            return "mismatch"
        moves = region.path_to(node)
        if moves:
            target = T.tile_target(state, *push.stand)
            tplan = T.Plan(target, moves, T.Approach(push.stand, None), len(moves), None)
            outcome = yield from m._follow(walk, tplan, target, start_map)
            if outcome == "replan":
                return "mismatch"
            if outcome != "arrived":
                return outcome
        state = walk.state
        player = state.player
        assert player is not None
        if (player.avatar_x, player.avatar_y) != push.stand:
            return "mismatch"
        # -- face the boulder (a turn tap never pushes: the game only turns for a short hold)
        if player.facing != push.direction:
            obs = yield Tap(BUTTON[push.direction], hold_frames=TURN_HOLD, wait_frames=TURN_WAIT)
            ok = yield from m._adopt(walk, obs, start_map)
            if ok != "ok":
                return ok
            if walk.state.player.facing != push.direction:
                return "failed"
        # -- never push blind: the game's rules must predict THIS push now
        state = walk.state
        pred = predict_step(state, push.direction)
        sim = simulate_push(state, push.boulder, push.direction)
        if (
            pred.outcome != "push_boulder"
            or pred.object_local_id != push.boulder
            or not sim.legal
            or sim.to_xy != push.dst
        ):
            return "mismatch"
        obs = yield Tap(BUTTON[push.direction], hold_frames=STEP_HOLD, wait_frames=STEP_WAIT)
        walk.steps += 1
        ok = yield from m._adopt(walk, obs, start_map)
        if ok != "ok":
            return ok
        after = self._boulder(walk.state, push.boulder)
        if push.effect == "fall":
            if after is None or after.hidden:
                return "pushed"
        elif after is not None and not after.hidden and after.xy == push.dst:
            return "pushed"
        if after is not None and not after.hidden and after.xy == push.src:
            return "refused"
        return "mismatch"


def macro_spec(macros: FieldMacros, plans: BoulderPlans) -> ExecutorSpec:
    return BoulderMacro(macros, plans).spec()


__all__ = ["BoulderMacro", "macro_spec", "replace"]
