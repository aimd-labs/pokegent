"""Issue #56: a reach/map-arrival objective on ANOTHER map must still emit intra-map
walk-forward tiles so a city/interior frame can cross the current map instead of seam-pinging
(Cerulean -> Route 5, live-0030)."""
from pokegent.actions.field.builder import FieldBuilder
from pokegent.goals.models import MapArrival, Objective, Target
from pokegent.goals.validate import ResolvedObjective, ResolvedProbe, ResolvedTarget
from pokegent.navigation.models import (
    NavigationEdge,
    NavigationNode,
    NavigationPlan,
    NavigationTarget,
)


def _edge(kind, sg, sn, sx, sy, dg, dn, dx, dy, key) -> NavigationEdge:
    return NavigationEdge(
        stable_key=key,
        source=NavigationNode(sg, sn, sx, sy),
        destination=NavigationNode(dg, dn, dx, dy),
        kind=kind,
        cost=1,
    )


def _objective() -> Objective:
    return Objective(
        objective_id="reach_route5",
        revision=1,
        kind="reach",
        target=Target(map="Route5"),
        success_probes=(MapArrival(map="Route5"),),
    )


def _resolved() -> ResolvedObjective:
    return ResolvedObjective(
        objective=_objective(),
        target=ResolvedTarget(group=3, number=23),
        probes=(ResolvedProbe("map_arrival", map_key=(3, 23)),),
    )


def _plan(walk_count: int) -> NavigationPlan:
    start = NavigationNode(3, 3, 8, 22)
    target = NavigationTarget(3, 23)
    edges = [
        _edge("walk", 3, 3, 8, 22 - i, 3, 3, 8, 22 - i - 1, f"walk:{i}")
        for i in range(walk_count)
    ]
    # after the walk prefix the plan leaves the current map (connection / warp)
    edges.append(_edge("connection", 3, 3, 8, 22 - walk_count, 3, 23, 0, 0, "edge:N:3:23"))
    return NavigationPlan(start=start, target=target, cost=walk_count, route=tuple(edges))


def test_reach_target_on_other_map_still_emits_in_map_walk_tiles():
    plan = _plan(walk_count=9)
    tiles = FieldBuilder._in_map_forward_walk_tiles(
        plan, (3, 3), _resolved()
    )
    assert len(tiles) >= 5  # the walk prefix through the city is exposed
    # every tile is ON the current map (forward through the city)
    assert all(isinstance(x, int) and isinstance(y, int) for x, y in tiles)


def test_no_walk_prefix_yields_empty_tiles():
    plan = _plan(walk_count=0)  # plan immediately leaves the map
    tiles = FieldBuilder._in_map_forward_walk_tiles(plan, (3, 3), _resolved())
    assert tiles == ()


def test_no_resolved_or_no_route_is_empty():
    assert FieldBuilder._in_map_forward_walk_tiles(None, (3, 3), _resolved()) == ()
    plan = _plan(3)
    assert FieldBuilder._in_map_forward_walk_tiles(plan, (3, 3), None) == ()
    assert FieldBuilder._in_map_forward_walk_tiles(plan, (3, 3), _resolved())  # sanity
