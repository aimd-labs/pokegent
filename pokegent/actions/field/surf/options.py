"""Options for Surf in ``field.free``: ``surf:<x>:<y>`` (START_SURF) and ``dismount:<x>:<y>``
(DISMOUNT_SURF).

* ``surf:<x>:<y>`` names a water body of the map by its smallest tile. LEGAL when the party can Surf
  (a member with the move AND the Soul Badge) and a shore edge is reachable on foot; otherwise a
  diagnostic (UNAVAILABLE ``missing_capability``) that says what is missing. Facts: where the water
  leads (``leads_to`` / ``exits``: the exits that only Surf reaches, judged by the relaxation
  search),
  ``water_tiles``, ``steps``, the shore, ``serves_goal`` (the objective with the map graph).
* ``dismount:<x>:<y>`` is offered while swimming: one option per piece of land the swimmer can
step onto
  (smallest landing tile as its id), with the exits on that land. Standing on the water, every
  ordinary
  option (edges, doors, people) is also planned through the dismount; these two are the explicit
  ones.

The answer to the game's "Would you like to SURF?" belongs to the dialog family (FIELD_MOVE, YES
legal);
the macro ends when the question is on the screen.
"""

from __future__ import annotations

from typing import Any

from pokegent.actions.field import nav as N
from pokegent.actions.field import targets as T
from pokegent.actions.field.surf import rules
from pokegent.actions.field.surf import targets as ST
from pokegent.games.gen3.firered.maps.map_schema import MapState
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.goals.models import Objective
from pokegent.goals.validate import ResolvedObjective
from pokegent.navigation.routing import MapGraph
from pokegent.navigation.serving import serves_goal
from pokegent.options.models import Legality, Option, Risk
from pokegent.options.render import register_fact_renderer, render_facts

START_SURF = "field.start_surf"
DISMOUNT = "field.dismount"
PC_PROMPT = "field.interaction_started"
PC_DISMOUNTED = "field.dismounted"
VERB_START = "START_SURF"
VERB_DISMOUNT = "DISMOUNT_SURF"

_COMPASS = {"N": "north", "S": "south", "W": "west", "E": "east"}
MAX_LISTED = 8


# -- facts rendering (the model sees the description, not the facts)
# --------------------------------


def _r_surf(f: dict[str, Any]) -> list[str]:
    kind = f.get("surf")
    if kind is None:
        return []
    out: list[str] = []
    if kind == "start":
        missing = f.get("missing")
        if isinstance(missing, (list, tuple)) and missing:
            out.append("needs " + " and ".join(str(m) for m in missing))
        elif f.get("capability") == "ready":
            out.append("a Pokemon in your party can Surf")
        tiles = f.get("water_tiles")
        if tiles:
            out.append(f"the water has {tiles} tiles")
        encounters = f.get("encounters")
        if isinstance(encounters, (list, tuple)):
            names = [
                str(row.get("name", "unknown")) for row in encounters[:8] if isinstance(row, dict)
            ]
            if names:
                suffix = (
                    f" +{len(encounters) - len(names)} more" if len(encounters) > len(names) else ""
                )
                out.append("wild encounters: " + ", ".join(names) + suffix)
        shore = f.get("shore")
        if shore:
            out.append(f"you start from the shore facing {shore['facing']}")
        leads = f.get("leads_to")
        if leads:
            out.append("from the water you can reach " + ", ".join(leads))
        elif leads == []:
            out.append("from the water no new exit of this area is reachable")
        out.append("the game asks a question first; YES starts the Surf")
    elif kind == "dismount":
        n = f.get("landings")
        if n:
            out.append(f"steps ashore on one of {n} shore tile{'s' if n != 1 else ''}")
        tiles = f.get("region_tiles")
        if tiles:
            out.append(f"the land there has {tiles} tiles")
        leads = f.get("leads_to")
        if leads:
            out.append("from there you can reach " + ", ".join(leads))
        elif leads == []:
            out.append("no exit of this area is reachable from that land")
    return out


register_fact_renderer("field_surf", _r_surf, replace=True)


def _names(exits) -> list[str]:
    seen: list[str] = []
    for e in exits:
        if e.dest_name not in seen:
            seen.append(e.dest_name)
    return seen[:MAX_LISTED]


def _serves(
    exits, here, objective, resolved, state, graph, *, tile_hit: bool = False, caps=()
) -> bool | None:
    goal = resolved or objective
    if goal is None:
        return None
    if tile_hit:
        return True
    verdicts = [
        serves_goal({"target": e.facts(here)}, goal, state, graph=graph, caps=caps) for e in exits
    ]
    if any(v is True for v in verdicts):
        return True
    return False if verdicts and all(v is False for v in verdicts) else None


# -- entry point
# ------------------------------------------------------------------------------------


def surf_options(
    snapshot: StateSnapshot,
    state: MapState,
    nav: N.Navigator,
    search: N.Search,
    *,
    ledger,
    signature: str,
    objective: Objective | None,
    resolved: ResolvedObjective | None,
    graph: MapGraph | None,
    tiles: tuple[tuple[int, int], ...] = (),
    caps=(),
) -> list[Option]:
    """The Surf options of this frame (empty when the map has no water the player can use)."""
    p = state.player
    assert p is not None
    if p.biking or p.underwater:
        return []
    if p.surfing:
        return _dismount_options(
            state, nav, search, ledger, signature, objective, resolved, graph, caps
        )
    return _start_options(
        snapshot, state, nav, search, ledger, signature, objective, resolved, graph, tiles, caps
    )


# -- start
# ------------------------------------------------------------------------------------------


def _start_options(
    snapshot, state, nav, search, ledger, signature, objective, resolved, graph, tiles, caps=()
) -> list[Option]:
    ident = state.identity
    map_key = (ident.group, ident.number)
    bodies = ST.water_bodies(state, nav)
    if not bodies:
        return []
    cap = rules.surf_capability(snapshot)
    exits = ST.exit_targets(state, nav)
    options: list[Option] = []
    unlocked: dict | None = (
        None  # the relaxation search is only built once, and only if a body has a shore
    )
    for body in bodies:
        edges = ST.shore_edges(state, body, search)
        if not edges:
            continue
        if unlocked is None:
            unlocked = ST.unlocked_by_body(state, bodies, exits, nav, search)
        found = unlocked.get(body.canon, [])
        target = T.Target(
            body.option_id,
            "surf",
            "talk",
            "Surf across the water",
            tuple(T.Approach(e.stand, e.face) for e in edges),
            at=body.canon,
            info={"x": body.canon[0], "y": body.canon[1]},
        )
        plan = T.plan_target(nav, search, target)
        if plan is None:
            continue
        shore = plan.approach
        missing = []
        if not cap.known:
            missing.append("a readable party")
        else:
            if not cap.carrier_species:
                missing.append("a Pokemon that knows Surf")
            if cap.badge not in (snapshot.player.badges if snapshot.player else ()):
                missing.append("the Soul Badge")
        tile_hit = any(_tile_only_via(state, body, t) for t in tiles) if tiles else False
        facts: dict[str, Any] = {
            "surf": "start",
            "target": {
                "kind": "surf",
                "map": list(map_key),
                "x": body.canon[0],
                "y": body.canon[1],
            },
            "needs": {"move": cap.name, "badge": cap.badge_name},
            "carrier": list(cap.carrier_species),
            "capability": "ready" if cap.available else "missing",
            "missing": missing,
            "water_tiles": len(body.tiles),
            "steps": plan.length,
            "shore": {"stand": list(shore.stand), "facing": _COMPASS[shore.face or "N"]},
            "leads_to": _names(found),
            "exits": [e.option_id for e in found][:12],
        }
        serves = _serves(
            found, map_key, objective, resolved, state, graph, tile_hit=tile_hit, caps=caps
        )
        if serves is not None:
            facts["serves_goal"] = serves
        description = f"Surf across the water. {render_facts(facts)}."[:600]
        if not cap.available:
            reason = "capability_unknown" if not cap.known else "missing_capability"
            options.append(
                _diagnostic(
                    body.option_id, "Surf across the water", description, facts, reason, VERB_START
                )
            )
            continue
        key = ledger.key(body.option_id, map_key, signature)
        if ledger.hidden(key):
            facts["history_hint"] = (
                f"history: tried Surf {ledger.failures(key)} time(s) without a verified completion"
            )
            description = f"Surf across the water. {render_facts(facts)}."[:600]
        options.append(
            Option(
                id=body.option_id,
                label="Surf across the water",
                description=description,
                legality=Legality.LEGAL,
                facts=facts,
                executor_ref=START_SURF,
                executor_args={"x": body.canon[0], "y": body.canon[1]},
                risk=Risk.NORMAL,
                postcondition_ref=PC_PROMPT,
                verb=VERB_START,
                expected_postcondition="the game asks 'Would you like to SURF?' (the dialog family answers it)",  # noqa: E501
            )
        )
    return options


def _tile_only_via(state: MapState, body: ST.WaterBody, tile: tuple[int, int]) -> bool:
    """The objective tile is reachable only by crossing this body (relaxation search)."""
    relaxed = N.Navigator(state, N.Constraints(water=True))
    rsearch = relaxed.search()
    foot = N.Navigator(state)
    fsearch = foot.search()
    target = T.tile_target(state, *tile)
    if T.plan_target(foot, fsearch, target) is not None:
        return False
    plan = T.plan_target(relaxed, rsearch, target)
    if plan is None:
        return False
    mount = next((m for m in plan.moves if m.kind == "surf_start"), None)
    return mount is not None and mount.dst in body.tiles


# -- dismount
# ---------------------------------------------------------------------------------------


def _dismount_options(
    state, nav, search, ledger, signature, objective, resolved, graph, caps=()
) -> list[Option]:
    ident = state.identity
    map_key = (ident.group, ident.number)
    options: list[Option] = []
    for group in ST.landing_groups(state, nav, search):
        target = T.Target(
            group.option_id,
            "landing",
            "none",
            "Step ashore",
            tuple(T.Approach(tile) for tile in group.landings),
            at=group.canon,
            info={"x": group.canon[0], "y": group.canon[1]},
        )
        plan = T.plan_target(nav, search, target)
        if plan is None:
            continue
        found = list(group.exits)
        facts: dict[str, Any] = {
            "surf": "dismount",
            "target": {
                "kind": "landing",
                "map": list(map_key),
                "x": group.canon[0],
                "y": group.canon[1],
            },
            "landings": len(group.landings),
            "landing_tiles": [list(tile) for tile in group.landings],
            "region_tiles": group.region_tiles,
            "steps": plan.length,
            "leads_to": _names(found),
            "exits": [e.option_id for e in found][:12],
        }
        serves = _serves(found, map_key, objective, resolved, state, graph, caps=caps)
        if serves is not None:
            facts["serves_goal"] = serves
        key = ledger.key(group.option_id, map_key, signature)
        description = f"Step ashore. {render_facts(facts)}."[:600]
        if ledger.hidden(key):
            facts["history_hint"] = (
                f"history: tried stepping ashore {ledger.failures(key)} time(s) without a verified completion"  # noqa: E501
            )
            description = f"Step ashore. {render_facts(facts)}."[:600]
        options.append(
            Option(
                id=group.option_id,
                label="Step ashore",
                description=description,
                legality=Legality.LEGAL,
                facts=facts,
                executor_ref=DISMOUNT,
                executor_args={"x": group.canon[0], "y": group.canon[1]},
                risk=Risk.NORMAL,
                postcondition_ref=PC_DISMOUNTED,
                verb=VERB_DISMOUNT,
                expected_postcondition="the player stands on land and no longer surfs",
            )
        )
    return options


def _diagnostic(option_id, label, description, facts, reason, verb) -> Option:
    facts = {**facts, "reason": reason}
    return Option(
        id=option_id,
        label=label,
        description=f"{description.rstrip('.')}. Not possible now: {reason.replace('_', ' ')}."[
            :600
        ],
        legality=Legality.UNAVAILABLE,
        facts=facts,
        verb=verb,
    )
