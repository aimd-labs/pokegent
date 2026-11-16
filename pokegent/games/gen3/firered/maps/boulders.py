"""Boulder planner: a bounded A* over (boulders, opened switches, the player's walkable region).

Pure: it reads a :class:`MapState` (the live block of a snapshot, or a static ROM map) and the
ROM puzzle
data (``references/boulders.yaml``) and never touches the emulator. The model is the one the
real game was
measured to follow (``the runtime contracts`` section 11):

* a **push** moves one boulder one tile; the player ends on the tile it pushed from (``stand``)
and must
  walk to the next push tile. The far tile must be free by ``boulder_push_check`` (the rule
  ``predict_step``
  uses), may not hold another boulder or a standing object, and may not be a warp tile unless it
  is a hole;
* a boulder that stops on a ``MB_STRENGTH_BUTTON`` tile whose script is known is **pinned**
there (the
  script ``copyobjectxytoperm``s it; the model never moves it again) and the switch's barrier
  tiles open
  (collision cleared, the elevation bits kept) for the rest of the plan;
* a boulder pushed into a hole is gone (and counted as a drop into that hole);
* walking uses the field :class:`~pokegent.actions.field.nav.Navigator` rules (elevation travels
with
  the node) with the boulders as extra obstacles; unit costs;
* a displaced boulder is only kept alive by the camera (``map_boulders.retained``): a push whose
  walk would take the player out of that box is not generated, so a plan never relies on a
  boulder that the
  game would respawn at its template tile.

Goals: ``switch`` (a boulder rests on the button tile), ``hole`` (a boulder falls into the
hole), ``warp`` /
``edge`` (the exit becomes reachable; composed from switch plans when one push sequence is not
enough).
The search is bounded by a state count (never a clock) so the result is deterministic; hitting
the bound is
a typed ``NoPlan(reason="bound")``, never a silent failure.
"""

from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

from pokegent.actions.field import nav as N
from pokegent.games.gen3.firered.maps import map_constants as K
from pokegent.games.gen3.firered.maps.map_behaviors import behavior_info
from pokegent.games.gen3.firered.maps.map_boulders import (
    PuzzleData,
    SwitchData,
    boulders,
    puzzle_for,
    retained,
    switch_tiles,
    with_player,
    with_tiles,
)
from pokegent.games.gen3.firered.maps.map_motion import boulder_push_check
from pokegent.games.gen3.firered.maps.map_schema import Facing, MapState

DIRECTIONS: tuple[Facing, ...] = ("N", "S", "W", "E")
MAX_STATES = 4000  # expanded search states per goal
EXIT_STATES = 400  # the blind (heuristic-free) search for "an exit becomes reachable" (few pushes)
MAX_DEPTH = 3  # switch plans composed for one exit goal

Node = tuple[int, int, int]
Tile = tuple[int, int]
GoalKind = Literal["switch", "hole", "warp", "edge"]


@dataclass(frozen=True, order=True)
class Goal:
    kind: str  # switch | hole | warp | edge
    key: tuple  # (x, y) | (x, y) | (warp index,) | (direction,)

    @property
    def goal_id(self) -> str:
        return ":".join([self.kind, *map(str, self.key)])

    @classmethod
    def parse(cls, text: str) -> Goal:
        kind, *rest = text.split(":")
        key = tuple(r if kind == "edge" else int(r) for r in rest)
        return cls(kind, key)


@dataclass(frozen=True)
class Push:
    """One push: boulder ``boulder`` rolls from ``src`` to ``dst`` while the player stands on
    ``stand``."""

    boulder: int  # local id
    direction: Facing
    src: Tile
    dst: Tile
    stand: Tile
    walk: int  # steps from where the previous push left the player
    effect: Literal["roll", "switch", "fall"] = "roll"


@dataclass(frozen=True)
class Plan:
    goal: Goal
    pushes: tuple[Push, ...]
    walk: int
    states: int
    opens: tuple[Tile, ...] = ()  # switch tiles triggered by the plan
    drops: tuple[Tile, ...] = ()  # holes used
    unlocks: tuple[str, ...] = ()  # exit goal ids that become reachable (warp:3, edge:N)

    @property
    def cost(self) -> int:
        return self.walk + len(self.pushes)


@dataclass(frozen=True)
class NoPlan:
    goal: Goal
    reason: str  # done | no_boulder | unsolvable | bound
    states: int = 0


Result = Plan | NoPlan

FELL = -1  # position code of a boulder that fell: -(1 + hole index)


@dataclass
class _Region:
    dist: dict[Node, int]
    warps: set[int]
    edges: set[str]
    parent: dict[Node, tuple[Node, N.Move]] = field(default_factory=dict)

    def at(self, tile: Tile) -> list[Node]:
        index = self.__dict__.get("_index")
        if index is None:
            index = {}
            for node in self.dist:
                index.setdefault((node[0], node[1]), []).append(node)
            self.__dict__["_index"] = index
        return index.get(tile, [])

    def path_to(self, node: Node) -> tuple[N.Move, ...]:
        moves: list[N.Move] = []
        cur = node
        while cur in self.parent:
            cur, move = self.parent[cur]
            moves.append(move)
        moves.reverse()
        return tuple(moves)


@dataclass
class BoulderWorld:
    """Everything static about one map state plus the search."""

    state: MapState
    data: PuzzleData | None
    ids: tuple[int, ...] = ()  # boulder local ids, the order of positions
    elev: tuple[int, ...] = ()
    start_positions: tuple[int, ...] = ()
    pinned_at_start: frozenset[int] = frozenset()
    templates: tuple[Tile, ...] = ()
    start: Node | None = None  # a static map has no player: the caller names the start
    fixed: dict[Tile, int] = field(default_factory=dict)  # standing objects: tile -> elevation
    warp_tiles: frozenset[Tile] = frozenset()
    holes: tuple[Tile, ...] = ()
    switches: tuple[SwitchData, ...] = ()
    done_switches: frozenset[Tile] = frozenset()
    buttons: frozenset[Tile] = frozenset()

    def __post_init__(self) -> None:
        self._variants: dict[int, tuple[MapState, N.Navigator]] = {}
        self._adj: dict[tuple[int, Node, str], tuple[Node, N.Move] | None] = {}
        self._push: dict[tuple[int, int, int, int, str], tuple[str, str | None]] = {}
        self._moves_cache: dict[tuple[int, Node], list] = {}
        self._memo: dict[tuple, Result] = {}
        self.width = self.state.layout.width
        self.expanded = 0
        # arrow mats are walkable (only the press in their direction fires); every other warp tile
        # fires on arrival (step, ladder, hole, cave door) or cannot be walked (door)
        self._arrows: dict[Tile, Facing] = {
            (w.x, w.y): w.trigger_direction
            for w in self.state.warps
            if w.trigger == "arrow" and w.trigger_direction is not None
        }

    # -- construction ------------------------------------------------------------------------------

    @classmethod
    def from_state(cls, state: MapState, start: Node | None = None) -> BoulderWorld:
        if state.player is None:
            if start is None:
                raise ValueError("a static map needs an explicit start node")
            state = with_player(state, start[0], start[1], start[2])
        data = puzzle_for(state)
        found = [b for b in boulders(state) if not b.hidden]
        buttons = frozenset(switch_tiles(state))
        switches = tuple(data.switches) if data else ()
        known = {s.tile for s in switches}
        switches += tuple(SwitchData(t, 0, (), ()) for t in sorted(buttons - known))
        done = frozenset(
            s.tile
            for s in switches
            if s.opens and all(state.collision(o.x, o.y) == 0 for o in s.opens)
        )
        fixed: dict[Tile, int] = {}
        for tile, objs in state.blocker_positions().items():
            for obj in objs:
                if obj.role != "boulder":
                    fixed[tile] = obj.live.elevation if obj.live is not None else obj.elevation
        world = cls(
            state=state,
            data=data,
            ids=tuple(b.local_id for b in found),
            elev=tuple(b.elevation for b in found),
            start_positions=tuple(b.xy[1] * state.layout.width + b.xy[0] for b in found),
            templates=tuple(b.template_xy for b in found),
            start=start,
            fixed=fixed,
            warp_tiles=frozenset((w.x, w.y) for w in state.warps),
            holes=tuple(h.tile for h in (data.holes if data else ()))
            or tuple(t for t in _holes(state)),
            switches=switches,
            done_switches=done,
            buttons=buttons,
        )
        world.pinned_at_start = frozenset(i for i, b in enumerate(found) if b.xy in buttons)
        return world

    def share_caches(self, other: BoulderWorld) -> None:
        """Reuse the static caches (variants, adjacency, push checks) of a world of the same
        layout."""
        self._variants = other._variants
        self._adj = other._adj
        self._push = other._push
        self._moves_cache = other._moves_cache

    def tile(self, pos: int) -> Tile:
        return (pos % self.width, pos // self.width)

    def code(self, tile: Tile) -> int:
        return tile[1] * self.width + tile[0]

    # -- variants by opened-switch mask
    # ----------------------------------------------------------------

    def _mask_of(self, opened: frozenset[Tile]) -> int:
        return sum(1 << i for i, s in enumerate(self.switches) if s.tile in opened)

    def _variant(self, mask: int) -> tuple[MapState, N.Navigator]:
        got = self._variants.get(mask)
        if got is not None:
            return got
        state = self.state
        tiles = {
            (o.x, o.y): (o.metatile, o.behavior)
            for k, sw in enumerate(self.switches)
            if mask >> k & 1
            for o in sw.opens
        }
        grid_state = with_tiles(state, tiles) if tiles else state
        grid = grid_state.grid
        stripped = tuple(o for o in state.objects if o.role != "boulder")
        variant = state.model_copy(update={"grid": grid, "objects": stripped})
        got = (variant, N.Navigator(variant))
        self._variants[mask] = got
        return got

    def _step(self, mask: int, node: Node, d: Facing):
        key = (mask, node, d)
        if key not in self._adj:
            self._adj[key] = self._variant(mask)[1].step(node, d)
        return self._adj[key]

    # -- regions
    # -------------------------------------------------------------------------------------

    def region(self, mask: int, positions: tuple[int, ...], start: Node) -> _Region:
        occupied: dict[Tile, int] = {}
        for i, p in enumerate(positions):
            if p >= 0:
                occupied[self.tile(p)] = self.elev[i]
        dist = {start: 0}
        parent: dict[Node, tuple[Node, N.Move]] = {}
        warps: set[int] = set()
        edges: set[str] = set()
        queue = deque([start])
        while queue:
            node = queue.popleft()
            base = dist[node] + 1
            for nxt, move, at, crossable in self._moves(mask, node):
                blocker = occupied.get(at)
                if blocker is not None and _compatible(nxt[2], blocker):
                    continue
                if move.warp_index is not None:
                    warps.add(move.warp_index)
                if move.connection is not None:
                    edges.add(move.connection)
                if nxt in dist:
                    continue
                dist[nxt] = base
                parent[nxt] = (node, move)
                if crossable:
                    queue.append(nxt)  # step warps and ring tiles are reached, never crossed
        return _Region(dist, warps, edges, parent)

    def _moves(self, mask: int, node: Node):
        """The walkable moves out of ``node`` (cached): (next node, move, its tile, crossable)."""
        key = (mask, node)
        got = self._moves_cache.get(key)
        if got is None:
            arrow = self._arrows.get((node[0], node[1]))
            got = []
            for d in DIRECTIONS:
                if d == arrow:
                    continue  # pressing the arrow's direction on an arrow mat fires the warp
                step = self._step(mask, node, d)
                if step is None:
                    continue
                nxt, move = step
                crossable = move.connection is None and not self._is_terminal(nxt)
                got.append((nxt, move, (nxt[0], nxt[1]), crossable))
            self._moves_cache[key] = got
        return got

    def start_node(self) -> Node:
        if self.start is not None:
            return self.start
        p = self.state.player
        assert p is not None, "a static map needs an explicit start node"
        return (p.avatar_x, p.avatar_y, p.elevation)

    # -- pushes
    # ----------------------------------------------------------------------------------------

    def pushes(
        self, mask: int, positions: tuple[int, ...], region: _Region
    ) -> list[tuple[int, Facing, int, Node, str]]:
        """(boulder index, direction, new position code, stand node, effect) for every legal
        push."""
        out = []
        occupied = {p for p in positions if p >= 0}
        pinned = self._pinned(positions)
        for i, pos in enumerate(positions):
            if pos < 0 or i in pinned:
                continue
            x, y = self.tile(pos)
            for d in DIRECTIONS:
                vx, vy = K.VECTOR[d]
                stand = (x - vx, y - vy)
                node = self._stand_node(region, stand, self.elev[i])
                if node is None or self._arrows.get(stand) == d:
                    continue
                far = (x + vx, y + vy)
                if far in self.fixed and _compatible(self.elev[i], self.fixed[far]):
                    continue
                fcode = self.code(far)
                key = (mask, x, y, self.elev[i], d)
                result = self._push.get(key)
                if result is None:
                    result = self._push[key] = boulder_push_check(
                        self._variant(mask)[0], x, y, self.elev[i], d
                    )
                outcome, _ = result
                if outcome == "blocked":
                    continue
                if outcome == "falls":
                    hole = self.holes.index(far) if far in self.holes else 0
                    out.append((i, d, -(1 + hole), node, "fall"))
                    continue
                if fcode in occupied or far in self.warp_tiles:
                    continue
                effect = "switch" if far in self.buttons else "roll"
                out.append((i, d, fcode, node, effect))
        return out

    def _pinned(self, positions: tuple[int, ...]) -> set[int]:
        return {i for i, p in enumerate(positions) if p >= 0 and self.tile(p) in self.buttons}

    def _stand_node(self, region: _Region, stand: Tile, boulder_elevation: int) -> Node | None:
        best = None
        for node in region.at(stand):
            # A warp reached while walking is terminal, but a map load may place the player on
            # its landing tile.  FireRed accepts a push directly from that current RAM position
            # before the player leaves it (Victory Road's dropped 2F boulder is immediately west
            # of such a landing).  Do not invent access to any other terminal warp tile.
            if not _compatible(node[2], boulder_elevation) or (
                self._is_terminal(node) and region.dist[node] != 0
            ):
                continue
            if best is None or region.dist[node] < region.dist[best]:
                best = node
        return best

    def _is_terminal(self, node: Node) -> bool:
        tile = (node[0], node[1])
        return tile in self.warp_tiles and tile not in self._arrows

    # -- search
    # ------------------------------------------------------------------------------------------

    def _opened_after(self, mask: int, tile: Tile) -> int:
        for i, s in enumerate(self.switches):
            if s.tile == tile:
                return mask | (1 << i)
        return mask

    def initial(self) -> tuple[tuple[int, ...], int]:
        mask = self._mask_of(self.done_switches)
        return self.start_positions, mask

    def solve(
        self,
        goal: Goal,
        *,
        positions: tuple[int, ...] | None = None,
        mask: int | None = None,
        start: Node | None = None,
        max_states: int = MAX_STATES,
        compose: bool = True,
    ) -> Result:
        """Cheapest push sequence for ``goal`` from the given (default: the live) configuration."""
        positions0, mask0 = self.initial()
        positions = positions0 if positions is None else positions
        mask = mask0 if mask is None else mask
        start = start or self.start_node()
        memo_key = (goal, positions, mask, start, max_states, compose)
        if memo_key not in self._memo:
            self._memo[memo_key] = self._solve(goal, positions, mask, start, max_states, compose)
        return self._memo[memo_key]

    def _solve(
        self, goal: Goal, positions, mask: int, start: Node, max_states: int, compose: bool
    ) -> Result:
        if goal.kind in ("warp", "edge"):
            return self._solve_exit(goal, positions, mask, start)
        target: Tile = goal.key  # type: ignore[assignment]
        if goal.kind == "switch":
            if target in {s.tile for s in self.switches if s.tile in self.done_switches}:
                return NoPlan(goal, "done")
            if any(p >= 0 and self.tile(p) == target for p in positions):
                return NoPlan(goal, "done")
        movable = [
            i for i, p in enumerate(positions) if p >= 0 and i not in self._pinned(positions)
        ]
        if not movable:
            return NoPlan(goal, "no_boulder")
        hole_index = (
            self.holes.index(target) if goal.kind == "hole" and target in self.holes else None
        )

        def reached(pos: tuple[int, ...]) -> bool:
            if goal.kind == "switch":
                return any(p >= 0 and self.tile(p) == target for p in pos)
            return hole_index is not None and -(1 + hole_index) in pos

        def h(pos: tuple[int, ...]) -> int:
            best = 10_000
            for i, p in enumerate(pos):
                if p < 0 or self.tile(p) in self.buttons:
                    continue
                x, y = self.tile(p)
                best = min(best, abs(x - target[0]) + abs(y - target[1]))
            return best

        direct = self._astar(
            goal, positions, mask, start, reached, h, max_states, goal_after_push=True
        )
        if compose and isinstance(direct, NoPlan) and direct.reason == "unsolvable":
            # maybe a barrier has to open first (the 3F drop sits behind the 3F switch)
            return self._compose(
                goal, positions, mask, start,
                lambda p, m, n: self._astar(goal, p, m, n, reached, h, max_states, goal_after_push=True),  # noqa: E501
            )  # fmt: skip
        return direct

    def _astar(self, goal, positions, mask, start, reached, h, max_states, *, goal_after_push):
        counter = 0
        heap: list = [(h(positions), 0, counter, positions, mask, start, ())]
        best_g: dict[tuple, int] = {}
        comps: dict[tuple, dict[Node, Node]] = {}
        expanded = 0
        while heap:
            f, g, _, pos, msk, node, trail = heapq.heappop(heap)
            if reached(pos):
                return self._finish(goal, trail, expanded)
            known = comps.get((pos, msk))
            rep = known.get(node) if known is not None else None
            if rep is not None and best_g.get((pos, msk, rep), 1 << 30) <= g:
                continue  # the same walkable region, already reached at least as cheaply
            region = self.region(msk, pos, node)
            if rep is None:
                rep = min(region.dist)
                comps.setdefault((pos, msk), {}).update(dict.fromkeys(region.dist, rep))
            key = (pos, msk, rep)
            if best_g.get(key, 1 << 30) <= g:
                continue
            best_g[key] = g
            expanded += 1
            if expanded > max_states:
                return NoPlan(goal, "bound", expanded)
            for i, d, new_code, stand, effect in self.pushes(msk, pos, region):
                walk = region.dist[stand]
                x, y = self.tile(pos[i])
                vx, vy = K.VECTOR[d]
                src = (x, y)
                dst = (x + vx, y + vy)
                # keep-alive: a displaced boulder must stay inside the retention box of the
                # player's walk
                if not self._keeps_alive(pos, i, region, stand):
                    continue
                new_pos = pos[:i] + (new_code,) + pos[i + 1 :]
                new_mask = self._opened_after(msk, dst) if effect == "switch" else msk
                step = Push(self.ids[i], d, src, dst, (stand[0], stand[1]), walk, effect)
                ng = g + walk + 1
                counter += 1
                heapq.heappush(
                    heap, (ng + h(new_pos), ng, counter, new_pos, new_mask, stand, (*trail, step))
                )
        return NoPlan(goal, "unsolvable", expanded)

    def _keeps_alive(self, pos: tuple[int, ...], pushed: int, region: _Region, stand: Node) -> bool:
        """Every displaced boulder other than the pushed one must stay retained while the
        player stands
        on the push tile (the player walks there; the pushed boulder is next to it)."""
        for j, p in enumerate(pos):
            if j == pushed or p < 0:
                continue
            tile = self.tile(p)
            if tile in self.buttons:
                continue  # pinned by its switch script (copyobjectxytoperm): the template tile moved too  # noqa: E501
            if tile != self.templates[j] and not retained((stand[0], stand[1]), tile):
                return False
        return True

    def _finish(self, goal: Goal, trail: tuple[Push, ...], states: int) -> Plan:
        opens = tuple(p.dst for p in trail if p.effect == "switch")
        drops = tuple(p.dst for p in trail if p.effect == "fall")
        return Plan(goal, trail, sum(p.walk for p in trail), states, opens, drops)

    # -- exits
    # ---------------------------------------------------------------------------------------------

    def exits_reachable(self, positions, mask, start) -> set[str]:
        r = self.region(mask, positions, start)
        return {f"warp:{w}" for w in r.warps} | {f"edge:{e}" for e in r.edges}

    def _reach_goal(self, goal: Goal):
        want = goal.key[0]
        if goal.kind == "warp":
            return lambda r: want in r.warps
        return lambda r: want in r.edges

    def _solve_exit(self, goal: Goal, positions, mask, start) -> Result:
        reach = self._reach_goal(goal)
        # A fall-warp is also the destination of a boulder hole.  Opening the barrier can make
        # that tile reachable by the player without performing the puzzle's cross-floor effect:
        # only the boulder fall clears the reveal flag for the floor below.  Treat the drop as a
        # prerequisite of this particular exit, using the same ROM-derived HoleData as the map.
        hole = (
            next((row for row in self.data.holes if row.warp == int(goal.key[0])), None)
            if goal.kind == "warp" and self.data is not None
            else None
        )
        if hole is not None and hole.tile in self.holes:
            hole_index = self.holes.index(hole.tile)
            if -(1 + hole_index) not in positions:
                dropped = self._solve(
                    Goal("hole", hole.tile), positions, mask, start, MAX_STATES, True
                )
                if not isinstance(dropped, Plan):
                    return NoPlan(goal, dropped.reason, dropped.states)
                return Plan(
                    goal,
                    dropped.pushes,
                    dropped.walk,
                    dropped.states,
                    dropped.opens,
                    dropped.drops,
                    (goal.goal_id,),
                )
        if reach(self.region(mask, positions, start)):
            return NoPlan(goal, "done")
        return self._compose(
            goal, positions, mask, start, lambda p, m, n: self._blind(goal, p, m, n, reach)
        )

    def _compose(self, goal: Goal, positions, mask, start, finish) -> Result:
        """``finish(positions, mask, node)`` solves the goal's last leg (a few pushes).
        When it cannot, the unmet switches are tried first (each by its own bounded A*),
        up to ``MAX_DEPTH`` of them in a row: the cheapest total wins. This is how
        "open the barrier, THEN reach the hole / the exit" is planned."""
        frontier = [((), positions, mask, start)]
        best: Plan | None = None
        states = 0
        for depth in range(MAX_DEPTH + 1):
            nxt = []
            for trail, pos, msk, node in frontier:
                last = finish(pos, msk, node)
                states += last.states
                if isinstance(last, Plan):
                    total = (*trail, *last.pushes)
                    plan = Plan(goal, total, sum(p.walk for p in total), states,
                                tuple(p.dst for p in total if p.effect == "switch"),
                                tuple(p.dst for p in total if p.effect == "fall"))  # fmt: skip
                    if best is None or plan.cost < best.cost:
                        best = plan
                    continue
                if depth == MAX_DEPTH:
                    continue
                for sw in self.switches:
                    if (
                        not sw.opens
                        or sw.tile in self._opened(msk)
                        or goal == Goal("switch", sw.tile)
                    ):
                        continue
                    sub = self.solve(
                        Goal("switch", sw.tile), positions=pos, mask=msk, start=node, compose=False
                    )
                    states += sub.states
                    if not isinstance(sub, Plan):
                        continue
                    pos2, msk2, node2 = self._apply(pos, msk, sub)
                    nxt.append(((*trail, *sub.pushes), pos2, msk2, node2 or node))
            if best is not None:
                return self._with_unlocks(best, positions, mask, start)
            frontier = nxt
            if not frontier:
                break
        return NoPlan(goal, "unsolvable", states)

    def _opened(self, mask: int) -> set[Tile]:
        return {s.tile for i, s in enumerate(self.switches) if mask >> i & 1}

    def _apply(self, positions, mask, plan: Plan):
        """Final (positions, mask, player node) after a plan."""
        pos = list(positions)
        msk = mask
        node = None
        for push in plan.pushes:
            i = self.ids.index(push.boulder)
            if push.effect == "fall":
                pos[i] = -(1 + (self.holes.index(push.dst) if push.dst in self.holes else 0))
            else:
                pos[i] = self.code(push.dst)
            if push.effect == "switch":
                msk = self._opened_after(msk, push.dst)
            node = (push.stand[0], push.stand[1], self._elev_at(push.stand))
        return tuple(pos), msk, node

    def _elev_at(self, tile: Tile) -> int:
        e = self.state.elevation(*tile)
        return 3 if e in (0, 15) else e

    def _blind(self, goal, positions, mask, start, reach) -> Result:
        counter = 0
        heap = [(0, counter, positions, mask, start, ())]
        seen: dict[tuple, int] = {}
        expanded = 0
        while heap:
            g, _, pos, msk, node, trail = heapq.heappop(heap)
            region = self.region(msk, pos, node)
            if reach(region):
                return Plan(goal, trail, sum(p.walk for p in trail), expanded,
                            tuple(p.dst for p in trail if p.effect == "switch"),
                            tuple(p.dst for p in trail if p.effect == "fall"))  # fmt: skip
            key = (pos, msk, min(region.dist))
            if key in seen:
                continue
            seen[key] = g
            expanded += 1
            if expanded > EXIT_STATES:
                return NoPlan(goal, "bound", expanded)
            if len(trail) >= 3:
                continue
            for i, d, new_code, stand, effect in self.pushes(msk, pos, region):
                if not self._keeps_alive(pos, i, region, stand):
                    continue
                x, y = self.tile(pos[i])
                vx, vy = K.VECTOR[d]
                dst = (x + vx, y + vy)
                step = Push(
                    self.ids[i], d, (x, y), dst, (stand[0], stand[1]), region.dist[stand], effect
                )
                new_pos = pos[:i] + (new_code,) + pos[i + 1 :]
                new_mask = self._opened_after(msk, dst) if effect == "switch" else msk
                counter += 1
                heapq.heappush(
                    heap,
                    (g + region.dist[stand] + 1, counter, new_pos, new_mask, stand, (*trail, step)),
                )
        return NoPlan(goal, "unsolvable", expanded)

    def _with_unlocks(self, plan: Plan, positions, mask, start) -> Plan:
        before = self.exits_reachable(positions, mask, start)
        pos2, msk2, node2 = self._apply(positions, mask, plan)
        after = self.exits_reachable(pos2, msk2, node2 or start) if plan.pushes else before
        return Plan(plan.goal, plan.pushes, plan.walk, plan.states, plan.opens, plan.drops,
                    tuple(sorted(after - before)))  # fmt: skip

    # -- goals
    # ------------------------------------------------------------------------------------------

    def goals(self, exits: tuple[Goal, ...] = ()) -> list[Goal]:
        out = [Goal("switch", s.tile) for s in self.switches if s.tile not in self.done_switches]
        out += [Goal("hole", h) for h in self.holes]
        out += list(exits)
        return out

    def plan_all(self, exits: tuple[Goal, ...] = ()) -> dict[Goal, Result]:
        results: dict[Goal, Result] = {}
        for goal in self.goals(exits):
            plan = self.solve(goal)
            if isinstance(plan, Plan) and goal.kind in ("switch", "hole"):
                plan = self._with_unlocks(plan, *self.initial(), self.start_node())
            results[goal] = plan
        return results

    # -- replay (verify an externally supplied push list against this model)
    # -----------------------------

    def replay(self, pushes: list[tuple[int, Facing]]) -> tuple[bool, int, str]:
        """Apply ``(local id, direction)`` pushes in order from the live configuration. Returns
        ``(ok, index of the first invalid push or len, reason)``."""
        pos, mask = self.initial()
        node = self.start_node()
        for n, (local_id, d) in enumerate(pushes):
            if local_id not in self.ids:
                return False, n, "no_such_boulder"
            region = self.region(mask, pos, node)
            legal = {
                (i, dd): (code, stand, effect)
                for i, dd, code, stand, effect in self.pushes(mask, pos, region)
            }
            i = self.ids.index(local_id)
            if (i, d) not in legal:
                return False, n, "illegal_push"
            code, stand, effect = legal[(i, d)]
            vx, vy = K.VECTOR[d]
            x, y = self.tile(pos[i])
            dst = (x + vx, y + vy)
            pos = pos[:i] + (code,) + pos[i + 1 :]
            if effect == "switch":
                mask = self._opened_after(mask, dst)
            node = stand
        return True, len(pushes), ""


def _compatible(a: int, b: int) -> bool:
    return a == 0 or b == 0 or a == b


def _holes(state: MapState) -> list[Tile]:
    from pokegent.games.gen3.firered.maps.map_boulders import hole_tiles

    return list(hole_tiles(state))


__all__ = [
    "BoulderWorld",
    "Goal",
    "NoPlan",
    "Plan",
    "Push",
    "behavior_info",
]
