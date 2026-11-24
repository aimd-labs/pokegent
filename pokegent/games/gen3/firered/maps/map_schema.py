"""Typed map state for FireRed (spec 4.3 ``map`` block, 7.1 Tile / ObjectEvent, M2.2).

Everything here is a frozen Pydantic v2 model without any memory access, so the models can be
built by tests, serialized, hashed and consumed by the navigator without an emulator.

Coordinates
-----------
All coordinates are **map coordinates** ``(x, y)`` as the game's scripts and ``SaveBlock1.pos``
use them: the top-left tile of the map is ``(0, 0)``. The raw RAM values carry a ``+7`` offset
(``MAP_OFFSET``); that offset never appears in these models. The collision grid also covers the
7-tile ring around the map (where connected maps and the border show), i.e. ``x`` in
``-7 .. width + 7`` and ``y`` in ``-7 .. height + 7``.

Knowledge (discovery policy, spec 7.1)
--------------------------------------
Every fact carries ``knowledge``: ``observed`` when it lies inside the visible window around the
player or was reported as seen by the caller, ``technical`` when it was read from the ROM/RAM
tables but never observed. Higher layers decide which of the two a model may see; this layer only
tags (it never hides) so no information is lost.

Terrain facts come from the game's own tables: map grid block (10 bit metatile id, 2 bit
collision, 4 bit elevation) plus the tileset's metatile attributes (behavior, terrain,
encounter type, layer type). ``Tile.walkable`` is the *static* answer (collision bits only);
whether a particular step succeeds also depends on elevation, edges, ledges and objects. The game's
rules are evaluated by :func:`pokegent.games.gen3.firered.maps.map_motion.predict_step`.
"""

from __future__ import annotations

import hashlib
import json
from functools import cached_property, lru_cache
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pokegent.games.gen3.firered.maps import map_constants as K
from pokegent.games.gen3.firered.maps.map_behaviors import TileKind, WarpTrigger, behavior_info

MAP_SCHEMA_VERSION = "1.0"

Facing = Literal["N", "S", "W", "E"]
Knowledge = Literal["observed", "technical"]
Region = Literal["map", "connection", "border"]
ObjectRole = Literal[
    "npc", "trainer", "item_ball", "cut_tree", "rock_smash", "boulder", "berry_tree",
    "fossil", "snorlax", "clone", "other",
]  # fmt: skip


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# -- discovery policy ------------------------------------------------------------------------


class ViewWindow(_Frozen):
    """The tiles the screen shows around the player: 15 x 10 metatiles (240 x 160 px / 16).

    The player stands on screen column 7 and row 4 (0-based), so the window is ``x - 7 .. x + 7``
    and ``y - 4 .. y + 5``. Derived from the screen size and the avatar's fixed screen position;
    not verified against pixels (spec 7.1 only needs a conservative "visible area").
    """

    left: int = 7
    right: int = 7
    up: int = 4
    down: int = 5

    def contains(self, px: int, py: int, x: int, y: int) -> bool:
        return px - self.left <= x <= px + self.right and py - self.up <= y <= py + self.down


class DiscoveryPolicy(_Frozen):
    """Inputs of the observed/technical tagging. All coordinates are map coordinates.

    ``seen``: tiles the caller already observed on this map (world-state ``seen_tiles``).
    ``known_hidden``: invisible things the caller legitimately knows about (a hidden item it was
    told about, a trigger it walked over); hidden items and coordinate events are never
    "observed" just because they lie in view.
    """

    view: ViewWindow = Field(default_factory=ViewWindow)
    seen: frozenset[tuple[int, int]] = frozenset()
    known_hidden: frozenset[tuple[int, int]] = frozenset()


# -- identity, layout, grid -------------------------------------------------------------------


class MapIdentity(_Frozen):
    group: int
    number: int
    name: str | None = None  # pret map name (CamelCase constant), None for an unknown id
    group_name: str | None = None
    layout_id: int
    region_section_id: int
    map_type: int
    map_type_name: str
    weather: int
    cave: int
    floor_num: int
    battle_type: int
    music: int
    biking_allowed: bool
    allow_escaping: bool
    allow_running: bool
    # True when the map header copy in RAM equals the ROM header at gMapGroups[group][number].
    identity_verified: bool


def snapshot_map_identity(value: Any) -> MapIdentity | None:
    """Return the active ROM map-header identity from a snapshot or MapState."""
    state = getattr(value, "map", value)
    return getattr(state, "identity", None) if state is not None else None


class MapLayoutInfo(_Frozen):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    border_width: int = Field(gt=0)
    border_height: int = Field(gt=0)
    border: tuple[int, ...]  # border blocks (border_width * border_height raw u16)
    primary_tileset: int  # ROM address of struct Tileset
    secondary_tileset: int
    layout_ptr: int  # ROM address of struct MapLayout

    @model_validator(mode="after")
    def _border_size(self) -> MapLayoutInfo:
        if len(self.border) != self.border_width * self.border_height:
            raise ValueError("border block count must be border_width * border_height")
        return self


class Tile(_Frozen):
    """One map tile (spec 7.1 ``Tile``): terrain facts plus the events that sit on it."""

    x: int
    y: int
    region: Region
    metatile_id: int
    collision: int  # 2 bit collision value of the map grid block
    elevation: int  # 4 bit elevation of the block (0 for undefined blocks)
    undefined: bool  # block is MAPGRID_UNDEFINED: the border block shows, movement is blocked
    behavior: int
    behavior_name: str
    kind: TileKind
    terrain: str
    encounter: str  # none / land / water: tile attribute, not a guarantee
    walkable: bool  # collision == 0 and not water/waterfall: static answer on foot
    surfable: bool
    tall_grass: bool
    ledge: Facing | None
    forced_move: Facing | None
    ice: bool
    blocked_sides: tuple[Facing, ...]
    warp_trigger: WarpTrigger | None
    warp_direction: Facing | None
    interactive: str | None
    warp_index: int | None = None
    knowledge: Knowledge = "technical"


# -- player ------------------------------------------------------------------------------------


class PlayerPlacement(_Frozen):
    """Where the player is, from two sources that disagree for a few frames during warps.

    ``save_*``: ``SaveBlock1.pos`` (the camera focus). ``avatar_*``: the player's object event
    (``currentCoords - 7``). WP1c measured that ``pos`` leads the avatar by about 6 frames after a
    map change. ``x``/``y`` is the position to plan from: ``SaveBlock1.pos`` (the same source as the
    map identity), valid only when ``position_consistent``.
    """

    save_x: int
    save_y: int
    avatar_x: int
    avatar_y: int
    prev_x: int
    prev_y: int
    x: int
    y: int
    position_consistent: bool
    facing: Facing | None
    elevation: int
    object_event_id: int
    avatar_flags: int
    surfing: bool
    underwater: bool
    biking: bool
    controllable_flag: bool  # PLAYER_AVATAR_FLAG_CONTROLLABLE; not a "player can act" signal
    forced: bool
    moving: bool  # mid-step: tile transition running or previous != current coordinates
    tile_transition_state: int
    running_state: int


# -- events ------------------------------------------------------------------------------------


class WarpInfo(_Frozen):
    index: int
    x: int
    y: int
    elevation: int
    dest_group: int
    dest_number: int
    dest_name: str | None
    dest_warp_id: int
    dynamic: bool  # destination is MAP_DYNAMIC (127, 127): the game picks it at run time
    tile_behavior: int | None  # None if the tile is outside the grid
    tile_kind: TileKind | None
    trigger: WarpTrigger | None
    trigger_direction: Facing | None
    knowledge: Knowledge = "technical"
    exit: bool = True  # False: plain-tile warp with no trigger; the player cannot step on it


class CoordEventInfo(_Frozen):
    index: int
    x: int
    y: int
    elevation: int
    var_id: int  # trigger variable: the script runs when VarGet(var_id) == var_value
    var_value: int
    script_ptr: int
    weather_only: bool  # script is NULL: a weather coord event (no-op in FireRed)
    # VarGet(var_id) == var_value right now: stepping on the tile runs the script (and locks the
    # controls). None for weather events and when vars are unreadable (static maps).
    active: bool | None = None
    knowledge: Knowledge = "technical"


class HiddenItem(_Frozen):
    item_id: int
    flag_id: int  # FLAG_HIDDEN_ITEMS_START + stored byte
    flag_set: bool | None  # None when flags were not readable
    quantity: int
    underfoot: bool


class BgEventInfo(_Frozen):
    index: int
    x: int
    y: int
    elevation: int
    kind_id: int
    kind: Literal["sign", "hidden_item", "secret_base", "unknown"]
    sign_facing: Facing | None  # side the player must face; None = any side
    hidden_item: HiddenItem | None
    script_ptr: int | None
    knowledge: Knowledge = "technical"


class LiveObject(_Frozen):
    """Runtime state of an object event (one of the 16 ``gObjectEvents`` slots)."""

    slot: int
    local_id: int
    x: int
    y: int
    prev_x: int
    prev_y: int
    initial_x: int
    initial_y: int
    facing: Facing | None
    elevation: int
    invisible: bool
    off_screen: bool
    moving: bool
    range_x: int
    range_y: int
    map_group: int
    map_number: int
    graphics_id: int
    movement_type: int


class MapObject(_Frozen):
    """Spec 7.1 ``ObjectEvent`` = template (ROM) merged with the live slot (RAM), if spawned."""

    local_id: int
    graphics_id: (
        int  # the live graphics id when spawned (OBJ_EVENT_GFX_VAR_n resolved), else template
    )
    template_graphics_id: int | None = (
        None  # raw template value; >= 240 is a var, see map_constants
    )
    role: ObjectRole
    x: int  # current position when live, else the template position
    y: int
    elevation: int
    movement_type: int
    movement_type_name: str | None
    range_x: int
    range_y: int
    trainer_type: int
    trainer_range: int  # sight radius of a trainer; berry tree id for berry trees
    script_ptr: int | None
    hidden_flag_id: int  # template flagId; 0 = no flag. The object is NOT spawned while it is set.
    hidden_flag_set: bool | None  # None when flags were not readable
    source: Literal["template+live", "template", "live"]
    live: LiveObject | None = None
    blocks_tile: bool  # an active live object, or a static obstacle that is not flagged away
    clone_of: tuple[int, int, int] | None = None  # (local_id, map_group, map_number) for clones
    knowledge: Knowledge = "technical"

    @property
    def spawned(self) -> bool:
        return self.live is not None

    @property
    def blocks_navigation(self) -> bool:
        """Whether planning must reserve this object's ROM tile.

        Item balls are immobile ROM objects. Outside the camera spawn window they have no live
        slot, but approaching them respawns them on their template tile. Treating that temporary
        absence as an open path creates routes that fail as soon as the item enters the window.
        """
        return self.hidden_flag_set is not True and (self.blocks_tile or self.role == "item_ball")


class ConnectionInfo(_Frozen):
    direction: Literal["N", "S", "W", "E", "dive", "emerge"]
    offset: int
    dest_group: int
    dest_number: int
    dest_name: str | None
    knowledge: Knowledge = "technical"


# -- grid + state ------------------------------------------------------------------------------


class MapGrid(_Frozen):
    """The game's backup map grid (``VMap`` / ``gBackupMapData``), ``columns x rows`` raw u16.

    Map coordinate ``(x, y)`` is at column ``x + 7`` and row ``y + 7``. Cells without a block
    (``0x3FF``) show the border; connected maps are filled in by the game.
    """

    columns: int
    rows: int
    blocks: tuple[int, ...]
    # metatile id -> u32 attributes, for every metatile that appears in the grid or the border
    attributes: tuple[tuple[int, int], ...]

    @model_validator(mode="after")
    def _shape(self) -> MapGrid:
        if len(self.blocks) != self.columns * self.rows:
            raise ValueError("blocks must hold columns * rows entries")
        return self

    @cached_property
    def attribute_map(self) -> dict[int, int]:
        return dict(self.attributes)

    def model_copy(self, *, update=None, deep=False):
        copied = super().model_copy(update=update, deep=deep)
        if update and "attributes" in update:
            copied.__dict__.pop("attribute_map", None)
        return copied


class MapState(_Frozen):
    schema_version: Literal["1.0"] = MAP_SCHEMA_VERSION  # type: ignore[assignment]
    identity: MapIdentity
    layout: MapLayoutInfo
    grid: MapGrid
    # None for a static (ROM-only) map: no player, no live objects, no connection ring.
    player: PlayerPlacement | None = None
    warps: tuple[WarpInfo, ...] = ()
    coord_events: tuple[CoordEventInfo, ...] = ()
    bg_events: tuple[BgEventInfo, ...] = ()
    objects: tuple[MapObject, ...] = ()
    connections: tuple[ConnectionInfo, ...] = ()
    view: ViewWindow = Field(default_factory=ViewWindow)
    seen_tiles: int = 0  # how many caller-supplied seen tiles were applied (diagnostic)
    strength_active: bool | None = None  # FLAG_SYS_USE_STRENGTH (None: flags unreadable)
    flash_level: int | None = None  # SaveBlock1.flashLevel (0: lit, >0: dark)

    # -- geometry ------------------------------------------------------------------------------

    def in_grid(self, x: int, y: int) -> bool:
        return 0 <= x + K.MAP_OFFSET < self.grid.columns and 0 <= y + K.MAP_OFFSET < self.grid.rows

    @cached_property
    def _tile_cache(self) -> dict[tuple[int, int], tuple[int, int, int | None]]:
        return {}

    def model_copy(self, *, update=None, deep=False):
        copied = super().model_copy(update=update, deep=deep)
        if update and ("grid" in update or "layout" in update):
            copied.__dict__.pop("_tile_cache", None)
        return copied

    def raw_block(self, x: int, y: int) -> int:
        """``GetMapGridBlockAt``: the grid block, or the border block (collision forced) outside."""
        cache = self._tile_cache
        key = (x, y)
        if key not in cache:
            if self.in_grid(x, y):
                block = self.grid.blocks[(y + K.MAP_OFFSET) * self.grid.columns + x + K.MAP_OFFSET]
            else:
                block = self._border_block(x, y)
            cache[key] = (block, -1, None)
        return cache[key][0]

    def _border_block(self, x: int, y: int) -> int:
        lay = self.layout
        bx = (x % lay.border_width) if lay.border_width else 0
        by = (y % lay.border_height) if lay.border_height else 0
        return lay.border[bx + by * lay.border_width] | K.MAPGRID_COLLISION_MASK

    def metatile_id(self, x: int, y: int) -> int:
        key = (x, y)
        entry = self._tile_cache.get(key)
        if entry is None:
            self.raw_block(x, y)
            entry = self._tile_cache[key]
        block, mid, attrs = entry
        if mid < 0:
            mid = (self._border_block(x, y) if block == K.MAPGRID_UNDEFINED else block)
            mid &= K.MAPGRID_METATILE_ID_MASK
            self._tile_cache[key] = (block, mid, attrs)
        return mid

    def collision(self, x: int, y: int) -> int:
        """``MapGridGetCollisionAt``: undefined blocks are impassable (3)."""
        block = self.raw_block(x, y)
        if block == K.MAPGRID_UNDEFINED:
            return 1
        return (block & K.MAPGRID_COLLISION_MASK) >> K.MAPGRID_COLLISION_SHIFT

    def elevation(self, x: int, y: int) -> int:
        """``MapGridGetElevationAt``: 0 for undefined blocks."""
        block = self.raw_block(x, y)
        if block == K.MAPGRID_UNDEFINED:
            return 0
        return block >> K.MAPGRID_ELEVATION_SHIFT

    def attributes(self, x: int, y: int) -> int:
        """u32 metatile attributes of the tile; ``0xFF`` behavior for ids >= 1024 (game rule)."""
        key = (x, y)
        entry = self._tile_cache.get(key)
        if entry is None:
            self.raw_block(x, y)
            entry = self._tile_cache[key]
        block, mid, attrs = entry
        if attrs is None:
            if mid < 0:
                mid = (self._border_block(x, y) if block == K.MAPGRID_UNDEFINED else block)
                mid &= K.MAPGRID_METATILE_ID_MASK
            attrs = (K.ATTR_UNKNOWN_BEHAVIOR if mid >= K.NUM_METATILES_TOTAL
                     else self.grid.attribute_map.get(mid, 0))
            self._tile_cache[key] = (block, mid, attrs)
        return attrs

    def behavior(self, x: int, y: int) -> int:
        """``MapGridGetMetatileBehaviorAt``."""
        return self.attributes(x, y) & K.ATTR_BEHAVIOR_MASK

    @cached_property
    def connection_flags(self) -> frozenset[str]:
        return frozenset(c.direction for c in self.connections)

    def border_id(self, x: int, y: int) -> int:
        """``GetMapBorderIdAt``: -1 invalid, 0 inside the map, 1..4 = S/N/W/E connection ring."""
        if self.raw_block(x, y) == K.MAPGRID_UNDEFINED:
            return -1
        vx, vy = x + K.MAP_OFFSET, y + K.MAP_OFFSET
        if vx >= self.grid.columns - (K.MAP_OFFSET + 1):
            return 4 if "E" in self.connection_flags else -1
        if vx < K.MAP_OFFSET:
            return 3 if "W" in self.connection_flags else -1
        if vy >= self.grid.rows - K.MAP_OFFSET:
            return 1 if "S" in self.connection_flags else -1
        if vy < K.MAP_OFFSET:
            return 2 if "N" in self.connection_flags else -1
        return 0

    # -- tiles ---------------------------------------------------------------------------------

    def region(self, x: int, y: int) -> Region:
        if 0 <= x < self.layout.width and 0 <= y < self.layout.height:
            return "map"
        if self.in_grid(x, y) and self.raw_block(x, y) != K.MAPGRID_UNDEFINED:
            return "connection"
        return "border"

    def tile(self, x: int, y: int, policy: DiscoveryPolicy | None = None) -> Tile:
        """The :class:`Tile` at map coordinates ``(x, y)`` (any coordinate; outside the grid it is
        the border). ``knowledge`` follows ``policy`` (default: the state's view window)."""
        block = self.raw_block(x, y)
        undefined = block == K.MAPGRID_UNDEFINED
        behavior = self.behavior(x, y)
        info = behavior_info(behavior)
        attrs = self.attributes(x, y)
        collision = self.collision(x, y)
        terrain = K.TERRAIN_NAMES.get((attrs & K.ATTR_TERRAIN_MASK) >> K.ATTR_TERRAIN_SHIFT, "?")
        encounter = K.ENCOUNTER_NAMES.get(
            (attrs & K.ATTR_ENCOUNTER_MASK) >> K.ATTR_ENCOUNTER_SHIFT, "?"
        )
        warp_index = next((w.index for w in self.warps if (w.x, w.y) == (x, y)), None)
        view_policy = policy or DiscoveryPolicy(view=self.view)
        known = (x, y) in view_policy.seen or (
            self.player is not None
            and view_policy.view.contains(self.player.x, self.player.y, x, y)
        )
        return Tile(
            x=x,
            y=y,
            region=self.region(x, y),
            metatile_id=self.metatile_id(x, y),
            collision=collision,
            elevation=self.elevation(x, y),
            undefined=undefined,
            behavior=behavior,
            behavior_name=info.name,
            kind=info.kind,
            terrain=terrain,
            encounter=encounter,
            walkable=collision == 0 and not info.surfable and not info.waterfall,
            surfable=info.surfable,
            tall_grass=info.tall_grass,
            ledge=info.ledge,
            forced_move=info.forced,
            ice=info.ice,
            blocked_sides=info.blocked_sides,
            warp_trigger=info.warp_trigger,
            warp_direction=info.warp_direction,
            interactive=info.interactive,
            warp_index=warp_index,
            knowledge="observed" if known else "technical",
        )

    def tiles(self, *, include_ring: bool = True) -> list[Tile]:
        """All tiles of the grid (the map, plus its 7-tile ring when ``include_ring``)."""
        if include_ring:
            xs = range(-K.MAP_OFFSET, self.grid.columns - K.MAP_OFFSET)
            ys = range(-K.MAP_OFFSET, self.grid.rows - K.MAP_OFFSET)
        else:
            xs, ys = range(self.layout.width), range(self.layout.height)
        return [self.tile(x, y) for y in ys for x in xs]

    # -- objects -------------------------------------------------------------------------------

    def blocker_positions(self) -> dict[tuple[int, int], list[MapObject]]:
        """Tiles blocked by objects: active live objects at their current *and* previous tile
        (``DoesObjectCollideWithObjectAt``), plus unspawned static obstacles at their template
        tile."""
        out: dict[tuple[int, int], list[MapObject]] = {}
        for obj in self.objects:
            if not obj.blocks_navigation:
                continue
            spots = {(obj.x, obj.y)}
            if obj.live is not None:
                spots.add((obj.live.prev_x, obj.live.prev_y))
            for spot in spots:
                out.setdefault(spot, []).append(obj)
        return out

    def object_by_local_id(self, local_id: int) -> MapObject | None:
        return next((o for o in self.objects if o.local_id == local_id), None)

    # -- hashing -------------------------------------------------------------------------------

    @cached_property
    def digest(self) -> str:
        """sha256 over the game-relevant content (no knowledge tags, no policy inputs)."""
        values = {
            "schema_version": self.schema_version,
            "identity": self.identity,
            "layout": self.layout,
            "grid": self.grid,
            "player": self.player,
            "warps": self.warps,
            "coord_events": self.coord_events,
            "bg_events": self.bg_events,
            "objects": self.objects,
            "connections": self.connections,
            "strength_active": self.strength_active,
        }
        # Most map blocks are identical across hundreds of walking snapshots. Cache their
        # canonical fragments independently; only the small player/object fragments churn.
        text = (
            "{"
            + ",".join(
                json.dumps(key, ensure_ascii=True) + ":" + _canonical_fragment(values[key])
                for key in sorted(values)
            )
            + "}"
        )
        return "sha256:" + hashlib.sha256(text.encode("ascii")).hexdigest()


_DROP_KEYS = frozenset({"knowledge", "view", "seen_tiles"})


def _strip(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k not in _DROP_KEYS}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


@lru_cache(maxsize=2048)
def _canonical_fragment(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    elif isinstance(value, tuple):
        value = [
            item.model_dump(mode="json") if isinstance(item, BaseModel) else item for item in value
        ]
    return json.dumps(_strip(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
