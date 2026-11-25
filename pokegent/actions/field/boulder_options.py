"""The ``boulder_plan:<map>:<goal>`` options of ``field.free`` and the plan cache they share
with the macro.

One option per goal of the current map that the planner (``boulders.py``) can reach:

* ``switch:<x>:<y>``: roll a boulder onto the floor switch (opens the barrier its script opens);
* ``hole:<x>:<y>``: roll a boulder into the hole (it falls to the floor below);
* ``warp:<index>`` / ``edge:<dir>``: clear the way to an exit that the boulders block (a switch
or a few
  pushes make it reachable).

The option stays on ``field.free`` (the field family owns that scene and the arming option
``strength:<id>`` is already there); the catalog scene ``special.strength_puzzle`` is NOT
claimed because
the options need no puzzle UI of their own: the board is the overworld. Facts (also in the English
description, because the model sees nothing else): ``pushes``, ``walk`` (steps between pushes),
``armed``
(Strength is active on this map load), ``activations`` (1 when the macro has to arm Strength
first and
the dialog family has to answer the prompt), ``opens`` / ``drops``, ``unlocks`` (exits that become
reachable), ``depends_on`` (a drop on another floor that a hidden boulder waits for),
``serves_goal``.

An unsolvable goal is a diagnostic with a typed reason, never silently absent: ``no_boulder``
(nothing
movable: all hidden / pinned), ``unsolvable`` (the search space is exhausted), ``search_bound``
(the state
bound was hit), ``missing_capability`` (no Strength carrier or no Rainbow badge).
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from pokegent.actions.field.boulders import BoulderWorld, Goal, NoPlan, Plan
from pokegent.actions.field.capabilities import field_capability
from pokegent.games.gen3.firered.maps.map_boulders import armed, boulders, puzzles
from pokegent.games.gen3.firered.maps.map_schema import MapState
from pokegent.navigation.routing import MapGraph
from pokegent.navigation.serving import serves_goal
from pokegent.options.models import Legality, Option, Risk
from pokegent.options.render import register_fact_renderer, render_facts

PUSH_BOULDERS = "field.push_boulders"
PC_BOULDER = "field.boulder_progress"
VERB = "PUSH_BOULDER"
MAX_CACHE = 48


def _r_boulder(f: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    pushes = f.get("pushes")
    if isinstance(pushes, int) and not isinstance(pushes, bool):
        out.append(f"{pushes} boulder push{'es' if pushes != 1 else ''}")
        if isinstance(f.get("walk"), int):
            out.append(f"{f['walk']} steps of walking between the pushes")
    if f.get("armed") is True:
        out.append("Strength is already active on this floor")
    elif f.get("armed") is False and pushes is not None:
        out.append("arms Strength first (a yes/no question follows)")
    if f.get("opens"):
        out.append("a boulder on the floor switch opens the barrier next to it")
    if f.get("drops"):
        out.append("the boulder falls through the hole to the floor below")
    names = f.get("unlocks_names")
    if isinstance(names, (list, tuple)) and names:
        out.append("makes reachable: " + ", ".join(str(n) for n in names))
    dep = f.get("depends_on")
    if isinstance(dep, str) and dep:
        out.append(f"the boulder it needs only appears after a boulder drops through {dep}")
    return out


register_fact_renderer("boulder", _r_boulder)


class BoulderPlans:
    """Worlds and plans, cached: a plan only changes when the boulders, the opened switches or the
    walkable region of the player change (bounded, deterministic)."""

    def __init__(self, max_entries: int = MAX_CACHE) -> None:
        self.max_entries = max_entries
        self._plans: OrderedDict[tuple, dict[Goal, Plan | NoPlan]] = OrderedDict()
        self._worlds: dict[tuple, BoulderWorld] = {}

    def world(self, state: MapState) -> BoulderWorld:
        """A world for ``state``; the heavy adjacency / push caches are shared per static layout."""
        world = BoulderWorld.from_state(state)
        ident = state.identity
        fixed = tuple(sorted(world.fixed.items()))
        key = (ident.group, ident.number, hash(state.grid.blocks), fixed)
        old = self._worlds.get(key)
        if old is not None:
            world.share_caches(old)
        else:
            if len(self._worlds) >= 8:
                self._worlds.pop(next(iter(self._worlds)))
            self._worlds[key] = world
        return world

    def plans(
        self, state: MapState, world: BoulderWorld, exits: tuple[Goal, ...]
    ) -> dict[Goal, Plan | NoPlan]:
        positions, mask = world.initial()
        region = world.region(mask, positions, world.start_node())
        ident = state.identity
        fixed = tuple(sorted(world.fixed.items()))
        key = (
            ident.group,
            ident.number,
            hash(state.grid.blocks),
            fixed,
            positions,
            mask,
            min(region.dist),
            exits,
        )
        got = self._plans.get(key)
        if got is None:
            got = world.plan_all(exits)
            self._plans[key] = got
            while len(self._plans) > self.max_entries:
                self._plans.popitem(last=False)
        else:
            self._plans.move_to_end(key)
        return got


def _exit_goals(options: list[Option]) -> tuple[Goal, ...]:
    """Current warp/edge goals that may need a boulder plan.

    The navigation graph plans boulder edges from the live map independently of whether the
    ordinary exit option is already marked unavailable.  Restricting this list to the
    ``needs_field_move`` diagnostic therefore drops plans for exits that are still chooseable in
    the field frame.
    """
    out: list[Goal] = []
    for o in options:
        t = o.facts.get("target", {})
        if t.get("kind") == "warp":
            out.append(Goal("warp", (int(t["index"]),)))
        elif t.get("kind") == "edge":
            out.append(Goal("edge", (str(t["direction"]),)))
    return tuple(sorted(set(out)))


def depends_on(state: MapState) -> str | None:
    """A hidden boulder of this map waits for a drop elsewhere: ``<Map>:hole:<x>:<y>`` of that
    drop."""
    hidden = [b for b in boulders(state) if b.hidden and b.hide_flag]
    for b in hidden:
        for data in puzzles(allow_empty=True).values():
            for other in data.boulders:
                if other.reveal_flag == b.hide_flag and data.holes:
                    return f"{data.name} ({data.holes[0].tile[0]}, {data.holes[0].tile[1]})"
    return None


def boulder_options(
    plans: BoulderPlans,
    *,
    snapshot,
    state: MapState,
    options: list[Option],
    objective,
    graph: MapGraph | None,
) -> list[Option]:
    """The ``boulder_plan:*`` options (and diagnostics) of ``state``; ``[]`` on a map without
    boulders."""
    if state.player is None or not any(not b.hidden for b in boulders(state)):
        return []
    if state.player.surfing or state.player.biking or state.player.underwater:
        return []
    world = plans.world(state)
    exits = _exit_goals(options)
    results = plans.plans(state, world, exits)
    capability = field_capability(snapshot, "strength")
    is_armed = armed(state) is True
    ident = state.identity
    here = [ident.group, ident.number]
    by_id = {o.id: o for o in options}
    waiting = depends_on(state)
    out: list[Option] = []
    for goal, result in results.items():
        if isinstance(result, NoPlan) and result.reason == "done":
            continue
        option_id = f"boulder_plan:{ident.name}:{goal.goal_id}"
        facts: dict[str, Any] = {
            "target": {"kind": "boulder_plan", "map": here, "goal": goal.goal_id},
            "needs": {"move": capability.name, "badge": capability.badge_name},
            "carrier": list(capability.carrier_species),
            "armed": is_armed,
        }
        label = _label(goal, by_id, state)
        if isinstance(result, NoPlan):
            reason = {
                "unsolvable": "unsolvable",
                "bound": "search_bound",
                "no_boulder": "no_boulder",
            }.get(result.reason, result.reason)
            facts["reason"] = reason
            if waiting and reason in ("no_boulder", "unsolvable"):
                facts["depends_on"] = waiting
            out.append(_diagnostic(option_id, label, facts, Legality.UNAVAILABLE, reason))
            continue
        facts.update(_plan_facts(result, state, by_id))
        if not capability.available:
            reason = "capability_unknown" if not capability.known else "missing_capability"
            facts["reason"] = reason
            out.append(_diagnostic(option_id, label, facts, Legality.UNAVAILABLE, reason))
            continue
        facts["steps"] = result.cost
        facts["activations"] = 0 if is_armed else 1
        serves = _serves(result, facts, state, by_id, objective, graph, goal)
        if serves is not None:
            facts["serves_goal"] = serves
        out.append(
            Option(
                id=option_id,
                label=label,
                description=_describe(label, facts),
                legality=Legality.LEGAL,
                facts=facts,
                executor_ref=PUSH_BOULDERS,
                executor_args=_args(goal),
                risk=Risk.NORMAL,
                postcondition_ref=PC_BOULDER,
                verb=VERB,
                expected_postcondition=_expected(goal),
            )
        )
    return out


#: goal kinds as integers: a string argument of 6+ characters would be an "internal reference"
# that the
#: payload check refuses to see in the option's text
GOAL_KINDS = ("switch", "hole", "warp", "edge")


def _args(goal: Goal) -> dict[str, Any]:
    kind = GOAL_KINDS.index(goal.kind)
    if goal.kind == "edge":
        return {"goal_kind": kind, "x": "NSWE".index(goal.key[0]), "y": 0}
    if goal.kind == "warp":
        return {"goal_kind": kind, "x": int(goal.key[0]), "y": 0}
    return {"goal_kind": kind, "x": int(goal.key[0]), "y": int(goal.key[1])}


def goal_from_args(args: Mapping[str, Any]) -> Goal:
    kind = GOAL_KINDS[args["goal_kind"]]
    if kind == "edge":
        return Goal("edge", ("NSWE"[args["x"]],))
    if kind == "warp":
        return Goal("warp", (int(args["x"]),))
    return Goal(kind, (int(args["x"]), int(args["y"])))


def _label(goal: Goal, by_id: Mapping[str, Option], state: MapState) -> str:
    if goal.kind == "switch":
        return "Solve the boulder puzzle: roll a boulder onto the floor switch"
    if goal.kind == "hole":
        return "Roll a boulder into the hole in the floor"
    ident = state.identity
    if goal.kind == "warp":
        base = by_id.get(f"warp:{ident.group}:{ident.number}:{goal.key[0]}")
    else:
        conn = next((c for c in state.connections if c.direction == goal.key[0]), None)
        base = (
            by_id.get(f"edge:{goal.key[0]}:{conn.dest_group}:{conn.dest_number}") if conn else None
        )
    dest = base.facts.get("destination") if base is not None else None
    return (
        f"Move the boulders out of the way to reach the way to {dest}"
        if dest
        else "Move the boulders out of the way of an exit"
    )


def _plan_facts(plan: Plan, state: MapState, by_id: Mapping[str, Option]) -> dict[str, Any]:
    ident = state.identity
    facts: dict[str, Any] = {"pushes": len(plan.pushes), "walk": plan.walk}
    if plan.opens:
        facts["opens"] = [list(t) for t in plan.opens]
    if plan.drops:
        facts["drops"] = [list(t) for t in plan.drops]
    ids: list[str] = []
    names: list[str] = []
    for exit_id in plan.unlocks:
        kind, _, key = exit_id.partition(":")
        if kind == "warp":
            oid = f"warp:{ident.group}:{ident.number}:{key}"
        else:
            conn = next((c for c in state.connections if c.direction == key), None)
            oid = f"edge:{key}:{conn.dest_group}:{conn.dest_number}" if conn else ""
        ids.append(oid)
        dest = by_id[oid].facts.get("destination") if oid in by_id else None
        names.append(str(dest) if dest else "the boulder-blocked exit")
    if ids:
        facts["unlocks"] = ids
        facts["unlocks_names"] = names
    return facts


def _serves(plan, facts, state, by_id, objective, graph, goal) -> bool | None:
    """The plan serves the objective when it is, or unlocks, an exit that does."""
    if objective is None:
        return None
    candidates = list(facts.get("unlocks", ()))
    if goal.kind in ("warp", "edge"):
        ident = state.identity
        if goal.kind == "warp":
            candidates.append(f"warp:{ident.group}:{ident.number}:{goal.key[0]}")
        else:
            conn = next((c for c in state.connections if c.direction == goal.key[0]), None)
            if conn is not None:
                candidates.append(f"edge:{goal.key[0]}:{conn.dest_group}:{conn.dest_number}")
    caps = _plan_caps(state, plan)
    answers = []
    for oid in candidates:
        base = by_id.get(oid)
        if base is None:
            continue
        answers.append(serves_goal(base.facts, objective, state, graph=graph, caps=caps))
    if any(a is True for a in answers):
        return True
    return False if answers else None


def _plan_caps(state: MapState, plan: Plan) -> frozenset[str]:
    """The conditional region edges this exact push plan opens or drops through."""
    data = puzzles(allow_empty=True).get((state.identity.group, state.identity.number))
    if data is None:
        return frozenset()
    caps: set[str] = set()
    opened = set(plan.opens)
    dropped = set(plan.drops)
    for switch in data.switches:
        if opened.intersection((tile.x, tile.y) for tile in switch.opens):
            caps.add(f"boulder_plan:{data.name}:switch:{switch.tile[0]}:{switch.tile[1]}")
    for hole in data.holes:
        if tuple(hole.tile) in dropped:
            caps.add(f"boulder_plan:{data.name}:hole:{hole.tile[0]}:{hole.tile[1]}")
    return frozenset(caps)


def _describe(label: str, facts: Mapping[str, Any]) -> str:
    parts = [label + "."]
    rendered = render_facts(
        {
            k: facts[k]
            for k in ("pushes", "walk", "armed", "opens", "drops", "unlocks_names", "depends_on")
            if k in facts
        }
    )
    if rendered:
        parts.append(rendered.rstrip(".") + ".")
    needs = facts.get("needs")
    if isinstance(needs, Mapping) and needs.get("move"):
        parts.append(
            f"Needs a Pokemon that knows {needs['move']} and the {needs.get('badge')} Badge."
        )
    # No objective sentence here: navigation annotates the route relation afterwards and the
    # criterion renders it from facts; a baked-in sentence would contradict it.
    return " ".join(parts)[:600]


def _expected(goal: Goal) -> str:
    return {
        "switch": "a boulder rests on the floor switch and the barrier opens",
        "hole": "a boulder has fallen through the hole",
    }.get(goal.kind, "the exit is no longer blocked by a boulder")


def _diagnostic(
    option_id: str, label: str, facts: dict[str, Any], legality: Legality, reason: str
) -> Option:
    parts = [f"{label}. Not possible now: {reason.replace('_', ' ')}."]
    rendered = render_facts({k: facts[k] for k in ("pushes", "walk", "depends_on") if k in facts})
    if rendered:
        parts.append(rendered.rstrip(".") + ".")
    needs = facts.get("needs")
    if isinstance(needs, Mapping) and needs.get("move") and reason == "missing_capability":
        parts.append(
            f"Needs a Pokemon that knows {needs['move']} and the {needs.get('badge')} Badge."
        )
    return Option(
        id=option_id,
        label=label,
        description=" ".join(parts)[:600],
        legality=legality,
        facts=facts,
        verb=VERB,
    )
