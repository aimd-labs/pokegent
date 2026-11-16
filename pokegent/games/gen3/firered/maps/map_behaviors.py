"""Metatile behavior classification (spec 7.1 "Tile", 6.B traversal facts).

Pure functions over the pret ``MB_*`` ids. ``classify_behavior`` turns a behavior id into the facts
a navigator needs: what kind of tile it is, whether it is water (needs Surf), a one-way ledge (and
which way), forced movement (ice, slide, current, conveyor), a warp tile (and how it fires), tall
grass (encounter risk) and which sides are blocked. The predicates mirror the decompile
(``src/metatile_behavior.c``, ``src/field_control_avatar.c``); the movement rules that use them live
in :mod:`pokegent.games.gen3.firered.maps.map_motion`.

Nothing here decides strategy; it only names what the game's own tables say.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pokegent.games.gen3.firered.maps.map_constants import MB, MB_NAME

Facing = Literal["N", "S", "W", "E"]

TileKind = Literal[
    "normal",
    "tall_grass",
    "water",
    "waterfall",
    "shallow_water",
    "ledge",
    "ice",
    "thin_ice",
    "cracked_ice",
    "slide",
    "conveyor",
    "spin",
    "stop_spin",
    "current",
    "door",
    "cave_door",
    "ladder",
    "stairs",
    "escalator",
    "arrow_warp",
    "hole",
    "warp_pad",
    "union_room_warp",
    "counter",
    "furniture",
    "sign",
    "sand",
    "rock_stairs",
    "hot_springs",
    "directional_block",
    "cave",
    "indoor_encounter",
    "mountain_top",
    "seaweed",
    "strength_button",
    "cycling_road",
    "unknown",
]

# How a warp event on a tile of this kind fires (src/field_control_avatar.c):
#   step          walking onto the tile (IsWarpMetatileBehavior: door, ladder, escalator, cave door,
#                 warp pad, fall warp, union room)
#   door          warp door: walking onto it, or pressing north toward it (TryDoorWarp)
#   arrow         standing on the tile and pressing the arrow's direction (TryArrowWarp)
#   stairs        standing on the tile and pressing east or west
#                 (IsDirectionalStairWarpMetatileBehavior)
#   none          the behavior does not fire warps by itself (a script or a special case does)
WarpTrigger = Literal["step", "door", "arrow", "stairs", "none"]


@dataclass(frozen=True)
class BehaviorInfo:
    behavior: int
    name: str
    kind: TileKind
    surfable: bool = False
    waterfall: bool = False
    tall_grass: bool = False
    ledge: Facing | None = None  # the direction a player must move to jump this ledge tile
    forced: Facing | None = None  # direction the tile pushes the player (ice excluded: see ice)
    ice: bool = False
    blocked_sides: tuple[Facing, ...] = ()  # MB_IMPASSABLE_*: sides of this tile that block
    warp_trigger: WarpTrigger | None = None
    warp_direction: Facing | None = None  # arrow/stairs direction
    interactive: str | None = None  # furniture/counter/sign family a player talks to
    no_running: bool = False


_N, _S, _W, _E = "N", "S", "W", "E"


def _b(name: str) -> int:
    return MB[name]


_BLOCKED_SIDES: dict[int, tuple[Facing, ...]] = {
    _b("MB_IMPASSABLE_EAST"): (_E,),
    _b("MB_IMPASSABLE_WEST"): (_W,),
    _b("MB_IMPASSABLE_NORTH"): (_N,),
    _b("MB_IMPASSABLE_SOUTH"): (_S,),
    _b("MB_IMPASSABLE_NORTHEAST"): (_N, _E),
    _b("MB_IMPASSABLE_NORTHWEST"): (_N, _W),
    _b("MB_IMPASSABLE_SOUTHEAST"): (_S, _E),
    _b("MB_IMPASSABLE_SOUTHWEST"): (_S, _W),
}
_LEDGES: dict[int, Facing] = {
    _b("MB_JUMP_EAST"): _E,
    _b("MB_JUMP_WEST"): _W,
    _b("MB_JUMP_NORTH"): _N,
    _b("MB_JUMP_SOUTH"): _S,
}
_FORCED: dict[int, tuple[Facing, TileKind]] = {
    _b("MB_WALK_EAST"): (_E, "conveyor"),
    _b("MB_WALK_WEST"): (_W, "conveyor"),
    _b("MB_WALK_NORTH"): (_N, "conveyor"),
    _b("MB_WALK_SOUTH"): (_S, "conveyor"),
    _b("MB_SLIDE_EAST"): (_E, "slide"),
    _b("MB_SLIDE_WEST"): (_W, "slide"),
    _b("MB_SLIDE_NORTH"): (_N, "slide"),
    _b("MB_SLIDE_SOUTH"): (_S, "slide"),
    _b("MB_EASTWARD_CURRENT"): (_E, "current"),
    _b("MB_WESTWARD_CURRENT"): (_W, "current"),
    _b("MB_NORTHWARD_CURRENT"): (_N, "current"),
    _b("MB_SOUTHWARD_CURRENT"): (_S, "current"),
    _b("MB_SPIN_RIGHT"): (_E, "spin"),
    _b("MB_SPIN_LEFT"): (_W, "spin"),
    _b("MB_SPIN_UP"): (_N, "spin"),
    _b("MB_SPIN_DOWN"): (_S, "spin"),
}
# src/metatile_behavior.c sBehaviorSurfable
_SURFABLE = {
    _b(n)
    for n in (
        "MB_POND_WATER",
        "MB_FAST_WATER",
        "MB_DEEP_WATER",
        "MB_WATERFALL",
        "MB_OCEAN_WATER",
        "MB_UNUSED_WATER",
        "MB_CYCLING_ROAD_WATER",
        "MB_EASTWARD_CURRENT",
        "MB_WESTWARD_CURRENT",
        "MB_NORTHWARD_CURRENT",
        "MB_SOUTHWARD_CURRENT",
    )
}
_WATER_KINDS: dict[int, TileKind] = {
    _b("MB_POND_WATER"): "water",
    _b("MB_FAST_WATER"): "water",
    _b("MB_DEEP_WATER"): "water",
    _b("MB_OCEAN_WATER"): "water",
    _b("MB_UNUSED_WATER"): "water",
    _b("MB_CYCLING_ROAD_WATER"): "water",
    _b("MB_WATERFALL"): "waterfall",
    _b("MB_PUDDLE"): "shallow_water",
    _b("MB_SHALLOW_WATER"): "shallow_water",
}
_ARROW_WARPS: dict[int, Facing] = {
    _b("MB_EAST_ARROW_WARP"): _E,
    _b("MB_WEST_ARROW_WARP"): _W,
    _b("MB_NORTH_ARROW_WARP"): _N,
    _b("MB_SOUTH_ARROW_WARP"): _S,
}
_STAIR_WARPS: dict[int, Facing] = {
    _b("MB_UP_RIGHT_STAIR_WARP"): _E,
    _b("MB_DOWN_RIGHT_STAIR_WARP"): _E,
    _b("MB_UP_LEFT_STAIR_WARP"): _W,
    _b("MB_DOWN_LEFT_STAIR_WARP"): _W,
}
# (kind, trigger) for the plain warp behaviors
_PLAIN_WARPS: dict[int, tuple[TileKind, WarpTrigger]] = {
    _b("MB_WARP_DOOR"): ("door", "door"),
    _b("MB_CAVE_DOOR"): ("cave_door", "step"),
    _b("MB_LADDER"): ("ladder", "step"),
    _b("MB_UP_ESCALATOR"): ("escalator", "step"),
    _b("MB_DOWN_ESCALATOR"): ("escalator", "step"),
    _b("MB_REGULAR_WARP"): ("warp_pad", "step"),
    _b("MB_FALL_WARP"): ("hole", "step"),
    _b("MB_UNION_ROOM_WARP"): ("union_room_warp", "step"),
    _b("MB_LAVARIDGE_1F_WARP"): ("warp_pad", "none"),  # unused in FireRed (Emerald leftover)
}
_INTERACTIVE_START, _INTERACTIVE_END = _b("MB_COUNTER"), _b("MB_TRAINER_TOWER_MONITOR")


def classify_behavior(behavior: int) -> BehaviorInfo:
    """Facts for one metatile behavior id (0..0x1FF). Unknown ids are ``kind='unknown'``."""
    name = MB_NAME.get(behavior, f"MB_0x{behavior:02X}")
    surfable = behavior in _SURFABLE
    if behavior in _BLOCKED_SIDES:
        return BehaviorInfo(
            behavior, name, "directional_block", blocked_sides=_BLOCKED_SIDES[behavior]
        )
    if behavior in _LEDGES:
        return BehaviorInfo(behavior, name, "ledge", ledge=_LEDGES[behavior])
    if behavior in _FORCED:
        direction, kind = _FORCED[behavior]
        return BehaviorInfo(behavior, name, kind, forced=direction, surfable=surfable)
    if behavior in _WATER_KINDS:
        kind = _WATER_KINDS[behavior]
        return BehaviorInfo(
            behavior, name, kind, surfable=surfable, waterfall=behavior == _b("MB_WATERFALL")
        )
    if behavior in _ARROW_WARPS:
        return BehaviorInfo(
            behavior,
            name,
            "arrow_warp",
            warp_trigger="arrow",
            warp_direction=_ARROW_WARPS[behavior],
        )
    if behavior in _STAIR_WARPS:
        return BehaviorInfo(
            behavior,
            name,
            "stairs",
            warp_trigger="stairs",
            warp_direction=_STAIR_WARPS[behavior],
        )
    if behavior in _PLAIN_WARPS:
        kind, trigger = _PLAIN_WARPS[behavior]
        return BehaviorInfo(behavior, name, kind, warp_trigger=trigger)
    if behavior in (_b("MB_TALL_GRASS"), _b("MB_CYCLING_ROAD_PULL_DOWN_GRASS")):
        return BehaviorInfo(behavior, name, "tall_grass", tall_grass=True)
    if behavior == _b("MB_ICE"):
        return BehaviorInfo(behavior, name, "ice", ice=True)
    simple: dict[str, TileKind] = {
        "MB_THIN_ICE": "thin_ice",
        "MB_CRACKED_ICE": "cracked_ice",
        "MB_STOP_SPINNING": "stop_spin",
        "MB_SAND": "sand",
        "MB_SAND_CAVE": "sand",
        "MB_ROCK_STAIRS": "rock_stairs",
        "MB_HOT_SPRINGS": "hot_springs",
        "MB_CAVE": "cave",
        "MB_INDOOR_ENCOUNTER": "indoor_encounter",
        "MB_MOUNTAIN_TOP": "mountain_top",
        "MB_SEAWEED": "seaweed",
        "MB_STRENGTH_BUTTON": "strength_button",
        "MB_CYCLING_ROAD_PULL_DOWN": "cycling_road",
        "MB_SIGNPOST": "sign",
        "MB_INDIGO_PLATEAU_SIGN_1": "sign",
        "MB_INDIGO_PLATEAU_SIGN_2": "sign",
        "MB_POKEMON_CENTER_SIGN": "sign",
        "MB_POKEMART_SIGN": "sign",
        "MB_COUNTER": "counter",
    }
    for mb_name, kind in simple.items():
        if behavior == MB[mb_name]:
            return BehaviorInfo(
                behavior,
                name,
                kind,
                interactive="counter"
                if kind == "counter"
                else ("sign" if kind == "sign" else None),
            )
    if _INTERACTIVE_START < behavior <= _INTERACTIVE_END:
        # bookshelf, PC, TV, shelves, cabinets ...: furniture a player interacts with from a side
        return BehaviorInfo(behavior, name, "furniture", interactive=name[3:].lower())
    if behavior == _b("MB_NORMAL") or behavior == _b("MB_UNUSED_01"):
        return BehaviorInfo(behavior, name, "normal")
    if behavior == _b("MB_RUNNING_DISALLOWED"):
        return BehaviorInfo(behavior, name, "normal", no_running=True)
    return BehaviorInfo(behavior, name, "unknown")


_CACHE: dict[int, BehaviorInfo] = {}


def behavior_info(behavior: int) -> BehaviorInfo:
    """Cached :func:`classify_behavior`."""
    info = _CACHE.get(behavior)
    if info is None:
        info = _CACHE[behavior] = classify_behavior(behavior)
    return info


def blocks_side(behavior: int, side: Facing) -> bool:
    """MetatileBehavior_Is{North,South,East,West}Blocked (src/metatile_behavior.c)."""
    return side in behavior_info(behavior).blocked_sides


def ledge_jump_direction(behavior: int) -> Facing | None:
    """The direction in which a tile with this behavior is jumped (MB_JUMP_*), else None."""
    return behavior_info(behavior).ledge


def is_directional_stair_warp(behavior: int, direction: Facing) -> bool:
    """IsDirectionalStairWarpMetatileBehavior: the stair tile fires on east/west presses only."""
    info = behavior_info(behavior)
    return info.kind == "stairs" and info.warp_direction == direction
