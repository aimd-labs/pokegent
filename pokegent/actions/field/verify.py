"""Postconditions of the field family (spec 11.3 "Movement/Path", "Warp", "NPC/Sign").

Each predicate is evaluated by the executor on fresh observations with a bounded wait. They look at
the OUTCOME the game shows, never at what the macro believed:

* warp: the readable map is the destination (when known) and the player stands on, or next to, the
  destination map's own warp ``dest_warp`` (a door arrival lands on the warp tile and steps out);
* edge: the readable map is the connected map;
* interaction: a script, dialog or battle now has the screen;
* tile: the player stands on the tile, same map;
* facing: the avatar faces the direction.
"""

from __future__ import annotations

from pokegent.actions._exec.registry import VerifierSpec, VerifyContext
from pokegent.actions.field import builder as B
from pokegent.actions.field import evidence as E
from pokegent.actions.field import fishing, fly
from pokegent.actions.field.boulder_options import PC_BOULDER
from pokegent.actions.items import EffectClass, ItemCatalog
from pokegent.games.gen3.firered.maps.map_schema import MapState


def _key(state: MapState) -> tuple[int, int]:
    return (state.identity.group, state.identity.number)


def warp_done(ctx: VerifyContext) -> bool:
    return warp_failure_reason(ctx) is None


def warp_failure_reason(ctx: VerifyContext) -> str | None:
    after, before = ctx.after.map, ctx.before.map
    if after is None or after.player is None or before is None:
        return "map_unreadable"
    group, number = ctx.args["dest_group"], ctx.args["dest_number"]
    if group >= 0:
        if _key(after) != (group, number):
            if _key(after) == _key(before):
                return _blocked_obstacle_reason(ctx)
            return "map_unchanged" if _key(after) == _key(before) else "map_destination_mismatch"
    elif _key(after) == _key(before):
        return _blocked_obstacle_reason(ctx)
    dest_warp = ctx.args["dest_warp"]
    if group >= 0 and 0 <= dest_warp < len(after.warps):
        w = after.warps[dest_warp]
        p = after.player
        scripted_arrival = ctx.option.facts.get("scripted_arrival")
        expected = tuple(scripted_arrival) if scripted_arrival is not None else (w.x, w.y)
        tolerance = 0 if scripted_arrival is not None else 1
        if abs(p.avatar_x - expected[0]) + abs(p.avatar_y - expected[1]) > tolerance:
            return "arrival_tile_mismatch"
    return None


def _blocked_obstacle_reason(ctx: VerifyContext) -> str:
    obstacle = ctx.option.facts.get("blocked_obstacle")
    if isinstance(obstacle, dict) and all(key in obstacle for key in ("obstacle", "map", "x", "y")):
        return (
            f"E_BLOCKED: blocked by {obstacle['obstacle']} at {obstacle['map']} "
            f"({obstacle['x']},{obstacle['y']})"
        )
    return "map_unchanged"


def edge_done(ctx: VerifyContext) -> bool:
    after = ctx.after.map
    if after is None or after.player is None:
        return False
    return _key(after) == (ctx.args["dest_group"], ctx.args["dest_number"])


def interaction_started(ctx: VerifyContext) -> bool:
    scene = ctx.scene.scene_id
    if scene.startswith(("dialog.", "battle.", "system.")) or scene in (
        E.TRAINER_VISION,
        "field.scripted_movement",
        "field.map_transition",
    ):
        return True
    ui = ctx.after.ui
    return bool(ui is not None and ui.valid and (ui.script.running or ui.script.controls_locked))


def waterfall_failure_reason(ctx: VerifyContext) -> str | None:
    after = ctx.after.map
    before = ctx.before.map
    if after is None or after.player is None or before is None:
        return "waterfall_arrival_unreadable"
    if _key(after) != _key(before):
        return "waterfall_arrival_map_changed"
    destination = ctx.option.facts.get("waterfall_destination")
    if destination is None:
        destination = (ctx.args["destination_x"], ctx.args["destination_y"])
    if (after.player.avatar_x, after.player.avatar_y) != tuple(destination):
        return "waterfall_arrival_mismatch"
    return None


def waterfall_done(ctx: VerifyContext) -> bool:
    return waterfall_failure_reason(ctx) is None


def object_gone(ctx: VerifyContext) -> bool:
    """Verify a field object disappeared or its saved hide flag changed.

    The field macro hands a live prompt to the dialog family; this predicate is also
    registered for the post-dialog integration path and never treats a prompt alone
    as the obstacle being cleared.
    """
    after = ctx.after.map
    if after is None:
        return False
    obj = after.object_by_local_id(ctx.args["local_id"])
    return obj is None or obj.hidden_flag_set is True or not obj.blocks_tile


def obstacle_cleared(ctx: VerifyContext) -> bool:
    """Verify the generic obstacle completion contract from fresh RAM."""

    completion = ctx.option.facts.get("completion")
    after = ctx.after.map
    if after is None:
        return False
    if completion == "strength_active":
        return after.strength_active is True
    hide_flag = ctx.option.facts.get("hide_flag")
    if isinstance(hide_flag, int) and hide_flag > 0 and ctx.after.flags is not None:
        if ctx.after.flags.is_set(hide_flag):
            return True
    obj = after.object_by_local_id(ctx.args["local_id"])
    # Battle alone is not evidence: incidental encounters must interrupt and retry the approach.
    return obj is None or obj.hidden_flag_set is True or not obj.blocks_tile


def strength_armed(ctx: VerifyContext) -> bool:
    after = ctx.after.map
    return after is not None and after.strength_active is True


def boulder_progress(ctx: VerifyContext) -> bool:
    """A ``boulder_plan`` made progress: a prompt/dialog took over (Strength arming), Strength
    became active, or a boulder is somewhere else / gone. The macro checks the goal push by push."""
    from pokegent.games.gen3.firered.maps.map_boulders import boulders

    if interaction_started(ctx):
        return True
    before, after = ctx.before.map, ctx.after.map
    if before is None or after is None or _key(before) != _key(after):
        return False
    if after.strength_active and not before.strength_active:
        return True
    moved = {b.local_id: (b.xy, b.hidden) for b in boulders(before)}
    return any(moved.get(b.local_id) != (b.xy, b.hidden) for b in boulders(after))


def at_tile(ctx: VerifyContext) -> bool:
    after, before = ctx.after.map, ctx.before.map
    if after is None or after.player is None or before is None:
        return False
    return _key(after) == _key(before) and (after.player.avatar_x, after.player.avatar_y) == (
        ctx.args["x"],
        ctx.args["y"],
    )


def facing(ctx: VerifyContext) -> bool:
    after = ctx.after.map
    return bool(
        after is not None and after.player is not None
        and after.player.facing == ctx.args["turn_to"]
    )  # fmt: skip


def script_done(ctx: VerifyContext) -> bool:
    return ctx.scene.scene_id != E.TRAINER_VISION


def bike_on(ctx: VerifyContext) -> bool:
    return bool(ctx.after.map and ctx.after.map.player and ctx.after.map.player.biking)


def bike_off(ctx: VerifyContext) -> bool:
    return bool(ctx.after.map and ctx.after.map.player and not ctx.after.map.player.biking)


def flash_done(ctx: VerifyContext) -> bool:
    return bool(ctx.after.map is not None and ctx.after.map.flash_level == 0)


def fly_done(ctx: VerifyContext) -> bool:
    after = ctx.after.map
    if after is None or after.player is None:
        return False
    if _key(after) != (ctx.args["destination_group"], ctx.args["destination_number"]):
        return False
    destination = fly.DESTINATIONS_BY_KEY.get(_key(after))
    if destination is None:
        return False
    return (
        abs(after.player.avatar_x - destination.landing[0])
        + abs(after.player.avatar_y - destination.landing[1])
        <= 1
    )


def fly_map_cancelled(ctx: VerifyContext) -> bool:
    """A resumed Fly map is closed only once the field detector owns the screen."""
    return ctx.scene.scene_id == E.FREE


def dig_teleport_done(ctx: VerifyContext) -> bool:
    before, after = ctx.before.map, ctx.after.map
    return bool(
        before
        and after
        and (before.identity.group, before.identity.number)
        != (after.identity.group, after.identity.number)
    )


def tool_done(ctx: VerifyContext) -> bool:
    if "mounting" in ctx.args:
        after = ctx.after.map
        return (
            after is not None
            and after.player is not None
            and (bool(after.player.biking) is bool(ctx.args["mounting"]))
        )
    slot = int(ctx.args.get("party_slot", -1))
    learned = any(
        mon.slot == slot and any(move.move_id == fly.FLY_MOVE_ID for move in mon.moves)
        for mon in ctx.after.party
    )
    ui = ctx.after.ui
    if not learned or ui is None or not ui.valid or ui.menu is None:
        return False
    if ui.main.callback2 is None or ui.main.callback2.name != "CB2_Overworld":
        return False
    if ui.script.running is not False or ui.script.controls_locked is not False:
        return False
    if ui.field.forced_movement is not False:
        return False
    menu = ui.menu
    return not any(
        (
            menu.start_menu is not None and menu.start_menu.task_active,
            menu.bag is not None and menu.bag.active_hint is True,
            menu.party is not None and menu.party.active_hint is True,
            menu.list_menu is not None and menu.list_menu.window_in_use,
            menu.menu is not None and menu.menu.window_in_use,
        )
    )


def item_used(ctx: VerifyContext) -> bool:
    item_id = int(ctx.args["item_id"])
    before = sum(
        item.quantity
        for pocket in ctx.before.bag
        for item in pocket.items
        if item.item_id == item_id
    )
    after = sum(
        item.quantity
        for pocket in ctx.after.bag
        for item in pocket.items
        if item.item_id == item_id
    )
    return after < before


def evolution_started(ctx: VerifyContext) -> bool:
    """The stone use handed control to the existing evolution family, or changed species."""
    if ctx.scene.scene_id.startswith("evolution."):
        return True
    slot = int(ctx.args["party_slot"])
    before = next((mon for mon in ctx.before.party if mon.slot == slot), None)
    after = next((mon for mon in ctx.after.party if mon.slot == slot), None)
    return before is not None and after is not None and after.species != before.species


def tm_taught(ctx: VerifyContext) -> bool:
    slot = int(ctx.args["party_slot"])
    move_id = int(ctx.args["move_id"])
    learned = any(
        mon.slot == slot and any(move.move_id == move_id for move in mon.moves)
        for mon in ctx.after.party
    )
    item_id = int(ctx.args["item_id"])
    before = sum(
        item.quantity
        for pocket in ctx.before.bag
        for item in pocket.items
        if item.item_id == item_id
    )
    after = sum(
        item.quantity
        for pocket in ctx.after.bag
        for item in pocket.items
        if item.item_id == item_id
    )
    return learned and after < before


def teach_target_ready(ctx: VerifyContext) -> bool:
    ui = ctx.after.ui
    party = ui.menu.party if ui is not None and ui.valid and ui.menu is not None else None
    return bool(
        ctx.scene.scene_id == "party.list"
        and party is not None
        and party.active_hint is True
        and party.action_name == "use_item"
        and party.item_id == int(ctx.args["item_id"])
        and any(task.func.name == "Task_HandleChooseMonInput" for task in ui.tasks.active)
    )


def teach_cancelled(ctx: VerifyContext) -> bool:
    ui = ctx.after.ui
    if ui is None or not ui.valid or ui.menu is None:
        return False
    menu = ui.menu
    return bool(
        ctx.scene.scene_id == E.FREE
        and ui.main.callback2 is not None
        and ui.main.callback2.name == "CB2_Overworld"
        and ui.script.running is False
        and ui.script.controls_locked is False
        and not any(
            (
                menu.start_menu is not None and menu.start_menu.task_active,
                menu.bag is not None and menu.bag.active_hint is True,
                menu.party is not None and menu.party.active_hint is True,
                menu.list_menu is not None and menu.list_menu.window_in_use,
                menu.menu is not None and menu.menu.window_in_use,
            )
        )
    )


def item_applied(ctx: VerifyContext, catalog: ItemCatalog | None) -> bool:
    """Verify the catalog-declared effect against fresh party, map, scene and bag RAM."""
    if catalog is None:
        return False
    item_id = int(ctx.args["item_id"])
    record = catalog.item(item_id)
    before_quantity = sum(
        item.quantity
        for pocket in ctx.before.bag
        for item in pocket.items
        if item.item_id == item_id
    )
    after_quantity = sum(
        item.quantity
        for pocket in ctx.after.bag
        for item in pocket.items
        if item.item_id == item_id
    )
    consumed = after_quantity < before_quantity

    if EffectClass.TEACH_MOVE in record.capability.effects:
        slot = int(ctx.args["party_slot"])
        move_id = int(ctx.args["move_id"])
        learned = any(
            mon.slot == slot and any(move.move_id == move_id for move in mon.moves)
            for mon in ctx.after.party
        )
        return learned and (
            after_quantity == before_quantity
            if not record.capability.consumable
            else after_quantity < before_quantity
        )
    if EffectClass.EVOLVE in record.capability.effects:
        slot = int(ctx.args["party_slot"])
        before = next((mon for mon in ctx.before.party if mon.slot == slot), None)
        after = next((mon for mon in ctx.after.party if mon.slot == slot), None)
        evolved = before is not None and after is not None and before.species != after.species
        return evolution_started(ctx) or (evolved and consumed)
    if record.capability.field_action == "bicycle":
        before = ctx.before.map.player if ctx.before.map else None
        after = ctx.after.map.player if ctx.after.map else None
        return before is not None and after is not None and before.biking != after.biking
    if record.capability.field_action == "rod":
        return interaction_started(ctx)

    slot = int(ctx.args.get("party_slot", -1))
    if slot >= 0:
        before = next((mon for mon in ctx.before.party if mon.slot == slot), None)
        after = next((mon for mon in ctx.after.party if mon.slot == slot), None)
        changed = before is not None and after is not None and before != after
        return changed and (consumed or not record.capability.consumable)
    if record.capability.field_action == "escape_rope":
        before_player, before_identity = E._snapshot_map_keys(ctx.before)
        after_player, after_identity = E._snapshot_map_keys(ctx.after)
        map_changed = (
            before_player is not None and after_player is not None and before_player != after_player
        ) or (
            before_identity is not None
            and after_identity is not None
            and before_identity != after_identity
        )
        return consumed and map_changed
    return consumed if record.capability.consumable else interaction_started(ctx)


def item_applied_failure_reason(ctx: VerifyContext, catalog: ItemCatalog | None) -> str | None:
    """Expose Rope transition evidence when a delayed field transition misses its bound."""
    if (
        catalog is None
        or catalog.item(int(ctx.args["item_id"])).capability.field_action != "escape_rope"
    ):
        return None
    item_id = int(ctx.args["item_id"])
    before_quantity = sum(
        item.quantity
        for pocket in ctx.before.bag
        for item in pocket.items
        if item.item_id == item_id
    )
    after_quantity = sum(
        item.quantity
        for pocket in ctx.after.bag
        for item in pocket.items
        if item.item_id == item_id
    )
    before = E._snapshot_map_keys(ctx.before)
    after = E._snapshot_map_keys(ctx.after)
    return f"rope before={before_quantity} after={after_quantity} maps={before}->{after}"


def verifier_specs(*, item_catalog: ItemCatalog | None = None) -> list[VerifierSpec]:
    return [
        VerifierSpec(
            B.PC_WARP,
            warp_done,
            max_frames=90,
            max_wall_s=10.0,
            failure_reason=warp_failure_reason,
        ),
        VerifierSpec(B.PC_EDGE, edge_done, max_frames=90, max_wall_s=10.0),
        VerifierSpec(B.PC_INTERACTION, interaction_started, max_frames=60, max_wall_s=10.0),
        VerifierSpec(
            B.PC_WATERFALL,
            waterfall_done,
            max_frames=1800,
            max_wall_s=90.0,
            failure_reason=lambda ctx: waterfall_failure_reason(ctx) or "waterfall_not_complete",
        ),
        VerifierSpec("field.object_gone", object_gone, max_frames=90, max_wall_s=10.0),
        VerifierSpec("field.obstacle_cleared", obstacle_cleared, max_frames=900, max_wall_s=30.0),
        VerifierSpec("field.strength_armed", strength_armed, max_frames=90, max_wall_s=10.0),
        VerifierSpec(PC_BOULDER, boulder_progress, max_frames=90, max_wall_s=10.0),
        VerifierSpec(B.PC_AT_TILE, at_tile, max_frames=30, max_wall_s=10.0),
        VerifierSpec(B.PC_FACING, facing, max_frames=30, max_wall_s=10.0),
        VerifierSpec(B.PC_SCRIPT_DONE, script_done, max_frames=60, max_wall_s=10.0),
        VerifierSpec("field.bike_on", bike_on, max_frames=180, max_wall_s=15.0),
        VerifierSpec("field.bike_off", bike_off, max_frames=180, max_wall_s=15.0),
        VerifierSpec("field.flash_done", flash_done, max_frames=900, max_wall_s=45.0),
        VerifierSpec(B.PC_FLY, fly_done, max_frames=180, max_wall_s=20.0),
        VerifierSpec(B.PC_FLY_MAP_CANCEL, fly_map_cancelled, max_frames=1800, max_wall_s=90.0),
        VerifierSpec(B.PC_DIG_TELEPORT, dig_teleport_done, max_frames=1800, max_wall_s=45.0),
        VerifierSpec(B.PC_TOOL, tool_done, max_frames=180, max_wall_s=20.0),
        VerifierSpec("field.item_used", item_used, max_frames=240, max_wall_s=20.0),
        VerifierSpec("field.fishing_resolved", fishing.resolved, max_frames=300, max_wall_s=10.0),
        VerifierSpec("field.evolution_started", evolution_started, max_frames=240, max_wall_s=20.0),
        VerifierSpec("field.tm_taught", tm_taught, max_frames=600, max_wall_s=45.0),
        VerifierSpec(B.PC_TEACH_TARGET_READY, teach_target_ready, max_frames=180, max_wall_s=20.0),
        VerifierSpec(B.PC_TEACH_CANCELLED, teach_cancelled, max_frames=600, max_wall_s=30.0),
        VerifierSpec(
            "item.applied",
            lambda ctx: item_applied(ctx, item_catalog),
            max_frames=1800,
            max_wall_s=90.0,
            failure_reason=lambda ctx: item_applied_failure_reason(ctx, item_catalog),
        ),
    ]
