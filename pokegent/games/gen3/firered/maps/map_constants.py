"""Map constants from the pret/pokefirered decompile (map data, WP M2.2).

Source: https://github.com/pret/pokefirered at ``PRET_COMMIT`` (also pinned in ``source-lock.md``).
Every block names the header it came from. Mechanics constants only: ROM-revision specific
addresses live in ``references/rom_profiles.yaml``.
"""

from __future__ import annotations

from pokegent.games.gen3.firered.rom.constants import (
    PRET_COMMIT,  # noqa: F401  (re-exported for provenance)
)

# include/fieldmap.h
MAP_OFFSET = 7
MAP_OFFSET_W = MAP_OFFSET * 2 + 1  # 15: the backup grid is (width + 15) x (height + 14)
MAP_OFFSET_H = MAP_OFFSET * 2  # 14
NUM_METATILES_IN_PRIMARY = 640
NUM_METATILES_TOTAL = 1024
MAX_MAP_DATA_SIZE = 0x2800

# include/global.fieldmap.h: map grid block = 10 bit metatile id, 2 bit collision, 4 bit elevation
MAPGRID_METATILE_ID_MASK = 0x03FF
MAPGRID_COLLISION_MASK = 0x0C00
MAPGRID_ELEVATION_MASK = 0xF000
MAPGRID_COLLISION_SHIFT = 10
MAPGRID_ELEVATION_SHIFT = 12
MAPGRID_UNDEFINED = MAPGRID_METATILE_ID_MASK

# src/fieldmap.c sMetatileAttrMasks / sMetatileAttrShifts (u32 per metatile in FireRed)
ATTR_BEHAVIOR_MASK, ATTR_BEHAVIOR_SHIFT = 0x000001FF, 0
ATTR_TERRAIN_MASK, ATTR_TERRAIN_SHIFT = 0x00003E00, 9
ATTR_ENCOUNTER_MASK, ATTR_ENCOUNTER_SHIFT = 0x07000000, 24
ATTR_LAYER_MASK, ATTR_LAYER_SHIFT = 0x60000000, 29
ATTR_UNKNOWN_BEHAVIOR = 0xFF  # GetAttributeByMetatileIdAndMapLayout for metatile >= 1024

# include/global.fieldmap.h: TILE_TERRAIN_*, TILE_ENCOUNTER_*
TERRAIN_NAMES = {0: "normal", 1: "grass", 2: "water", 3: "waterfall"}
ENCOUNTER_NAMES = {0: "none", 1: "land", 2: "water"}

# include/constants/global.h: DIR_* and CONNECTION_*
DIR_NONE, DIR_SOUTH, DIR_NORTH, DIR_WEST, DIR_EAST = 0, 1, 2, 3, 4
FACING_FROM_DIR = {DIR_SOUTH: "S", DIR_NORTH: "N", DIR_WEST: "W", DIR_EAST: "E"}
DIR_FROM_FACING = {v: k for k, v in FACING_FROM_DIR.items()}
VECTOR = {"S": (0, 1), "N": (0, -1), "W": (-1, 0), "E": (1, 0)}
OPPOSITE = {"S": "N", "N": "S", "W": "E", "E": "W"}
CONNECTION_NAMES = {1: "S", 2: "N", 3: "W", 4: "E", 5: "dive", 6: "emerge"}

# include/constants/event_bg.h
BG_EVENT_PLAYER_FACING_ANY = 0
BG_EVENT_PLAYER_FACING_NORTH, BG_EVENT_PLAYER_FACING_SOUTH = 1, 2
BG_EVENT_PLAYER_FACING_EAST, BG_EVENT_PLAYER_FACING_WEST = 3, 4
BG_EVENT_HIDDEN_ITEM = 7
BG_EVENT_SECRET_BASE = 8
BG_EVENT_SIGN_FACING = {0: None, 1: "N", 2: "S", 3: "E", 4: "W"}  # None = readable from any side

# include/constants/flags.h
FLAG_HIDDEN_ITEMS_START = 1000
FLAG_SYS_USE_STRENGTH = 0x800 + 0x5
# include/global.fieldmap.h hidden item bit layout: item 16 | flag 8 | quantity 7 | underfoot 1

# include/constants/event_objects.h
OBJ_KIND_NORMAL = 0
OBJ_KIND_CLONE = 255
OBJ_EVENT_GFX_ITEM_BALL = 92
OBJ_EVENT_GFX_CUT_TREE = 95
OBJ_EVENT_GFX_ROCK_SMASH_ROCK = 96
OBJ_EVENT_GFX_PUSHABLE_BOULDER = 97
OBJ_EVENT_GFX_FOSSIL = 98
OBJ_EVENT_GFX_SNORLAX = 109
OBJ_EVENT_GFX_NURSE = 64
OBJ_EVENT_GFX_CLERK = 68
OBJ_EVENT_GFX_VARS = 240  # OBJ_EVENT_GFX_VAR_0..F: resolved through a game var at spawn time
# include/constants/trainer_types.h
TRAINER_TYPE_NONE, TRAINER_TYPE_NORMAL, TRAINER_TYPE_SEE_ALL_DIRECTIONS, TRAINER_TYPE_BURIED = (
    0,
    1,
    2,
    3,
)
# include/constants/event_object_movement.h (selected; the raw id is always kept as well)
MOVEMENT_TYPE_NONE = 0x00
MOVEMENT_TYPE_BERRY_TREE_GROWTH = 0x0C
MOVEMENT_TYPE_NAMES = {
    0x00: "none",
    0x01: "look_around",
    0x02: "wander_around",
    0x03: "wander_up_and_down",
    0x04: "wander_down_and_up",
    0x05: "wander_left_and_right",
    0x06: "wander_right_and_left",
    0x07: "face_up",
    0x08: "face_down",
    0x09: "face_left",
    0x0A: "face_right",
    0x0B: "player",
    0x0C: "berry_tree_growth",
    0x0D: "face_down_and_up",
    0x0E: "face_left_and_right",
    0x19: "walk_up_and_down",
    0x1A: "walk_down_and_up",
    0x1B: "walk_left_and_right",
    0x1C: "walk_right_and_left",
    0x35: "copy_player",
}

# include/constants/map_types.h
MAP_TYPE_NAMES = {
    0: "none",
    1: "town",
    2: "city",
    3: "route",
    4: "underground",
    5: "underwater",
    6: "ocean_route",
    7: "unknown",
    8: "indoor",
    9: "secret_base",
}

# include/constants/metatile_behaviors.h (complete list of the pinned commit)
MB: dict[str, int] = {
    "MB_NORMAL": 0x00,
    "MB_UNUSED_01": 0x01,
    "MB_TALL_GRASS": 0x02,
    "MB_CAVE": 0x08,
    "MB_RUNNING_DISALLOWED": 0x0A,
    "MB_INDOOR_ENCOUNTER": 0x0B,
    "MB_MOUNTAIN_TOP": 0x0C,
    "MB_POND_WATER": 0x10,
    "MB_FAST_WATER": 0x11,
    "MB_DEEP_WATER": 0x12,
    "MB_WATERFALL": 0x13,
    "MB_OCEAN_WATER": 0x15,
    "MB_PUDDLE": 0x16,
    "MB_SHALLOW_WATER": 0x17,
    "MB_UNDERWATER_BLOCKED_ABOVE": 0x19,
    "MB_UNUSED_WATER": 0x1A,
    "MB_CYCLING_ROAD_WATER": 0x1B,
    "MB_STRENGTH_BUTTON": 0x20,
    "MB_SAND": 0x21,
    "MB_SEAWEED": 0x22,
    "MB_ICE": 0x23,
    "MB_THIN_ICE": 0x26,
    "MB_CRACKED_ICE": 0x27,
    "MB_HOT_SPRINGS": 0x28,
    "MB_ROCK_STAIRS": 0x2A,
    "MB_SAND_CAVE": 0x2B,
    "MB_IMPASSABLE_EAST": 0x30,
    "MB_IMPASSABLE_WEST": 0x31,
    "MB_IMPASSABLE_NORTH": 0x32,
    "MB_IMPASSABLE_SOUTH": 0x33,
    "MB_IMPASSABLE_NORTHEAST": 0x34,
    "MB_IMPASSABLE_NORTHWEST": 0x35,
    "MB_IMPASSABLE_SOUTHEAST": 0x36,
    "MB_IMPASSABLE_SOUTHWEST": 0x37,
    "MB_JUMP_EAST": 0x38,
    "MB_JUMP_WEST": 0x39,
    "MB_JUMP_NORTH": 0x3A,
    "MB_JUMP_SOUTH": 0x3B,
    "MB_WALK_EAST": 0x40,
    "MB_WALK_WEST": 0x41,
    "MB_WALK_NORTH": 0x42,
    "MB_WALK_SOUTH": 0x43,
    "MB_SLIDE_EAST": 0x44,
    "MB_SLIDE_WEST": 0x45,
    "MB_SLIDE_NORTH": 0x46,
    "MB_SLIDE_SOUTH": 0x47,
    "MB_TRICK_HOUSE_PUZZLE_8_FLOOR": 0x48,
    "MB_EASTWARD_CURRENT": 0x50,
    "MB_WESTWARD_CURRENT": 0x51,
    "MB_NORTHWARD_CURRENT": 0x52,
    "MB_SOUTHWARD_CURRENT": 0x53,
    "MB_SPIN_RIGHT": 0x54,
    "MB_SPIN_LEFT": 0x55,
    "MB_SPIN_UP": 0x56,
    "MB_SPIN_DOWN": 0x57,
    "MB_STOP_SPINNING": 0x58,
    "MB_CAVE_DOOR": 0x60,
    "MB_LADDER": 0x61,
    "MB_EAST_ARROW_WARP": 0x62,
    "MB_WEST_ARROW_WARP": 0x63,
    "MB_NORTH_ARROW_WARP": 0x64,
    "MB_SOUTH_ARROW_WARP": 0x65,
    "MB_FALL_WARP": 0x66,
    "MB_REGULAR_WARP": 0x67,
    "MB_LAVARIDGE_1F_WARP": 0x68,
    "MB_WARP_DOOR": 0x69,
    "MB_UP_ESCALATOR": 0x6A,
    "MB_DOWN_ESCALATOR": 0x6B,
    "MB_UP_RIGHT_STAIR_WARP": 0x6C,
    "MB_UP_LEFT_STAIR_WARP": 0x6D,
    "MB_DOWN_RIGHT_STAIR_WARP": 0x6E,
    "MB_DOWN_LEFT_STAIR_WARP": 0x6F,
    "MB_UNION_ROOM_WARP": 0x71,
    "MB_COUNTER": 0x80,
    "MB_BOOKSHELF": 0x81,
    "MB_POKEMART_SHELF": 0x82,
    "MB_PC": 0x83,
    "MB_SIGNPOST": 0x84,
    "MB_REGION_MAP": 0x85,
    "MB_TELEVISION": 0x86,
    "MB_POKEMON_CENTER_SIGN": 0x87,
    "MB_POKEMART_SIGN": 0x88,
    "MB_CABINET": 0x89,
    "MB_KITCHEN": 0x8A,
    "MB_DRESSER": 0x8B,
    "MB_SNACKS": 0x8C,
    "MB_CABLE_CLUB_WIRELESS_MONITOR": 0x8D,
    "MB_BATTLE_RECORDS": 0x8E,
    "MB_QUESTIONNAIRE": 0x8F,
    "MB_FOOD": 0x90,
    "MB_INDIGO_PLATEAU_SIGN_1": 0x91,
    "MB_INDIGO_PLATEAU_SIGN_2": 0x92,
    "MB_BLUEPRINTS": 0x93,
    "MB_PAINTING": 0x94,
    "MB_POWER_PLANT_MACHINE": 0x95,
    "MB_TELEPHONE": 0x96,
    "MB_COMPUTER": 0x97,
    "MB_ADVERTISING_POSTER": 0x98,
    "MB_FOOD_SMELLS_TASTY": 0x99,
    "MB_TRASH_BIN": 0x9A,
    "MB_CUP": 0x9B,
    "MB_PORTHOLE": 0x9C,
    "MB_WINDOW": 0x9D,
    "MB_BLINKING_LIGHTS": 0x9E,
    "MB_NEATLY_LINED_UP_TOOLS": 0x9F,
    "MB_IMPRESSIVE_MACHINE": 0xA0,
    "MB_VIDEO_GAME": 0xA1,
    "MB_BURGLARY": 0xA2,
    "MB_TRAINER_TOWER_MONITOR": 0xA3,
    "MB_CYCLING_ROAD_PULL_DOWN": 0xD0,
    "MB_CYCLING_ROAD_PULL_DOWN_GRASS": 0xD1,
}
MB_NAME: dict[int, str] = {value: name for name, value in MB.items()}
