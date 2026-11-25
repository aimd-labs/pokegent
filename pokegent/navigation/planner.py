"""A*/Dijkstra planner over :mod:`pokegent.navigation.graph`."""

from __future__ import annotations

import heapq
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from itertools import count

from pokegent.events import EventSink, EventType
from pokegent.games.gen3.firered.maps.map_schema import MapState
from pokegent.navigation.graph import NavigationGraph
from pokegent.navigation.models import (
    CHOICE_REQUIREMENTS,
    CapabilityState,
    NavigationEdge,
    NavigationNode,
    NavigationPlan,
    NavigationTarget,
)

OBJECTIVE_FRAME_PLAN_BUDGET = 75_000
OBJECTIVE_FRAME_BUDGET_FACT = "objective route search budget exhausted"
_objective_work: ContextVar[list[int] | None] = ContextVar("objective_work", default=None)


@contextmanager
def objective_frame_budget(events, limit):
    work = [0, 0, limit]  # expansions used, exhausted, allowance
    token = _objective_work.set(work)
    try:
        yield work
    finally:
        _objective_work.reset(token)


def _take_objective_expansion(events) -> bool:
    work = _objective_work.get()
    if work is None:
        return True
    if work[0] >= work[2]:
        if not work[1]:
            work[1] = 1
            if events is not None:
                events.emit(EventType.PLAN_BUDGET_EXCEEDED, public_visibility=True,
                            details={"scope": "objective", "explored": work[0],
                                     "limit": work[2],
                                     "reason": "expansion_limit"})
        return False
    work[0] += 1
    return True


def objective_frame_budget_state():
    return _objective_work.get()


def objective_frame_budget_exhausted() -> bool:
    work = _objective_work.get()
    return work is not None and bool(work[1])


def _choice_step(route: tuple[NavigationEdge, ...]) -> str | None:
    """Stable key of the first edge on ``route`` that only a decision can unblock.

    A fossil choice is not a missing capability: no bag item, badge or flag makes the edge
    available, so the edge is a route step the agent must take instead of an unreachable
    barrier. The whole route must qualify: a route that still needs a capability (a fossil
    followed by a Cut tree) stays capability-blocked.
    """
    unmet = [(edge, row) for edge in route for row in edge.requirements if not row.met]
    if unmet and all(row.key in CHOICE_REQUIREMENTS for edge, row in unmet):
        return unmet[0][0].stable_key
    return None


def _choice_edge(edge: NavigationEdge) -> bool:
    """Whether every unmet requirement of this edge is a decision requirement."""
    unmet = [row for row in edge.requirements if not row.met]
    return bool(unmet) and all(row.key in CHOICE_REQUIREMENTS for row in unmet)


@dataclass(frozen=True)
class _SearchResult:
    route: tuple[NavigationEdge, ...] | None
    cost: int | None
    explored: int
    exhausted: bool = False
    last: NavigationNode | None = None


class NavigationPlanner:
    def __init__(self, graph: NavigationGraph, *, events: EventSink | None = None) -> None:
        self.graph = graph
        self.events = events

    def plan(
        self,
        start: NavigationNode,
        target: NavigationTarget,
        capabilities: CapabilityState = CapabilityState(),
        *,
        live: MapState | None = None,
        limit: int = 250_000,
    ) -> NavigationPlan:
        available = self._search(
            start, target, capabilities, live=live, include_unmet=False, limit=limit
        )
        if available.route is not None:
            return NavigationPlan(
                start,
                target,
                available.cost,
                available.route,
                explored=available.explored,
            )
        work = _objective_work.get()
        if work is not None and (available.exhausted or work[1]):
            return NavigationPlan(start, target, None, explored=work[0],
                                  blocked_route_fact=OBJECTIVE_FRAME_BUDGET_FACT)
        blocked = self._search(
            start, target, capabilities, live=live, include_unmet=True, limit=limit
        )
        exhausted = available if available.exhausted else blocked if blocked.exhausted else None
        if _objective_work.get() is not None and exhausted is not None:
            return NavigationPlan(start, target, None, explored=_objective_work.get()[0],
                                  blocked_route_fact=OBJECTIVE_FRAME_BUDGET_FACT)
        budget_fact = self._budget_fact(exhausted, limit) if exhausted is not None else None
        if blocked.route is None:
            choice = self._choice_frontier(
                start, target, capabilities, live=live, limit=limit
            )
            if choice is not None:
                return choice
            return NavigationPlan(
                start,
                target,
                None,
                explored=max(available.explored, blocked.explored),
                blocked_route_fact=budget_fact,
            )
        unmet = tuple(dict.fromkeys(label for edge in blocked.route for label in edge.unmet))
        return NavigationPlan(
            start,
            target,
            blocked.cost,
            blocked.route,
            unmet=unmet,
            explored=blocked.explored,
            # A capped available-edge proof cannot honestly relabel a guarded
            # route as the only route.  Keep the route for executor context but
            # surface the incomplete proof in the frame.
            blocked_route_fact=budget_fact if unmet else None,
            blocked_choice_step=_choice_step(blocked.route),
        )

    def plan_next_decision(
        self,
        start: NavigationNode,
        target: NavigationTarget,
        capabilities: CapabilityState = CapabilityState(),
        *,
        live: MapState | None = None,
        limit: int = 250_000,
    ) -> NavigationPlan | None:
        """Expose the bounded current-map decision search for the no-route case.

        ``plan`` runs this fallback automatically when a search proves there is no route
        (the Mt Moon fossil shape).  Wild- and training-target entry points
        (``plan_to_maps`` / ``plan_to_tiles``) return no-route plans directly, and the
        adapter calls this to give the agent the next decision step instead of an empty
        frame when nothing on the way is reachable yet.
        """
        return self._choice_frontier(start, target, capabilities, live=live, limit=limit)

    def _choice_frontier(
        self,
        start: NavigationNode,
        target: NavigationTarget,
        capabilities: CapabilityState,
        *,
        live: MapState | None,
        limit: int,
    ) -> NavigationPlan | None:
        """The next decision step when no route could be found at all.

        The Mt Moon fossil choice sits on the only path out of B2F. When the searches cannot
        produce a route (the world search can exceed its budget with unmet edges, and a target
        tile behind the fossils is unreachable until the choice is made), the frame is empty
        and the agent has nothing to follow. A bounded search over the current map still finds
        the decision that has to be made: the nearest edge whose unmet requirements are all
        decision requirements. The plan keeps no route (none is known) and names that step.
        """
        sequence = count()
        distance: dict[NavigationNode, int] = {start: 0}
        heap: list[tuple[int, int, NavigationNode]] = [(0, next(sequence), start)]
        explored = 0
        while heap and explored < limit:
            cost, _order, node = heapq.heappop(heap)
            if cost != distance.get(node):
                continue
            if not _take_objective_expansion(self.events):
                return None
            explored += 1
            for edge in self.graph.edges_from(
                node,
                capabilities,
                live=live if node.map_key == start.map_key else None,
                include_unmet=True,
            ):
                if edge.unmet:
                    if not _choice_edge(edge):
                        continue
                    labels = tuple(dict.fromkeys(edge.unmet))
                    return NavigationPlan(
                        start,
                        target,
                        None,
                        unmet=labels,
                        explored=explored,
                        blocked_route_fact=(
                            f"no route known to {self._map_name(target.map_key)}; "
                            f"next decision: {labels[0]} at "
                            f"{self._map_name(edge.destination.map_key)} "
                            f"({edge.destination.x},{edge.destination.y})"
                        ),
                        blocked_choice_step=edge.stable_key,
                    )
                if edge.destination.map_key != start.map_key:
                    # The step is the decision on the current map, never a guess about a
                    # route through another map whose gates were not searched.
                    continue
                new_cost = cost + edge.cost
                if new_cost >= distance.get(edge.destination, 1 << 60):
                    continue
                distance[edge.destination] = new_cost
                heapq.heappush(heap, (new_cost, next(sequence), edge.destination))
        return None

    def _map_name(self, map_key: tuple[int, int]) -> str:
        state = self.graph.state(map_key)
        name = getattr(getattr(state, "identity", None), "name", None)
        return name or str(map_key)

    def _budget_fact(self, result: _SearchResult, limit: int) -> str:
        node = result.last
        state = self.graph.state(node.map_key) if node is not None else None
        name = (
            state.identity.name if state is not None else str(node.map_key if node else "unknown")
        )
        return (
            f"search budget exhausted after {min(result.explored, limit)} states; "
            f"last reachable map: {name}"
        )

    def plan_to_maps(
        self,
        start: NavigationNode,
        map_keys: frozenset[tuple[int, int]],
        capabilities: CapabilityState = CapabilityState(),
        *,
        live: MapState | None = None,
        limit: int = 250_000,
        exclude_kinds: frozenset[str] = frozenset(),
    ) -> NavigationPlan | None:
        """Find the nearest reachable map in one Dijkstra search.

        Training areas are sets of maps. Searching each floor independently repeats almost the
        entire world traversal; this matcher stops the first time Dijkstra reaches any suitable
        floor and preserves that floor as the selected target.
        """
        if not map_keys:
            return None
        distance: dict[NavigationNode, int] = {start: 0}
        parent: dict[NavigationNode, tuple[NavigationNode, NavigationEdge]] = {}
        heap: list[tuple[int, NavigationNode]] = [(0, start)]
        explored = 0
        while heap and explored < limit:
            cost, node = heapq.heappop(heap)
            if cost != distance.get(node):
                continue
            if not _take_objective_expansion(self.events):
                return None
            explored += 1
            if node.map_key in map_keys:
                return NavigationPlan(
                    start,
                    NavigationTarget(*node.map_key),
                    cost,
                    self._route(parent, node),
                    explored=explored,
                )
            for edge in self.graph.edges_from(
                node,
                capabilities,
                live=live if node.map_key == start.map_key else None,
            ):
                if edge.kind in exclude_kinds:
                    continue
                new_cost = cost + edge.cost
                if new_cost >= distance.get(edge.destination, 1 << 60):
                    continue
                distance[edge.destination] = new_cost
                parent[edge.destination] = node, edge
                heapq.heappush(heap, (new_cost, edge.destination))
        return None

    def plan_to_tiles(
        self,
        start: NavigationNode,
        map_key: tuple[int, int],
        tiles: frozenset[tuple[int, int]],
        capabilities: CapabilityState = CapabilityState(),
        *,
        live: MapState | None = None,
        limit: int = 250_000,
    ) -> NavigationPlan | None:
        """Find the nearest reachable tile in one Dijkstra search."""
        if not tiles:
            return None
        distance: dict[NavigationNode, int] = {start: 0}
        parent: dict[NavigationNode, tuple[NavigationNode, NavigationEdge]] = {}
        heap: list[tuple[int, NavigationNode]] = [(0, start)]
        explored = 0
        while heap and explored < limit:
            cost, node = heapq.heappop(heap)
            if cost != distance.get(node):
                continue
            if not _take_objective_expansion(self.events):
                return None
            explored += 1
            if node.map_key == map_key and (node.x, node.y) in tiles:
                return NavigationPlan(
                    start,
                    NavigationTarget(map_key[0], map_key[1], x=node.x, y=node.y),
                    cost,
                    self._route(parent, node),
                    explored=explored,
                )
            for edge in self.graph.edges_from(
                node,
                capabilities,
                live=live if node.map_key == start.map_key else None,
            ):
                new_cost = cost + edge.cost
                if new_cost >= distance.get(edge.destination, 1 << 60):
                    continue
                distance[edge.destination] = new_cost
                parent[edge.destination] = node, edge
                heapq.heappush(heap, (new_cost, edge.destination))
        return None

    def plan_to_corridor_frontier(
        self,
        start: NavigationNode,
        target: NavigationTarget,
        corridor: tuple[tuple[int, int], ...],
        capabilities: CapabilityState = CapabilityState(),
        *,
        live: MapState | None = None,
        limit: int = 250_000,
    ) -> NavigationPlan | None:
        """Find the deepest closed transition on a map corridor in one search.

        Available edges are explored normally. Unmet edges are considered only when they cross
        to the next map on ``corridor`` and are never enqueued, so the returned route cannot
        contain steps beyond its first closed gate.
        """
        rank = {map_key: index for index, map_key in enumerate(corridor)}
        if len(rank) < 2:
            return None
        tracked_flags = self.graph.stateful_flags_toward(start.map_key, target.map_key)
        initial_flags = frozenset(flag for flag in tracked_flags if flag in capabilities.flags)
        start_state = (start, initial_flags)
        distance: dict[tuple[NavigationNode, frozenset[int]], int] = {start_state: 0}
        parent: dict[
            tuple[NavigationNode, frozenset[int]],
            tuple[tuple[NavigationNode, frozenset[int]], NavigationEdge],
        ] = {}
        sequence = count()
        heap: list[tuple[int, int, tuple[NavigationNode, frozenset[int]]]] = [
            (0, next(sequence), start_state)
        ]
        explored = 0
        frontier: (
            tuple[
                int,
                int,
                tuple[NavigationNode, frozenset[int]],
                NavigationEdge,
            ]
            | None
        ) = None
        while heap and explored < limit:
            cost, _order, search_state = heapq.heappop(heap)
            node, stateful_flags = search_state
            if cost != distance.get(search_state):
                continue
            if not _take_objective_expansion(self.events):
                return None
            explored += 1
            effective = self._capabilities_for_state(capabilities, stateful_flags, tracked_flags)
            for edge in self.graph.edges_from(
                node,
                effective,
                live=live if node.map_key == start.map_key else None,
                include_unmet=True,
            ):
                if edge.unmet:
                    source_rank = rank.get(node.map_key)
                    destination_rank = rank.get(edge.destination.map_key)
                    if (
                        source_rank is not None
                        and destination_rank == source_rank + 1
                        and (
                            frontier is None
                            or destination_rank > frontier[0]
                            or (destination_rank == frontier[0] and cost + edge.cost < frontier[1])
                        )
                    ):
                        frontier = destination_rank, cost + edge.cost, search_state, edge
                    continue
                new_cost = cost + edge.cost
                next_flags = self._next_stateful_flags(stateful_flags, edge, tracked_flags)
                next_state = edge.destination, next_flags
                if new_cost >= distance.get(next_state, 1 << 60):
                    continue
                distance[next_state] = new_cost
                parent[next_state] = search_state, edge
                heapq.heappush(heap, (new_cost, next(sequence), next_state))
        if frontier is None:
            return None
        _, cost, search_state, edge = frontier
        route = self._route_stateful(parent, search_state) + (edge,)
        unmet = tuple(dict.fromkeys(label for row in route for label in row.unmet))
        return NavigationPlan(
            start,
            target,
            cost,
            route,
            unmet=unmet,
            explored=explored,
        )

    def _search(
        self,
        start: NavigationNode,
        target: NavigationTarget,
        capabilities: CapabilityState,
        *,
        live: MapState | None,
        include_unmet: bool,
        limit: int,
    ) -> _SearchResult:
        matches = self._matcher(target)
        tracked_flags = self.graph.stateful_flags_toward(start.map_key, target.map_key)
        initial_flags = frozenset(flag for flag in tracked_flags if flag in capabilities.flags)
        start_state = (start, initial_flags)
        distance: dict[tuple[NavigationNode, frozenset[int]], int] = {start_state: 0}
        parent: dict[
            tuple[NavigationNode, frozenset[int]],
            tuple[tuple[NavigationNode, frozenset[int]], NavigationEdge],
        ] = {}
        sequence = count()
        heap: list[tuple[int, int, int, tuple[NavigationNode, frozenset[int]]]] = [
            (self._heuristic(start, target), 0, next(sequence), start_state)
        ]
        explored = 0
        last = start
        while heap:
            _score, cost, _order, search_state = heapq.heappop(heap)
            node, stateful_flags = search_state
            if cost != distance.get(search_state):
                continue
            if explored >= limit or not _take_objective_expansion(self.events):
                if self.events is not None:
                    if _objective_work.get() is None:
                        self.events.emit(
                        EventType.PLAN_BUDGET_EXCEEDED,
                        public_visibility=True,
                        details={
                            "limit": limit,
                            "expanded": explored,
                            "phase": "blocked" if include_unmet else "available",
                            "reason": "expansion_limit",
                        },
                        )
                return _SearchResult(None, None, explored, exhausted=True, last=node)
            explored += 1
            last = node
            if matches(node):
                return _SearchResult(self._route_stateful(parent, search_state), cost, explored)
            effective = self._capabilities_for_state(capabilities, stateful_flags, tracked_flags)
            for edge in self.graph.edges_from(
                node,
                effective,
                live=live if node.map_key == start.map_key else None,
                include_unmet=include_unmet,
            ):
                penalty = 10_000 * len(edge.unmet) if include_unmet else 0
                new_cost = cost + edge.cost + penalty
                next_flags = self._next_stateful_flags(stateful_flags, edge, tracked_flags)
                next_state = (edge.destination, next_flags)
                if new_cost >= distance.get(next_state, 1 << 60):
                    continue
                distance[next_state] = new_cost
                parent[next_state] = search_state, edge
                score = new_cost + self._heuristic(edge.destination, target)
                heapq.heappush(heap, (score, new_cost, next(sequence), next_state))
        return _SearchResult(None, None, explored, last=last)

    def _capabilities_for_state(
        self,
        capabilities: CapabilityState,
        stateful_flags: frozenset[int],
        tracked_flags: frozenset[int],
    ) -> CapabilityState:
        flags = (capabilities.flags - tracked_flags) | stateful_flags
        return replace(capabilities, flags=frozenset(flags))

    def _next_stateful_flags(
        self,
        stateful_flags: frozenset[int],
        edge: NavigationEdge,
        tracked_flags: frozenset[int],
    ) -> frozenset[int]:
        if edge.kind != "toggle":
            return stateful_flags
        flag = edge.target_dict().get("flag")
        if not isinstance(flag, int) or flag not in tracked_flags:
            return stateful_flags
        next_flags = set(stateful_flags)
        if edge.target_dict().get("toggle_to") is True:
            next_flags.add(flag)
        else:
            next_flags.discard(flag)
        return frozenset(next_flags)

    @staticmethod
    def _route_stateful(
        parent: dict[
            tuple[NavigationNode, frozenset[int]],
            tuple[tuple[NavigationNode, frozenset[int]], NavigationEdge],
        ],
        state: tuple[NavigationNode, frozenset[int]],
    ) -> tuple[NavigationEdge, ...]:
        edges: list[NavigationEdge] = []
        while state in parent:
            state, edge = parent[state]
            edges.append(edge)
        edges.reverse()
        return tuple(edges)

    def _matcher(self, target: NavigationTarget) -> Callable[[NavigationNode], bool]:
        state = self.graph.maps.get(target.map_key)
        warp = None
        obj = None
        if state is not None and target.warp_index is not None:
            warp = next((row for row in state.warps if row.index == target.warp_index), None)
        if state is not None and target.local_id is not None:
            obj = state.object_by_local_id(target.local_id)

        def matches(node: NavigationNode) -> bool:
            if node.map_key != target.map_key:
                return False
            if target.x is not None and target.y is not None:
                return (node.x, node.y) == (target.x, target.y)
            if warp is not None:
                return (node.x, node.y) == (warp.x, warp.y)
            if obj is not None:
                return abs(node.x - obj.x) + abs(node.y - obj.y) == 1
            return True

        return matches

    @staticmethod
    def _heuristic(node: NavigationNode, target: NavigationTarget) -> int:
        if node.map_key != target.map_key or target.x is None or target.y is None:
            return 0
        return abs(node.x - target.x) + abs(node.y - target.y)

    @staticmethod
    def _route(
        parent: dict[NavigationNode, tuple[NavigationNode, NavigationEdge]],
        node: NavigationNode,
    ) -> tuple[NavigationEdge, ...]:
        edges: list[NavigationEdge] = []
        while node in parent:
            previous, edge = parent[node]
            edges.append(edge)
            node = previous
        edges.reverse()
        return tuple(edges)


def next_semantic_edge(plan: NavigationPlan) -> NavigationEdge | None:
    """First executor-sized action, after any ordinary walking prefix.

    A target option already contains the walk to it, so the builder relates an option to the
    first warp/connection/field move/forced segment rather than exposing individual buttons.
    """
    for edge in plan.route:
        if edge.kind not in ("walk", "bike", "ledge", "forced"):
            return edge
    return plan.next_step
