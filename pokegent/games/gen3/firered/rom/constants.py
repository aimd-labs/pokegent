"""Game constants taken from the pret/pokefirered decompile.

Source: https://github.com/pret/pokefirered at ``PRET_COMMIT`` (also pinned in
``source-lock.md``). These are mechanics constants that do not change between FireRed USA
revisions; ROM-revision specific values (RAM addresses) live in ``references/rom_profiles.yaml``.
Every value names the header or source file it came from.
"""

from __future__ import annotations

PRET_COMMIT = "037335f4c725d7c9aecdac87066f2002b4bd7e14"

# include/constants/global.h
PARTY_SIZE = 6
MAX_MON_MOVES = 4
POKEMON_NAME_LENGTH = 10
PLAYER_NAME_LENGTH = 7

# include/pokemon.h: struct BoxPokemon (0x50) and struct Pokemon (0x64)
BOX_MON_SIZE = 80
PARTY_MON_SIZE = 100

# include/constants/species.h: NUM_SPECIES == SPECIES_EGG == 412; real species are 1..411
SPECIES_EGG = 412
NUM_SPECIES = SPECIES_EGG
# include/constants/moves.h: MOVES_COUNT; include/constants/items.h: ITEMS_COUNT
MOVES_COUNT = 355
ITEMS_COUNT = 375
# include/constants/pokemon.h
MAX_LEVEL = 100
MAX_PER_STAT_EVS = 255
MAX_TOTAL_EVS = 510

# include/constants/flags.h (FLAGS_COUNT = FLAG_0x8FF + 1) and include/global.h
FLAGS_COUNT = 0x900
NUM_FLAG_BYTES = (FLAGS_COUNT + 7) // 8  # 0x120
SYS_FLAGS = 0x800
FLAG_BADGE01_GET = SYS_FLAGS + 0x20
NUM_BADGES = 8
# include/constants/trainers.h
TRAINER_CLASS_LEADER = 84
TRAINER_CLASS_ELITE_FOUR = 87
FLAG_SYS_POKEMON_GET = SYS_FLAGS + 0x28
FLAG_SYS_POKEDEX_GET = SYS_FLAGS + 0x29
FLAG_SYS_GAME_CLEAR = SYS_FLAGS + 0x2C
FLAG_SYS_NOT_SOMEONES_PC = SYS_FLAGS + 0x34

# include/constants/vars.h: VARS_START .. VARS_END, u16 each
VARS_START = 0x4000
VARS_END = 0x40FF
VARS_COUNT = VARS_END - VARS_START + 1  # 0x100

# src/money.c MAX_MONEY, include/constants/coins.h MAX_COINS, src/item.c (quantity <= 999)
MAX_MONEY = 999_999
MAX_COINS = 9_999
MAX_BAG_QUANTITY = 999

# include/constants/global.h: POCKET_* (1-based ids) and BAG_*_COUNT capacities
BAG_POCKETS: tuple[tuple[str, str, int], ...] = (
    # (pocket name, SaveBlock1 field, capacity)
    ("items", "bagPocket_Items", 42),
    ("key_items", "bagPocket_KeyItems", 30),
    ("poke_balls", "bagPocket_PokeBalls", 13),
    ("tms_hms", "bagPocket_TMHM", 58),
    ("berries", "bagPocket_Berries", 43),
)

# include/constants/global.h: DIR_* (facing); the player faces one of these four
DIR_SOUTH = 1
DIR_NORTH = 2
DIR_WEST = 3
DIR_EAST = 4
FACING_BY_DIR = {DIR_SOUTH: "S", DIR_NORTH: "N", DIR_WEST: "W", DIR_EAST: "E"}

# include/global.fieldmap.h: PLAYER_AVATAR_FLAG_* (bit positions)
AVATAR_FLAG_ON_FOOT = 1 << 0
AVATAR_FLAG_MACH_BIKE = 1 << 1
AVATAR_FLAG_ACRO_BIKE = 1 << 2
AVATAR_FLAG_SURFING = 1 << 3
AVATAR_FLAG_UNDERWATER = 1 << 4
AVATAR_FLAG_CONTROLLABLE = 1 << 5
AVATAR_FLAG_FORCED = 1 << 6
AVATAR_FLAG_DASH = 1 << 7

# include/global.fieldmap.h: struct ObjectEvent (size 0x24)
OBJECT_EVENT_SIZE = 0x24
OBJECT_EVENTS_COUNT = 16  # include/constants/global.h: OBJECT_EVENTS_COUNT
OBJ_EVENT_FACING_BYTE = 0x18  # low nibble: facingDirection
OBJ_EVENT_ACTIVE_BIT = 1 << 0  # byte 0x00 bit 0: active
OBJ_EVENT_IS_PLAYER_BIT = 1 << 0  # byte 0x02 bit 0: isPlayer
# struct PlayerAvatar: flags @0x00, objectEventId @0x05
AVATAR_FLAGS_OFFSET = 0x00
AVATAR_OBJECT_EVENT_ID_OFFSET = 0x05
AVATAR_READ_SIZE = 8
