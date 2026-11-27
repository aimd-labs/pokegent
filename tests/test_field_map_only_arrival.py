"""Map-only targets arrived on their map must not re-offer warps out of it (#413)."""
from test_field_warp_trigger_seam import _state, _warp
from test_planner_fossil_step import _FakeGraph
from test_probe_primitives import snapshot

from pokegent.actions.field.builder import FieldBuilder
from pokegent.games.gen3.firered.maps.map_behaviors import MB
from pokegent.goals.models import Objective
from pokegent.goals.validate import ResolvedObjective, ResolvedTarget
from pokegent.navigation import models as NM
from pokegent.navigation.adapter import NavigationAdapter, NavigationLookup
from pokegent.options.registry import BuildContext
from pokegent.state.scenes import SceneResult

PP, ROUTE10 = (1, 95), (3, 28)


class _Graph(_FakeGraph):
    def scripted_warp_arrival(self, *_args, **_kwargs):
        return None


def _power_plant():
    return _state((_warp(1, 2, 1, 2, behavior=MB["MB_CAVE_DOOR"], kind="cave_door",
                         trigger="step", dest=ROUTE10),), extra=(((2, 1), 4),),
                  identity=PP, name="Power Plant")


def _place_resolved(map_key):
    objective = Objective(objective_id="powerplant_map", revision=1, kind="reach",
                          target={"map": "Power Plant"})
    return ResolvedObjective(objective, ResolvedTarget(group=map_key[0], number=map_key[1],
                                                       map_name="Power Plant"), ())


def _draft(plan, resolved, goal_id):
    adapter = NavigationAdapter(_Graph(names={PP: "Power Plant", ROUTE10: "Route 10"}))
    adapter.plan_result = lambda *_a, **_k: NavigationLookup(plan)
    snap = snapshot().model_copy(update={"map": _power_plant()})
    ctx = BuildContext(snapshot=snap, scene=SceneResult(scene_id="field.free",
                                                        confidence=1, detector="invented"),
                       goal_id=goal_id)
    return FieldBuilder(field_menu=False, objectives=lambda _g: resolved,
                        navigation=adapter).build(ctx)


def _leaving(options, map_key):
    return [o for o in options if o.chooseable and isinstance(o.facts.get("target"), dict)
            and o.facts["target"].get("kind") in {"warp", "edge"}
            and o.facts["target"].get("dest") is not None
            and tuple(o.facts["target"]["dest"]) != map_key]


def test_arrived_map_only_target_does_not_offer_exit_warps():
    start = NM.NavigationNode(*PP, 2, 2)
    plan = NM.NavigationPlan(start, NM.NavigationTarget(*PP), cost=0, route=())
    assert not _leaving(_draft(plan, _place_resolved(PP), "powerplant_map").options, PP)


def test_not_arrived_map_only_target_keeps_exit_warps():
    start = NM.NavigationNode(*PP, 2, 2)
    route = (NM.NavigationEdge("warp:95:1", start, NM.NavigationNode(*ROUTE10, 8, 40), "warp", 5),)
    plan = NM.NavigationPlan(start, NM.NavigationTarget(*ROUTE10), cost=5, route=route)
    draft = _draft(plan, _place_resolved(ROUTE10), "route10_map")
    assert next(o for o in draft.options if o.id == "warp:1:95:1").chooseable
