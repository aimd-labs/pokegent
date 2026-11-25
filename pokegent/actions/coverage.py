"""Catalog-only coverage dispositions for P0 rows without a scene owner.

These are reporting metadata, not runtime claims.  A folded row is a screen that an
existing executor owns while traversing a higher-level option; the reference is the
macro's allowed-scene/transition code.  The report deliberately keeps real gaps visible.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CoverageDisposition:
    kind: str
    reason: str
    ref: str
    runtime_scene: str | None = None

    @property
    def note(self) -> str:
        return f"{self.reason} [{self.ref}]"


def _folded(
    scene_ids: tuple[str, ...], reason: str, ref: str, runtime_scene: str
) -> dict[str, CoverageDisposition]:
    return {sid: CoverageDisposition("folded", reason, ref, runtime_scene) for sid in scene_ids}


def _not_needed(
    scene_ids: tuple[str, ...], reason: str, ref: str
) -> dict[str, CoverageDisposition]:
    return {sid: CoverageDisposition("not_needed_for_e4", reason, ref) for sid in scene_ids}


# The references intentionally point at the executor's allowed-scene sets or transition code.
# They are validated by historical local validation, so this cannot silently become prose.
FOLDED: dict[str, CoverageDisposition] = {
    **_folded(
        (
            "bag.battle_item",
            "bag.context",
            "bag.error",
            "bag.items",
            "bag.pocket",
            "bag.use",
            "bag.use_target",
        ),
        "field item macro traverses the bag and recovers to field.free",
        "ExecutorSpec:field.use_item",
        "field.free",
    ),
    **_folded(
        ("party.battle_pick", "party.battle_switch_prompt"),
        "battle switch/send-out macro traverses the party screen",
        "ExecutorSpec:battle.switch_to",
        "battle.party",
    ),
    **_folded(
        (
            "party.heal_other",
            "party.item_target",
            "party.pp_target",
            "party.tm_target",
            "party.tutor_target",
        ),
        "field item/TM macro traverses party targeting and returns to the field",
        "ExecutorSpec:field.use_item",
        "party.list",
    ),
    **_folded(
        ("tm.context", "tm.learn_confirm", "tm.list", "tm.replace_move", "tm.teach_target"),
        "TM/HM teaching macro owns the TM case, target, and replacement flow",
        "ExecutorSpec:field.teach_tm",
        "tm.list",
    ),
    **_folded(
        ("menu.bag_message", "menu.blackout_message"),
        "field item/recovery macro consumes the transient message and verifies the exit",
        "ExecutorSpec:field.use_item",
        "bag.items",
    ),
    **_folded(
        ("menu.fly_map",),
        "Fly macro owns the map selection and bounded cancel recovery",
        "ExecutorSpec:field.fly_map_cancel",
        "menu.fly_map",
    ),
    **_folded(
        ("pc.pokemon_mode",),
        "PC storage macro owns the storage mode and exits back through pc.main",
        "ExecutorSpec:system_flow.pc_menu_choice",
        "pc.main",
    ),
    **_folded(
        ("field.fishing",),
        "rod use macro owns the fishing state and verifies the bounded catch result",
        "ExecutorSpec:field.fish",
        "field.free",
    ),
}

_FIELD_FOLDED_REFS = {
    "field.bicycle": "ExecutorSpec:field.use_bike",
    "field.bike_forced": "ExecutorSpec:field.use_bike",
    "field.conveyor_spinner": "ExecutorSpec:field.walk_to",
    "field.cut_tree": "ExecutorSpec:field.teach_hm",
    "field.dig_teleport": "ExecutorSpec:field.use_dig_teleport",
    "field.door": "ExecutorSpec:field.enter_warp",
    "field.face": "ExecutorSpec:field.face",
    "field.grass": "ExecutorSpec:field.walk_in_grass",
    "field.hole": "ExecutorSpec:field.enter_warp",
    "field.interactive_object": "ExecutorSpec:field.interact_with",
    "field.item_ball": "ExecutorSpec:field.pick_up",
    "field.ladder_stairs": "ExecutorSpec:field.enter_warp",
    "field.ledge": "ExecutorSpec:field.enter_warp",
    "field.map_connection": "ExecutorSpec:field.travel_edge",
    "field.npc": "ExecutorSpec:field.talk_to",
    "field.path_target": "ExecutorSpec:field.walk_to",
    "field.sign": "ExecutorSpec:field.talk_to",
    "field.smashable_rock": "ExecutorSpec:field.interact_with",
    "field.strength_boulder": "ExecutorSpec:field.interact_with",
    "field.surf_edge": "ExecutorSpec:field.start_surf",
    "field.warp": "ExecutorSpec:field.enter_warp",
    "field.waterfall": "ExecutorSpec:field.teach_hm",
}
for _scene_id, _ref in _FIELD_FOLDED_REFS.items():
    FOLDED[_scene_id] = CoverageDisposition(
        "folded", "field executor owns this transient field state", _ref, "field.free"
    )

FOLDED.update(
    _folded(
        ("party.summary", "party.summary_moves"),
        "TM/HM and move-learning macros drive the Pokemon summary callback",
        "ExecutorSpec:field.teach_tm",
        "field.free",
    )
)

NOT_NEEDED_FOR_E4: dict[str, CoverageDisposition] = {
    **_not_needed(
        (
            "bag.deposit",
            "bag.give_item",
            "bag.register",
            "bag.sell",
            "bag.sell_quantity",
            "bag.toss_confirm",
            "bag.toss_quantity",
        ),
        "optional bag feature is not used by the Elite Four plan",
        "pokegent.actions.coverage:NOT_NEEDED_FOR_E4",
    ),
    **_not_needed(
        (
            "berry.confirm",
            "berry.context",
            "berry.list",
            "berry.quantity",
            "berry.target",
            "egg.hatching",
        ),
        "berry/egg handling is outside the Elite Four plan",
        "pokegent.actions.coverage:NOT_NEEDED_FOR_E4",
    ),
    **_not_needed(
        (
            "dialog.ferry",
            "dialog.move_tutor",
            "dialog.picture",
            "dialog.quantity",
            "dialog.save_prompt",
            "dialog.script_puzzle",
            "dialog.starter_choice",
            "dialog.trade_offer",
        ),
        "optional dialog or foreign UI is outside the Elite Four plan",
        "pokegent.actions.menu.dialog:DialogFamily",
    ),
    **_not_needed(
        ("intro.player_name", "intro.starter_choice", "intro.starter_confirm", "naming.pokemon"),
        "new-game setup is outside an Elite Four run from a loaded save",
        "ExecutorSpec:system_flow.advance_intro",
    ),
    **_not_needed(
        (
            "menu.ferry",
            "menu.help_controls",
            "menu.options",
            "menu.pokedex",
            "menu.region_map",
            "menu.save",
            "menu.save_overwrite",
            "menu.teachy_tv",
            "menu.trainer_card",
            "move.deleter",
            "move.relearner_list",
            "party.daycare",
            "party.mail",
            "pokedex.category",
            "pokedex.detail",
            "pokedex.list",
        ),
        "optional menu, dex, move, or party utility is outside the Elite Four plan",
        "pokegent.actions.coverage:NOT_NEEDED_FOR_E4",
    ),
    **_not_needed(
        ("shop.game_corner_exchange", "shop.special_service", "shop.vending_machine"),
        "special shop service is outside the E4 supply plan",
        "pokegent.actions.menu.shop.macros:executor_specs",
    ),
    **_not_needed(
        ("special.unknown_script", "system.boot", "system.unsupported_rom", "title.options"),
        "boot/options/unsupported-script handling is not an E4 decision point",
        "ExecutorSpec:system_flow.press_start",
    ),
}

# These are the rows where landing on the screen is a real interruption today.  The
# recommendation order is repeated in the PR body: navigation hazards first, then recovery.
REAL_GAPS: dict[str, CoverageDisposition] = {
    sid: CoverageDisposition(
        "REAL_GAP",
        "field hazard/action has no detector, builder, or bounded exit",
        "pokegent.actions.field.detect:FieldDetector",
    )
    for sid in (
        "field.hidden_item",
        "field.ice_slide",
        "field.sweet_scent",
        "field.unknown_obstacle",
    )
}

ALL_DISPOSITIONS = {**FOLDED, **NOT_NEEDED_FOR_E4, **REAL_GAPS}
