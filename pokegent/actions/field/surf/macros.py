"""Executors of the Surf options: walk to the shore and press A; swim to a landing and step ashore.

They are the field family's own walker (:class:`~pokegent.actions.field.macros.FieldMacros`)
with two
targets of their own, so everything it guarantees holds here too: short verified segments, the
position,
facing and map re-checked after every tap, ``predict_step`` before every press, a foreign scene at a
boundary stops the macro, the attempt ledger records typed failures.

* ``field.start_surf`` (args: the smallest water tile of the body): walk to the cheapest
reachable shore
  edge, turn to face the water (a blocked-side turn tap), re-check the start rule on the fresh
  state, press A.
  The macro ends when the game's question is on the screen (``took_over``); the dialog family
  answers it
  and the Surf animation runs in the scenes of the dialog family. The macro never presses YES
  itself.
* ``field.dismount`` (args: the smallest landing tile of the group): the plan swims (every step
is a water
  step the game allows) and ends with the step onto land that ends the Surf; arrival is read back.
"""

from __future__ import annotations

from pokegent.actions._exec.models import ExecutionKind
from pokegent.actions._exec.registry import Arg, ExecutorSpec, StepContext
from pokegent.actions.field import evidence as E
from pokegent.actions.field import macros as M
from pokegent.actions.field import nav as N
from pokegent.actions.field import targets as T
from pokegent.actions.field.attempts import AttemptLedger
from pokegent.actions.field.surf import options as O
from pokegent.actions.field.surf import targets as ST
from pokegent.games.gen3.firered.maps.map_schema import MapState
from pokegent.games.gen3.firered.maps.map_surf import start_surf


def resolve_start(state: MapState, nav: N.Navigator, x: int, y: int) -> T.Target | None:
    """The water body that contains ``(x, y)`` as a target whose approaches are all its shore
    edges."""
    for body in ST.water_bodies(state, nav):
        if (x, y) in body.tiles:
            edges = ST.shore_edges(state, body)
            if not edges:
                return None
            return T.Target(
                body.option_id,
                "surf",
                "talk",
                "Surf across the water",
                tuple(T.Approach(e.stand, e.face) for e in edges),
                at=body.canon,
                info={"x": body.canon[0], "y": body.canon[1]},
            )
    return None


def resolve_landing(state: MapState, nav: N.Navigator, x: int, y: int) -> T.Target | None:
    for group in ST.landing_groups(state, nav, nav.search()):
        if (x, y) == group.canon or (x, y) in group.landings:
            return T.Target(
                group.option_id,
                "landing",
                "none",
                "Step ashore",
                tuple(T.Approach(tile) for tile in group.landings),
                at=group.canon,
                info={"x": group.canon[0], "y": group.canon[1]},
            )
    return None


class SurfMacros(M.FieldMacros):
    def __init__(self, ledger: AttemptLedger | None = None) -> None:
        super().__init__(ledger)

    def specs(self) -> list[ExecutorSpec]:
        coord = Arg(int, min=0, max=1023)
        budget = dict(max_inputs=500, max_frames=9000, settle_frames=900)
        allowed = frozenset({E.FREE, M.UNKNOWN})
        return [
            ExecutorSpec(
                O.START_SURF,
                ExecutionKind.INTENT,
                self.start_surf,
                args={"x": coord, "y": coord},
                allowed_scenes=allowed,
                **budget,
            ),  # fmt: skip
            ExecutorSpec(
                O.DISMOUNT,
                ExecutionKind.INTENT,
                self.dismount,
                args={"x": coord, "y": coord},
                allowed_scenes=allowed,
                **budget,
            ),  # fmt: skip
        ]

    def start_surf(self, ctx: StepContext):
        x, y = ctx.args["x"], ctx.args["y"]
        return self._run(ctx, lambda st, nav: resolve_start(st, nav, x, y), press_a=True)

    def dismount(self, ctx: StepContext):
        x, y = ctx.args["x"], ctx.args["y"]
        return self._run(ctx, lambda st, nav: resolve_landing(st, nav, x, y))

    @staticmethod
    def _interaction_still_valid(state: MapState, approach: T.Approach, target: T.Target) -> bool:
        if target.kind == "surf":
            # the start rule on the fresh state: the water in front is still water the prompt
            # accepts
            return (
                approach.face is not None
                and start_surf(state, approach.face, from_xy=approach.stand).ok
            )
        return M.FieldMacros._interaction_still_valid(state, approach, target)


def executor_specs(ledger: AttemptLedger | None = None) -> list[ExecutorSpec]:
    return SurfMacros(ledger).specs()
