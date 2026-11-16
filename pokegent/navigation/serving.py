"""``serves_goal``: does this option move the active objective forward? (a fact, never a choice)

Options of the field family carry ``facts["target"]`` (kind, the map they are on, the thing they
lead to). The helper compares that with the objective:

* ``reach``: the option is the named warp / tile of the current map, or it leads to the target map,
  or (with a :class:`MapGraph`) it leads toward it by the technical route;
* ``talk`` / ``fight``: the option is that person on that map (or leads toward the map);
* ``heal``: a person with the nurse's graphics on the current map; when the objective names a
  map, travel options are judged against that explicit map like any other route;
* ``hold`` or no objective: ``None`` (nothing is served; the decider uses the goal-less facts).

Returns ``True`` / ``False`` when it can tell and ``None`` when it cannot (no route graph and the
option does not lead to the target directly). Callers show ``True`` only; ``False`` is a fact for
the diagnostics.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pokegent.games.gen3.firered.maps.map_schema import MapState
from pokegent.goals.models import Objective
from pokegent.goals.validate import ResolvedObjective, resolve_map
from pokegent.navigation.routing import MapGraph


def serves_goal(
    option: Any,
    objective: Objective | ResolvedObjective | None,
    map_state: MapState | None = None,
    *,
    graph: MapGraph | None = None,
    caps=(),
) -> bool | None:
    """``option`` is an ``Option`` or its ``facts`` mapping."""
    if objective is None:
        return None
    resolved = objective if isinstance(objective, ResolvedObjective) else None
    obj = resolved.objective if resolved else objective
    assert isinstance(obj, Objective)
    if resolved is not None and resolved.navigation_reason is not None:
        return False
    if obj.kind == "hold":
        return None
    facts: Mapping[str, Any] = option if isinstance(option, Mapping) else option.facts
    indexed_gift_species = "gift_species" in facts
    gift_species = {
        species for species in facts.get("gift_species", ()) if isinstance(species, int)
    }
    if not indexed_gift_species and isinstance(facts.get("species"), int):
        gift_species.add(facts["species"])
    if (
        obj.kind == "collect"
        and obj.target.species is not None
        and facts.get("gift_kind") == "pokemon"
        and obj.target.species in gift_species
    ):
        return True
    # Some local interactions prove their effect directly from the ROM script.  Match those
    # declared effects to the objective's durable success probes before considering travel: a
    # same-map objective may be completed by an interaction rather than by another map edge.
    set_flags = set(facts.get("set_flags", ()))
    gift_flag = facts.get("gift_flag")
    if isinstance(gift_flag, int):
        set_flags.add(gift_flag)
    for probe in obj.success_probes:
        if probe.kind == "flag" and probe.expected and probe.flag in set_flags:
            return True
        if probe.kind == "var" and any(
            isinstance(row, (tuple, list))
            and len(row) == 2
            and row[0] == probe.var
            and row[1] == probe.value
            for row in facts.get("set_vars", ())
        ):
            return True
        if (
            probe.kind == "party_species"
            and facts.get("gift_kind") == "pokemon"
            and probe.species in gift_species
        ):
            return True
        if (
            probe.kind == "dex_owned"
            and facts.get("gift_kind") == "pokemon"
            and probe.species in gift_species
            and (indexed_gift_species or facts.get("gift_received") is False)
        ):
            return True
        if (
            probe.kind == "item"
            and facts.get("gift_kind") == "item"
            and facts.get("item_id") == probe.item
        ):
            return True
    target = facts.get("target")
    if not isinstance(target, Mapping):
        return None
    here = tuple(target["map"]) if "map" in target else _map_key(map_state)
    goal_map = _goal_map(obj, resolved)
    kind = target.get("kind")
    dest = tuple(target["dest"]) if target.get("dest") else None

    # A defeated fight target is no longer an actionable NPC.  Once its RAM trainer flag is
    # set, continue routing to an unmet map-arrival probe (for example, the next Elite Four
    # room) instead of repeatedly offering the defeated person.
    defeated_fight = (
        obj.kind == "fight"
        and kind == "npc"
        and target.get("local_id") == obj.target.npc
        and facts.get("trainer_defeated") is True
    )
    if defeated_fight:
        probe_maps = [
            resolve_map(probe.map)
            for probe in obj.success_probes
            if probe.kind == "map_arrival"
        ]
        remaining_probe_map = next(
            (probe_map for probe_map in probe_maps if probe_map is not None and probe_map != here),
            None,
        )
        if remaining_probe_map is None:
            return False
        if kind == "npc":
            return False
        goal_map = remaining_probe_map
    elif obj.kind == "fight" and kind != "npc" and here == goal_map:
        # Movement options on the fight's map should be judged against the next map-arrival
        # probe.  The exact NPC option remains the direct fight target and wins when available.
        remaining_probe_map = next(
            (
                resolve_map(probe.map)
                for probe in obj.success_probes
                if probe.kind == "map_arrival" and resolve_map(probe.map) != here
            ),
            None,
        )
        if remaining_probe_map is not None:
            goal_map = remaining_probe_map

    if obj.kind == "heal":
        healed = _heal(facts, target)
        # a heal that names its Pokemon Center is a trip there while the player stands elsewhere:
        # route it like a reach (live 2026-10-09: 'heal in Viridian' from Victory Road 3F offered
        # the trainer next door as an option and the run fought it with a weak team)
        if healed is not None or goal_map is None or here == goal_map:
            return healed
    if obj.kind == "train":
        if here == goal_map:
            return kind == "grass"
        if dest is not None:
            if dest == goal_map:
                return True
            if graph is not None and here is not None:
                return graph.leads_toward(here, dest, goal_map)
        return False
    if goal_map is None:
        return None
    # Use the ROM-resolved target when available.  The public objective accepts item names,
    # while mart inventories carry numeric ROM item ids; comparing against the original target
    # string makes a valid clerk look unrelated on maps that expose the native shop flow.
    t = resolved.target if resolved is not None else obj.target
    if here == goal_map:
        if obj.kind == "reach":
            if t.warp is not None:
                return kind == "warp" and target.get("index") == t.warp
            if t.x is not None:
                return kind == "tile" and (target.get("x"), target.get("y")) == (t.x, t.y)
            return False  # already on the target map
        if obj.kind in ("talk", "fight"):
            return kind in ("npc",) and target.get("local_id") == t.npc
        if obj.kind == "collect":
            if t.species is not None:
                return (
                    kind == "npc"
                    and facts.get("gift_kind") == "pokemon"
                    and t.species in gift_species
                )
            return (
                kind == "item" or kind == "npc" and facts.get("gift_kind") == "item"
            ) and facts.get("item_id") == t.item
        if obj.kind == "shop":
            return kind == "npc" and (
                (facts.get("gift_kind") == "item" and facts.get("item_id") == t.item)
                or any(row.get("item_id") == t.item for row in facts.get("mart_inventory", ()))
            )
        if obj.kind == "dex":
            return kind == "grass" and (
                t.species is None or t.species in facts.get("encounter_species", ())
            )
        return None
    if kind == "npc" and obj.kind in ("reach", "collect", "shop", "heal", "dex"):
        # On the way to a map a person is not a route step (pre-fork hop filter). Found live:
        # "talk to the man, 0 steps away" won 100+ decisions on Victory Road.
        return False
    if dest is not None:
        if dest == goal_map:
            return True
        if graph is not None and here is not None:
            source_region = facts.get("source_region")
            destinations = facts.get("destination_regions")
            if graph.regions is not None and source_region is not None and destinations:
                return any(
                    graph.region_leads_toward(
                        here, int(source_region), dest, goal_map, caps=caps, dest_region=int(region)
                    )
                    for region in destinations
                )
            return graph.leads_toward(here, dest, goal_map)
    return None


def _heal(facts, target) -> bool | None:
    if target.get("kind") == "npc" and facts.get("role") == "nurse":
        return True
    return None


def _goal_map(obj: Objective, resolved: ResolvedObjective | None) -> tuple[int, int] | None:
    if resolved is not None:
        target = resolved.navigation_target or resolved.target
        if target.map_key is not None:
            return target.map_key
    if obj.target.map:
        return resolve_map(obj.target.map)
    return None


def _map_key(state: MapState | None) -> tuple[int, int] | None:
    if state is None:
        return None
    return (state.identity.group, state.identity.number)
