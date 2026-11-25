"""Option builder of the field family: semantic, intent-level options for the overworld.

The model never chooses a button. In ``field.free`` it chooses a *target* of the current map
(a door, an exit, a person, a sign, an item, a spot the objective names) and the executor walks
there. Every option states its consequences as facts and in the English description (the model sees
the label and the description, nothing else):

====================  =========================================================================
fact                  meaning
====================  =========================================================================
``steps``             length of the path in moves (a ledge jump is one move)
``grass_tiles``       tall-grass tiles on the path (wild encounter risk)
``encounter_tiles``   ROM-marked land/water encounter tiles on the path
``ledges``            one-way ledge jumps on the path (no way back that way)
``script_tiles``      tiles on the path that start a script and take the controls
``blocked_by_npc``    reachable only if a walking person moves out of the way (CONDITIONAL)
``serves_goal``       the active objective (``True`` only when the builder can tell)
``already_visited``   the destination map was visited before (this run's builds)
``destination``       English name of the map a warp / exit leads to
``target``            machine facts (kind, map, ids) for ``serves_goal`` and the logs
====================  =========================================================================

Legality: LEGAL (a path exists now), CONDITIONAL (a path exists once a walking NPC moves),
UNAVAILABLE (no path: ``no_path`` / ``blocked_by_static_npc``), UNSUPPORTED
(only with a field move, surfing or forced movement: the typed reason says which). Unreachable
targets are diagnostics: never offered, always explained.

Order: by path length, then by id. The fingerprint is the decision-relevant state (map, the
player's tile, facing, the chooseable ids and their arguments, the objective): the positions of
walking NPCs are NOT part of it, so a rebuilt frame only differs when something that matters did.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from pokegent.actions._exec.contracts import ActionContract, ExpectedState
from pokegent.actions.field import bike, fieldmenu, fly
from pokegent.actions.field import evidence as E
from pokegent.actions.field import nav as N
from pokegent.actions.field import targets as T
from pokegent.actions.field.attempts import AttemptLedger, capability_signature
from pokegent.actions.field.boulder_options import BoulderPlans, boulder_options
from pokegent.actions.field.capabilities import field_capability
from pokegent.actions.field.surf.options import surf_options
from pokegent.actions.field.surf.rules import surf_capability
from pokegent.actions.field.surf.targets import movement_tiles
from pokegent.actions.items import ItemCatalog, ItemOptionBuilder
from pokegent.games.gen3 import mechanics_ids as I
from pokegent.games.gen3.firered.maps import map_constants as K
from pokegent.games.gen3.firered.maps.map_boulders import puzzles
from pokegent.games.gen3.firered.maps.map_motion import predict_step
from pokegent.games.gen3.firered.maps.map_schema import MapState, snapshot_map_identity
from pokegent.games.gen3.firered.maps.obstacles import kind_for_role
from pokegent.games.gen3.firered.maps.world import WorldAdapter, WorldView
from pokegent.games.gen3.firered.rom.constants import FLAG_BADGE01_GET, NUM_BADGES
from pokegent.goals.models import Objective, probe_leaves
from pokegent.goals.tracker import evaluate_probe
from pokegent.goals.validate import ResolvedObjective
from pokegent.navigation.objectives import resolve_collect_navigation
from pokegent.navigation.routing import MapGraph
from pokegent.navigation.serving import serves_goal
from pokegent.navigation.trigger_matching import first_planned_gate_at
from pokegent.options.models import (
    OBJECTIVE_RELATION_NOT_SERVING,
    Legality,
    Option,
    Risk,
    fingerprint,
)
from pokegent.options.registry import BuildContext, FrameDraft
from pokegent.options.render import render_facts, route_blockers

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pokegent.navigation.adapter import NavigationAdapter

# executor and postcondition references (the only names options may carry)
ENTER_WARP = "field.enter_warp"
TRAVEL_EDGE = "field.travel_edge"
USE_WATERFALL = "field.use_waterfall"
TALK_TO = "field.talk_to"
INTERACT_WITH = "field.interact_with"
USE_PC = "field.use_pc"
BEGIN_TEACH_TM = "field.begin_teach_tm"
FINISH_TEACH_TM = "field.finish_teach_tm"
CANCEL_TEACH_TM = "field.cancel_teach_tm"
PC_TEACH_TARGET_READY = "field.teach_target_ready"
PC_TEACH_CANCELLED = "field.teach_cancelled"
PICK_UP = "field.pick_up"
WALK_TO = "field.walk_to"
FACE = "field.face"
WAIT_SCRIPT = "field.wait_script"
WALK_IN_GRASS = "field.walk_in_grass"
TOGGLE_BIKE = "field.toggle_bike"
TEACH_HM = "field.teach_hm"
USE_FLASH = "field.use_flash"
FLY_MAP_CANCEL = "field.fly_map_cancel"
FLY_MAP_RESUME = "field.fly_map_resume"
PC_FLY_MAP_CANCEL = "field.fly_map_cancelled"


def _usable_escape_moves(map_state, party) -> tuple[tuple[int, str, int], ...]:
    """Return RAM-proven Dig/Teleport carriers usable on the current ROM map."""
    identity = snapshot_map_identity(map_state)
    if not identity.identity_verified:
        return ()
    allowed: dict[int, str] = {}
    if identity.allow_escaping:
        allowed[I.MOVE_DIG] = "Dig"
    if identity.map_type in fly.FLY_MAP_TYPES:
        allowed[I.MOVE_TELEPORT] = "Teleport"
    usable: list[tuple[int, str, int]] = []
    for mon in party:
        for move in mon.moves:
            name = allowed.get(move.move_id)
            if name is not None and int(move.pp) > 0:
                usable.append((move.move_id, name, mon.slot))
    return tuple(usable)


def _mark_teach_options_serving_objective(
    options: list[Option], objective: Objective | ResolvedObjective | None, snapshot: Any
) -> list[Option]:
    """Mark legal teaching options for an unmet ``party_knows_move`` probe."""
    if objective is None:
        return options
    resolved = objective if isinstance(objective, ResolvedObjective) else None
    obj = resolved.objective if resolved is not None else objective
    probes = resolved.probes if resolved is not None else obj.success_probes
    move_ids = {
        probe.move_id
        for probe in probe_leaves(probes)
        if probe.kind == "party_knows_move" and probe.move_id is not None
    }
    known_moves = {
        move.move_id
        for mon in getattr(snapshot, "party", ())
        for move in getattr(mon, "moves", ())
        if move.move_id
    }
    unmet = move_ids - known_moves
    if not unmet:
        return options
    marked: list[Option] = []
    for option in options:
        if (
            option.chooseable
            and "teach_move" in option.facts.get("effect_classes", ())
            and option.facts.get("move_id") in unmet
        ):
            marked.append(
                option.model_copy(update={"facts": {**dict(option.facts), "serves_goal": True}})
            )
        else:
            marked.append(option)
    return marked


def _objective_order_key(option: Option, objective: object | None) -> tuple[int, int, str]:
    """Put the planner's one immediate step first for an explicit objective."""
    return (
        0 if objective is not None and option.facts.get("route_step") is True else 1,
        *_order_key(option),
    )


def _shop_clerk_local_id(state: MapState, view: WorldView, item_id: int | None) -> int | None:
    """Select the ROM clerk whose decoded mart contains the requested item."""
    if item_id is not None:
        for local_id, offers in view.marts:
            if any(offer.item_id == item_id for offer in offers):
                return local_id
    return next(
        (obj.local_id for obj in state.objects if obj.graphics_id == K.OBJ_EVENT_GFX_CLERK),
        None,
    )


def _shop_target_is_local(resolved: ResolvedObjective | None, map_key: tuple[int, int]) -> bool:
    return resolved is not None and resolved.target.map_key == map_key


def _failure_counts_toward_demotion(failure: Mapping[str, Any]) -> bool:
    """Return whether a failed attempt is evidence that the option is genuinely stuck.

    An interrupted field macro has handed control to another RAM-observed scene (most commonly
    a battle or dialog). That is a resumable handoff, not a failed route, regardless of the
    option's target kind. Only an actual non-completion with no interruption evidence should
    consume the recent-failure demotion budget.
    """
    if failure.get("progressed") is True:
        return False
    if str(failure.get("status", "")).lower() == "interrupted":
        return False
    if failure.get("interrupted_scene"):
        return False
    error_code = str(failure.get("error_code", ""))
    if error_code in {"E_INTERRUPTED_SCENE", "ErrorCode.E_INTERRUPTED_SCENE"}:
        return False
    reason = str(failure.get("failure_reason", "")).lower()
    return "interrupted" not in reason


USE_FLY = "field.use_fly"
USE_BIKE = "field.use_bike"
PC_TOOL = "field.tool_done"
PC_FLY = "field.fly_done"
USE_DIG_TELEPORT = "field.use_dig_teleport"
PC_DIG_TELEPORT = "field.dig_teleport_done"
PC_WARP = "field.warp_done"
PC_EDGE = "field.edge_done"
PC_INTERACTION = "field.interaction_started"
PC_WATERFALL = "field.waterfall_done"
PC_AT_TILE = "field.at_tile"
PC_FACING = "field.facing"
PC_SCRIPT_DONE = "field.script_done"
SWAP_PARTY = "field.swap_party"

#: spec-6.B vocabulary tokens of the options (the catalog row ``field.free`` lists the raw MOVE_*
#: verbs; the interface uses semantic options, so these belong to sibling rows of section B)
VERBS = {
    "warp": "ENTER_WARP",
    "edge": "TRAVEL_TO_CONNECTED_MAP",
    "npc": "TALK_TO",
    "sign": "INTERACT_WITH",
    "item": "PICK_UP",
    "tile": "NAVIGATE_TO",
    "face": "FACE",
    "wait": "WAIT_FOR_SCRIPT",
    "obstacle": "INTERACT_WITH",
    "pc": "USE_PC",
    "grass": "WALK_IN_GRASS",
}

_COMPASS = {"N": "north", "S": "south", "W": "west", "E": "east"}

ObjectiveSource = Callable[[str | None], "Objective | ResolvedObjective | None"]


def store_source(store) -> ObjectiveSource:
    """Adapt an ``ObjectiveStore`` to the builder: the resolved active objective whose id
    matches."""

    def source(goal_id: str | None):
        if goal_id is None:
            return None
        current = store.current()
        if current is None or current.objective_id != goal_id:
            return None
        return store.resolved()

    return source


@dataclass
class VisitedMaps:
    """Maps this run has stood in (the builder notes the current one at every build)."""

    maps: set[tuple[int, int]] = field(default_factory=set)

    def note(self, key: tuple[int, int]) -> None:
        self.maps.add(key)

    def __contains__(self, key: object) -> bool:
        return key in self.maps


class FieldBuilder:
    def __init__(
        self,
        ledger: AttemptLedger | None = None,
        *,
        objectives: ObjectiveSource | None = None,
        graph: MapGraph | None = None,
        visited: VisitedMaps | None = None,
        plans: BoulderPlans | None = None,
        tables=None,
        item_catalog: ItemCatalog | None = None,
        planning_caps: frozenset[str] | None = None,
        field_menu: bool | None = None,
        navigation: NavigationAdapter | None = None,
        world: WorldAdapter | None = None,
    ) -> None:
        env_menu = os.environ.get("POKEGENT_FIELD_MENU")
        self.field_menu = (
            field_menu
            if field_menu is not None
            else env_menu is None or env_menu.lower() in {"1", "true", "yes", "on"}
        )
        self.plans = plans if plans is not None else BoulderPlans()
        self.ledger = ledger if ledger is not None else AttemptLedger()
        self.objectives = objectives
        self.graph = graph
        self.visited = visited if visited is not None else VisitedMaps()
        self.planning_caps = planning_caps
        self.tables = tables
        self.item_options = ItemOptionBuilder(item_catalog) if item_catalog is not None else None
        self.navigation = navigation
        self.world = world

    # -- entry -------------------------------------------------------------------------------------

    def build(self, ctx: BuildContext) -> FrameDraft:
        scene_id = ctx.scene.scene_id
        if scene_id == "menu.fly_map":
            return self._fly_map(ctx)
        if scene_id == E.TRAINER_VISION:
            return self._trainer_vision(ctx)
        if scene_id not in (E.FREE, E.DARKNESS):
            return FrameDraft(options=(), state_fingerprint=fingerprint(scene_id))
        return self._free(ctx)

    def _fly_map(self, ctx: BuildContext) -> FrameDraft:
        """Recover a Fly map left open across restart, using only typed RAM evidence."""
        snap = ctx.snapshot
        ui = snap.ui
        current_map = getattr(snap, "map", None)
        current_identity = getattr(current_map, "identity", None)
        current_map_key = (
            (current_identity.group, current_identity.number)
            if current_identity is not None
            else None
        )
        info = getattr(ui, "fly_map", None) if ui is not None else None
        objective, resolved = self._objective(ctx)
        target = getattr(resolved, "target", None)
        target_key = getattr(target, "map_key", None)
        if target_key is None and objective is not None:
            target_key = getattr(getattr(objective, "target", None), "map_key", None)
        can_select = bool(
            info is not None
            and info.input_ready is True
            and info.region in (0, None)
            and isinstance(info.cursor_x, int)
            and isinstance(info.cursor_y, int)
        )
        options: list[Option] = []
        if can_select:
            for destination in fly.visited(snap):
                if current_map_key is not None and destination.map_key == current_map_key:
                    continue
                serves_goal = tuple(destination.map_key) == tuple(target_key or ())
                options.append(
                    Option(
                        id=f"fly:{destination.name}",
                        label=f"Fly to {T.pretty_map_name(destination.name)}",
                        description=(
                            f"Select the visited {T.pretty_map_name(destination.name)} destination "
                            "from the open Fly map; arrival is verified by map RAM."
                        ),
                        legality=Legality.LEGAL,
                        facts={
                            "destination": destination.name,
                            "visited": True,
                            "serves_goal": serves_goal,
                            "route_step": serves_goal,
                            "next_route_step": serves_goal,
                            "target": {"kind": "fly", "dest": list(destination.map_key)},
                        },
                        executor_ref=FLY_MAP_RESUME,
                        executor_args={
                            "mapsec": destination.mapsec,
                            "destination_group": destination.map_key[0],
                            "destination_number": destination.map_key[1],
                            "cell_x": destination.cell[0],
                            "cell_y": destination.cell[1],
                        },
                        risk=Risk.NORMAL,
                        postcondition_ref=PC_FLY,
                        verb="FLY_TO",
                        expected_postcondition=(
                            f"Arrive at {T.pretty_map_name(destination.name)}."
                        ),
                    )
                )
        options.append(
            Option(
                id="cancel",
                label="Cancel",
                description="Close the open Fly map and return to field control; RAM verifies field.free.",  # noqa: E501
                legality=Legality.LEGAL,
                facts={"effect": "return to field", "target": {"kind": "cancel"}, "escape": True},
                executor_ref=FLY_MAP_CANCEL,
                risk=Risk.LOW,
                postcondition_ref=PC_FLY_MAP_CANCEL,
                verb="CANCEL",
                expected_postcondition="Return to field.free.",
            )
        )
        return FrameDraft(
            options=tuple(options),
            state_fingerprint=fingerprint(
                "menu.fly_map",
                cursor=(getattr(info, "cursor_x", None), getattr(info, "cursor_y", None)),
                visited=[d.map_key for d in fly.visited(snap)],
                can_select=can_select,
                objective=target_key,
            ),
            context={"situation": "The Fly region map was open when the run resumed."},
        )

    # -- field.trainer_vision: a deterministic wait ------------------------------------------------

    def _trainer_vision(self, ctx: BuildContext) -> FrameDraft:
        wait = Option(
            id="wait_script",
            label="Wait while the trainer approaches",
            description="A trainer has spotted you and the scene is playing out. Nothing to do.",
            legality=Legality.LEGAL,
            executor_ref=WAIT_SCRIPT,
            risk=Risk.LOW,
            postcondition_ref=PC_SCRIPT_DONE,
            verb=VERBS["wait"],
        )
        p = ctx.snapshot.player
        return FrameDraft(
            options=(wait,),
            state_fingerprint=fingerprint(
                ctx.scene.scene_id, map=[p.map_group, p.map_num] if p else None
            ),
            context={"situation": "A trainer saw you; the encounter is starting."},
        )

    # -- field.free --------------------------------------------------------------------------------

    def _free(self, ctx: BuildContext) -> FrameDraft:
        snap = ctx.snapshot
        state = snap.map
        scene_id = ctx.scene.scene_id
        if state is None or state.player is None:
            diag = self._diagnostic("map_unreadable", "The map could not be read",
                                    Legality.UNKNOWN, "map_unreadable")  # fmt: skip
            return FrameDraft((diag,), fingerprint(scene_id, diag="map_unreadable"))
        # The battle family owns Snorlax like any other wild battle.  Its first subsequent
        # field.free snapshot is therefore the safe point to confirm the post-battle script's
        # durable flag/object state before route planning resumes.
        self.ledger.settle_obstacle_checks(snap)
        p = state.player
        ident = state.identity
        map_key = (ident.group, ident.number)
        self.visited.note(map_key)
        if p.underwater:  # surfing is supported by surf/ (A4)
            diag = self._diagnostic(
                "mounted_underwater", "Moving while underwater is not supported",
                Legality.UNSUPPORTED, "mode_underwater",
            )  # fmt: skip
            return FrameDraft(
                (diag,), fingerprint(scene_id, map=list(map_key), diag="underwater"),
                context={"map": T.pretty_map_name(ident.name)},
            )  # fmt: skip

        objective, resolved = self._objective(ctx)
        resolved, collect_sources = resolve_collect_navigation(snap, resolved, self.world)
        nav = N.Navigator(state)
        search = nav.search()
        signature = capability_signature(snap)
        tiles = self._objective_tiles(resolved, map_key)
        if ctx.goal_id is None:
            tiles = tuple(
                dict.fromkeys((*tiles, *self._no_objective_local_tiles(ctx, state, nav)))
            )
        if self.navigation is not None:
            from pokegent.navigation.planner import OBJECTIVE_FRAME_PLAN_BUDGET

            navigation_plan = self.navigation.plan(
                snap, resolved, objective_budget=OBJECTIVE_FRAME_PLAN_BUDGET
            )
        else:
            navigation_plan = None
        # A reach/map-arrival objective whose target lies on ANOTHER map crosses the current
        # map on foot first.  ``_objective_tiles`` returns () for an off-map target, so in a
        # city/interior there would be NO walk option at all -- only border edges and building
        # warps -- and the agent would seam-ping instead of crossing the city toward the goal
        # (issue #56, live-0030: Cerulean -> Route 5).  Emit intra-map walk-forward tiles from
        # the plan's walk edges on the current map so the map can actually be crossed.
        # A same-map point behind a script switch is not locally reachable yet. Preserve
        # that target and expose only the existing walk prefix up to the first interaction.
        needs_toggle_approach = bool(
            tiles
            and navigation_plan is not None
            and navigation_plan.reachable
            and navigation_plan.target.map_key == map_key
            and next(
                (edge.kind for edge in navigation_plan.route
                 if edge.kind not in ("walk", "bike", "ledge", "forced")),
                None,
            ) == "toggle"
            and all(T.plan_target(nav, search, T.tile_target(state, x, y)) is None
                    for x, y in tiles)
        )
        if navigation_plan is not None and (not tiles or needs_toggle_approach):
            nav_tiles = self._in_map_forward_walk_tiles(
                navigation_plan, map_key, resolved
            )
            if nav_tiles:
                tiles = tuple(dict.fromkeys((*tiles, *nav_tiles)))
        objective_target = (
            None if resolved is None else resolved.navigation_target or resolved.target
        )
        route_map = (
            navigation_plan.target.map_key
            if navigation_plan is not None
            else objective_target.map_key
            if objective_target is not None
            else None
        )
        required_npc = (
            navigation_plan.target.local_id
            if navigation_plan is not None and navigation_plan.target.local_id is not None
            else objective_target.npc
            if objective_target is not None
            else None
        )
        world_view = self.world.view(snap) if self.world is not None else None
        shop_target_clerk = None
        if (
            world_view is not None
            and resolved is not None
            and resolved.objective.kind == "shop"
            and resolved.target.map_key == map_key
        ):
            shop_target_clerk = _shop_clerk_local_id(state, world_view, resolved.target.item)
        if (
            required_npc is None
            and resolved is not None
            and resolved.objective.kind == "shop"
            and resolved.target.map_key == map_key
        ):
            # Keep a ROM-declared clerk in the target set even when its live object slot is
            # outside the current camera window.  WorldAdapter then proves the mart from the
            # clerk script and the objective ranking can protect this direct interaction.
            required_npc = shop_target_clerk
        tiles = tuple(dict.fromkeys((*tiles, *movement_tiles(state, nav))))
        targets = T.enumerate_targets(
            state,
            nav,
            tiles=tiles,
            required_npc=(required_npc if route_map == map_key else None),
            exempt_required_npc_hidden=(
                objective is not None and objective.kind != "talk"
            ),
            training=(
                objective is not None
                and (
                    objective.kind == "train"
                    or (
                        resolved is not None
                        and objective.kind in {"collect", "dex"}
                        and map_key in resolved.navigation_map_keys
                        and "land"
                        in dict(resolved.navigation_map_methods).get(map_key, frozenset())
                    )
                )
                and (
                    resolved is None
                    or resolved.target.map_key is None
                    or resolved.target.map_key == map_key
                )
            ),
        )
        shop_objective_reason: str | None = None
        if (
            world_view is not None
            and resolved is not None
            and resolved.objective.kind == "shop"
            and resolved.target.map_key == map_key
        ):
            target_item = resolved.target.item
            offers = {
                offer.item_id: offer.name
                for _local_id, rows in world_view.marts
                for offer in rows
            }
            if target_item not in offers:
                stock = ", ".join(dict.fromkeys(offers.values())) or "none"
                item_name = str(resolved.objective.target.item)
                if self.world is not None:
                    try:
                        item_name = self.world.tables.item(target_item).name
                    except (IndexError, TypeError, ValueError):
                        pass
                shop_objective_reason = (
                    f"unmet: {item_name} is not sold on "
                    f"{ident.name} (stock: {stock})"
                )
        pokemon_gift_sources = (
            self.world.pokemon_gift_sources(snap, None, include_used=True)
            if self.world is not None
            and any(hasattr(source, "species") for source in collect_sources)
            else ()
        )
        relaxed = _Relaxed(state)

        options: list[Option] = []
        if self.field_menu and len(snap.party) > 1:
            for member in snap.party:
                if member.slot == 0:
                    continue
                options.append(
                    Option(
                        id=f"swap:{member.personality:08x}",
                        label=f"Swap party order with slot {member.slot + 1}",
                        description=(
                            f"Swap the party order of slot 1 and slot {member.slot + 1}; "
                            "the resulting order is verified from party RAM."
                        ),
                        legality=Legality.LEGAL,
                        facts={
                            "party_order": True,
                            "party_slot": member.slot,
                            "personality": member.personality,
                        },
                        executor_ref=SWAP_PARTY,
                        executor_args={"target_personality": member.personality},
                        postcondition_ref="start_party.expected",
                        action_contract=ActionContract(
                            cursor_key=f"party:{member.personality:08x}",
                            expected=(ExpectedState(kind="party_order_changed"),),
                        ),
                        verb="SWAP_WITH",
                        risk=Risk.LOW,
                    )
                )
        if (
            scene_id == E.DARKNESS
            and state.flash_level
            and field_capability(snap, "flash").available
        ):
            capability = field_capability(snap, "flash")
            carrier = next(
                mon
                for mon in snap.party
                if any(move.move_id == capability.move_id for move in mon.moves)
            )
            options.append(
                Option(
                    id="use_flash",
                    label="Use Flash to light the cave",
                    description=(
                        "Use Flash to illuminate this dark cave; walking remains available "
                        "without it."
                    ),
                    legality=Legality.LEGAL,
                    facts={
                        "field_action": "flash",
                        "move_id": capability.move_id,
                        "party_slot": carrier.slot,
                        "flash_level": state.flash_level,
                        "optional": True,
                    },
                    executor_ref=USE_FLASH,
                    executor_args={"party_slot": carrier.slot, "move_id": capability.move_id},
                    postcondition_ref="field.flash_done",
                    verb="USE_FLASH",
                )
            )
        for target in targets:
            options.append(self._option(ctx, state, target, nav, search, relaxed,
                                        signature, objective, resolved))  # fmt: skip
        options += self._coord_trigger_options(
            ctx, state, nav, search, signature, objective, resolved, navigation_plan
        )
        options += boulder_options(
            self.plans, snapshot=snap, state=state, options=options,
            objective=resolved or objective, graph=self.graph,
        )  # fmt: skip
        options += self._face_options(state)
        options += surf_options(snap, state, nav, search, ledger=self.ledger, signature=signature,
                                objective=objective, resolved=resolved, graph=self.graph, tiles=tiles,  # noqa: E501
                                caps=self._planning_caps(snap))  # fmt: skip
        options += self._tool_options(ctx, state, resolved, navigation_plan)
        for move_id, move_name, party_slot in _usable_escape_moves(state, snap.party):
            is_dig = move_id == I.MOVE_DIG
            options.append(
                Option(
                    id=f"move:{move_id}:heal",
                    label=(
                        "Use Dig to exit the current area"
                        if is_dig
                        else "Use Teleport to return to the last healing location"
                    ),
                    description=(
                        "Use Dig to leave this area."
                        if is_dig
                        else "Use Teleport to return to the last healing location."
                    ),
                    legality=Legality.LEGAL,
                    facts={
                        "field_action": "dig" if is_dig else "teleport",
                        "move_id": move_id,
                        "move_name": move_name,
                        "pp_available": True,
                        "outcome": (
                            "exits the current area"
                            if is_dig
                            else "returns to the last healing location"
                        ),
                    },
                    executor_ref=USE_DIG_TELEPORT,
                    executor_args={"party_slot": party_slot, "move_id": move_id},
                    postcondition_ref=PC_DIG_TELEPORT,
                    verb="USE_FIELD_MOVE",
                )
            )
        field_items = (
            self.item_options.build(snap, context="field") if self.item_options is not None else []
        )
        options += [
            option
            for option in field_items
            if option.chooseable and option.facts.get("field_action") is not None
        ]
        teach_options = [
            option
            for option in field_items
            if "teach_move" in option.facts.get("effect_classes", ())
        ]
        teach_options = _mark_teach_options_serving_objective(
            teach_options, resolved or objective, snap
        )
        options += teach_options
        if self.item_options is not None:
            item_options = [
                option
                for option in field_items
                if "teach_move" not in option.facts.get("effect_classes", ())
                and option.facts.get("field_action") is None
            ]
            if objective is not None:
                item_options = self._authorized_item_options(item_options, objective)
            else:
                item_options = [option for option in item_options if option.chooseable]
            options += item_options
        elif self._menu_allowed(resolved):  # legacy tables path
            if objective is not None:
                options += fieldmenu.item_options(
                    snap, allow_heal=objective.kind == "heal", tables=self.tables
                )
        options += bike.options(snap, state, resolved)
        # Mansion statues are field interactions, not an exclusive scene.  Import lazily because
        # the special module's executor uses field navigation helpers at module import time.
        from pokegent.actions.special.mansion import statues_on_map
        from pokegent.actions.special.switch_puzzle import MansionBuilder

        if statues_on_map(state):
            options += list(
                MansionBuilder().build(BuildContext(snapshot=snap, scene=ctx.scene)).options
            )
        options = self._dedupe_options(
            options
        )  # live-0072: two builders emitted 'bike_on' -> invalid frame
        lineage = {option.id: option for option in options}
        if self.navigation is not None:
            options, navigation_plan = self.navigation.annotate(
                options, snap, resolved, plan=navigation_plan
            )
        if world_view is not None:
            enriched = self._world_options(
                options,
                world_view,
                objective,
                resolved=resolved,
                snapshot=snap,
                pokemon_gift_sources=pokemon_gift_sources,
            )
            enriched = self._world_route_opportunity(enriched)
            options = enriched
            # World facts (story gates, gifts, trainers, healing, and PC gaps) are added
            # after navigation annotation. Re-run the generic composer over those enriched
            # legal options so the initial navigation pass cannot hide every alternative
            # before it receives its relevance facts.
        if objective is not None and objective.kind == "heal" and objective.target.map is None:
            options = [
                option.model_copy(
                    update={"facts": {**dict(option.facts), "objective_no_route": True}}
                )
                for option in options
            ]
        options = self._filter_closed_navigation_warps(options, snap, navigation_plan)
        chooseable = [o for o in options if o.chooseable]
        chooseable.sort(key=lambda option: _objective_order_key(option, objective))
        diagnostics = [o for o in options if not o.chooseable]
        ordered = tuple(chooseable) + tuple(diagnostics)

        context: dict[str, Any] = {
            "map": T.pretty_map_name(ident.name),
            "facing": _COMPASS.get(p.facing or "", "unknown"),
        }
        if collect_sources:
            context["collect_sources"] = [
                {
                    "map": source.map_name,
                    "object_id": source.object_id,
                    "kind": getattr(source, "kind", "npc"),
                    "status": source.status,
                    "spawned_by": getattr(source, "spawned_by", None),
                    **(
                        {
                            "species": source.species,
                            "level": source.level,
                            "gate_flags": list(source.gate_flags),
                            "gate_unset_flags": list(source.gate_unset_flags),
                            "gate_items": list(source.gate_items),
                            "gate_money": source.gate_money,
                            "gate_coins": source.gate_coins,
                            "gate_unknown": source.gate_unknown,
                            "gates": list(source.gates),
                        }
                        if hasattr(source, "species")
                        else {}
                    ),
                }
                for source in collect_sources
            ]
        if resolved is not None and resolved.navigation_reason is not None:
            context["blocked_route"] = resolved.navigation_reason
        if p.biking:
            context["mode"] = "biking: mounted; walking macros verify each tile"
        elif p.surfing:
            context["mode"] = "surfing: on the water, land is reached by stepping ashore"
        exits = [
            option
            for option in options
            if option.facts.get("target", {}).get("kind") in ("warp", "edge")
        ]
        if exits and all(option.legality is Legality.UNAVAILABLE for option in exits):
            context["sealed"] = True
            context["no_reachable_exit"] = True
        if objective is not None:
            context["objective"] = objective.summary()
        else:
            context["objective"] = "none set: choose by the facts of each option"
        if shop_objective_reason:
            context["objective_unmet_reason"] = shop_objective_reason.removeprefix(
                "unmet: "
            ).strip()
        if resolved is not None and tuple(resolved.target.map_key or ()) == map_key:
            unmet = [probe for probe in resolved.probes if not self._probe_satisfied(probe, snap)]
            if unmet:
                context["unmet_success_probes"] = [self._probe_text(probe) for probe in unmet]
        if world_view is not None:
            context["pokedex"] = {
                "seen": world_view.dex.seen_count,
                "caught": world_view.dex.caught_count,
                "unseen_species_here": [row.name for row in world_view.dex.new_species],
            }
            if world_view.dex.oak_next_count is not None:
                context["pokedex"]["oak_next_evaluation"] = {
                    "caught": world_view.dex.oak_next_count,
                    "reward": world_view.dex.oak_next_reward,
                }
            if world_view.party_need:
                context["team_needs"] = list(world_view.party_need)
            if not snap.party:
                context["early_game_guidance"] = (
                    "You have no Pokémon yet: pick a starter at Professor Oak's lab table."
                )
            elif world_view.party_facts:
                context["starter_in_party"] = [
                    {
                        "name": member.name,
                        "species": member.species,
                        "level": member.level,
                        "moves": list(member.moves),
                    }
                    for member in world_view.party_facts
                ]
        failed = [
            f"a field action ({r['error_code']})"
            for r in ctx.recent_failed
            if str(r.get("scene_id", "")).startswith("field.")
        ]
        if failed:
            context["recent_failures"] = failed[-3:]
        bounced = max((int(o.facts.get("edge_bounced", 0)) for o in options), default=0)
        if bounced:
            context["navigation_warning"] = (
                "A repeated edge is not getting closer to the objective; choose the route marked "
                "as serving the objective when available."
            )
        route_needs = sorted(
            {need for option in options for need in option.facts.get("route_needs", ())}
        )
        route_blocked_fact = next(
            (
                option.facts.get("route_blocked")
                for option in options
                if isinstance(option.facts.get("route_blocked"), str)
            ),
            None,
        )
        if route_blocked_fact:
            context["blocked_route"] = route_blocked_fact
        serving = [
            option.facts.get("destination")
            for option in options
            if option.facts.get("serves_goal") and option.facts.get("destination")
        ]
        if route_needs and not route_blocked_fact:
            context["blocked_route"] = (
                "Blocked by "
                + " and ".join(route_blockers(route_needs))
                + "; route via "
                + (
                    ", ".join(dict.fromkeys(serving))
                    if serving
                    else "an unavailable gated transition"
                )
                + "."
            )
        if navigation_plan is not None and self.navigation is not None:
            navigation_block = self.navigation.blocked_route(navigation_plan)
            if navigation_block is not None:
                context["blocked_route"] = navigation_block
        if navigation_plan is not None and navigation_plan.unmet and self.navigation is not None:
            context["blocked_route"] = self.navigation.blocked_route(navigation_plan) or (
                "blocked: needs " + " and ".join(route_blockers(navigation_plan.unmet))
            )
        if navigation_plan is not None and navigation_plan.cost is None and resolved is not None:
            context["blocked_route"] = "blocked: " + (
                "needs " + " and ".join(route_blockers(navigation_plan.unmet))
                if navigation_plan.unmet
                else (
                    navigation_plan.blocked_route_fact
                    or f"no verified route to {T.pretty_map_name(navigation_plan.target.map_key)}"
                )
            )
        if resolved is not None and resolved.navigation_reason is not None:
            context["blocked_route"] = resolved.navigation_reason
        if navigation_plan is not None and navigation_plan.reachable:
            first_nonwalk = next(
                (
                    edge
                    for edge in navigation_plan.route
                    if edge.kind not in {"walk", "bike", "forced", "ledge"}
                ),
                None,
            )
            if first_nonwalk is not None and not any(
                option.facts.get("route_step") is True for option in options
            ):
                at = first_nonwalk.target_dict().get("at")
                if first_nonwalk.kind == "gate" and isinstance(at, (tuple, list)):
                    map_state = (
                        self.navigation.graph.state(first_nonwalk.source.map_key)
                        if self.navigation is not None
                        else None
                    )
                    map_name = (
                        map_state.identity.name
                        if map_state is not None
                        else str(first_nonwalk.source.map_key)
                    )
                    context["blocked_route"] = (
                        f"next planned step: gate at ({at[0]},{at[1]}) on "
                        f"{T.pretty_map_name(map_name)}, "
                        "no option executes it"
                    )
            route_maps = []
            for edge in navigation_plan.route:
                state = (
                    self.navigation.graph.state(edge.destination.map_key)
                    if self.navigation
                    else None
                )
                name = T.pretty_map_name(state.identity.name) if state is not None else None
                if name and (not route_maps or route_maps[-1] != name):
                    route_maps.append(name)
            context["navigation"] = {
                "cost": navigation_plan.cost,
                "route": [self._navigation_step_label(edge) for edge in navigation_plan.route[:32]],
                "maps": route_maps[:6],
                "unmet": list(navigation_plan.unmet),
            }
        if objective is not None and objective.kind == "heal" and objective.target.map is None:
            no_route = "active heal objective has no route because no target map was given"
            prior = context.get("blocked_route")
            context["blocked_route"] = f"{prior}; {no_route}" if prior else no_route

        fp = fingerprint(
            scene_id,
            map=list(map_key),
            at=[p.avatar_x, p.avatar_y, p.elevation],
            facing=p.facing,
            options=[[o.id, dict(o.executor_args)] for o in chooseable],
            goal=[objective.objective_id, objective.revision] if objective else None,
            cap=signature,
            world=(
                [[item.source, item.ref, item.item_id, item.collected] for item in world_view.items]
                if world_view is not None
                else None
            ),
        )
        visible_ids = {option.id for option in chooseable}
        filtered = tuple(
            lineage[option.id].model_copy(
                update={
                    "facts": {
                        **dict(lineage[option.id].facts),
                        **dict(option.facts),
                        "focus_filtered": option.facts["selection_reason"],
                    }
                }
            )
            for option in options
            if (
                option.facts.get("selection_suppressed")
                or (not option.chooseable and option.facts.get("selection_reason"))
            )
            and option.id not in visible_ids
            and option.facts.get("selection_reason")
            and lineage[option.id].chooseable
        )
        # Navigation is foundational, not an ordinary alternative: every legal warp/edge must
        # remain directly selectable even when the objective focus filtered other interactions.
        # The later pipeline pass caps the non-transition alternatives; keeping transitions here
        # also preserves the direct builder/ROM corpus contract.
        hidden_transitions = tuple(
            option
            for option in filtered
            if isinstance(option.facts.get("target"), dict)
            and option.facts["target"].get("kind")
            in {"warp", "edge", "ladder", "elevator", "door", "stairs"}
            and option.facts.get("selection_reason") != "alternating_without_progress"
            and option.facts.get("selection_reason") != "closed"
        )
        if hidden_transitions:
            promoted_ids = {option.id for option in hidden_transitions}
            ordered = tuple((*ordered, *hidden_transitions))
            filtered = tuple(option for option in filtered if option.id not in promoted_ids)
        return FrameDraft(
            options=ordered,
            state_fingerprint=fp,
            context=context,
            deadline_ms=3000,
            all_options=filtered,
        )

    def _coord_trigger_options(
        self, ctx, state, nav, search, signature, objective, resolved, navigation_plan=None
    ) -> list[Option]:
        """Expose every armed, reachable ROM coordinate-event script as a walk option."""
        from pokegent.navigation.capabilities import from_snapshot

        route_graph = self.graph
        script_graph = getattr(self.navigation, "graph", None)
        if script_graph is None or not hasattr(script_graph, "_script_gate_at"):
            script_graph = route_graph
        if script_graph is None or not hasattr(script_graph, "_script_gate_at"):
            return []
        out: list[Option] = []
        map_key = (state.identity.group, state.identity.number)
        caps = from_snapshot(ctx.snapshot)
        player = getattr(state, "player", None)
        here_xy = (player.avatar_x, player.avatar_y) if player is not None else None
        for event in state.coord_events:
            if (event.x, event.y) == here_xy:
                # Coordinate events fire on step-on; choosing the current tile does nothing.
                continue
            planned_gate = bool(
                navigation_plan is not None
                and first_planned_gate_at(
                    navigation_plan.route,
                    (event.x, event.y),
                    map_key,
                    player_xy=here_xy,
                    surfing=bool(getattr(player, "surfing", False)),
                )
            )
            planned_gate_edge = (
                next(
                    (
                        edge
                        for edge in navigation_plan.route
                        if edge.kind == "gate"
                        and edge.target_dict().get("at") == (event.x, event.y)
                        and edge.source.map_key == map_key
                    ),
                    None,
                )
                if planned_gate and navigation_plan is not None
                else None
            )
            if (
                (not event.active and not planned_gate)
                or event.weather_only
                or event.script_ptr is None
            ):
                continue
            gate = script_graph._script_gate_at(state, event.x, event.y)
            if gate is not None and gate.triggerable and not planned_gate:
                # NavigationAdapter will rewrite the route's #221 gate option into the
                # executable trigger; do not publish a second option for the same tile.
                continue
            target = T.tile_target(state, event.x, event.y)
            plan = T.plan_target(nav, search, target)
            if plan is None:
                continue
            try:
                result = script_graph._script_interpretation(
                    event.script_ptr,
                    state,
                    trigger=(event.var_id, event.var_value),
                    player_xy=(event.x, event.y),
                )
                effects = script_graph._complete_effects(
                    "coord_trigger_options", result, state=state, site=(event.x, event.y)
                )
            except (AttributeError, KeyError, TypeError, ValueError):
                effects = ()
            proven_effects = tuple(
                effect
                for effect in effects
                if all(
                    (requirement := script_graph._requirement_from_guard(guard, caps)) is not None
                    and requirement.met
                    for guard in effect.guards
                )
            )
            script_name = self._script_name(script_graph, event.script_ptr)
            writes = tuple(
                (effect.value("var"), effect.value("value"))
                for effect in proven_effects
                if effect.kind == "setvar"
                and isinstance(effect.value("var"), int)
                and isinstance(effect.value("value"), int)
            )
            facts = {
                "target": {"kind": "tile", "map": list(map_key), "x": event.x, "y": event.y},
                "trigger_var": event.var_id,
                "trigger_value": event.var_value,
                "script_name": script_name,
                "starts_scene": not planned_gate,
                "set_vars": [list(row) for row in writes],
                "steps": plan.length,
                "grass_tiles": plan.grass_tiles,
                "encounter_tiles": plan.encounter_tiles,
                "ledges": plan.ledges,
                "script_tiles": plan.script_tiles,
            }
            # A badge guard is a normal, passable graph edge once its ROM-derived flag is
            # present, but its coordinate event still appears as a separate walk option.  Tie
            # that executable option to the graph edge it crosses so it remains the objective's
            # route step (and not a goal-less trigger) without a per-map exception.
            if planned_gate:
                facts.update(
                    {
                        "route_relation": "next route step",
                        "route_step": True,
                        "next_route_step": True,
                        "on_route": True,
                        "serves_goal": True,
                    }
                )
            trainer_battle = any(effect.kind == "trainerbattle" for effect in effects)
            declinable = trainer_battle and script_graph._coordinate_battle_is_declinable(effects)
            gate_requirement = (
                planned_gate_edge.target_dict().get("requirement")
                if planned_gate_edge is not None
                else None
            )
            badge_text = gate_requirement or "the required requirement"
            badge_requirement = False
            if isinstance(gate_requirement, str) and gate_requirement.startswith("flag:"):
                try:
                    flag = int(gate_requirement.split(":", 1)[1])
                    badge_flags = range(FLAG_BADGE01_GET, FLAG_BADGE01_GET + NUM_BADGES)
                    if flag in badge_flags:
                        badge_text = f"badge {flag - FLAG_BADGE01_GET + 1}"
                        badge_requirement = True
                    else:
                        badge_text = f"flag 0x{flag:X} set"
                except ValueError:
                    pass
            facts.update(
                {
                    "starts_trainer_battle": trainer_battle,
                    "forced_battle": trainer_battle,
                    "declinable": declinable,
                    "scene_kind": "trainer_battle" if trainer_battle else "scripted_scene",
                }
            )
            serves = serves_goal(facts, resolved or objective, state, graph=route_graph)
            if serves is not None and not planned_gate:
                facts["serves_goal"] = serves
                if serves:
                    facts["satisfies_objective"] = True
            out.append(
                Option(
                    id=f"trigger:{event.x}:{event.y}",
                    label=f"Walk onto story trigger at ({event.x}, {event.y})",
                    description=(
                        (f"Walk past the badge check; passes with {badge_text}.")
                        if planned_gate and badge_requirement
                        else (
                            f"Walk past the requirement at ({event.x}, {event.y}); passes with "
                            f"{badge_text}."
                        )
                        if planned_gate
                        else (
                            f"Walk onto ({event.x}, {event.y}); ROM script {script_name} "
                            "starts a scene. "
                            f"It is armed by var 0x{event.var_id:X} == {event.var_value}."
                        )
                    ),
                    legality=Legality.LEGAL,
                    facts=facts,
                    executor_ref=WALK_TO,
                    executor_args={"x": event.x, "y": event.y},
                    postcondition_ref=PC_AT_TILE,
                    verb="NAVIGATE_TO",
                    risk=Risk.HIGH if trainer_battle else Risk.NORMAL,
                )
            )
        return out

    @staticmethod
    def _script_name(graph, pointer: int) -> str:
        profile = getattr(graph, "_profile", None)
        if profile is not None:
            for name, entry in profile.symbols.items():
                if (entry.address & ~1) == (pointer & ~1):
                    return name
        return f"script_{pointer:#010x}"

    @staticmethod
    def _probe_satisfied(probe, snapshot) -> bool:
        return evaluate_probe(probe, snapshot)

    @staticmethod
    def _probe_text(probe) -> str:
        if probe.kind in ("any_of", "var_at_least"):
            return probe.label()
        if probe.kind == "var":
            return f"var 0x{probe.var:X} == {probe.value}"
        if probe.kind == "map_arrival":
            return f"map arrival at {probe.map_key}"
        return probe.kind

    def _filter_closed_navigation_warps(
        self, options: list[Option], snapshot, navigation_plan=None
    ) -> list[Option]:
        """Keep closed warps in external ``all`` output, but out of System One choices."""
        if self.navigation is None:
            return options
        # A closed-door verdict is meaningful only when the route planner has marked the
        # current navigation plan blocked.  Do not launch a separate planner search for every
        # visible warp on an otherwise reachable/current-map frame.
        if navigation_plan is None or not navigation_plan.unmet:
            return options
        out: list[Option] = []
        for option in options:
            target = option.facts.get("target")
            if (
                not option.chooseable
                or not isinstance(target, dict)
                or target.get("kind") != "warp"
                or not isinstance(target.get("index"), int)
                or not self.navigation.warp_is_closed(snapshot, target["index"])
            ):
                out.append(option)
                continue
            # This is the one visibility exception: ``warp_is_closed`` is a current RAM/ROM
            # proof, so the warp becomes a normal unavailable diagnostic.
            out.append(self._selection_suppressed(option, "closed", preserve_legal=False))
        return out

    @staticmethod
    def _selection_suppressed(
        option: Option, reason: str, *, preserve_legal: bool = False
    ) -> Option:
        return option.model_copy(
            update={
                **({} if preserve_legal else {"legality": Legality.UNAVAILABLE}),
                "facts": {
                    **dict(option.facts),
                    "selection_reason": reason,
                    "selection_suppressed": preserve_legal,
                    **(
                        {"objective_relation": OBJECTIVE_RELATION_NOT_SERVING}
                        if preserve_legal and reason in {"route_focus", "local_objective_focus"}
                        else {}
                    ),
                    "reason": dict(option.facts).get("reason") or f"filtered: {reason}",
                },
            }
        )

    @staticmethod
    def _present_only(options, candidates, reason: str):
        selected = {option.id for option in candidates}
        return [
            option
            if not option.chooseable or option.id in selected
            else FieldBuilder._selection_suppressed(option, reason, preserve_legal=True)
            for option in options
        ]

    def _navigation_step_label(self, edge) -> str:
        if edge.kind in ("walk", "bike", "forced", "ledge", "surf", "waterfall"):
            return f"walk {_COMPASS.get(edge.direction, edge.direction or 'forward').lower()}"
        if edge.kind == "dismount":
            return "step ashore"
        destination = (
            self.navigation.graph.state(edge.destination.map_key) if self.navigation else None
        )
        name = destination.identity.name if destination is not None else "the next area"
        labels = {
            "warp": "door to",
            "connection": "path to",
            "elevator": "elevator to",
            "fly": "fly to",
        }
        if edge.kind in labels:
            return f"{labels[edge.kind]} {name}"
        if edge.kind == "field_move":
            return f"use {str(dict(edge.executor_target).get('role', 'the obstacle')).replace('_', ' ')}"  # noqa: E501
        if edge.kind == "boulder_push":
            return "push the boulder"
        if edge.kind == "gate":
            return "open the gate"
        if edge.kind == "toggle":
            target = edge.target_dict()
            if target.get("local_id"):
                return f"interact with NPC {target['local_id']}"
            return f"interact with sign {target.get('event')}"
        return f"travel to {name}"

    @staticmethod
    def _world_options(
        options: list[Option],
        view: WorldView,
        objective: Objective | None,
        *,
        resolved: ResolvedObjective | None = None,
        snapshot=None,
        pokemon_gift_sources=(),
    ) -> list[Option]:
        """Project ROM identities and live availability onto today's proven field actions."""
        out: list[Option] = []
        shop_step_assigned = False
        for option in options:
            target = option.facts.get("target", {})
            facts = dict(option.facts)
            label = option.label
            additions: list[str] = []
            if target.get("kind") == "item":
                npc = view.npc(int(target.get("local_id", -1)))
                if npc is not None and npc.gift is not None and npc.gift.kind == "pokemon":
                    gift = npc.gift
                    facts.update(
                        {
                            "starter_choice": True,
                            "gift_kind": gift.kind,
                            "gift_flag": npc.gift_flag,
                            "gift_received": npc.gift_received,
                            "set_flags": list(npc.set_flags),
                            "species": gift.id,
                            "species_name": gift.name,
                            "types": list(gift.types),
                            "level": gift.level,
                            "strength_vs_brock": gift.strength_vs_brock,
                            "irreversible": True,
                            "effect_known": True,
                        }
                    )
                    serves = serves_goal(
                        facts,
                        objective,
                        snapshot.map if snapshot is not None else None,
                    )
                    if serves is not None:
                        facts["serves_goal"] = serves
                    if serves is True:
                        facts["satisfies_objective"] = True
                    type_text = "/".join(gift.types)
                    display_type_text = "/".join(kind.title() for kind in gift.types)
                    strength = gift.strength_vs_brock or "strength vs Brock is unknown"
                    label = f"Choose {gift.name} ({type_text})"
                    additions.append(
                        f"This is {gift.name}, a {type_text}-type Pokémon at Lv {gift.level}; "
                        f"it is {strength}. Choosing it is your irreversible starter choice."
                    )
                    out.append(
                        option.model_copy(
                            update={
                                "facts": facts,
                                "label": label,
                                "description": (option.description + " " + " ".join(additions))[
                                    :600
                                ],
                                "irreversible": True,
                                "semantic_prompt": (
                                    f"Choose {gift.name} ({display_type_text}) as your starter; "
                                    "this cannot be undone."
                                ),
                                "effect_description": (
                                    f"Choose {gift.name} ({display_type_text}) as your starter "
                                    "Pokémon; this cannot be undone."
                                ),
                                "risk": Risk.HIGH,
                            }
                        )
                    )
                    continue
                item = view.item_object(int(target.get("local_id", -1)))
                if item is not None:
                    facts.update(
                        {
                            "item_id": item.item_id,
                            "item": item.item_name,
                            "quantity": item.quantity,
                            "collection_flag": item.flag_id,
                            "collected": item.collected,
                            "owned": item.owned,
                            "new_item": item.new,
                            "useful_for": item.useful,
                            "hidden": item.source == "hidden",
                        }
                    )
                    label = (
                        f"Pick up hidden {item.item_name}"
                        if item.source == "hidden"
                        else f"Pick up {item.item_name}"
                    )
                    additions.append(
                        f"It is {item.item_name}; you have {item.owned}, and it is useful for {item.useful}."  # noqa: E501
                    )
                    if objective is not None and objective.kind == "collect":
                        facts["serves_goal"] = objective.target.item == item.item_id
            elif target.get("kind") == "grass":
                encounters = [row for row in view.dex.encounters if row.method == "land"]
                facts["encounter_species"] = [row.species for row in encounters]
                facts["unseen_species"] = [row.name for row in encounters if row.seen is False]
                facts["encounters"] = [
                    {
                        "species": row.species,
                        "name": row.name,
                        "chance": row.probability,
                        "levels": [row.min_level, row.max_level],
                        "seen": row.seen,
                        "caught": row.caught,
                        "owned": row.caught,
                        "new": None if row.caught is None else row.caught is False,
                        "unseen": row.seen is False,
                    }
                    for row in encounters
                ]
                if objective is not None and (
                    objective.kind == "dex"
                    or (objective.kind == "collect" and objective.target.species is not None)
                ):
                    facts["serves_goal"] = (
                        objective.target.species is None
                        or objective.target.species in facts["encounter_species"]
                    )
            elif target.get("kind") in ("surf", "landing"):
                method = "water" if target.get("kind") == "surf" else "water"
                encounters = [row for row in view.dex.encounters if row.method == method]
                facts["encounters"] = [
                    {
                        "species": row.species,
                        "name": row.name,
                        "chance": row.probability,
                        "levels": [row.min_level, row.max_level],
                        "seen": row.seen,
                        "caught": row.caught,
                        "owned": row.caught,
                        "new": None if row.caught is None else row.caught is False,
                        "unseen": row.seen is False,
                    }
                    for row in encounters
                ]
            elif target.get("kind") == "npc" and "local_id" in target:
                npc = view.npc(int(target["local_id"]))
                if npc is not None:
                    facts["role"] = npc.role
                    facts.update(
                        {
                            "trainer": npc.trainer_id is not None,
                            "trainer_id": npc.trainer_id,
                            "trainer_class": getattr(npc, "trainer_class", None),
                            "trainer_kind": getattr(npc, "trainer_kind", None),
                            "leader_badge": getattr(npc, "leader_badge", None),
                            "trainer_defeated": getattr(npc, "trainer_defeated", None),
                        }
                    )
                    target_map = tuple(target.get("map", ()))
                    object_gift_sources = tuple(
                        source
                        for source in pokemon_gift_sources
                        if source.map_key == target_map and source.object_id == npc.local_id
                    )
                    active_gift_sources = tuple(
                        source
                        for source in object_gift_sources
                        if source.status not in {"used", "locked"}
                    )
                    if object_gift_sources:
                        gift_species = tuple(sorted({row.species for row in active_gift_sources}))
                        facts.update(
                            {
                                "gift_kind": "pokemon",
                                "gift_species": list(gift_species),
                                "gift_received": (
                                    True
                                    if all(row.status == "used" for row in object_gift_sources)
                                    else False
                                    if any(row.status == "available" for row in active_gift_sources)
                                    else None
                                ),
                            }
                        )
                        if len(gift_species) == 1:
                            facts["species"] = gift_species[0]
                        if active_gift_sources:
                            facts.update(
                                {
                                    "npc_interaction": "gift",
                                    "gift_available": True,
                                    "exploration_opportunity": True,
                                }
                            )
                            names = ", ".join(
                                dict.fromkeys(row.species_name for row in active_gift_sources)
                            )
                            additions.append(f"Offers a Pokémon gift: {names}.")
                        elif all(row.status == "used" for row in object_gift_sources):
                            facts.update(
                                {
                                    "npc_interaction": "nothing_new",
                                    "no_new_information": True,
                                }
                            )
                            additions.append("Every Pokémon gift here was already received.")
                        else:
                            facts["npc_interaction"] = "gift_unavailable"
                        serves = serves_goal(
                            facts,
                            objective,
                            snapshot.map if snapshot is not None else None,
                        )
                        if serves is not None:
                            facts["serves_goal"] = serves
                        if serves is True:
                            facts["satisfies_objective"] = True
                    if npc.gift is not None and npc.gift.kind == "item":
                        facts.update(
                            {
                                "gift": {
                                    "kind": npc.gift.kind,
                                    "id": npc.gift.id,
                                    "name": npc.gift.name,
                                    "quantity": npc.gift.quantity,
                                    "level": npc.gift.level,
                                },
                                "gift_flag": npc.gift_flag,
                                "gift_received": npc.gift_received,
                                "set_flags": list(npc.set_flags),
                                "gift_kind": npc.gift.kind,
                                "item_id": npc.gift.id if npc.gift.kind == "item" else None,
                                "species": npc.gift.id if npc.gift.kind == "pokemon" else None,
                            }
                        )
                        if objective is not None and objective.kind in ("collect", "shop"):
                            if npc.gift_received is None:
                                facts["objective_unknown"] = True
                            else:
                                facts["serves_goal"] = npc.gift_received is False
                        if npc.gift_received is False:
                            facts.update(
                                {
                                    "npc_interaction": "gift",
                                    "gift_available": True,
                                    "exploration_opportunity": True,
                                }
                            )
                            additions.append(
                                f"Gives {npc.gift.name}; it has not been received yet."
                            )
                        elif npc.gift_received is True:
                            facts.update(
                                {
                                    "npc_interaction": "nothing_new",
                                    "no_new_information": True,
                                }
                            )
                            additions.append(f"The {npc.gift.name} gift was already received.")
                        else:
                            facts["npc_interaction"] = "gift_status_unknown"
                    if npc.trainer_id is not None:
                        facts.update(
                            {
                                "trainer_level_approx": npc.approximate_level,
                                "line_of_sight_risk": npc.line_of_sight,
                            }
                        )
                        trainer_unbattleable = npc.trainer_defeated or (
                            snapshot is not None
                            and snapshot.party
                            and not any(member.hp > 0 for member in snapshot.party)
                        )
                        if trainer_unbattleable:
                            facts["serves_goal"] = False
                            facts["trainer_unbattleable"] = True
                        if npc.trainer_defeated is True:
                            # A defeated trainer remains visible and selectable, but is a
                            # normal conversation: it must not carry trainer risk or the battle
                            # action contract that caused repeated rematch talks.
                            label = "Talk to a person"
                            facts["trainer"] = False
                            facts["npc_interaction"] = "conversation_only"
                        else:
                            facts["npc_interaction"] = "trainer"
                            if npc.trainer_kind == "gym_leader":
                                label = "Talk to a gym leader"
                            elif npc.trainer_kind == "elite_four":
                                label = "Talk to an Elite Four member"
                        state = (
                            "already defeated"
                            if npc.trainer_defeated is True
                            else "not defeated"
                            if npc.trainer_defeated is False
                            else "defeat status unknown"
                        )
                        level = (
                            f", around Lv {npc.approximate_level}"
                            if npc.approximate_level is not None
                            else ""
                        )
                        additions.append(f"Trainer is {state}{level}.")
                        if npc.trainer_defeated is not True:
                            facts["exploration_opportunity"] = True
                        if (
                            objective is not None
                            and not trainer_unbattleable
                            and npc.trainer_defeated is not True
                        ):
                            probes = objective.success_probes
                            if any(
                                getattr(probe, "kind", None) == "trainer_defeated"
                                and getattr(probe, "trainer_id", None) == npc.trainer_id
                                for probe in probe_leaves(probes)
                            ):
                                facts["serves_goal"] = True
                            if npc.trainer_kind == "gym_leader" and any(
                                getattr(probe, "kind", None) == "badge"
                                and getattr(probe, "badge", None) == facts["leader_badge"]
                                for probe in probe_leaves(probes)
                            ):
                                facts["serves_goal"] = True
                    if npc.gift is None and not object_gift_sources and npc.trainer_id is None:
                        facts.update(
                            {
                                "npc_interaction": "conversation_only",
                                "conversation_only": True,
                            }
                        )
                        additions.append("Conversation only; novelty is unknown.")
                    if npc.story_gate:
                        facts["story_gate"] = True
                        additions.append("This person's script checks or changes story progress.")
                    if npc.mart:
                        offers = next(
                            (offers for local_id, offers in view.marts if local_id == npc.local_id),
                            (),
                        )
                        facts["mart_inventory"] = [
                            {"item_id": row.item_id, "name": row.name, "price": row.price}
                            for row in offers
                        ]
                        additions.append(f"Sells {len(offers)} items.")
                        if (
                            objective is not None
                            and objective.kind == "shop"
                            and _shop_target_is_local(
                                resolved,
                                tuple(facts["target"].get("map", ())),
                            )
                        ):
                            target_item = (
                                resolved.target.item
                                if resolved is not None
                                else objective.target.item
                            )
                            serves_shop = any(
                                row.get("item_id") == target_item
                                or (
                                    isinstance(objective.target.item, str)
                                    and str(row.get("name", "")).casefold()
                                    == objective.target.item.casefold()
                                )
                                for row in facts["mart_inventory"]
                            )
                            facts["serves_goal"] = serves_shop
                            if serves_shop and not shop_step_assigned:
                                facts.update(
                                    {
                                        "route_relation": "next route step",
                                        "route_step": True,
                                        "next_route_step": True,
                                        "on_route": True,
                                    }
                                )
                                additions.append("Objective: next route step.")
                                shop_step_assigned = True
                    if npc.role == "nurse":
                        facts["heals_party"] = True
                        facts["heal_needed"] = bool(view.party_need)
                        facts["pc_access"] = "party healing"
                        if view.party_need:
                            additions.append(
                                "Healing is useful: " + "; ".join(view.party_need) + "."
                            )
                        if objective is not None and objective.kind == "heal":
                            facts["serves_goal"] = True
            description = option.description
            if facts.get("next_route_step") is True:
                description = description.replace(
                    "This does not serve the current objective.",
                    "Objective: next route step.",
                )
            if additions:
                description = (description + " " + " ".join(additions))[:600]
            out.append(
                option.model_copy(
                    update={"facts": facts, "label": label, "description": description}
                )
            )
        return out

    @staticmethod
    def _world_route_opportunity(options: list[Option]) -> list[Option]:
        available = [
            option
            for option in options
            if option.facts.get("target", {}).get("kind") == "item"
            and option.facts.get("collected") is False
            and option.chooseable
            and isinstance(option.facts.get("steps"), int)
        ]
        if not available:
            return options
        nearest = min(available, key=lambda option: (option.facts["steps"], option.id))
        if nearest.facts["steps"] > 20:
            return options
        opportunity = {
            "kind": "uncollected_item",
            "item": nearest.facts.get("item"),
            "distance_steps": nearest.facts["steps"],
            "option_id": nearest.id,
        }
        out = []
        annotated = 0
        for option in options:
            if option.facts.get("target", {}).get("kind") not in ("warp", "edge"):
                out.append(option)
                continue
            if annotated >= 3:
                out.append(option)
                continue
            facts = {**dict(option.facts), "nearby_opportunity": opportunity}
            out.append(option.model_copy(update={"facts": facts}))
            annotated += 1
        return out

    @staticmethod
    def _bike_options(
        snapshot, state: MapState, resolved: ResolvedObjective | None
    ) -> list[Option]:
        """Offer the key-item toggle only when it can advance a travel objective."""
        if not snapshot.usable or not any(
            i.item_id == 360 and i.quantity > 0 for p in snapshot.bag for i in p.items
        ):
            return []
        here = (state.identity.group, state.identity.number)
        goal = resolved.target.map_key if resolved is not None else None
        if state.player.biking:
            needed = (
                not state.identity.biking_allowed
                or bool(state.identity.cave)
                or (resolved is not None and goal == here)
            )
            if not needed:
                return []
            registered = snapshot.registered_item
            action = (
                "Press SELECT to dismount the registered Bicycle."
                if registered == bike.BICYCLE_ITEM_ID
                else "Use Bicycle from Key Items to walk on foot for this map or objective."
            )
            return [
                Option(
                    id="bike_off",
                    label="Dismount the Bicycle",
                    description=action,
                    legality=Legality.LEGAL if needed else Legality.UNAVAILABLE,
                    facts={
                        "mode": "biking",
                        "serves_goal": needed,
                        "item_id": bike.BICYCLE_ITEM_ID,
                        "registered_item": registered,
                        "uses_select": registered == bike.BICYCLE_ITEM_ID,
                    },
                    executor_ref=TOGGLE_BIKE if needed else None,
                    postcondition_ref="field.bike_off" if needed else None,
                    verb="DISMOUNT_BICYCLE",
                )
            ]
        if goal is None or goal == here or not state.identity.biking_allowed:
            return []
        registered = snapshot.registered_item
        if registered == bike.BICYCLE_ITEM_ID:
            action = "Press SELECT to ride the registered Bicycle."
        elif registered == 0:
            action = "Register the Bicycle through Key Items once, then press SELECT to ride it."
        else:
            action = "The registered item is in use; use Bicycle from Key Items as a safe fallback."
        return [
            Option(
                id="bike_on",
                label="Ride the Bicycle",
                description=action,
                legality=Legality.LEGAL,
                facts={
                    "mode": "on_foot",
                    "serves_goal": True,
                    "item_id": bike.BICYCLE_ITEM_ID,
                    "registered_item": registered,
                    "uses_select": registered == bike.BICYCLE_ITEM_ID,
                },
                executor_ref=TOGGLE_BIKE,
                postcondition_ref="field.bike_on",
                verb="RIDE_BICYCLE",
            )
        ]

    @staticmethod
    def _dedupe_options(options: list[Option]) -> list[Option]:
        """The first occurrence of each id wins. Two builders must never emit one option id: the
        frame fails validation and the run safe-pauses (live-0072: 'bike_on' from _tool_options
        and _bike_options once the Bicycle was in the bag)."""
        seen: dict[str, Any] = {}
        out: list[Option] = []
        for option in options:
            if option.id not in seen:
                seen[option.id] = option.facts.get("verb")
                out.append(option)
            else:
                logger.warning(
                    "dropped duplicate option %s (first verb=%s, later verb=%s; first wins)",
                    option.id,
                    seen[option.id],
                    option.facts.get("verb"),
                )
        return out

    # -- objective ---------------------------------------------------------------------------------

    def _objective(self, ctx: BuildContext) -> tuple[Objective | None, ResolvedObjective | None]:
        if self.objectives is None or ctx.goal_id is None:
            return None, None
        got = self.objectives(ctx.goal_id)
        if got is None:
            return None, None
        if isinstance(got, ResolvedObjective):
            return got.objective, got
        return got, None

    @staticmethod
    def _no_objective_local_tiles(
        ctx: BuildContext, state: MapState, nav: N.Navigator
    ) -> tuple[tuple[int, int], ...]:
        """Bounded ordinary adjacent land walks for a genuinely goal-less frame (issue #162).

        Only the immediate N,S,W,E neighbours the existing Navigator can walk to are selected;
        a warp, connection, script, ledge, forced-motion, water or unknown tile is never an
        ordinary walk.  The existing tile target and walk executor still prove the path.
        """
        p = state.player
        if (
            ctx.goal_id is not None
            or ctx.scene.busy
            or not ctx.snapshot.health.valid
            or not state.identity.identity_verified
            or p is None
            or p.surfing
            or p.underwater
            or not p.controllable_flag
            or p.forced
            or p.moving
        ):
            return ()
        out: list[tuple[int, int]] = []
        start = nav.start_node()
        for direction in N.DIRECTIONS:
            stepped = nav.step(start, direction)
            if stepped is None:
                continue
            _node, move = stepped
            if (
                move.kind != "walk"
                or move.warp_index is not None
                or move.connection is not None
                or move.script
            ):
                continue
            outside, _collision, _elevation, info = nav._tile(*move.dst)
            if (
                outside
                or info.kind == "unknown"
                or info.surfable
                or info.waterfall
                or info.warp_trigger is not None
                or info.ledge is not None
                or info.forced is not None
                or info.ice
            ):
                continue
            out.append(move.dst)
        return tuple(out[:4])

    @staticmethod
    def _objective_tiles(
        resolved: ResolvedObjective | None, map_key: tuple[int, int]
    ) -> tuple[tuple[int, int], ...]:
        if resolved is None or resolved.objective.kind == "hold":
            return ()
        t = resolved.target
        if t.map_key == map_key and t.x is not None and t.y is not None:
            return ((t.x, t.y),)
        return ()

    @staticmethod
    def _in_map_forward_walk_tiles(
        navigation_plan, map_key: tuple[int, int], resolved: ResolvedObjective | None
    ) -> tuple[tuple[int, int], ...]:
        """Intra-map walk-forward tiles on the CURRENT map toward a plan's goal.

        For a reach/map-arrival objective whose target lies on another map (so ``_objective_tiles``
        is ()), the plan must CROSS the current map on foot to reach its exit before the next
        map.  City/interior frames otherwise offer no walk option at all (only border edges and
        building warps), which is the seam-ping root cause of issue #56.  We collect the
        destination tiles of the plan's ``walk`` edges that stay on the current map and lead
        forward, so the builder can emit genuine walk options to cross it.
        """
        if navigation_plan is None or resolved is None or not navigation_plan.route:
            return ()
        route = navigation_plan.route
        out: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        for edge in route:
            src = edge.source
            if (src.map_group, src.map_number) != map_key:
                continue  # we only cross THIS map first
            if edge.kind not in ("walk", "bike", "ledge", "forced"):
                # The plan leaves the current map via its border connection / warp next; any
                # pure walk prefix on this map has already been collected.
                break
            dst = edge.destination
            tile = (dst.x, dst.y)
            if tile not in seen:
                seen.add(tile)
                out.append(tile)
            # The forward path crosses the map boundary on the next non-walk edge; the walk
            # tiles collected so far let the agent walk toward the exit.
            if len(out) >= 8:
                break
        # Only emit walks when they actually PROGRESS toward the goal map (they all originate on
        # map_key and the destination crossed out above).  Empty when the plan doesn't walk on
        # this map.
        return tuple(out)

    def _planning_caps(self, snapshot) -> frozenset[str]:
        override = getattr(self, "planning_caps", None)
        if override is not None:
            return override
        caps: set[str] = set()
        for obstacle, capability in (
            ("cut", "cut"),
            ("smash", "rock_smash"),
            ("strength", "strength"),
            ("snorlax", "flute"),
        ):
            if field_capability(snapshot, obstacle).available:
                caps.add(capability)
        if surf_capability(snapshot).available:
            caps.add("surf")
        if (
            snapshot.player is not None
            and fly.THUNDER_BADGE in snapshot.player.badges
            and any(mv.move_id == fly.FLY_MOVE_ID for mon in snapshot.party for mv in mon.moves)
        ):
            caps.add("fly")
        flags = snapshot.flags
        vars_table = snapshot.vars
        if flags is not None or vars_table is not None:
            for puzzle in puzzles(allow_empty=True).values():
                name = puzzle.name
                for switch in puzzle.switches:
                    if vars_table is not None and vars_table.get(switch.var) == 100:
                        caps.add(f"boulder_plan:{name}:switch:{switch.tile[0]}:{switch.tile[1]}")
                if flags is not None:
                    for boulder in puzzle.boulders:
                        if boulder.hide_flag is None or not flags.is_set(boulder.hide_flag):
                            continue
                        for hole in puzzle.holes:
                            caps.add(f"boulder_plan:{name}:hole:{hole.tile[0]}:{hole.tile[1]}")
        return frozenset(caps)

    def _tool_options(
        self,
        ctx: BuildContext,
        state: MapState,
        resolved: ResolvedObjective | None,
        navigation_plan=None,
    ) -> list[Option]:
        """Navigation tools are offered only when the objective makes them useful.

        This is intentionally after ordinary exits are built: tools never replace legal
        navigation facts, and an idle frame never gets a distance-zero toggle.
        """
        snap = ctx.snapshot
        from pokegent.navigation.capabilities import from_snapshot as navigation_capabilities

        out: list[Option] = []
        current_map_key = (
            (state.identity.group, state.identity.number) if hasattr(state, "identity") else None
        )
        semantic = navigation_plan.next_step if navigation_plan is not None else None
        if navigation_plan is not None:
            for edge in navigation_plan.route:
                if edge.kind not in ("walk", "bike", "ledge", "forced"):
                    semantic = edge
                    break
        if (
            semantic is not None
            and semantic.kind == "waterfall"
            and semantic.direction == "N"
            and navigation_capabilities(snap).has("surf")
            and navigation_capabilities(snap).has("waterfall")
        ):
            direction = semantic.direction
            if direction == "N":
                source = semantic.source
                destination = semantic.destination
                out.append(
                    Option(
                        id=semantic.stable_key,
                        label="Climb the waterfall",
                        description=(
                            f"Use Waterfall while surfing from ({source.x},{source.y}) "
                            f"toward the planned waterfall tile ({destination.x},{destination.y}); "
                            "arrival at the top is verified from map RAM."
                        ),
                        legality=Legality.LEGAL,
                        facts={
                            "target": {
                                "kind": "waterfall",
                                "map": [source.map_group, source.map_number],
                                "source": [source.x, source.y],
                                "destination": [destination.x, destination.y],
                                "direction": direction,
                            },
                            "field_move": "waterfall",
                            "move_id": I.MOVE_WATERFALL,
                            "next_route_step": True,
                            "on_route": True,
                            "serves_goal": True,
                            "route_cost": navigation_plan.cost,
                        },
                        executor_ref=USE_WATERFALL,
                        executor_args={
                            "source_x": source.x,
                            "source_y": source.y,
                            "direction": direction,
                            "destination_x": destination.x,
                            "destination_y": destination.y,
                        },
                        postcondition_ref=PC_WATERFALL,
                        verb="USE_WATERFALL",
                    )
                )
        dest = None
        cost = None
        tail = 0
        route_selected = False
        if semantic is not None and semantic.kind == "fly":
            dest_key = semantic.target_dict().get("dest")
            dest = fly.DESTINATIONS_BY_KEY.get(tuple(dest_key)) if dest_key is not None else None
            if dest is not None and navigation_plan is not None:
                cost = navigation_plan.cost
                tail = cost - semantic.cost if cost is not None else 0
            route_selected = dest is not None
        elif resolved is not None and resolved.target.map_key is not None:
            # An explicit Fly destination is itself an executable objective, even when the
            # shortest graph plan currently prefers walking or has no verified route tail.
            requested = fly.DESTINATIONS_BY_KEY.get(resolved.target.map_key)
            if (
                requested is not None
                and (current_map_key is None or requested.map_key != current_map_key)
                and fly.capability(snap, state)["available"]
                and (not hasattr(state, "identity") or fly.can_launch(state))
                and any(row.map_key == requested.map_key for row in fly.visited(snap))
            ):
                dest = requested
                cost = fly.FLY_OVERHEAD
                route_selected = True
        # Lightweight builder unit doubles predate the typed map identity; live snapshots must
        # pass the same launch-cell proof used by the executor.
        if (
            dest is not None
            and (current_map_key is None or dest.map_key != current_map_key)
            and (not hasattr(state, "identity") or fly.can_launch(state))
        ):
            out.append(
                Option(
                    id=f"fly:{dest.name}",
                    label=f"Fly to {T.pretty_map_name(dest.name)}",
                    description=(
                        f"Fly to the visited {T.pretty_map_name(dest.name)} heal location. "
                        f"Estimated cost {cost}; walking costs {cost + fly.FLY_MIN_SAVING} or more."
                    ),
                    legality=Legality.LEGAL,
                    facts={
                        "destination": dest.name,
                        "landing": dest.landing,
                        "fly_cost": cost,
                        "after_cost": tail,
                        "serves_goal": True,
                        "visited": True,
                        "target": {"kind": "fly", "dest": list(dest.map_key)},
                    },
                    executor_ref=USE_FLY,
                    executor_args={
                        "mapsec": dest.mapsec,
                        "destination_group": dest.map_key[0],
                        "destination_number": dest.map_key[1],
                        "party_slot": fly.carrier_slots(snap)[0],
                    },
                    risk=Risk.NORMAL,
                    postcondition_ref=PC_FLY,
                    verb="FLY_TO",
                    expected_postcondition=f"Arrive at {T.pretty_map_name(dest.name)}.",
                )
            )
        if not route_selected and hasattr(state, "identity") and fly.can_launch(state):
            # No planner edge or explicit objective selected Fly, but the scene is otherwise
            # ready.  Expose a small, stable set of visited destinations as general travel
            # choices.  They are never marked as serving the objective because no route proves
            # that; the executor still proves the live cursor before input, and an indoor or
            # unverified map still refuses above.
            for destination in fly.offered_destinations(snap, state):
                out.append(
                    Option(
                        id=f"fly:{destination.name}",
                        label=f"Fly to {T.pretty_map_name(destination.name)}",
                        description=(
                            f"Fly to the visited {T.pretty_map_name(destination.name)} heal "
                            "location. This is a general travel choice; the current plan does "
                            "not prove it serves the objective or saves walking."
                        ),
                        legality=Legality.LEGAL,
                        facts={
                            "destination": destination.name,
                            "landing": destination.landing,
                            "fly_cost": fly.FLY_OVERHEAD,
                            "serves_goal": False,
                            "visited": True,
                            "target": {
                                "kind": "fly",
                                "dest": list(destination.map_key),
                            },
                        },
                        executor_ref=USE_FLY,
                        executor_args={
                            "mapsec": destination.mapsec,
                            "destination_group": destination.map_key[0],
                            "destination_number": destination.map_key[1],
                            "party_slot": fly.carrier_slots(snap)[0],
                        },
                        risk=Risk.NORMAL,
                        postcondition_ref=PC_FLY,
                        verb="FLY_TO",
                        expected_postcondition=(
                            f"Arrive at {T.pretty_map_name(destination.name)}."
                        ),
                    )
                )
        return out

    # -- one target -> one option ------------------------------------------------------------------

    def _menu_allowed(self, resolved: ResolvedObjective | None) -> bool:
        """Non-teaching party-menu items require the process switch or a team-prep goal."""
        if self.field_menu:
            return True
        if resolved is None:
            return False
        return resolved.objective.kind == "team-prep"

    @classmethod
    def _authorized_item_options(cls, options: list[Option], objective: Objective) -> list[Option]:
        """Expose every legal field item, with goal-serving items ranked first.

        Goal relevance is a ranking signal, not a policy gate. Game-rule diagnostics and the
        explicit objective ``forbid`` policy remain the only reasons an item is unavailable.
        """
        serving: list[Option] = []
        other: list[Option] = []
        for option in options:
            if not option.chooseable:
                continue
            facts = dict(option.facts)
            if objective.kind == "heal" and set(facts.get("effect_classes", ())) & {
                "heal_hp",
                "revive",
                "cure_status",
            }:
                facts["serves_goal"] = True
                option = option.model_copy(update={"facts": facts})
            if option.facts.get("serves_goal") is True:
                serving.append(option)
            else:
                other.append(option)
        return serving + other

    def _option(
        self,
        ctx: BuildContext,
        state: MapState,
        target: T.Target,
        nav: N.Navigator,
        search: N.Search,
        relaxed: _Relaxed,
        signature: str,
        objective: Objective | None,
        resolved: ResolvedObjective | None,
    ) -> Option:
        ident = state.identity
        map_key = (ident.group, ident.number)
        facts = self._target_facts(state, target)
        if (
            target.kind == "warp"
            and self.navigation is not None
            and target.info.get("dest") is not None
        ):
            scripted_arrival = self.navigation.graph.scripted_warp_arrival(
                tuple(target.info["dest"]),
                int(target.info["dest_warp"]),
                tuple(ctx.snapshot.vars.nonzero) if ctx.snapshot.vars is not None else (),
            )
            if scripted_arrival is not None:
                facts["scripted_arrival"] = list(scripted_arrival)
        label = target.label
        if target.kind == "sign" and self.world is not None:
            is_sign_text = self.world.bg_event_is_sign_text(target.info.get("script_ptr"))
            if is_sign_text is False:
                label = "Examine/use: scripted interaction"
        if target.kind == "sign" and objective is not None:
            sign_is_target = self._sign_is_target(target, map_key, resolved)
            if (
                self.ledger.sign_was_read(target.option_id, map_key, ctx.goal_id)
                and not sign_is_target
            ):
                facts.update(
                    {
                        "already_read": True,
                        "no_new_information": True,
                        "serves_goal": False,
                        "history_hint": "history: this sign was already read in this run",
                    }
                )
            if sign_is_target:
                facts["serves_goal"] = True
        if target.kind == "obstacle":
            obstacle_kind = kind_for_role(target.info["role"])
            capability_kind = obstacle_kind.capability_kind if obstacle_kind is not None else None
            capability = (
                field_capability(ctx.snapshot, capability_kind) if capability_kind else None
            )
            if capability is not None:
                requirement = (
                    {
                        "kind": "key_item",
                        "name": capability.name,
                        "item_id": capability.item_id,
                    }
                    if obstacle_kind is not None and obstacle_kind.requirement_kind == "key_item"
                    else {
                        "kind": "field_move",
                        "move": capability.name,
                        "badge": capability.badge_name,
                    }
                )
                facts.update(
                    {
                        "requirement": requirement,
                        "carrier": list(capability.carrier_species),
                        "capability_known": capability.known,
                        "capability": "ready" if capability.available else "missing",
                    }
                )
                if requirement["kind"] == "field_move":
                    facts["needs"] = {
                        "move": capability.name,
                        "badge": capability.badge_name,
                    }
                if not capability.known:
                    target = replace(target, unavailable="capability_unknown")
                elif not capability.available:
                    target = replace(target, unavailable="missing_capability")
                elif capability_kind == "strength" and state.strength_active is True:
                    # Found live (Victory Road 3F): 'Arm Strength ... you are already there' was the
                    # nearest option and the decider armed it again and again instead of pushing.
                    target = replace(target, unavailable="strength_already_active")
            if target.info["role"] == "cut_tree" and self.ledger.field_move_was_declined(
                target.option_id, map_key, ctx.goal_id
            ):
                facts.update(
                    {
                        "history_hint": "history: Cut was declined at this obstacle earlier in this run",  # noqa: E501
                    }
                )
            if target.info["role"] == "fossil":
                facts.update(
                    {
                        "obstacle": "fossil",
                        "unlocks": T.pretty_map_name(state.identity.name),
                        "irreversible": True,
                    }
                )
            else:
                facts.update(
                    {
                        "obstacle": target.info["role"],
                        "unlocks": T.pretty_map_name(state.identity.name),
                        "unblock_requirement": target.info.get("requirement_key"),
                        "completion": target.info.get("completion"),
                        "hide_flag": target.info.get("hide_flag"),
                    }
                )
                if target.info.get("irreversible"):
                    facts.update(
                        {
                            "irreversible": True,
                            "consequence": "wakes a battle",
                        }
                    )
        elif target.kind == "grass":
            patch = target.info["patch"]
            facts.update(
                {
                    "patch": target.option_id.split(":", 1)[1],
                    "patch_size": len(patch),
                    "bound_steps": 120,
                    "encounter_table": None,
                    "surface": target.info.get("surface", "grass"),
                }
            )
        if target.unsupported:
            return self._unavailable(target, facts, Legality.UNSUPPORTED, target.unsupported)
        if target.unavailable:
            return self._unavailable(target, facts, Legality.UNAVAILABLE, target.unavailable)
        key = self.ledger.key(target.option_id, map_key, signature)
        attempt = self.ledger.entries.get(key)
        recent_failures = sum(
            1
            for failure in ctx.recent_failed
            if failure.get("scene_id") == "field.free"
            and failure.get("option_id") == target.option_id
            and _failure_counts_toward_demotion(failure)
        )
        # Special-family field interactions do not share the field macro's ledger.  The runner's
        # bounded recent-failure window is the common demotion source for those options and for
        # field macros whose generator was stopped before its finally block recorded a ledger row.
        repeated_failure = self.ledger.hidden(key) or recent_failures >= self.ledger.limit
        if attempt is not None and attempt.total:
            facts["history_hint"] = (
                f"history: tried this target {attempt.total} time(s) without a verified completion"
            )
            if attempt.last_reason:
                facts["history_hint"] += f"; last result {attempt.last_reason}"
        if repeated_failure:
            failed_attempts = attempt.total if attempt is not None else recent_failures
            facts["history_hint"] = (
                f"history: tried this target {failed_attempts} time(s) without a verified completion"  # noqa: E501
            )
            facts["selection_reason"] = "repeated_failure"

        plan = T.plan_target(nav, search, target)
        # Keep the existing contract that a repeated target remains chooseable, but let route
        # annotation treat it as a demoted candidate.  This preserves manual recovery while
        # preventing a failed field macro from becoming the planner's next step forever.
        legality = Legality.LEGAL
        if plan is None:
            # Forced movement is executable in the current world, so test it before diagnostic
            # relaxations remove static NPCs or obstacles and describe a counterfactual route.
            forced_nav = N.Navigator(state, N.Constraints(forced=True))
            plan = T.plan_target(forced_nav, forced_nav.search(), target)
            if plan is not None:
                facts["forced_movement"] = True
                facts["forced_landing"] = plan.forced_landing
            else:
                reason, relaxed_plan = relaxed.explain(target)
                if reason == "blocked_by_npc" and relaxed_plan is not None:
                    plan = relaxed_plan
                    legality = Legality.CONDITIONAL
                    facts["blocked_by_npc"] = True
            if plan is None:
                kind = (
                    Legality.UNSUPPORTED
                    if reason in ("needs_field_move", "needs_surf", "forced_movement")
                    else Legality.UNAVAILABLE
                )
                return self._unavailable(target, facts, kind, reason)

        if target.at is not None and target.kind in ("npc", "item", "sign"):
            p = state.player
            facts["bearing"] = _bearing(target.at[0] - p.x, target.at[1] - p.y)
        if target.info.get("untested"):
            facts["trigger_untested"] = True
            legality = Legality.CONDITIONAL
        facts["steps"] = plan.length
        facts["grass_tiles"] = plan.grass_tiles
        facts["encounter_tiles"] = plan.encounter_tiles
        facts["ledges"] = plan.ledges
        facts["one_way"] = plan.ledges > 0
        facts["script_tiles"] = plan.script_tiles
        facts["forced_tiles"] = plan.forced_tiles
        if plan.forced_landing is not None:
            facts["forced_landing"] = plan.forced_landing
        dest = facts["target"].get("dest")
        if dest is not None:
            facts["already_visited"] = tuple(dest) in self.visited
        if (
            self.graph is not None
            and self.graph.regions is not None
            and target.kind in ("warp", "edge")
        ):
            rg = self.graph.regions
            player = state.player
            if target.kind == "warp":
                # The route starts where the avatar stands, not on the destination
                # warp tile (which may be outside the static region grid).
                source_region = rg.region_at(map_key, player.x, player.y)
                destinations = (
                    rg.warp_destinations(tuple(dest), int(target.info["dest_warp"])) if dest else ()
                )
            else:
                source_region = rg.region_at(
                    map_key, *(plan.moves[-1].src if plan.moves else (player.x, player.y))
                )
                destinations = (
                    rg.connection_destinations(map_key, source_region, tuple(dest), target.edge)
                    if dest and source_region is not None
                    else ()
                )
            if source_region is not None:
                facts["source_region"] = source_region
                facts["destination_regions"] = list(destinations)
                goal_map = resolved.target.map_key if resolved is not None else None
                if goal_map is not None and destinations:
                    route = self.graph.regions.route(
                        (map_key[0], map_key[1], source_region),
                        goal_map,
                        self._planning_caps(ctx.snapshot),
                    )
                    if route is None:
                        missing = self.graph.regions.missing_for(
                            (map_key[0], map_key[1], source_region),
                            goal_map,
                            self._planning_caps(ctx.snapshot),
                        )
                        if missing:
                            facts["route_needs"] = list(missing)
        serves = serves_goal(
            facts,
            resolved or objective,
            state,
            graph=self.graph,
            caps=self._planning_caps(ctx.snapshot),
        )
        if target.kind == "sign" and self._sign_is_target(target, map_key, resolved):
            serves = True
        if serves is not None:
            facts["serves_goal"] = serves
        if objective is None and resolved is None and target.kind == "tile":
            # A goal-less local walk serves nothing and has no route relation; say so rather
            # than leaving a gap the later composer could read as unproven relevance.
            facts["serves_goal"] = False
            facts.pop("route_step", None)
            facts.pop("on_route", None)
        if serves is True and target.kind not in {"warp", "edge", "fly", "grass"}:
            facts["satisfies_objective"] = True

        executor_ref, args = self._executor(target)
        irreversible = target.kind == "obstacle" and (
            target.info["role"] == "fossil" or bool(target.info.get("irreversible"))
        )
        training_target = bool(
            (resolved is not None and resolved.objective.kind == "train")
            or (
                resolved is not None
                and resolved.objective.kind in {"collect", "dex"}
                and map_key in resolved.navigation_map_keys
                and "land" in dict(resolved.navigation_map_methods).get(map_key, frozenset())
            )
            or (resolved is None and objective is not None and objective.kind == "train")
        )
        option = Option(
            id=target.option_id,
            label=label,
            description=_describe(target, facts, plan.length, label=label),
            legality=legality,
            facts=facts,
            executor_ref=executor_ref,
            executor_args=args,
            risk=Risk.HIGH if irreversible else Risk.NORMAL,
            postcondition_ref=self._postcondition(target),
            verb=target.info.get("verb", VERBS[target.kind]),
            expected_postcondition=_expected(target),
            action_contract=(
                ActionContract(
                    expected_outcome_scenes=(
                        "battle.entering",
                        "battle.animation",
                        "battle.message",
                        "battle.action",
                    )
                )
                if (
                    target.kind == "obstacle"
                    and target.info.get("completion") == "object_gone_or_battle"
                )
                else ActionContract(
                    expected=(
                        ExpectedState(
                            kind="map_changed",
                            map_group=target.info["dest"][0],
                            map_number=target.info["dest"][1],
                        ),
                    ),
                    expected_outcome_scenes=(
                        "battle.entering",
                        "battle.animation",
                        "battle.message",
                        "battle.action",
                    ),
                )
                if target.kind == "warp" and target.info.get("dest") is not None
                else (
                    ActionContract(expected_outcome_scenes=("battle.message",))
                    if target.kind == "grass" and training_target
                    else (
                        ActionContract(
                            expected_outcome_scenes=(
                                "dialog.printing",
                                "dialog.advance",
                                "dialog.end",
                                "battle.entering",
                                "battle.animation",
                                "battle.message",
                                "battle.action",
                            )
                        )
                        if (
                            target.kind == "npc"
                            and not facts.get("trainer_unbattleable")
                            and (
                                facts.get("npc_interaction") == "trainer"
                                or (
                                    resolved is not None
                                    and resolved.objective.kind == "fight"
                                    and target.info.get("local_id") == resolved.target.npc
                                )
                            )
                        )
                        else None
                    )
                )
            ),
            semantic_prompt=(
                "Use the Poke Flute and wake this Snorlax into a battle?"
                if target.info.get("completion") == "object_gone_or_battle"
                else "Choose this fossil?"
                if irreversible
                else None
            ),
            effect_description=(
                "The blocker is removed; waking Snorlax irreversibly starts a battle."
                if target.info.get("completion") == "object_gone_or_battle"
                else "The other fossil is gone."
                if irreversible
                else None
            ),
            irreversible=irreversible,
        )
        return option

    @staticmethod
    def _sign_is_target(
        target: T.Target, map_key: tuple[int, int], resolved: ResolvedObjective | None
    ) -> bool:
        """A coordinate reach objective may explicitly ask to stand at the sign."""
        if resolved is None or resolved.objective.kind != "reach":
            return False
        goal = resolved.target
        return goal.map_key == map_key and (goal.x, goal.y) == target.at

    # -- facts / ids -------------------------------------------------------------------------------

    def _target_facts(self, state: MapState, target: T.Target) -> dict[str, Any]:
        ident = state.identity
        here = [ident.group, ident.number]
        info = target.info
        t: dict[str, Any] = {"kind": target.kind, "map": here}
        facts: dict[str, Any] = {}
        if target.kind == "warp":
            t.update(
                index=info["warp"],
                dest=info["dest"],
                dest_warp=info["dest_warp"],
                dynamic=info["dest"] is None,
            )
            facts["destination"] = info["dest_name"]
            facts["trigger"] = info["trigger"]
        elif target.kind == "edge":
            t.update(direction=info["direction"], dest=info["dest"])
            facts["destination"] = info["dest_name"]
        elif target.kind in ("npc", "item"):
            obj = state.object_by_local_id(info["local_id"])
            t.update(local_id=info["local_id"])
            facts["trainer"] = bool(info["trainer"])
            if obj is not None and obj.graphics_id == K.OBJ_EVENT_GFX_NURSE:
                facts["role"] = "nurse"
            elif obj is not None and obj.graphics_id == K.OBJ_EVENT_GFX_CLERK:
                facts["role"] = "clerk"
        elif target.kind == "sign":
            t.update(bg=info["bg"])
        elif target.kind == "pc":
            t.update(x=info["x"], y=info["y"], role="pc")
        elif target.kind == "tile":
            t.update(x=info["x"], y=info["y"])
        elif target.kind == "obstacle":
            t.update(local_id=info["local_id"], role=info["role"], at=target.at)
        elif target.kind == "grass":
            t.update(patch=target.option_id.split(":", 1)[1])
        facts["target"] = t
        return facts

    @staticmethod
    def _executor(target: T.Target) -> tuple[str, dict[str, Any]]:
        info = target.info
        if target.kind == "warp":
            dest = info["dest"] or (-1, -1)
            return ENTER_WARP, {
                "warp": info["warp"],
                "dest_group": dest[0],
                "dest_number": dest[1],
                "dest_warp": info["dest_warp"],
            }
        if target.kind == "edge":
            return TRAVEL_EDGE, {
                "heading": info["direction"],
                "dest_group": info["dest"][0],
                "dest_number": info["dest"][1],
            }
        if target.kind == "npc":
            return TALK_TO, {"local_id": info["local_id"]}
        if target.kind == "item":
            return PICK_UP, {"local_id": info["local_id"]}
        if target.kind == "sign":
            return INTERACT_WITH, {"bg": info["bg"]}
        if target.kind == "pc":
            return USE_PC, {"x": info["x"], "y": info["y"]}
        if target.kind == "obstacle":
            return TALK_TO, {"local_id": info["local_id"]}
        if target.kind == "grass":
            first, last = info["endpoints"]
            return WALK_IN_GRASS, {
                "start_x": first[0],
                "start_y": first[1],
                "end_x": last[0],
                "end_y": last[1],
                "max_steps": 120,
            }
        return WALK_TO, {"x": info["x"], "y": info["y"]}

    @staticmethod
    def _postcondition(target: T.Target) -> str | None:
        return {
            "warp": PC_WARP,
            "edge": PC_EDGE,
            "npc": PC_INTERACTION,
            "item": PC_INTERACTION,
            "sign": PC_INTERACTION,
            "pc": PC_INTERACTION,
            "obstacle": "field.obstacle_cleared",
            "grass": None,
            "tile": PC_AT_TILE,
        }[target.kind]

    # -- diagnostics -------------------------------------------------------------------------------

    def _unavailable(
        self, target: T.Target, facts: dict[str, Any], legality: Legality, reason: str
    ) -> Option:
        facts = {**facts, "reason": reason}
        return Option(
            id=target.option_id,
            label=target.label,
            description=(
                f"{target.label}. "
                + (
                    "Already read in this objective and map; it contains no new information. "
                    "It does not serve the current objective."
                    if reason == "already_read"
                    else (
                        "Prerequisites are not met. "
                        if facts.get("capability") == "missing"
                        else ""
                    )
                    + f"{render_facts(facts)}. Not possible now: {reason.replace('_', ' ')}."
                )
            )[:600],
            legality=legality,
            facts=facts,
            verb=target.info.get("verb", VERBS[target.kind]),
        )

    @staticmethod
    def _diagnostic(option_id: str, label: str, legality: Legality, reason: str) -> Option:
        return Option(
            id=f"diag:{option_id}",
            label=label,
            description=label,
            legality=legality,
            facts={"reason": reason},
        )

    # -- face options ------------------------------------------------------------------------------

    @staticmethod
    def _face_options(state: MapState) -> list[Option]:
        p = state.player
        assert p is not None
        out = []
        for d in N.DIRECTIONS:
            if d == p.facing:
                continue
            pred = predict_step(state, d)
            if pred.outcome != "blocked":
                continue  # a press toward a free tile walks; only blocked sides can be faced
            out.append(
                Option(
                    id=f"face:{d}",
                    label=f"Turn to face {_COMPASS[d]}",
                    description=(
                        f"Turn to face {_COMPASS[d]} without moving (something blocks that way)."
                    ),
                    legality=Legality.LEGAL,
                    facts={"target": {"kind": "face", "direction": d}, "steps": 0},
                    executor_ref=FACE,
                    executor_args={"turn_to": d},
                    risk=Risk.LOW,
                    postcondition_ref=PC_FACING,
                    verb=VERBS["face"],
                )
            )
        return out


# -- helpers ---------------------------------------------------------------------------------------


def _order_key(option: Option) -> tuple[int, str]:
    steps = option.facts.get("steps")
    is_face = option.id.startswith("face:")
    return (10_000 if is_face else int(steps) if steps is not None else 9_999, option.id)


def _bearing(dx: int, dy: int) -> str:
    """English bearing of something ``dx`` east / ``dy`` south of the player."""
    parts = []
    if dy:
        parts.append(
            f"{abs(dy)} tile{'s' if abs(dy) != 1 else ''} {'south' if dy > 0 else 'north'}"
        )
    if dx:
        parts.append(f"{abs(dx)} tile{'s' if abs(dx) != 1 else ''} {'east' if dx > 0 else 'west'}")
    return " and ".join(parts) if parts else "right here"


def _steps_phrase(steps: int) -> str:
    return "You are already there" if steps == 0 else f"{steps} step{'s' if steps != 1 else ''}"


def _describe(
    target: T.Target, facts: dict[str, Any], steps: int, *, label: str | None = None
) -> str:
    parts = [(label or target.label).rstrip(".") + "."]
    if facts.get("bearing") and facts["bearing"] != "right here":
        parts.append(f"It is {facts['bearing']} from you.")
    parts.append(_steps_phrase(steps) + ".")
    grass = facts.get("grass_tiles", 0)
    if grass:
        surface = "cave floor" if facts.get("surface") == "cave" else "tall grass"
        parts.append(f"Crosses {grass} {surface} tile{'s' if grass != 1 else ''} (wild Pokemon).")
    encounter_tiles = facts.get("encounter_tiles", 0)
    if encounter_tiles > grass:
        parts.append(
            f"Crosses {encounter_tiles} encounter-risk tile"
            f"{'s' if encounter_tiles != 1 else ''} (wild Pokemon)."
        )
    if facts.get("ledges"):
        parts.append("Includes a one-way ledge jump: you cannot come back this way.")
    if facts.get("script_tiles"):
        parts.append("Crosses a spot that starts an event.")
    if facts.get("trigger_untested"):
        parts.append("Whether walking in triggers it is not certain.")
    if facts.get("blocked_by_npc"):
        parts.append("Someone is in the way right now; this waits for them to move.")
    if facts.get("trainer"):
        parts.append("This is a trainer: talking may start a battle.")
    if facts.get("already_visited"):
        parts.append("You have been there before.")
    if facts.get("serves_goal"):
        parts.append("This leads toward the current objective.")
    elif facts.get("serves_goal") is False:
        parts.append("This does not serve the current objective.")
    if facts.get("bounce_count"):
        count = facts["bounce_count"]
        parts.append(
            f"You have tried this person {count} time{'s' if count != 1 else ''} without progress."
        )
    if facts.get("edge_bounced"):
        parts.append(f"You have crossed here {facts['edge_bounced']} times without getting closer.")
    if facts.get("route_needs"):
        parts.append(
            "The route is blocked by " + " and ".join(route_blockers(facts["route_needs"])) + "."
        )
    if facts.get("capability") == "ready":
        if facts.get("requirement", {}).get("kind") == "key_item":
            parts.append(f"Prerequisite is met: {facts['requirement']['name']} is in the Bag.")
        else:
            parts.append(
                "Prerequisites are met: a Pokemon knows the move and the required badge is present."
            )
    if facts.get("last_failure"):
        parts.append(f"The last attempt failed: {facts['last_failure'].replace('_', ' ')}.")
    if facts.get("history_hint"):
        parts.append(str(facts["history_hint"]))
    rendered = render_facts(
        {
            key: facts[key]
            for key in (
                "obstacle",
                "needs",
                "requirement",
                "carrier",
                "unlocks",
                "irreversible",
                "declined_before",
                "patch_size",
                "bound_steps",
                "encounter_table",
                "encounters",
                "forced_tiles",
                "forced_landing",
            )
            if key in facts
        }
    )
    if rendered:
        parts.append(rendered.rstrip(".") + ".")
    return " ".join(parts)[:600]


def _expected(target: T.Target) -> str:
    return {
        "warp": "the player stands in the destination map",
        "edge": "the player stands in the connected map",
        "npc": "a dialog or a battle starts",
        "item": "a dialog starts (the item message)",
        "sign": "a dialog starts (the sign text)",
        "pc": "the PC menu opens",
        "obstacle": "the obstacle is gone or its field-move state changed",
        "grass": "the grass walk reaches its step bound or a battle takes over",
        "tile": "the player stands on the tile",
    }[target.kind]


class _Relaxed:
    """The cumulative relaxations, built lazily and shared by every unreachable target."""

    def __init__(self, state: MapState) -> None:
        self._state = state
        self._cache: dict[str, tuple[N.Navigator, N.Search]] = {}

    def _level(self, reason: str, constraints: N.Constraints) -> tuple[N.Navigator, N.Search]:
        if reason not in self._cache:
            nav = N.Navigator(self._state, constraints)
            self._cache[reason] = (nav, nav.search())
        return self._cache[reason]

    def explain(self, target: T.Target) -> tuple[str, T.Plan | None]:
        """(reason, plan on the relaxed world). ``no_path`` has no plan."""
        for reason, constraints in N.relaxation_levels():
            nav, search = self._level(reason, constraints)
            plan = T.plan_target(nav, search, target)
            if plan is not None:
                return reason, plan
        return "no_path", None
