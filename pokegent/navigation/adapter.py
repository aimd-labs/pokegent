"""Objective-facing navigation adapter."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any

from pokegent.games.gen3.firered.maps.map_names import map_name
from pokegent.games.gen3.firered.maps.obstacles import option_key
from pokegent.games.gen3.firered.reader.capabilities import field_capability
from pokegent.goals.validate import ResolvedObjective, resolve_map
from pokegent.navigation.capabilities import from_snapshot
from pokegent.navigation.graph import NavigationGraph
from pokegent.navigation.models import (
    NavigationEdge,
    NavigationNode,
    NavigationPlan,
    NavigationTarget,
)
from pokegent.navigation.planner import NavigationPlanner, next_semantic_edge
from pokegent.navigation.trigger_matching import executable_route, first_planned_gate_at
from pokegent.options.models import Legality, Option

_PLAN_UNSET = object()


def _map_label(graph: NavigationGraph, map_key: tuple[int, int]) -> str:
    state = graph.state(map_key)
    if state is not None and state.identity.name:
        return state.identity.name
    return map_name(*map_key) or str(map_key)


def _dynamic_warp_destination(graph: NavigationGraph, dynamic) -> NavigationNode | None:
    """Normalize a live WarpData, including WARP_ID_NONE's coordinate arrival."""
    if dynamic is None:
        return None
    map_key = (dynamic.map_group, dynamic.map_number)
    state = graph.maps.get(map_key)
    if state is None:
        return None
    if not (0 <= dynamic.x < state.grid.columns and 0 <= dynamic.y < state.grid.rows):
        return None
    return NavigationNode(dynamic.map_group, dynamic.map_number, dynamic.x, dynamic.y)


@dataclass
class _Visited:
    maps: set[tuple[int, int]] = field(default_factory=set)

    def note(self, key: tuple[int, int]) -> None:
        self.maps.add(key)


class NavigationAdapter:
    def __init__(
        self,
        graph: NavigationGraph,
        *,
        visited: Any = None,
        plan_cache_size: int = 64,
    ) -> None:
        self.graph = graph
        self.planner = NavigationPlanner(graph)
        self.visited = visited if visited is not None else _Visited()
        self._plan_cache_size = max(0, plan_cache_size)
        self._plan_cache: OrderedDict[tuple[object, ...], NavigationPlan | None] = OrderedDict()
        self._closed_warp_cache: dict[tuple[object, ...], bool] = {}
        self._closed_warp_cache_map: tuple[int, int] | None = None

    def plan(self, snapshot, resolved: ResolvedObjective | None) -> NavigationPlan | None:
        state = snapshot.map
        if state is None or state.player is None or resolved is None:
            return None
        current = (state.identity.group, state.identity.number)
        self.visited.note(current)
        start = self.graph.node(current, state.player.avatar_x, state.player.avatar_y)
        capabilities = from_snapshot(snapshot, visited_maps=frozenset(self.visited.maps))
        # Objective policy is part of the agent's routing contract, not just an option-frame
        # filter.  In particular, a hard ``forbid: ["fly"]`` must keep the graph search from
        # selecting a Fly edge as the cheapest route.
        if "fly" in getattr(resolved.objective, "forbid", ()):
            capabilities = replace(
                capabilities,
                capabilities=frozenset(
                    capability for capability in capabilities.capabilities if capability != "fly"
                ),
            )
        target = self._target(resolved)
        # MapState and CapabilityState are immutable value objects.  Keeping the complete live
        # map in the key deliberately over-keys this cache: every RAM-backed fact the graph may
        # inspect (player mode/facing, live grid, object visibility/positions, and puzzle state)
        # must compare equal before a plan can be reused.  CapabilityState adds badges, field
        # moves, bag items, flags, vars and visited Fly destinations; its non-comparing
        # ``live_objects`` field is covered by MapState.
        cache_key = (
            start,
            getattr(state.player, "facing", None),
            getattr(state.player, "surfing", False),
            target,
            getattr(resolved.objective, "objective_id", None),
            resolved.objective.kind,
            getattr(resolved.objective, "revision", None),
            capabilities,
            state,
            tuple(sorted(getattr(resolved, "navigation_map_keys", frozenset()))),
        )
        try:
            hash(cache_key)
        except TypeError:
            # Unit-test adapters and third-party callers may supply mutable stand-ins instead of
            # the immutable MapState contract.  They are valid planner inputs but not safe cache
            # identities, so preserve the pre-cache behavior for them.
            cache_key = None
        if cache_key is not None:
            cached = self._plan_cache.get(cache_key, _PLAN_UNSET)
            if cached is not _PLAN_UNSET:
                self._plan_cache.move_to_end(cache_key)
                return cached
        train_plan = None
        if resolved.objective.kind == "train":
            target, train_plan = self._train_target(
                start,
                target,
                capabilities,
                state,
                resolved,
            )
        wild_maps = getattr(resolved, "navigation_map_keys", frozenset())
        if wild_maps:
            plan = self.planner.plan_to_maps(
                start, wild_maps, capabilities, live=state
            )
            if plan is not None:
                target = plan.target
            else:
                unreachable = min(wild_maps)
                target = NavigationTarget(*unreachable)
                name = _map_label(self.graph, unreachable)
                plan = NavigationPlan(
                    start,
                    target,
                    None,
                    blocked_route_fact=(
                        f"no reachable ROM wild encounter map for the target; "
                        f"candidate map: {name}"
                    ),
                )
                decision = self.planner.plan_next_decision(
                    start, target, capabilities, live=state
                )
                if decision is not None:
                    plan = NavigationPlan(
                        start,
                        target,
                        None,
                        unmet=decision.unmet,
                        explored=max(plan.explored, decision.explored),
                        blocked_route_fact=decision.blocked_route_fact or plan.blocked_route_fact,
                        blocked_choice_step=decision.blocked_choice_step,
                    )
        elif target is not None:
            plan = train_plan or self.planner.plan(start, target, capabilities, live=state)
        else:
            plan = None
        if plan is not None:
            plan = self._route_to_corridor_frontier(plan, target, capabilities, state)
            plan = self._route_to_boulder_prerequisite(plan, target, capabilities, state)
            plan = self._stage_boulder_action(plan, target)
            # A target NPC is a destination *within* the target map.  The live map can
            # temporarily make its adjacent tile unwalkable (another event object may be
            # blocking it), while the route to that map is still the useful next step.  Do
            # not lose the map-level objective to an over-specific final-tile match.
            if plan.cost is None and target.local_id is not None:
                map_plan = self.planner.plan(
                    start,
                    NavigationTarget(target.map_group, target.map_number),
                    capabilities,
                    live=state,
                )
                plan = NavigationPlan(
                    map_plan.start,
                    target,
                    map_plan.cost,
                    map_plan.route,
                    unmet=map_plan.unmet,
                    explored=map_plan.explored,
                    blocked_choice_step=map_plan.blocked_choice_step,
                )
            if plan.cost is None and not plan.route and plan.blocked_route_fact is None:
                # A finite search with no unmet edge is still useful evidence: the target is
                # disconnected from the current RAM position in the ROM graph.  Preserve the
                # last reachable map so a caller never turns an active objective into a silent
                # goal-less frame.
                here = self.graph.state(current)
                target_state = self.graph.state(target.map_key)
                here_name = here.identity.name if here is not None else str(current)
                target_name = (
                    target_state.identity.name if target_state is not None else str(target.map_key)
                )
                plan = replace(
                    plan,
                    blocked_route_fact=(
                        f"no path known from {here_name} to {target_name}; "
                        f"last reachable map: {here_name}"
                    ),
                )
            plan = self._route_to_counted_trainer(plan, capabilities, state)
            self._remember_plan(cache_key, plan)
            return plan
        self._remember_plan(cache_key, None)
        return None

    def _route_to_corridor_frontier(self, plan, target, capabilities, live):
        """Keep progress up to, but never through, the first closed corridor transition.

        One bounded search finds a ROM-proven gate after a reachable prefix. The gate remains a
        diagnostic fact, while only edges ending at its source are executable route steps.
        """
        if plan.cost is not None or plan.route or plan.blocked_route_fact is not None:
            return plan
        corridor = self.graph.map_route(plan.start.map_key, target.map_key)
        frontier = self.planner.plan_to_corridor_frontier(
            plan.start,
            target,
            corridor,
            capabilities,
            live=live,
        )
        if frontier is None:
            return plan
        blocked_index = next(
            (index for index, edge in enumerate(frontier.route) if edge.unmet), None
        )
        if blocked_index is None:
            return frontier
        blocked_route_fact = self._blocked_route(frontier, blocked_index)
        route = frontier.route[:blocked_index]
        return NavigationPlan(
            plan.start,
            target,
            sum(edge.cost for edge in route),
            route,
            unmet=frontier.route[blocked_index].unmet,
            explored=max(plan.explored, frontier.explored),
            blocked_route_fact=blocked_route_fact,
        )

    def _remember_plan(self, key: tuple[object, ...] | None, plan: NavigationPlan | None) -> None:
        if key is None or self._plan_cache_size == 0:
            return
        self._plan_cache[key] = plan
        self._plan_cache.move_to_end(key)
        while len(self._plan_cache) > self._plan_cache_size:
            self._plan_cache.popitem(last=False)

    def _train_target(
        self, start, target, capabilities, live, resolved
    ) -> tuple[NavigationTarget | None, NavigationPlan | None]:
        """Resolve training to the nearest reachable encounter tile on the target map.

        A map can have several walkable components (for example Route 15's west and east
        segments).  A map-only target treats the first component as arrival and leaves the
        field builder with no grass option.  Search the ROM's encounter tiles as concrete
        destinations instead, so the normal graph route crosses any intervening warp maps.
        """
        if target is None:
            return None, None
        destination = self.graph.state(target.map_key)
        if destination is None:
            return target, None
        encounter_tiles = tuple(
            (x, y)
            for y in range(destination.layout.height)
            for x in range(destination.layout.width)
            if (
                destination.tile(x, y).walkable
                and (
                    destination.tile(x, y).tall_grass or destination.tile(x, y).encounter == "land"
                )
            )
        )
        selected = self.planner.plan_to_tiles(
            start,
            target.map_key,
            frozenset(encounter_tiles),
            capabilities,
            live=live,
        )
        if selected is not None:
            return selected.target, selected
        map_name = destination.identity.name or resolved.target.map_name or str(target.map_key)
        plan = NavigationPlan(
            start,
            target,
            None,
            blocked_route_fact=f"no reachable encounter tile on {map_name}",
        )
        decision = self.planner.plan_next_decision(
            start, target, capabilities, live=live
        )
        if decision is not None:
            plan = NavigationPlan(
                start,
                target,
                None,
                unmet=decision.unmet,
                explored=max(plan.explored, decision.explored),
                blocked_route_fact=decision.blocked_route_fact or plan.blocked_route_fact,
                blocked_choice_step=decision.blocked_choice_step,
            )
        return target, plan

    @staticmethod
    def _stage_boulder_action(plan, target):
        """Stop the advertised route at the first state-changing boulder macro.

        The graph edge crosses the newly unlocked exit so the world search can prove direction,
        while the executor deliberately stops after the pushes.  Facts after that edge describe a
        world state that does not exist yet; publish the macro as a reachable stage and replan from
        its verified RAM result instead of reporting a downstream barrier as currently blocked.
        """
        index = next((i for i, edge in enumerate(plan.route) if edge.kind == "boulder_push"), None)
        if index is None:
            return plan
        route = plan.route[: index + 1]
        return NavigationPlan(
            plan.start,
            target,
            sum(edge.cost for edge in route),
            route,
            explored=plan.explored,
        )

    def _route_to_boulder_prerequisite(self, plan, target, capabilities, live):
        """Route to the floor that creates a hidden boulder needed by the blocked live floor.

        Victory Road's 2F boulder 12 is hidden until 3F boulder 8 falls through its ROM-declared
        hole.  A tile graph cannot mutate flags while searching across floors, so when the only
        current route is blocked by the hidden boulder's switch variable, expose the reachable
        dependency floor as the next route segment.  Once there, the ordinary live boulder planner
        owns the push/drop; the following RAM snapshot then contains the revealed boulder.
        """
        if not plan.unmet or live.player is None:
            return plan
        blocked_keys = {
            requirement.key
            for edge in plan.route
            for requirement in edge.requirements
            if not requirement.met
        }
        puzzle_map = self.graph.provider.puzzles(allow_empty=True)
        dependencies = []
        for blocked in puzzle_map.values():
            for switch in blocked.switches:
                if f"var:{switch.var}:100" not in blocked_keys:
                    continue
                for template in blocked.boulders:
                    if (
                        template.local_id not in switch.pins
                        or template.hide_flag is None
                        or template.hide_flag not in capabilities.flags
                    ):
                        continue
                    for source in puzzle_map.values():
                        if source.holes and any(
                            row.reveal_flag == template.hide_flag for row in source.boulders
                        ):
                            dependencies.append((source, source.holes[0]))
        if not dependencies:
            return plan

        current = (live.identity.group, live.identity.number)
        for source, hole in dependencies:
            if (source.group, source.number) != current:
                continue
            edge = next(
                (
                    edge
                    for edge in self.graph.edges_from(
                        plan.start, capabilities, live=live, include_unmet=False
                    )
                    if edge.kind == "boulder_push"
                    and edge.target_dict().get("goal_kind") == 1
                    and edge.target_dict().get("x") == hole.tile[0]
                    and edge.target_dict().get("y") == hole.tile[1]
                ),
                None,
            )
            if edge is not None:
                return NavigationPlan(
                    plan.start,
                    target,
                    edge.cost,
                    (edge,),
                    explored=plan.explored,
                )

        candidates = []
        for source, _hole in dependencies:
            candidate = self.planner.plan_to_maps(
                plan.start,
                frozenset({(source.group, source.number)}),
                capabilities,
                live=live,
                exclude_kinds=frozenset({"boulder_push"}),
            )
            if candidate is None:
                continue
            if candidate.reachable and candidate.route:
                candidates.append(candidate)
        if not candidates:
            return plan
        prerequisite = min(candidates, key=lambda candidate: candidate.cost or 0)
        return NavigationPlan(
            plan.start,
            target,
            prerequisite.cost,
            prerequisite.route,
            explored=prerequisite.explored,
        )

    def _route_to_counted_trainer(self, plan, capabilities, live):
        """Route to the nearest unbeaten ROM trainer counted by the first blocked barrier."""
        blocked = next((edge for edge in plan.route if edge.unmet), None)
        if blocked is None or not capabilities.flags_known:
            return plan
        requirement = next((row for row in blocked.requirements if not row.met), None)
        if requirement is None:
            return plan
        trainers = tuple(
            trainer
            for trainer in self.graph.counted_trainers(blocked.source.map_key, requirement.key)
            if trainer.flag not in capabilities.flags
        )
        if not trainers:
            return plan

        candidates = []
        descriptions: list[str] = []
        for order, trainer in enumerate(trainers):
            state = self.graph.state(trainer.map_key)
            map_name = state.identity.name if state is not None else str(trainer.map_key)
            target = NavigationTarget(*trainer.map_key, local_id=trainer.local_id)
            candidate = self.planner.plan(plan.start, target, capabilities, live=live)
            location = (
                f"{map_name} object {trainer.local_id} at ({trainer.x},{trainer.y}) "
                f"[trainer flag 0x{trainer.flag:X}]"
            )
            if candidate.reachable:
                candidates.append((candidate.cost or 0, order, candidate, trainer))
                descriptions.append(location)
            else:
                reason = (
                    "needs " + " and ".join(candidate.unmet)
                    if candidate.unmet
                    else "no verified ROM route"
                )
                descriptions.append(f"{location} (unreachable: {reason})")
        prefix = f"{requirement.label}; unbeaten counted trainers: " + "; ".join(descriptions)
        if not candidates:
            return NavigationPlan(
                plan.start,
                plan.target,
                plan.cost,
                plan.route,
                unmet=plan.unmet,
                explored=plan.explored,
                blocked_route_fact=prefix,
            )
        _cost, _order, selected, trainer = min(candidates, key=lambda row: (row[0], row[1]))
        return NavigationPlan(
            selected.start,
            selected.target,
            selected.cost,
            selected.route,
            unmet=selected.unmet,
            explored=selected.explored,
            blocked_route_fact=(
                f"{prefix}; routing to nearest reachable counted trainer: object {trainer.local_id}"
            ),
        )

    @staticmethod
    def _live_objects_key(state) -> tuple[tuple[object, ...], ...]:
        return tuple(
            sorted(
                (
                    obj.local_id,
                    obj.x,
                    obj.y,
                    obj.role,
                    obj.hidden_flag_set,
                    obj.blocks_tile,
                )
                for obj in state.objects
            )
        )

    @staticmethod
    def _drop_gate_under_player(snapshot, plan):
        """Drop route steps the live RAM state has already performed (see executable_route)."""
        state = getattr(snapshot, "map", None)
        if state is None or state.player is None or not plan.route:
            return plan
        route = executable_route(
            plan.route,
            player_xy=(state.player.avatar_x, state.player.avatar_y),
            map_key=(state.identity.group, state.identity.number),
            surfing=bool(getattr(state.player, "surfing", False)),
        )
        return plan if route == tuple(plan.route) else replace(plan, route=route)

    def warp_is_closed(self, snapshot, warp_index: int) -> bool:
        """Return whether the live route to a current-map warp crosses a closed gate."""
        state = getattr(snapshot, "map", None)
        if state is None or state.player is None:
            return False
        current = (state.identity.group, state.identity.number)
        self.visited.note(current)
        capabilities = from_snapshot(snapshot, visited_maps=frozenset(self.visited.maps))
        capability_key = tuple(
            getattr(capabilities, name, None)
            for name in (
                "capabilities",
                "items",
                "flags",
                "vars",
                "visited_maps",
                "biking",
                "surfing",
                "strength_active",
            )
        )
        if self._closed_warp_cache_map != current:
            self._closed_warp_cache.clear()
            self._closed_warp_cache_map = current
        cache_key = (warp_index, capability_key, self._live_objects_key(state))
        cached = self._closed_warp_cache.get(cache_key, ...)
        if cached is not ...:
            return cached
        if len(self._closed_warp_cache) >= 2048:
            self._closed_warp_cache.clear()
        start = self.graph.node(current, state.player.avatar_x, state.player.avatar_y)
        plan = self.planner.plan(
            start,
            NavigationTarget(current[0], current[1], warp_index=warp_index),
            capabilities,
            live=state,
        )
        closed = bool(plan.unmet and any(edge.kind == "gate" and edge.unmet for edge in plan.route))
        self._closed_warp_cache[cache_key] = closed
        return closed

    def annotate(
        self,
        options: list[Option],
        snapshot,
        resolved: ResolvedObjective | None,
        *,
        plan: NavigationPlan | None | object = _PLAN_UNSET,
    ) -> tuple[list[Option], NavigationPlan | None]:
        state = getattr(snapshot, "map", None)
        if state is not None and state.player is not None:
            self.visited.note((state.identity.group, state.identity.number))
        if plan is _PLAN_UNSET:
            plan = self.plan(snapshot, resolved)
        if plan is None:
            return options, None
        assert isinstance(plan, NavigationPlan)
        plan = self._use_live_elevator_exit(snapshot, plan, options)
        script_unknown = self.graph.route_script_unknown(plan)
        if plan.cost is None and not plan.route:
            blocked_route = self.blocked_route(plan)
            for index, option in enumerate(options):
                facts = dict(option.facts)
                key = self._option_key(option) or option.id
                if plan.blocked_choice_step is not None and key == plan.blocked_choice_step:
                    # No route is known, but this decision is the step toward finding one.
                    # ``serves_goal`` and ``on_route`` stay unset: the target may be unrelated
                    # or unreachable through this step, and the frame must not claim otherwise.
                    facts.update(
                        {
                            "route_relation": "next route step",
                            "route_step": True,
                            "next_route_step": True,
                            "route_needs": list(plan.unmet),
                            "blocked_choice": plan.blocked_choice_step,
                            **(
                                {"blocked_route": blocked_route}
                                if blocked_route is not None
                                else {}
                            ),
                        }
                    )
                else:
                    facts.update(
                        {
                            "no_route": True,
                            "blocked_route": blocked_route or "no path known",
                        }
                    )
                options[index] = option.model_copy(update={"facts": facts})
            return options, plan
        local_step = self._current_map_step(options, plan, resolved)
        if (
            local_step is None
            and resolved is not None
            and plan.target.local_id is not None
            and plan.route
            and all(edge.kind in ("walk", "bike", "ledge", "forced") for edge in plan.route)
        ):
            # Same-map NPC interactions may require a walking prefix; resolve the
            # executor-sized interaction without changing map-level route semantics.
            local_step = self._current_map_step(options, replace(plan, route=()), resolved)
        if local_step is None:
            local_step = self._train_route_step(options, plan, resolved)
        if local_step is None:
            local_step = self._wild_route_step(options, plan, resolved)
        if resolved is not None and getattr(resolved.objective, "kind", None) == "fight":
            probe_step = self._defeated_fight_probe_step(options, resolved)
            if probe_step is not None:
                local_step = probe_step
        if plan.cost is not None and not plan.route and local_step is None:
            return options, plan
        plan = self._drop_gate_under_player(snapshot, plan)
        semantic = next_semantic_edge(plan)
        route_keys = {self._option_key_for_edge(edge) for edge in plan.route}

        def has_explicit_toggle_option(edge: NavigationEdge) -> bool:
            if edge.kind != "toggle":
                return False
            target = edge.target_dict()
            return any(
                option.chooseable
                and option.facts.get("state_bit") == target.get("flag")
                and tuple(option.facts.get("stand_tile", ())) == (edge.source.x, edge.source.y)
                for option in options
            )

        def route_option_key(edge: NavigationEdge) -> str:
            if edge.kind == "surf":
                # A water connection is Surf in the graph but an edge option in the field UI.
                return edge.stable_key
            return self._option_key_for_edge(edge)

        first_nonwalk_edge = next(
            (edge for edge in plan.route if edge.kind not in ("walk", "bike", "ledge", "forced")),
            None,
        )
        route_candidates = (
            (first_nonwalk_edge,)
            if first_nonwalk_edge is not None and first_nonwalk_edge.kind == "gate"
            else plan.route
        )

        def executes_route_edge(option: Option, edge: NavigationEdge) -> bool:
            """One predicate for "this option performs this planned edge".

            Used both to pick the route-step edge and to pick the option that carries the
            marker, so the two can never disagree (e.g. a sign switch must not win over an
            explicit statue/toggle option for the same flag).
            """
            if not option.chooseable or option.facts.get("selection_reason") == "repeated_failure":
                return False
            if self._option_key(option) == route_option_key(edge):
                return True
            if edge.kind in ("ferry", "boulder_push"):
                return self._matches(option, edge, plan)
            if edge.kind in ("toggle", "surf", "dismount"):
                target = option.facts.get("target")
                return self._matches(option, edge, plan, allow_equivalent_warp=False) and not (
                    isinstance(target, dict)
                    and target.get("kind") == "sign"
                    and has_explicit_toggle_option(edge)
                )
            if edge.kind == "gate":
                target = option.facts.get("target")
                if (
                    isinstance(target, dict)
                    and target.get("kind") == "warp"
                    and gate_has_trigger_option(edge)
                ):
                    # The coordinate trigger IS the gate; a later warp is not its executor.
                    return False
                return self._matches(
                    option,
                    edge,
                    plan,
                    allow_equivalent_warp=False,
                    gate_index=plan.route.index(edge),
                )
            if edge.kind == "elevator":
                # The panel executes the ride; a dynamic exit warp is never the elevator step.
                target = option.facts.get("target")
                return (
                    isinstance(target, dict)
                    and target.get("kind") != "warp"
                    and self._matches(option, edge, plan, allow_equivalent_warp=False)
                )
            return False

        def gate_has_trigger_option(edge: NavigationEdge) -> bool:
            gate_index = plan.route.index(edge)
            return any(
                option.chooseable
                and isinstance(option.facts.get("target"), dict)
                and option.facts["target"].get("kind") == "tile"
                and self._matches(
                    option, edge, plan, allow_equivalent_warp=False, gate_index=gate_index
                )
                for option in options
            )

        route_step_edge = next(
            (
                edge
                for edge in route_candidates
                if edge.kind not in ("walk", "bike", "ledge", "forced")
                and any(executes_route_edge(option, edge) for option in options)
            ),
            None,
        )
        # Keep the stable route key even when the executor option is a semantic toggle (whose
        # field id is ``switch:<floor>:...`` rather than ``toggle:...``).  This prevents a later
        # warp on the same plan from also being advertised as the next action.
        route_step_key = route_option_key(route_step_edge) if route_step_edge is not None else None
        route_step_option_id = (
            next(
                (o.id for o in options if executes_route_edge(o, route_step_edge)),
                None,
            )
            if route_step_edge is not None
            else None
        )
        gate_index = (
            plan.route.index(semantic) if semantic is not None and semantic.kind == "gate" else None
        )
        blocked_index = (
            next((index for index, edge in enumerate(plan.route) if edge.unmet), None)
            if plan.unmet
            else None
        )
        if blocked_index is None and plan.unmet and plan.blocked_route_fact is not None:
            blocked_index = len(plan.route)
        blocked_route = self.blocked_route(plan)
        blocked_obstacle = self._blocked_obstacle(plan, blocked_index)
        exact_semantic_available = bool(
            semantic is not None
            and any(
                option.chooseable
                and option.facts.get("selection_reason") != "repeated_failure"
                and self._option_key(option) == semantic.stable_key
                for option in options
            )
        )
        elevator_panel_available = bool(
            semantic is not None
            and semantic.kind == "elevator"
            and any(
                option.chooseable
                and isinstance(option.facts.get("target"), dict)
                and option.facts["target"].get("kind") == "sign"
                and option.facts["target"].get("bg") == semantic.target_dict().get("panel_bg")
                for option in options
            )
        )
        out: list[Option] = []
        for option in options:
            key = self._option_key(option)
            facts = dict(option.facts)
            repeated_failure = facts.get("selection_reason") == "repeated_failure"
            route_step_match = option.id == route_step_option_id
            legality = option.legality
            facts.pop("route_step", None)
            facts.pop("next_route_step", None)
            facts.pop("on_route", None)
            route_key = key if key in route_keys else option.id
            # A route whose only blocker is a decision (the fossil choice) advertises the
            # option that takes that step even when the planner's blocked route was
            # truncated before it: no capability can satisfy the requirement, the agent
            # decides, and the option keeps its own irreversible facts.
            choice_match = bool(
                plan.blocked_choice_step is not None
                and route_key == plan.blocked_choice_step
            )
            option_target = option.facts.get("target")
            if (
                semantic is not None
                and semantic.kind == "elevator"
                and isinstance(option_target, dict)
                and option_target.get("kind") == "elevator"
                and tuple(option_target.get("dest", ())) == semantic.destination.map_key
            ):
                edge_target = semantic.target_dict()
                facts.update(
                    {
                        "floor_var": edge_target.get("floor_var"),
                        "floor_value": edge_target.get("floor_value"),
                        "floor_writes_var": edge_target.get("floor_var") is not None,
                    }
                )
            equivalent_clearer = not repeated_failure and self._equivalent_obstacle(
                option, plan, blocked_index, blocked_obstacle
            )
            semantic_match = bool(
                not repeated_failure
                and semantic is not None
                and self._matches(
                    option,
                    semantic,
                    plan,
                    # If the planner's canonical warp is not executable in this
                    # live frame, an equivalent legal warp is the executable
                    # first step.  This matters for unmet plans too: the
                    # route may be blocked only after the player leaves the
                    # current map, while the canonical source warp itself can
                    # be a closed/hidden tile in the current RAM state.
                    allow_equivalent_warp=not exact_semantic_available,
                    gate_index=gate_index,
                )
            )
            trigger_gate_match = False
            if not repeated_failure and isinstance(option_target, dict):
                at = (option_target.get("x"), option_target.get("y"))
                if option_target.get("kind") == "tile" and all(
                    isinstance(value, int) for value in at
                ):
                    trigger_gate_match = first_planned_gate_at(
                        plan.route, at, tuple(option_target.get("map", ()))
                    )
            semantic_match = semantic_match or trigger_gate_match
            if (
                semantic is not None
                and semantic.kind == "gate"
                and route_step_edge is semantic
                and route_step_option_id is not None
                and option.id != route_step_option_id
            ):
                # A coordinate trigger is the executable gate option when it is offered;
                # do not also advertise the crossing warp as the same route step.
                semantic_match = False
            if (
                semantic_match
                and semantic is not None
                and semantic.kind == "toggle"
                and isinstance(option_target, dict)
                and option_target.get("kind") == "sign"
                and has_explicit_toggle_option(semantic)
            ):
                semantic_match = False
            planned_connection = next(
                (
                    edge
                    for edge in plan.route
                    if edge.stable_key.startswith("edge:") and key == edge.stable_key
                ),
                None,
            )
            if planned_connection is not None:
                executor_args = dict(option.executor_args)
                executor_args.update(
                    seam_x=planned_connection.source.x,
                    seam_y=planned_connection.source.y,
                )
                facts["planned_seam"] = [
                    planned_connection.source.x,
                    planned_connection.source.y,
                ]
                option = option.model_copy(update={"executor_args": executor_args})
            planned_approach = next(
                (
                    edge
                    for edge in plan.route
                    if (edge.kind == "field_move" and self._option_key_for_edge(edge) == key)
                    or (
                        edge.kind == "surf"
                        and not edge.stable_key.startswith("edge:")
                        and self._matches(
                            option,
                            edge,
                            plan,
                            allow_equivalent_warp=False,
                        )
                    )
                    or (
                        edge.kind == "dismount"
                        and self._matches(
                            option,
                            edge,
                            plan,
                            allow_equivalent_warp=False,
                        )
                    )
                ),
                None,
            )
            if planned_approach is not None:
                approach_tile = (
                    planned_approach.destination
                    if planned_approach.kind == "dismount"
                    else planned_approach.source
                )
                facts["planned_approach"] = {
                    "stand": [approach_tile.x, approach_tile.y],
                    "face": (
                        None if planned_approach.kind == "dismount" else planned_approach.direction
                    ),
                }
            if (
                elevator_panel_available
                and semantic is not None
                and semantic.kind == "elevator"
                and isinstance(option_target, dict)
                and option_target.get("kind") == "warp"
                and option_target.get("dynamic") is True
            ):
                # The panel is the executable first step. A dynamic exit has no
                # statically known destination and must not compete with it.
                semantic_match = False
            dynamic_exit_suppressed = (
                elevator_panel_available
                and semantic is not None
                and semantic.kind == "elevator"
                and isinstance(option_target, dict)
                and option_target.get("kind") == "warp"
                and option_target.get("dynamic") is True
            )
            switch_option = bool(
                semantic_match
                and semantic is not None
                and semantic.kind == "warp"
                and isinstance(option_target, dict)
                and option_target.get("kind") == "sign"
                and semantic.target_dict().get("switch_bg") == option_target.get("bg")
            )
            if plan.cost is None:
                facts.pop("route_relation", None)
                facts.pop("serves_goal", None)
            elif (
                not repeated_failure
                and plan.unmet
                and blocked_index is not None
                and (
                    route_key in route_keys
                    or semantic_match
                    or route_step_match
                    or equivalent_clearer
                    or choice_match
                )
            ):
                edge_index = next(
                    (
                        index
                        for index, edge in enumerate(plan.route)
                        if self._option_key_for_edge(edge) == route_key
                    ),
                    None,
                )
                if edge_index is None and equivalent_clearer:
                    edge_index = blocked_index
                if edge_index is None and route_step_match and route_step_edge is not None:
                    edge_index = plan.route.index(route_step_edge)
                if edge_index is None and semantic_match and semantic is not None:
                    edge_index = plan.route.index(semantic)
                if edge_index is None and choice_match:
                    edge_index = blocked_index
                triggerable_gate = bool(
                    semantic_match
                    and semantic is not None
                    and semantic.kind == "gate"
                    and semantic.target_dict().get("triggerable") is True
                )
                if triggerable_gate:
                    edge_index = blocked_index
                choice_step = bool(
                    choice_match
                    and blocked_index is not None
                    and all(
                        row.kind in ("walk", "bike", "ledge", "forced")
                        for row in plan.route[:blocked_index]
                    )
                )
                clears_blocker = bool(
                    edge_index == blocked_index
                    and (
                        choice_step
                        or (
                            semantic is not None
                            and (
                                (
                                    blocked_obstacle
                                    and semantic.kind in ("field_move", "boulder_push")
                                )
                                or triggerable_gate
                            )
                        )
                    )
                )
                if edge_index is not None and (
                    edge_index < blocked_index or clears_blocker or switch_option
                ):
                    relation = (
                        "next route step"
                        if (clears_blocker or semantic_match or route_step_match)
                        else "on route"
                    )
                    facts.update(
                        {
                            "route_relation": relation,
                            "route_step": relation == "next route step",
                            "next_route_step": relation == "next route step",
                            "on_route": True,
                            "serves_goal": True,
                            "route_cost": plan.cost,
                        }
                    )
                    if blocked_route:
                        facts["route_blocked"] = blocked_route
                    if blocked_obstacle:
                        facts["blocked_obstacle"] = blocked_obstacle
                    if triggerable_gate and semantic is not None:
                        gate_at = semantic.target_dict().get("at")
                        if (
                            isinstance(gate_at, (tuple, list))
                            and len(gate_at) == 2
                            and all(isinstance(value, int) for value in gate_at)
                        ):
                            declinable = semantic.target_dict().get("declinable") is True
                            facts.update(
                                {
                                    "script_trigger": list(gate_at),
                                    "forced_battle": True,
                                    "declinable": declinable,
                                    **(
                                        {
                                            "decline_outcome": (
                                                "the story variable stays unchanged and the "
                                                "trigger remains armed"
                                            )
                                        }
                                        if declinable
                                        else {}
                                    ),
                                }
                            )
                            legality = Legality.LEGAL
                            option = option.model_copy(
                                update={
                                    "label": (
                                        f"Walk onto the story trigger at "
                                        f"({gate_at[0]}, {gate_at[1]})"
                                    ),
                                    "description": (
                                        f"Walk to the ROM story trigger at "
                                        f"({gate_at[0]}, {gate_at[1]}). "
                                        + (
                                            "A YES/NO scene follows; declining leaves the "
                                            "trigger armed, while accepting starts the forced "
                                            "trainer-battle sequence."
                                            if declinable
                                            else "This starts a forced trainer-battle sequence."
                                        )
                                    ),
                                    "legality": Legality.LEGAL,
                                    "executor_ref": "field.walk_to",
                                    "executor_args": {"x": gate_at[0], "y": gate_at[1]},
                                    "postcondition_ref": None,
                                }
                            )
                    out.append(option.model_copy(update={"facts": facts, "legality": legality}))
                    continue
                relation = "unreachable"
                facts.update(
                    {
                        "route_relation": relation,
                        "route_needs": list(plan.unmet),
                        "serves_goal": False,
                        "reason": f"route blocked: needs {' and '.join(plan.unmet)}",
                    }
                )
                if blocked_route:
                    facts["route_blocked"] = blocked_route
                if blocked_obstacle:
                    facts["blocked_obstacle"] = blocked_obstacle
                if blocked_obstacle is None:
                    legality = Legality.UNAVAILABLE
            elif plan.unmet:
                relation = facts.get("route_relation", "unrelated")
            elif local_step is not None and option.id == local_step.id:
                relation = "next route step"
                facts.update(
                    {
                        "route_relation": relation,
                        "route_step": True,
                        "next_route_step": True,
                        "on_route": True,
                        "serves_goal": True,
                        "route_cost": plan.cost,
                    }
                )
            elif local_step is not None:
                relation = "off route"
                facts.update({"route_relation": relation, "serves_goal": False})
            elif (
                not repeated_failure
                and semantic is not None
                and not dynamic_exit_suppressed
                and (
                    semantic_match
                    or route_step_match
                    or self._matches(
                        option,
                        semantic,
                        plan,
                        allow_equivalent_warp=not exact_semantic_available,
                        gate_index=gate_index,
                    )
                )
                and not (
                    semantic.kind == "toggle"
                    and isinstance(option_target, dict)
                    and option_target.get("kind") == "sign"
                    and has_explicit_toggle_option(semantic)
                )
            ):
                relation = "next route step"
                facts.update(
                    {
                        "route_relation": relation,
                        "route_step": True,
                        "next_route_step": True,
                        "on_route": True,
                        "serves_goal": True,
                        "route_cost": plan.cost,
                    }
                )
            elif key is not None and key in route_keys and key != route_step_key:
                relation = "on route"
                facts.update(
                    {
                        "route_relation": relation,
                        "on_route": True,
                        "serves_goal": True,
                        "route_cost": plan.cost,
                    }
                )
            elif route_step_key is not None and (
                key == route_step_key or option.id == route_step_option_id
            ):
                relation = "next route step"
                facts.update(
                    {
                        "route_relation": relation,
                        "route_step": True,
                        "next_route_step": True,
                        "on_route": True,
                        "serves_goal": True,
                        "route_cost": plan.cost,
                    }
                )
            else:
                relation = "off route"
                facts.update({"route_relation": relation, "serves_goal": False})
            facts["route"] = [self._readable_step(edge) for edge in plan.route[:32]]
            out.append(option.model_copy(update={"facts": facts, "legality": legality}))
        if route_step_edge is not None and route_step_option_id is not None:
            # Exactly one planned edge is the next step.  Options that execute a different
            # edge (e.g. a later warp, or a semantic match for another edge) stay on route.
            # Equivalent executors of the same edge (two fossils clearing one blocker) remain.
            demoted = []
            for option in out:
                if (
                    option.facts.get("next_route_step")
                    and option.id != route_step_option_id
                    and not executes_route_edge(option, route_step_edge)
                    and not (
                        blocked_index is not None
                        and plan.route.index(route_step_edge) == blocked_index
                        and self._equivalent_obstacle(option, plan, blocked_index, blocked_obstacle)
                    )
                ):
                    facts = dict(option.facts)
                    facts.pop("next_route_step", None)
                    facts.pop("route_step", None)
                    facts["route_relation"] = "on route"
                    demoted.append(option.model_copy(update={"facts": facts}))
                else:
                    demoted.append(option)
            out = demoted
        out = self._annotate_teach_route_steps(out, snapshot, plan)
        if script_unknown is not None:
            out = [
                option.model_copy(
                    update={"facts": {**option.facts, "script_unknown": script_unknown}}
                )
                if option.facts.get("on_route") or option.facts.get("route_step")
                else option
                for option in out
            ]
        return out, plan

    @staticmethod
    def _annotate_teach_route_steps(options, snapshot, plan):
        """Advertise a legal bag HM when the blocked next edge needs its field move.

        The item builder has already proved bag presence and ROM learnset compatibility.  This
        post-pass is deliberately after the normal route annotation so that navigation remains
        the sole arbiter of route facts.
        """
        if not any(
            option.chooseable
            and "teach_move" in option.facts.get("effect_classes", ())
            and option.facts.get("learned_move_is_hm") is True
            for option in options
        ):
            return options
        needed = {}
        for edge in plan.route:
            if not edge.unmet:
                continue
            for requirement in edge.requirements:
                if requirement.key in {"cut", "smash", "strength", "flash"} and not requirement.met:
                    needed[requirement.key] = field_capability(snapshot, requirement.key)
            if needed:
                break
        if not needed:
            return options
        marked = []
        for option in options:
            facts = dict(option.facts)
            if (
                not option.chooseable
                or "teach_move" not in facts.get("effect_classes", ())
                or facts.get("learned_move_is_hm") is not True
            ):
                marked.append(option)
                continue
            capability = next(
                (value for value in needed.values() if value.move_id == facts.get("move_id")),
                None,
            )
            if capability is None or capability.carrier_species:
                marked.append(option)
                continue
            compatible = facts.get("compatible", ())
            if not compatible and facts.get("target_name"):
                compatible = (facts["target_name"],)
            item_name = facts.get("item_name", "HM")
            reason = (
                f"route needs {capability.name}; no party member knows it; "
                f"{item_name} in bag, compatible: {', '.join(map(str, compatible))}"
            )
            facts.update(
                {
                    "route_step": True,
                    "next_route_step": True,
                    "serves_goal": True,
                    "route_reason": reason,
                }
            )
            marked.append(option.model_copy(update={"facts": facts, "description": reason}))
        if not any(
            option.facts.get("next_route_step")
            and "teach_move" in option.facts.get("effect_classes", ())
            for option in marked
        ):
            return marked
        # The route is blocked on a missing field move: teaching it is the one next step.
        # Prefix steps toward the blocker stay on route but are no longer advertised as next.
        out = []
        for option in marked:
            if option.facts.get("next_route_step") and "teach_move" not in option.facts.get(
                "effect_classes", ()
            ):
                facts = dict(option.facts)
                facts.pop("next_route_step", None)
                facts.pop("route_step", None)
                out.append(option.model_copy(update={"facts": facts}))
            else:
                out.append(option)
        return out

    def ferry_menu_entries(
        self,
        snapshot,
        resolved: ResolvedObjective | None,
        count: int,
        entries: tuple[tuple[str, str], ...] | None,
        *,
        menu_id: int | None = None,
    ) -> tuple[tuple[str, str], ...] | None:
        """Label the live ferry menu and mark only the destination selected by the plan."""
        plan = self.plan(snapshot, resolved)
        if plan is None:
            return entries
        current = (
            (snapshot.player.map_group, snapshot.player.map_num)
            if snapshot.player is not None
            else None
        )
        ferry = next(
            (
                edge
                for edge in plan.route
                if edge.kind == "ferry" and edge.source.map_key == current
            ),
            None,
        )
        if ferry is None:
            return entries
        target = ferry.target_dict()
        script = getattr(getattr(snapshot, "ui", None), "script", None)
        expected_pc = target.get("menu_pc")
        if menu_id == 0xFF and count in {5, 6}:
            expected_pc = dict(target.get("menu_pcs", ())).get(0 if count == 6 else 1)
        if (
            menu_id is None
            or target.get("menu_id") != menu_id
            or expected_pc is None
            or getattr(script, "script_ptr", None) != expected_pc
        ):
            return entries
        selector = target.get("selector")
        menu_index = target.get("menu_index")
        rows = list(entries or ())
        if isinstance(menu_index, int) and len(rows) == count and 0 <= menu_index < count:
            key, label = rows[menu_index]
            rows[menu_index] = (f"route:ferry:{selector}:{key}", label)
            return tuple(rows)
        if (
            menu_id != 0xFF
            or not isinstance(selector, int)
            or current is None
            or count not in {5, 6}
        ):
            return entries
        origin = next(
            (
                value
                for value, (group, number, _warp, _x, _y) in self.graph._seagallop_warps
                if (group, number) == current
            ),
            None,
        )
        if origin is None:
            return entries
        candidates = (
            list(range(0, 5) if origin < 5 else range(0, 4))
            if count == 6
            else list(range(5, 8))
            if origin < 5
            else list(range(4, 8))
        )
        destinations = [value for value in candidates if value != origin]
        if len(destinations) != count - 2:
            return entries
        rows = []
        for value in destinations:
            destination = dict(self.graph._seagallop_warps).get(value)
            state = self.graph.state(destination[:2]) if destination is not None else None
            if state is None:
                return entries
            key = f"ferry:{value}"
            if value == selector:
                key = f"route:{key}"
            rows.append((key, state.identity.name.replace("_", " ")))
        more_key = "route:ferry:more" if selector not in destinations else "ferry:more"
        rows.extend(((more_key, "Other destinations"), ("ferry:exit", "Exit")))
        return tuple(rows)

    def _use_live_elevator_exit(
        self, snapshot, plan: NavigationPlan, options: list[Option]
    ) -> NavigationPlan:
        """Replace a completed elevator ride with its dynamic exit warp.

        The ROM script leaves the player on the shared elevator map after selecting the
        current floor.  The exit destination is the live SaveBlock1 dynamic warp; the
        decoded floor variable is only a stale selection hint and is never authoritative.
        """
        semantic = next_semantic_edge(plan)
        if semantic is None or semantic.kind != "elevator":
            return plan
        target = semantic.target_dict()
        dynamic = getattr(snapshot, "dynamic_warp", None)
        destination = _dynamic_warp_destination(self.graph, dynamic)
        panel_available = any(
            option.chooseable
            and isinstance(option.facts.get("target"), dict)
            and option.facts["target"].get("kind") == "sign"
            for option in options
        )
        if destination is None:
            if panel_available:
                return plan
            return replace(plan, route=tuple(edge for edge in plan.route if edge is not semantic))
        if panel_available and destination.map_key != semantic.destination.map_key:
            return plan
        warp_index = target.get("warp")
        if not isinstance(warp_index, int):
            return plan
        if not any(
            option.chooseable
            and isinstance(option.facts.get("target"), dict)
            and option.facts["target"].get("kind") == "warp"
            and option.facts["target"].get("index") == warp_index
            for option in options
        ):
            return plan
        exit_edge = replace(
            semantic,
            stable_key=f"warp:{semantic.source.map_group}:{semantic.source.map_number}:{warp_index}",
            destination=destination,
            kind="warp",
            executor_target=(
                ("kind", "warp"),
                ("warp", warp_index),
                ("dest", destination.map_key),
                ("dest_warp", dynamic.warp_id),
            ),
            prompt=False,
        )
        route = list(plan.route)
        route[plan.route.index(semantic)] = exit_edge
        return replace(plan, route=tuple(route))

    def _blocked_route(self, plan: NavigationPlan, blocked_index: int | None) -> str | None:
        if blocked_index is None or blocked_index >= len(plan.route):
            return None
        edge = plan.route[blocked_index]
        source_name = _map_label(self.graph, edge.source.map_key)
        destination_name = _map_label(self.graph, edge.destination.map_key)
        target_name = _map_label(self.graph, plan.target.map_key)
        obstacle = self._blocked_obstacle(plan, blocked_index)
        if obstacle:
            return (
                f"route blocked by {obstacle['obstacle']} at {obstacle['map']} "
                f"({obstacle['x']},{obstacle['y']})"
            )
        needs = ", ".join(edge.unmet) or "unknown"
        if edge.kind == "gate" and edge.target_dict().get("triggerable") is True:
            at = edge.target_dict().get("at")
            if isinstance(at, (tuple, list)) and len(at) == 2:
                return (
                    f"route blocked at coordinate event on {source_name} tile "
                    f"({at[0]},{at[1]}): {needs}; last reachable map: {source_name}; "
                    f"target map: {target_name}"
                )
        return (
            f"route blocked at {source_name} -> {destination_name}: {needs}; "
            f"last reachable map: {source_name}; target map: {target_name}"
        )

    def blocked_route(self, plan: NavigationPlan) -> str | None:
        """Return the ROM/RAM explanation for the first unmet edge in ``plan``."""
        if plan.blocked_route_fact is not None:
            return plan.blocked_route_fact
        blocked_index = next((index for index, edge in enumerate(plan.route) if edge.unmet), None)
        return self._blocked_route(plan, blocked_index)

    def _blocked_obstacle(
        self, plan: NavigationPlan, blocked_index: int | None
    ) -> dict[str, object] | None:
        if blocked_index is None or blocked_index >= len(plan.route):
            return None
        edge = plan.route[blocked_index]
        if edge.kind not in ("field_move", "boulder_push"):
            return None
        state = self.graph.state(edge.source.map_key)
        if state is None:
            return None
        role = str(dict(edge.executor_target).get("role", "obstacle")).replace("_", " ")
        return {
            "obstacle": role,
            "map": state.identity.name or str(edge.source.map_key),
            "x": edge.destination.x,
            "y": edge.destination.y,
        }

    @staticmethod
    def _equivalent_obstacle(
        option: Option,
        plan: NavigationPlan,
        blocked_index: int | None,
        blocked_obstacle: dict[str, object] | None,
    ) -> bool:
        """Match alternate clearers only when they touch the blocked edge corridor."""
        if blocked_index is None or blocked_obstacle is None:
            return False
        target = option.facts.get("target")
        if not isinstance(target, dict) or target.get("kind") != "obstacle":
            return False
        if str(target.get("role", "")).replace("_", " ") != blocked_obstacle.get("obstacle"):
            return False
        at = target.get("at")
        if not isinstance(at, (tuple, list)) or len(at) != 2:
            return False
        edge = plan.route[blocked_index]
        return any(
            abs(int(at[0]) - tile.x) + abs(int(at[1]) - tile.y) <= 1
            for tile in (edge.source, edge.destination)
        )

    @staticmethod
    def _train_route_step(
        options: list[Option], plan: NavigationPlan, resolved: ResolvedObjective | None
    ) -> Option | None:
        """Mark the grass action for a same-map walk to a concrete train destination."""
        semantic = next_semantic_edge(plan)
        if (
            plan.cost is None
            or plan.unmet
            or not plan.route
            or resolved is None
            or getattr(getattr(resolved, "objective", None), "kind", None) != "train"
            or plan.start.map_key != plan.target.map_key
            or plan.target.x is None
            or plan.target.y is None
            or semantic is None
            or semantic.kind not in ("walk", "bike", "ledge", "forced")
        ):
            return None
        candidates: list[tuple[int, Option]] = []
        for option in options:
            if not option.chooseable:
                continue
            if option.facts.get("selection_reason") == "repeated_failure":
                continue
            target = option.facts.get("target")
            if not isinstance(target, dict) or target.get("kind") != "grass":
                continue
            patch = target.get("patch")
            if not isinstance(patch, str):
                continue
            try:
                x, y = (int(part) for part in patch.split(":", 1))
            except (TypeError, ValueError):
                continue
            distance = abs(x - plan.target.x) + abs(y - plan.target.y)
            candidates.append((distance, option))
        return min(candidates, key=lambda row: (row[0], row[1].id))[1] if candidates else None

    @staticmethod
    def _wild_route_step(
        options: list[Option], plan: NavigationPlan, resolved: ResolvedObjective | None
    ) -> Option | None:
        """Mark the encounter tile when a catch/dex target is already on this map."""
        if (
            plan.cost is None
            or plan.unmet
            or plan.route
            or resolved is None
            or not getattr(resolved, "navigation_map_keys", frozenset())
            or plan.start.map_key not in resolved.navigation_map_keys
        ):
            return None
        candidates = [
            option
            for option in options
            if option.chooseable
            and option.facts.get("selection_reason") != "repeated_failure"
            and option.facts.get("target", {}).get("kind") in {"grass", "surf", "landing"}
        ]
        methods = dict(getattr(resolved, "navigation_map_methods", ())).get(
            plan.start.map_key, frozenset()
        )
        candidates = [
            option
            for option in candidates
            if (
                "land" in methods
                and option.facts.get("target", {}).get("kind") == "grass"
            )
            or (
                "water" in methods
                and option.facts.get("target", {}).get("kind") in {"surf", "landing"}
            )
        ]
        return min(
            candidates,
            key=lambda option: (int(option.facts.get("steps", 9_999)), option.id),
            default=None,
        )

    @staticmethod
    def _defeated_fight_probe_step(
        options: list[Option], resolved: ResolvedObjective
    ) -> Option | None:
        """Prefer the next map-arrival warp when the fight target is already defeated."""
        target_id = resolved.target.npc
        if target_id is None:
            return None
        defeated = any(
            option.facts.get("target", {}).get("kind") == "npc"
            and option.facts.get("target", {}).get("local_id") == target_id
            and (
                option.facts.get("trainer_defeated") is True
                or option.facts.get("trainer") is False
                or option.facts.get("npc_interaction") == "conversation_only"
            )
            for option in options
        )
        if not defeated:
            return None
        probe_maps = {
            resolve_map(probe.map)
            for probe in resolved.objective.success_probes
            if probe.kind == "map_arrival"
        }
        candidates = [
            option
            for option in options
            if option.chooseable
            and option.facts.get("target", {}).get("kind") == "warp"
            and tuple(option.facts["target"].get("dest", ())) in probe_maps
        ]
        return (
            min(candidates, key=lambda option: (int(option.facts.get("steps", 9_999)), option.id))
            if candidates
            else None
        )

    @staticmethod
    def _current_map_step(
        options: list[Option],
        plan: NavigationPlan,
        resolved: ResolvedObjective | None,
    ) -> Option | None:
        """Choose the executor-sized local step for a reachable zero-edge plan."""
        if plan.cost is None or plan.unmet or plan.route or resolved is None:
            return None
        if plan.start.map_key != plan.target.map_key:
            return None

        target = plan.target
        exact: list[Option] = []
        for option in options:
            if not option.chooseable:
                continue
            if option.facts.get("selection_reason") == "repeated_failure":
                continue
            facts_target = option.facts.get("target")
            if not isinstance(facts_target, dict):
                continue
            if target.local_id is not None and (
                facts_target.get("kind") == "npc"
                and facts_target.get("local_id") == target.local_id
            ):
                exact.append(option)
            elif (
                target.x is not None
                and target.y is not None
                and (
                    facts_target.get("kind") == "tile"
                    and (facts_target.get("x"), facts_target.get("y")) == (target.x, target.y)
                )
            ):
                exact.append(option)
            elif target.warp_index is not None and (
                facts_target.get("kind") == "warp"
                and facts_target.get("index") == target.warp_index
            ):
                exact.append(option)
        objective = getattr(resolved, "objective", None)
        if exact and not (
            getattr(objective, "kind", None) == "fight"
            and any(
                option.facts.get("trainer_defeated") is True
                or option.facts.get("trainer") is False
                or option.facts.get("npc_interaction") == "conversation_only"
                for option in exact
            )
        ):
            return min(
                exact,
                key=lambda option: (int(option.facts.get("steps", 9_999)), option.id),
            )

        # A direct interaction on the objective map wins over a scripted movement hand-off.
        # ``satisfies_objective`` comes from RAM/ROM option facts, never labels or goal text.
        direct_service = [
            option
            for option in options
            if option.chooseable
            and option.facts.get("satisfies_objective") is True
            and isinstance(option.facts.get("target"), dict)
            and option.facts["target"].get("kind") not in {"warp", "edge", "fly"}
        ]
        if direct_service:
            return min(
                direct_service,
                key=lambda option: (int(option.facts.get("steps", 9_999)), option.id),
            )

        if getattr(objective, "kind", None) == "fight":
            probe_maps = {
                resolve_map(probe.map)
                for probe in getattr(objective, "success_probes", ())
                if probe.kind == "map_arrival"
            }
            probe_warps = [
                option
                for option in options
                if option.chooseable
                and option.facts.get("target", {}).get("kind") == "warp"
                and tuple(option.facts["target"].get("dest", ())) in probe_maps
            ]
            if probe_warps:
                return min(
                    probe_warps,
                    key=lambda option: (int(option.facts.get("steps", 9_999)), option.id),
                )
        if getattr(objective, "kind", None) == "train":
            training = [
                option
                for option in options
                if option.chooseable
                and isinstance(option.facts.get("target"), dict)
                and option.facts["target"].get("kind") == "grass"
            ]
            if training:
                return min(
                    training,
                    key=lambda option: (int(option.facts.get("steps", 9_999)), option.id),
                )

        probes = getattr(objective, "success_probes", ())
        if not any(
            getattr(probe, "kind", None) in {"flag", "trainer_defeated", "var"} for probe in probes
        ):
            return None
        scripted = []
        for option in options:
            target_facts = option.facts.get("target")
            if (
                option.chooseable
                and isinstance(target_facts, dict)
                and target_facts.get("kind") in {"warp", "edge", "tile"}
                and int(option.facts.get("script_tiles", 0)) > 0
            ):
                scripted.append(option)
        if not scripted:
            return None
        return min(
            scripted,
            key=lambda option: (int(option.facts.get("steps", 9_999)), option.id),
        )

    @staticmethod
    def _target(resolved: ResolvedObjective) -> NavigationTarget | None:
        target = resolved.navigation_target or resolved.target
        if resolved.navigation_reason is not None and resolved.navigation_target is None:
            return None
        if target.map_key is None:
            return None
        return NavigationTarget(
            target.map_key[0],
            target.map_key[1],
            x=target.x,
            y=target.y,
            warp_index=target.warp,
            local_id=target.npc,
        )

    @staticmethod
    def _option_key(option: Option) -> str | None:
        target = option.facts.get("target")
        if not isinstance(target, dict):
            return option.id if option.id.startswith(("fly:", "elevator:")) else None
        kind = target.get("kind")
        if kind in ("warp", "edge"):
            return option.id
        if kind == "item":
            return option.id
        if kind == "fly":
            return option.id
        if kind == "obstacle":
            role = target.get("role")
            resolved = option_key(str(role), int(target.get("local_id", -1)))
            return resolved or f"{role}:{int(target.get('local_id', -1))}"
        if kind in ("surf", "landing"):
            return str(kind)
        if kind == "waterfall":
            return option.id
        return None

    @staticmethod
    def _option_key_for_edge(edge: NavigationEdge) -> str:
        if edge.kind == "surf":
            return edge.stable_key
        if edge.kind == "fall":
            # A hole/thin-ice fall is executed by entering the ROM warp tile.  The graph uses a
            # distinct edge kind so callers can describe the forced fall, while field.free keeps
            # the existing warp option and executor.
            return edge.stable_key.replace("fall:", "warp:", 1)
        return edge.stable_key

    def _readable_step(self, edge: NavigationEdge) -> str:
        if edge.kind in ("walk", "bike", "forced", "ledge", "surf", "waterfall", "fall"):
            direction = {"N": "north", "S": "south", "W": "west", "E": "east"}.get(
                edge.direction or "", edge.direction or "forward"
            )
            return f"walk {direction}"
        if edge.kind == "dismount":
            return "step ashore"
        destination = self.graph.state(edge.destination.map_key)
        name = destination.identity.name if destination is not None else "the next area"
        labels = {
            "warp": "door to",
            "connection": "path to",
            "elevator": "elevator to",
            "fly": "fly to",
            "ferry": "ferry to",
        }
        if edge.kind in labels:
            return f"{labels[edge.kind]} {name}"
        if edge.kind == "field_move":
            role = dict(edge.executor_target).get("role", "the obstacle")
            return f"use {str(role).replace('_', ' ')}"
        if edge.kind == "boulder_push":
            return "push the boulder"
        if edge.kind == "gate":
            return "open the gate"
        if edge.kind == "toggle":
            target = dict(edge.executor_target)
            return (
                f"answer quiz at sign {target.get('event')} / open gate flag {target.get('flag')}"
            )
        return f"travel to {name}"

    def _matches(
        self,
        option: Option,
        edge: NavigationEdge,
        plan: NavigationPlan,
        *,
        allow_equivalent_warp: bool = True,
        gate_index: int | None = None,
    ) -> bool:
        key = self._option_key(option)
        if edge.kind == "ferry":
            target = option.facts.get("target")
            edge_target = edge.target_dict()
            return bool(
                edge.source.map_key == plan.start.map_key
                and isinstance(target, dict)
                and target.get("kind") == "npc"
                and target.get("local_id") == edge_target.get("local_id")
            )
        if edge.kind == "toggle":
            target = edge.target_dict()
            option_target = option.facts.get("target")
            if (
                isinstance(option_target, dict)
                and option_target.get("kind") == "sign"
                and option_target.get("bg") == target.get("event")
            ):
                return True
            stand = option.facts.get("stand_tile")
            state_bit = option.facts.get("state_bit")
            return (
                isinstance(stand, (tuple, list))
                and len(stand) == 2
                and tuple(stand) == (edge.source.x, edge.source.y)
                and state_bit == target.get("flag")
            )
        if edge.kind == "fall":
            target = option.facts.get("target")
            edge_target = edge.target_dict()
            if not isinstance(target, dict) or target.get("kind") != "warp":
                return False
            if target.get("index") == edge_target.get("warp"):
                return True
            planned_dest_warp = edge_target.get("dest_warp")
            return planned_dest_warp is not None and target.get("dest_warp") == planned_dest_warp
        if edge.kind == "warp":
            target = option.facts.get("target")
            edge_target = edge.target_dict()
            if (
                isinstance(target, dict)
                and target.get("kind") == "warp"
                and target.get("index") == edge_target.get("warp")
            ):
                return True
            switch_bg = edge.target_dict().get("switch_bg")
            if (
                switch_bg is not None
                and edge.unmet
                and isinstance(target, dict)
                and target.get("kind") == "sign"
                and target.get("bg") == switch_bg
            ):
                return True
            # Several source warps can be equivalent exits to the same next map.  The
            # planner's canonical warp may be unavailable from the live tile while another
            # legal warp is the same semantic route step.
            if (
                allow_equivalent_warp
                and isinstance(target, dict)
                and target.get("kind") == "warp"
                and target.get("dynamic") is not True
            ):
                destination = target.get("dest")
                planned_dest_warp = edge_target.get("dest_warp")
                option_dest_warp = target.get("dest_warp")
                if (
                    destination is not None
                    and tuple(destination) == edge.destination.map_key
                    and (
                        planned_dest_warp is None
                        or option_dest_warp is None
                        or option_dest_warp == planned_dest_warp
                    )
                ):
                    return True
                option_map = target.get("map")
                option_index = target.get("index")
                option_warp = None
                if (
                    isinstance(option_map, (list, tuple))
                    and len(option_map) == 2
                    and isinstance(option_index, int)
                ):
                    option_state = self.graph.state(tuple(option_map))
                    if option_state is not None:
                        option_warp = next(
                            (warp for warp in option_state.warps if warp.index == option_index),
                            None,
                        )
                if (
                    option_warp is not None
                    and destination is not None
                    and tuple(destination) == edge.destination.map_key
                    and tuple(option_map) == edge.source.map_key
                    and max(
                        abs(option_warp.x - edge.source.x),
                        abs(option_warp.y - edge.source.y),
                    )
                    <= 1
                ):
                    return True
        if edge.kind == "surf":
            if edge.stable_key.startswith("edge:"):
                return key == edge.stable_key
            body = edge.target_dict().get("water_body")
            if body is None:
                return key == "surf"
            target = option.facts.get("target")
            return (
                isinstance(target, dict)
                and target.get("kind") == "surf"
                and (target.get("x"), target.get("y")) == tuple(body)
            )
        if edge.kind == "dismount":
            target = option.facts.get("target")
            landing = (edge.destination.x, edge.destination.y)
            return (
                isinstance(target, dict)
                and target.get("kind") == "landing"
                and any(
                    isinstance(tile, (tuple, list)) and tuple(tile) == landing
                    for tile in option.facts.get("landing_tiles", ())
                )
            )
        if edge.kind == "waterfall":
            target = option.facts.get("target")
            return (
                isinstance(target, dict)
                and target.get("kind") == "waterfall"
                and key == edge.stable_key
            )
        if edge.kind == "elevator":
            target = option.facts.get("target")
            edge_target = edge.target_dict()
            return isinstance(target, dict) and (
                (target.get("kind") == "sign" and target.get("bg") == edge_target.get("panel_bg"))
                or (
                    target.get("kind") == "elevator"
                    and tuple(target.get("dest", ())) == edge.destination.map_key
                )
                or (
                    target.get("kind") == "warp"
                    and "warp" in edge_target
                    and target.get("index") == edge_target["warp"]
                )
            )
        if edge.kind == "gate":
            target = option.facts.get("target")
            if isinstance(target, dict) and target.get("kind") == "tile":
                at = (target.get("x"), target.get("y"))
                if all(isinstance(value, int) for value in at):
                    return first_planned_gate_at(plan.route, at, tuple(target.get("map", ())))
            if (
                isinstance(target, dict)
                and target.get("kind") == "sign"
                and target.get("bg") == edge.target_dict().get("bg")
            ):
                return True
            if edge.target_dict().get("triggerable") is True and isinstance(target, dict):
                if gate_index is None:
                    raise ValueError("gate index required when matching a gate")
                crossing = next(
                    (
                        later
                        for later in plan.route[gate_index + 1 :]
                        if later.kind in {"warp", "connection"}
                    ),
                    None,
                )
                return bool(
                    crossing is not None
                    and target.get("kind") in {"warp", "edge"}
                    and target.get("dest") is not None
                    and tuple(target["dest"]) == crossing.destination.map_key
                )
            if isinstance(target, dict) and target.get("kind") == "warp":
                if gate_index is None:
                    raise ValueError("gate index required when matching a gate")
                crossing_warp = next(
                    (later for later in plan.route[gate_index + 1 :] if later.kind == "warp"),
                    None,
                )
                return bool(
                    crossing_warp is not None
                    and target.get("dest") is not None
                    and tuple(target["dest"]) == crossing_warp.destination.map_key
                )
            return False
        if edge.kind == "boulder_push" and option.id.startswith("boulder_plan:"):
            return option.id == edge.stable_key
        if edge.kind in ("walk", "bike", "ledge", "forced"):
            if (
                plan.unmet
                and plan.blocked_route_fact is not None
                and plan.route
                and not any(route_edge.unmet for route_edge in plan.route)
            ):
                return self._matches_route_end(option, plan.route[-1].destination)
            target = option.facts.get("target")
            if not isinstance(target, dict):
                return False
            if plan.target.local_id is not None:
                return (
                    target.get("kind") == "npc" and target.get("local_id") == plan.target.local_id
                )
            if plan.target.x is not None and plan.target.y is not None:
                return target.get("kind") == "tile" and (target.get("x"), target.get("y")) == (
                    plan.target.x,
                    plan.target.y,
                )
        return key == edge.stable_key

    @staticmethod
    def _matches_route_end(option: Option, end: NavigationNode) -> bool:
        target = option.facts.get("target")
        if not isinstance(target, dict):
            return False
        target_map = target.get("map")
        if target_map is not None and tuple(target_map) != end.map_key:
            return False
        if target.get("kind") == "tile":
            return (target.get("x"), target.get("y")) == (end.x, end.y)
        at = target.get("at")
        return isinstance(at, (tuple, list)) and tuple(at) == (end.x, end.y)
