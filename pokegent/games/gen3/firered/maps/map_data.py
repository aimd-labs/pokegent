"""Read the current map from RAM and ROM (spec 4.2 "Map-Events", 4.3 ``map`` block, 7.1; M2.2).

``MapReader.read()`` turns one coherent memory view (RAM + ROM bytes behind a
:class:`~pokegent.games.gen3.firered.reader.memory_port.MemoryPort`, normally an atomic emulator
snapshot) into a
typed :class:`~pokegent.games.gen3.firered.maps.map_schema.MapState`: identity, layout, the
collision grid
with tileset metatile attributes, the player's placement, warps, coordinate and background
events, object events (ROM templates merged with the live object slots) and map connections.

Sources, all by pointer (no per-map addresses are ever hard-coded):

* ``SaveBlock1.pos`` / ``location`` / ``mapLayoutId``: where the game thinks the player is.
* ``gMapHeader`` (EWRAM copy of the active ROM ``MapHeader``): which layout, events and
  connections are active. It is checked against the ROM header at ``gMapGroups[group][number]``;
  a mismatch (a map change in flight) is a read error, never a guess.
* ``VMap`` / ``gBackupMapData``: the game's own collision grid, ``(width + 15) x (height + 14)``
  blocks, with connected maps already filled in around the map.
* ROM: ``MapLayout``, the two ``Tileset`` attribute arrays (u32 per metatile), ``MapEvents``
  and its arrays, ``MapConnections``.
* ``gObjectEvents`` / ``gPlayerAvatar``: the 16 runtime object slots and the avatar.

Failure model (spec invariant 3/5): any unreadable range, bad pointer, impossible size or
inconsistent pair of sources yields ``MapReadResult.state is None`` with typed
:class:`~pokegent.games.gen3.firered.reader.schemas.ReadError` entries (subsystem ``map``).
Never a partial map.

Read-only: nothing here writes RAM or the ROM.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from pokegent.games.gen3.firered.maps import map_constants as K
from pokegent.games.gen3.firered.maps.map_behaviors import behavior_info
from pokegent.games.gen3.firered.maps.map_names import (
    group_count,
    group_name,
    group_sizes,
    map_name,
)
from pokegent.games.gen3.firered.maps.map_schema import (
    BgEventInfo,
    ConnectionInfo,
    CoordEventInfo,
    DiscoveryPolicy,
    HiddenItem,
    LiveObject,
    MapGrid,
    MapIdentity,
    MapLayoutInfo,
    MapObject,
    MapState,
    PlayerPlacement,
    WarpInfo,
)
from pokegent.games.gen3.firered.maps.obstacles import ROLES as FIELD_OBSTACLE_ROLES
from pokegent.games.gen3.firered.reader.memory_port import (
    EWRAM_END,
    EWRAM_START,
    MemoryPort,
    MemoryReadError,
)
from pokegent.games.gen3.firered.reader.schemas import ReadError
from pokegent.games.gen3.firered.rom.rom_data import ProfileError, RomProfile

# Interaction obstacles still occupy their ROM-template tile until hidden by their flag.
FIELD_OBSTACLE_ROLES = FIELD_OBSTACLE_ROLES | {"fossil"}

ROM_LO = 0x0800_0000
ROM_HI = 0x0A00_0000  # 32 MiB cartridge window
MAX_GRID_DIM = 0x200  # sanity: real layouts are far smaller (bound before multiplying)
MAX_OBJECT_TEMPLATES = 64
MAX_EVENTS = 255
MAX_CONNECTIONS = 8
K_OBJECT_COUNT = 16  # OBJECT_EVENTS_COUNT
K_OBJECT_SIZE = 0x24
K_AVATAR_READ_SIZE = 8
MAP_DYNAMIC = (127, 127)  # MAP_DYNAMIC: group 127, number 127 (the game picks the target)

_REQUIRED_SYMBOLS = (
    "gSaveBlock1Ptr",
    "gMapHeader",
    "VMap",
    "gBackupMapData",
    "gMapGroups",
    "gObjectEvents",
    "gPlayerAvatar",
)


class MapReadError(Exception):
    """Internal: a violated guard. ``code`` becomes ``ReadError.code``."""

    def __init__(self, code: str, message: str, addr: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.addr = addr


def _rom_ok(addr: int, size: int = 1) -> bool:
    return ROM_LO <= addr and addr + size <= ROM_HI


@dataclass(frozen=True)
class MapReadResult:
    """Outcome of a map read. ``state`` is None whenever ``errors`` is not empty."""

    state: MapState | None
    errors: tuple[ReadError, ...] = ()

    @property
    def usable(self) -> bool:
        return self.state is not None


@dataclass
class RomMapCache:
    """Cache of immutable ROM-derived data (layouts, attributes, events). One ROM image only."""

    layouts: dict = field(default_factory=dict)
    attributes: dict = field(default_factory=dict)
    events: dict = field(default_factory=dict)
    connections: dict = field(default_factory=dict)


@dataclass(frozen=True)
class _Header:
    layout_ptr: int
    events_ptr: int
    scripts_ptr: int
    connections_ptr: int
    music: int
    layout_id: int
    region_section_id: int
    cave: int
    weather: int
    map_type: int
    biking_allowed: bool
    allow_escaping: bool
    allow_running: bool
    floor_num: int
    battle_type: int


@dataclass(frozen=True)
class _Layout:
    width: int
    height: int
    border_ptr: int
    map_ptr: int
    primary_ptr: int
    secondary_ptr: int
    border_width: int
    border_height: int
    border: tuple[int, ...]


def _signed8(value: int) -> int:
    return value - 256 if value >= 128 else value


class _Rom:
    """Bounds-checked ROM reads through the port; failures become MapReadError."""

    def __init__(self, port: MemoryPort) -> None:
        self.port = port

    def read(self, addr: int, size: int, what: str) -> bytes:
        if not _rom_ok(addr, size):
            raise MapReadError("pointer_invalid", f"{what}: 0x{addr:08X}+{size} is not ROM", addr)
        try:
            return self.port.read_range(addr, size)
        except MemoryReadError as exc:
            raise MapReadError("rom_unreadable", f"{what}: {exc}", addr) from exc

    def u32(self, addr: int, what: str) -> int:
        return struct.unpack_from("<I", self.read(addr, 4, what))[0]


class MapReader:
    """Reads :class:`MapState` from a memory port for one supported ROM profile."""

    def __init__(
        self, port: MemoryPort, profile: RomProfile, *, cache: RomMapCache | None = None
    ) -> None:
        if not profile.supported:
            raise ProfileError(f"profile {profile.profile_id} is not supported; refusing to read")
        for name in _REQUIRED_SYMBOLS:
            profile.symbol(name)
        self._port = port
        self._profile = profile
        self._rom = _Rom(port)
        self.cache = cache if cache is not None else RomMapCache()

    # -- public ------------------------------------------------------------------------------

    def read(self, *, policy: DiscoveryPolicy | None = None) -> MapReadResult:
        """Read the active map. Never raises for bad game memory."""
        try:
            return MapReadResult(self._read(policy or DiscoveryPolicy()))
        except MapReadError as exc:
            return MapReadResult(None, (self._error(exc.code, exc.message, exc.addr),))
        except MemoryReadError as exc:
            return MapReadResult(None, (self._error("read_failed", str(exc), exc.addr),))

    # -- helpers -----------------------------------------------------------------------------

    @staticmethod
    def _error(code: str, message: str, addr: int | None = None) -> ReadError:
        return ReadError(subsystem="map", code=code, message=message, addr=addr)

    def _sym(self, name: str) -> int:
        return self._profile.symbol(name).address

    def _f(self, struct_name: str, field_name: str) -> int:
        return self._profile.field(struct_name, field_name).offset

    def _ram(self, addr: int, size: int, what: str) -> bytes:
        try:
            return self._port.read_range(addr, size)
        except MemoryReadError as exc:
            raise MapReadError("read_failed", f"{what}: {exc}", addr) from exc

    # -- the read ----------------------------------------------------------------------------

    def _read(self, policy: DiscoveryPolicy) -> MapState:
        profile = self._profile
        # 1. save block pointer and the player's saved position / location / layout id
        sb1_ptr = struct.unpack("<I", self._ram(self._sym("gSaveBlock1Ptr"), 4, "gSaveBlock1Ptr"))[
            0
        ]
        sb1_size = self._f("SaveBlock1", "vars") + 0x200
        if sb1_ptr % 4 or not (EWRAM_START <= sb1_ptr and sb1_ptr + sb1_size <= EWRAM_END):
            raise MapReadError("sb1_pointer_invalid", f"gSaveBlock1Ptr=0x{sb1_ptr:08X}", sb1_ptr)
        pos_off, loc_off = self._f("SaveBlock1", "pos"), self._f("SaveBlock1", "location")
        layout_off = self._f("SaveBlock1", "mapLayoutId")
        head_size = max(layout_off + 2, self._f("SaveBlock1", "flashLevel") + 1)
        head = self._ram(sb1_ptr, head_size, "SaveBlock1 head")
        save_x, save_y = struct.unpack_from("<hh", head, pos_off)
        group, number = head[loc_off], head[loc_off + 1]
        sb1_layout_id = struct.unpack_from("<H", head, layout_off)[0]
        flag_off, flag_size = (
            self._f("SaveBlock1", "flags"),
            profile.field("SaveBlock1", "flags").size,
        )
        flags_raw = self._ram(sb1_ptr + flag_off, flag_size, "SaveBlock1 flags")
        vars_off, vars_size = (
            self._f("SaveBlock1", "vars"),
            profile.field("SaveBlock1", "vars").size,
        )
        vars_raw = self._ram(sb1_ptr + vars_off, vars_size, "SaveBlock1 vars")

        # 2. RAM: map header copy + object events + avatar (one span), VMap
        header_addr = self._sym("gMapHeader")
        objs_addr, avatar_addr = self._sym("gObjectEvents"), self._sym("gPlayerAvatar")
        span_end = max(objs_addr + K_OBJECT_COUNT * K_OBJECT_SIZE, avatar_addr + K_AVATAR_READ_SIZE)
        if not header_addr < objs_addr <= avatar_addr:
            raise ProfileError(
                "profile symbol order gMapHeader < gObjectEvents < gPlayerAvatar broken"
            )
        span = self._ram(header_addr, span_end - header_addr, "gMapHeader..gPlayerAvatar")
        header_raw = span[:0x1C]
        objs_raw = span[
            objs_addr - header_addr : objs_addr - header_addr + K_OBJECT_COUNT * K_OBJECT_SIZE
        ]
        avatar_raw = span[
            avatar_addr - header_addr : avatar_addr - header_addr + K_AVATAR_READ_SIZE
        ]
        vmap = self._ram(self._sym("VMap"), 12, "VMap")
        columns, rows, grid_ptr = struct.unpack("<iiI", vmap)

        header = self._parse_header(header_raw)

        # 3. identity: the RAM header copy must be the ROM header of (group, number)
        identity_verified = self._verify_identity(group, number, header_raw)
        if header.layout_id != sb1_layout_id:
            raise MapReadError(
                "layout_id_mismatch",
                f"gMapHeader.mapLayoutId {header.layout_id} != SaveBlock1.mapLayoutId "
                f"{sb1_layout_id} (map change in flight?)",
            )

        # 4. layout (ROM) and its consistency with VMap
        layout = self._layout(header.layout_ptr)
        if (columns, rows) != (layout.width + K.MAP_OFFSET_W, layout.height + K.MAP_OFFSET_H):
            raise MapReadError(
                "vmap_mismatch",
                f"VMap {columns}x{rows} != layout {layout.width}x{layout.height} + "
                f"({K.MAP_OFFSET_W},{K.MAP_OFFSET_H}) (map change in flight?)",
            )
        if columns * rows > K.MAX_MAP_DATA_SIZE:
            raise MapReadError("vmap_too_large", f"VMap {columns}x{rows} exceeds gBackupMapData")
        expected_grid = self._sym("gBackupMapData")
        if grid_ptr != expected_grid:
            raise MapReadError(
                "grid_pointer_mismatch",
                f"VMap.map=0x{grid_ptr:08X} != gBackupMapData 0x{expected_grid:08X}",
                grid_ptr,
            )
        blocks = struct.unpack(
            f"<{columns * rows}H", self._ram(grid_ptr, columns * rows * 2, "gBackupMapData")
        )

        # 5. tileset attributes for every metatile that appears
        attributes = self._attributes_for(layout, blocks)

        # 6. events and connections (ROM)
        events = self._events(header.events_ptr)
        connections = self._connections(header.connections_ptr)

        # 7. player + live objects
        player, live_objects = self._player_and_objects(
            avatar_raw, objs_raw, save_x, save_y, layout
        )
        if not (0 <= save_x + K.MAP_OFFSET < columns and 0 <= save_y + K.MAP_OFFSET < rows):
            raise MapReadError("player_out_of_grid", f"SaveBlock1.pos ({save_x},{save_y})")

        flag_set = _SaveFacts(flags_raw, vars_raw)
        grid = MapGrid(columns=columns, rows=rows, blocks=tuple(blocks), attributes=attributes)
        identity = self._identity(group, number, header, identity_verified)
        layout_info = self._layout_info(header, layout)
        partial = MapState(
            identity=identity,
            layout=layout_info,
            grid=grid,
            player=player,
            flash_level=head[self._f("SaveBlock1", "flashLevel")],
        )
        return self._assemble(
            partial, events, connections, live_objects, flag_set, policy, (group, number)
        )

    @staticmethod
    def _identity(group: int, number: int, header: _Header, verified: bool) -> MapIdentity:
        return MapIdentity(
            group=group,
            number=number,
            name=map_name(group, number),
            group_name=group_name(group),
            layout_id=header.layout_id,
            region_section_id=header.region_section_id,
            map_type=header.map_type,
            map_type_name=K.MAP_TYPE_NAMES.get(header.map_type, "?"),
            weather=header.weather,
            cave=header.cave,
            floor_num=header.floor_num,
            battle_type=header.battle_type,
            music=header.music,
            biking_allowed=header.biking_allowed,
            allow_escaping=header.allow_escaping,
            allow_running=header.allow_running,
            identity_verified=verified,
        )

    @staticmethod
    def _layout_info(header: _Header, layout: _Layout) -> MapLayoutInfo:
        return MapLayoutInfo(
            width=layout.width,
            height=layout.height,
            border_width=layout.border_width,
            border_height=layout.border_height,
            border=layout.border,
            primary_tileset=layout.primary_ptr,
            secondary_tileset=layout.secondary_ptr,
            layout_ptr=header.layout_ptr,
        )

    def read_static(self, group: int, number: int) -> MapReadResult:
        """Any map from the ROM alone (no RAM state, no player, no live objects).

        The grid is the layout's own map data inside an all-undefined ring: connected maps are
        not filled in (the game does that at load time), and ``player`` is None. Flags are not
        applied (``hidden_flag_set`` is None). Use it to plan routes into maps not yet entered.
        """
        try:
            return MapReadResult(self._read_static(group, number))
        except MapReadError as exc:
            return MapReadResult(None, (self._error(exc.code, exc.message, exc.addr),))
        except MemoryReadError as exc:
            return MapReadResult(None, (self._error("read_failed", str(exc), exc.addr),))

    def _read_static(self, group: int, number: int) -> MapState:
        address = self.rom_header_address(group, number)
        if address is None:
            raise MapReadError("unknown_map", f"map ({group}, {number}) is not in the group table")
        header = self._parse_header(self._rom.read(address, 0x1C, f"MapHeader({group},{number})"))
        layout = self._layout(header.layout_ptr)
        columns, rows = layout.width + K.MAP_OFFSET_W, layout.height + K.MAP_OFFSET_H
        if columns * rows > K.MAX_MAP_DATA_SIZE:
            raise MapReadError("vmap_too_large", f"map grid {columns}x{rows} exceeds the game's")
        raw = self._rom.read(layout.map_ptr, layout.width * layout.height * 2, "map blocks")
        map_blocks = struct.unpack(f"<{layout.width * layout.height}H", raw)
        blocks = [K.MAPGRID_UNDEFINED] * (columns * rows)
        for row in range(layout.height):
            start = (row + K.MAP_OFFSET) * columns + K.MAP_OFFSET
            blocks[start : start + layout.width] = map_blocks[
                row * layout.width : (row + 1) * layout.width
            ]
        grid = MapGrid(
            columns=columns,
            rows=rows,
            blocks=tuple(blocks),
            attributes=self._attributes_for(layout, blocks),
        )
        base = MapState(
            identity=self._identity(group, number, header, True),
            layout=self._layout_info(header, layout),
            grid=grid,
        )
        return self._assemble(
            base,
            self._events(header.events_ptr),
            self._connections(header.connections_ptr),
            [],
            _SaveFacts(b"", b""),
            DiscoveryPolicy(),
            (group, number),
        )

    # -- header / identity -------------------------------------------------------------------

    def _parse_header(self, raw: bytes) -> _Header:
        f = self._f
        layout_ptr = struct.unpack_from("<I", raw, f("MapHeader", "mapLayout"))[0]
        events_ptr = struct.unpack_from("<I", raw, f("MapHeader", "events"))[0]
        scripts_ptr = struct.unpack_from("<I", raw, f("MapHeader", "mapScripts"))[0]
        conn_ptr = struct.unpack_from("<I", raw, f("MapHeader", "connections"))[0]
        if not _rom_ok(layout_ptr, 0x1C):
            raise MapReadError(
                "header_pointer_invalid", f"mapLayout=0x{layout_ptr:08X}", layout_ptr
            )
        if not _rom_ok(events_ptr, 0x14):
            raise MapReadError("header_pointer_invalid", f"events=0x{events_ptr:08X}", events_ptr)
        if conn_ptr and not _rom_ok(conn_ptr, 8):
            raise MapReadError("header_pointer_invalid", f"connections=0x{conn_ptr:08X}", conn_ptr)
        flags = raw[f("MapHeader", "flags")]
        return _Header(
            layout_ptr=layout_ptr,
            events_ptr=events_ptr,
            scripts_ptr=scripts_ptr,
            connections_ptr=conn_ptr,
            music=struct.unpack_from("<H", raw, f("MapHeader", "music"))[0],
            layout_id=struct.unpack_from("<H", raw, f("MapHeader", "mapLayoutId"))[0],
            region_section_id=raw[f("MapHeader", "regionMapSectionId")],
            cave=raw[f("MapHeader", "cave")],
            weather=raw[f("MapHeader", "weather")],
            map_type=raw[f("MapHeader", "mapType")],
            biking_allowed=bool(raw[f("MapHeader", "bikingAllowed")]),
            allow_escaping=bool(flags & 1),
            allow_running=bool(flags & 2),
            floor_num=_signed8(raw[f("MapHeader", "floorNum")]),
            battle_type=raw[f("MapHeader", "battleType")],
        )

    def rom_header_address(self, group: int, number: int) -> int | None:
        """ROM address of ``MapHeader`` ``(group, number)`` via ``gMapGroups``, or None if the id
        is outside the pret table. Reads two pointers from the ROM."""
        sizes = group_sizes()
        if not (0 <= group < group_count() and 0 <= number < sizes[group]):
            return None
        table = self._sym("gMapGroups")
        rom = self._rom
        base = rom.u32(table + 4 * group, "gMapGroups entry")
        header = rom.u32(base + 4 * number, "gMapGroup entry") if _rom_ok(base) else 0
        return header if _rom_ok(header, 0x1C) else None

    def _verify_identity(self, group: int, number: int, header_raw: bytes) -> bool:
        address = self.rom_header_address(group, number)
        if address is None:
            raise MapReadError(
                "unknown_map", f"map ({group}, {number}) is not in the map group table"
            )
        rom_header = self._rom.read(address, 0x1C, f"MapHeader({group},{number})")
        if rom_header != header_raw:
            raise MapReadError(
                "header_identity_mismatch",
                f"gMapHeader differs from the ROM header of ({group}, {number}) "
                "(map change in flight, or a corrupt location)",
                address,
            )
        return True

    # -- ROM structures ----------------------------------------------------------------------

    def _layout(self, ptr: int) -> _Layout:
        cached = self.cache.layouts.get(ptr)
        if cached is not None:
            return cached
        raw = self._rom.read(ptr, 0x1A, "MapLayout")
        f = self._f
        width = struct.unpack_from("<i", raw, f("MapLayout", "width"))[0]
        height = struct.unpack_from("<i", raw, f("MapLayout", "height"))[0]
        border_ptr = struct.unpack_from("<I", raw, f("MapLayout", "border"))[0]
        map_ptr = struct.unpack_from("<I", raw, f("MapLayout", "map"))[0]
        primary = struct.unpack_from("<I", raw, f("MapLayout", "primaryTileset"))[0]
        secondary = struct.unpack_from("<I", raw, f("MapLayout", "secondaryTileset"))[0]
        bw, bh = raw[f("MapLayout", "borderWidth")], raw[f("MapLayout", "borderHeight")]
        if not (0 < width <= MAX_GRID_DIM and 0 < height <= MAX_GRID_DIM):
            raise MapReadError("layout_size_invalid", f"layout size {width}x{height}", ptr)
        if not (0 < bw <= 16 and 0 < bh <= 16):
            raise MapReadError("layout_border_invalid", f"border size {bw}x{bh}", ptr)
        for name, p in (("border", border_ptr), ("map", map_ptr), ("tileset", primary)):
            if not _rom_ok(p):
                raise MapReadError("layout_pointer_invalid", f"{name}=0x{p:08X}", p)
        if not _rom_ok(secondary):
            raise MapReadError("layout_pointer_invalid", f"secondary=0x{secondary:08X}", secondary)
        border = struct.unpack(f"<{bw * bh}H", self._rom.read(border_ptr, bw * bh * 2, "border"))
        layout = _Layout(width, height, border_ptr, map_ptr, primary, secondary, bw, bh, border)
        self.cache.layouts[ptr] = layout
        return layout

    def _tileset_attributes(self, tileset_ptr: int, count: int) -> tuple[int, ...]:
        key = (tileset_ptr, count)
        cached = self.cache.attributes.get(key)
        if cached is not None:
            return cached
        raw = self._rom.read(tileset_ptr, 0x18, "Tileset")
        attr_ptr = struct.unpack_from("<I", raw, self._f("Tileset", "metatileAttributes"))[0]
        if not _rom_ok(attr_ptr, count * 4):
            raise MapReadError("tileset_pointer_invalid", f"attributes=0x{attr_ptr:08X}", attr_ptr)
        values = struct.unpack(f"<{count}I", self._rom.read(attr_ptr, count * 4, "attributes"))
        self.cache.attributes[key] = values
        return values

    def _attributes_for(
        self, layout: _Layout, blocks: tuple[int, ...] | list[int]
    ) -> tuple[tuple[int, int], ...]:
        primary = self._tileset_attributes(layout.primary_ptr, K.NUM_METATILES_IN_PRIMARY)
        secondary = self._tileset_attributes(
            layout.secondary_ptr, K.NUM_METATILES_TOTAL - K.NUM_METATILES_IN_PRIMARY
        )
        ids = {b & K.MAPGRID_METATILE_ID_MASK for b in blocks}
        ids.update(b & K.MAPGRID_METATILE_ID_MASK for b in layout.border)
        ids.discard(K.MAPGRID_UNDEFINED)  # never looked up: undefined cells use the border block
        out = []
        for mid in sorted(ids):
            if mid < K.NUM_METATILES_IN_PRIMARY:
                out.append((mid, primary[mid]))
            elif mid < K.NUM_METATILES_TOTAL:
                out.append((mid, secondary[mid - K.NUM_METATILES_IN_PRIMARY]))
        return tuple(out)

    def _events(self, ptr: int):
        cached = self.cache.events.get(ptr)
        if cached is not None:
            return cached
        rom, f = self._rom, self._f
        raw = rom.read(ptr, 0x14, "MapEvents")
        n_obj = raw[f("MapEvents", "objectEventCount")]
        n_warp = raw[f("MapEvents", "warpCount")]
        n_coord = raw[f("MapEvents", "coordEventCount")]
        n_bg = raw[f("MapEvents", "bgEventCount")]
        ptrs = {
            name: struct.unpack_from("<I", raw, f("MapEvents", name))[0]
            for name in ("objectEvents", "warps", "coordEvents", "bgEvents")
        }
        if n_obj > MAX_OBJECT_TEMPLATES:
            raise MapReadError("events_invalid", f"{n_obj} object events", ptr)
        sizes = {"objectEvents": (n_obj, 0x18), "warps": (n_warp, 8),
                 "coordEvents": (n_coord, 0x10), "bgEvents": (n_bg, 0x0C)}  # fmt: skip
        blobs: dict[str, bytes] = {}
        for name, (count, size) in sizes.items():
            if count and not _rom_ok(ptrs[name], count * size):
                raise MapReadError("events_invalid", f"{name}=0x{ptrs[name]:08X} x{count}", ptr)
            blobs[name] = rom.read(ptrs[name], count * size, name) if count else b""
        result = (blobs["objectEvents"], blobs["warps"], blobs["coordEvents"], blobs["bgEvents"])
        self.cache.events[ptr] = result
        return result

    def _connections(self, ptr: int) -> bytes:
        if not ptr:
            return b""
        cached = self.cache.connections.get(ptr)
        if cached is not None:
            return cached
        raw = self._rom.read(ptr, 8, "MapConnections")
        count, table = struct.unpack("<iI", raw)
        if not 0 <= count <= MAX_CONNECTIONS or (count and not _rom_ok(table, count * 12)):
            raise MapReadError("connections_invalid", f"count={count} table=0x{table:08X}", ptr)
        blob = self._rom.read(table, count * 12, "MapConnection[]") if count else b""
        self.cache.connections[ptr] = blob
        return blob

    # -- player and live objects -------------------------------------------------------------

    def _player_and_objects(
        self,
        avatar_raw: bytes,
        objs_raw: bytes,
        save_x: int,
        save_y: int,
        layout: _Layout,
    ) -> tuple[PlayerPlacement, list[LiveObject]]:
        flags = avatar_raw[0]
        running_state = avatar_raw[2]
        transition_state = avatar_raw[3]
        index = avatar_raw[5]
        if index >= K_OBJECT_COUNT:
            raise MapReadError("avatar_index_invalid", f"object event index {index}")
        f = self._f
        live: list[LiveObject] = []
        player_obj: bytes | None = None
        for slot in range(K_OBJECT_COUNT):
            raw = objs_raw[slot * K_OBJECT_SIZE : (slot + 1) * K_OBJECT_SIZE]
            if not raw[f("ObjectEvent", "flags0")] & 1:
                continue
            if slot == index:
                if not raw[f("ObjectEvent", "flags2")] & 1:
                    raise MapReadError(
                        "avatar_not_player", f"object event {slot} is not the player"
                    )
                player_obj = raw
                continue
            live.append(self._live_object(slot, raw))
        if player_obj is None:
            raise MapReadError("avatar_not_player", f"player object event {index} is not active")
        cx, cy = struct.unpack_from("<hh", player_obj, f("ObjectEvent", "currentCoords"))
        px, py = struct.unpack_from("<hh", player_obj, f("ObjectEvent", "previousCoords"))
        facing = K.FACING_FROM_DIR.get(player_obj[f("ObjectEvent", "directions")] & 0x0F)
        elevation = player_obj[f("ObjectEvent", "elevation")] & 0x0F
        avatar_x, avatar_y = cx - K.MAP_OFFSET, cy - K.MAP_OFFSET
        prev_x, prev_y = px - K.MAP_OFFSET, py - K.MAP_OFFSET
        single = bool(player_obj[f("ObjectEvent", "flags0")] & 2)
        placement = PlayerPlacement(
            save_x=save_x,
            save_y=save_y,
            avatar_x=avatar_x,
            avatar_y=avatar_y,
            prev_x=prev_x,
            prev_y=prev_y,
            x=save_x,
            y=save_y,
            position_consistent=(avatar_x, avatar_y) == (save_x, save_y),
            facing=facing,
            elevation=elevation,
            object_event_id=index,
            avatar_flags=flags,
            surfing=bool(flags & 0x08),
            underwater=bool(flags & 0x10),
            biking=bool(flags & 0x06),
            controllable_flag=bool(flags & 0x20),
            forced=bool(flags & 0x40),
            moving=single or (prev_x, prev_y) != (avatar_x, avatar_y) or transition_state != 0,
            tile_transition_state=transition_state,
            running_state=running_state,
        )
        return placement, live

    def _live_object(self, slot: int, raw: bytes) -> LiveObject:
        f = self._f
        cx, cy = struct.unpack_from("<hh", raw, f("ObjectEvent", "currentCoords"))
        px, py = struct.unpack_from("<hh", raw, f("ObjectEvent", "previousCoords"))
        ix, iy = struct.unpack_from("<hh", raw, f("ObjectEvent", "initialCoords"))
        rng = raw[f("ObjectEvent", "range")]
        flags0, flags1 = raw[f("ObjectEvent", "flags0")], raw[f("ObjectEvent", "flags1")]
        elevations = raw[f("ObjectEvent", "elevation")]
        return LiveObject(
            slot=slot,
            local_id=raw[f("ObjectEvent", "localId")],
            x=cx - K.MAP_OFFSET,
            y=cy - K.MAP_OFFSET,
            prev_x=px - K.MAP_OFFSET,
            prev_y=py - K.MAP_OFFSET,
            initial_x=ix - K.MAP_OFFSET,
            initial_y=iy - K.MAP_OFFSET,
            facing=K.FACING_FROM_DIR.get(raw[f("ObjectEvent", "directions")] & 0x0F),
            elevation=elevations & 0x0F,
            invisible=bool(flags1 & 0x20),
            off_screen=bool(flags1 & 0x40),
            moving=bool(flags0 & 2) or (cx, cy) != (px, py),
            range_x=rng & 0x0F,
            range_y=rng >> 4,
            map_group=raw[f("ObjectEvent", "mapGroup")],
            map_number=raw[f("ObjectEvent", "mapNum")],
            graphics_id=raw[f("ObjectEvent", "graphicsId")],
            movement_type=raw[f("ObjectEvent", "movementType")],
        )

    # -- assembly: events + live objects + knowledge ---------------------------------------

    def _assemble(
        self,
        base: MapState,
        events: tuple[bytes, bytes, bytes, bytes],
        connection_blob: bytes,
        live_objects: list,
        flag_set,
        policy: DiscoveryPolicy,
        here: tuple[int, int],
    ) -> MapState:
        player = base.player  # None for a static map
        obj_blob, warp_blob, coord_blob, bg_blob = events

        def in_view(x: int, y: int) -> bool:
            if (x, y) in policy.seen:
                return True
            return player is not None and policy.view.contains(player.x, player.y, x, y)

        def hidden_known(x: int, y: int) -> bool:
            return (x, y) in policy.known_hidden

        # warps
        warps: list[WarpInfo] = []
        for index in range(len(warp_blob) // 8):
            x, y, elevation, warp_id, num, grp = struct.unpack_from("<hhBBBB", warp_blob, index * 8)
            info = behavior_info(base.behavior(x, y)) if base.in_grid(x, y) else None
            warps.append(
                WarpInfo(
                    index=index,
                    x=x,
                    y=y,
                    elevation=elevation,
                    dest_group=grp,
                    dest_number=num,
                    dest_name=map_name(grp, num),
                    dest_warp_id=warp_id,
                    dynamic=(grp, num) == MAP_DYNAMIC,
                    tile_behavior=info.behavior if info else None,
                    tile_kind=info.kind if info else None,
                    trigger=info.warp_trigger if info else None,
                    trigger_direction=info.warp_direction if info else None,
                    knowledge="observed" if in_view(x, y) else "technical",
                )
            )

        # coordinate events
        coords: list[CoordEventInfo] = []
        for index in range(len(coord_blob) // 0x10):
            x, y, elevation, _pad, var_id, var_value, _pad2, script = struct.unpack_from(
                "<HHBBHHHI", coord_blob, index * 0x10
            )
            coords.append(
                CoordEventInfo(
                    index=index,
                    x=x,
                    y=y,
                    elevation=elevation,
                    var_id=var_id,
                    var_value=var_value,
                    script_ptr=script,
                    weather_only=script == 0,
                    active=(
                        None
                        if script == 0 or flag_set.var(var_id) is None
                        else flag_set.var(var_id) == var_value
                    ),
                    knowledge="observed" if hidden_known(x, y) else "technical",
                )
            )

        # background events
        bgs: list[BgEventInfo] = []
        for index in range(len(bg_blob) // 0x0C):
            x, y, elevation, kind_id = struct.unpack_from("<HHBB", bg_blob, index * 0x0C)
            body = bg_blob[index * 0x0C + 8 : index * 0x0C + 12]
            hidden = None
            script = None
            if kind_id == K.BG_EVENT_HIDDEN_ITEM:
                item, flag_byte, qty_byte = struct.unpack("<HBB", body)
                flag_id = K.FLAG_HIDDEN_ITEMS_START + flag_byte
                hidden = HiddenItem(
                    item_id=item,
                    flag_id=flag_id,
                    flag_set=flag_set(flag_id),
                    quantity=qty_byte & 0x7F,
                    underfoot=bool(qty_byte & 0x80),
                )
                kind = "hidden_item"
                known = hidden_known(x, y)
            elif kind_id in K.BG_EVENT_SIGN_FACING:
                script = struct.unpack("<I", body)[0]
                kind = "sign"
                known = in_view(x, y)
            elif kind_id == K.BG_EVENT_SECRET_BASE:
                kind, known = "secret_base", hidden_known(x, y)
            else:
                kind, known = "unknown", hidden_known(x, y)
            bgs.append(
                BgEventInfo(
                    index=index,
                    x=x,
                    y=y,
                    elevation=elevation,
                    kind_id=kind_id,
                    kind=kind,
                    sign_facing=K.BG_EVENT_SIGN_FACING.get(kind_id) if kind == "sign" else None,
                    hidden_item=hidden,
                    script_ptr=script,
                    knowledge="observed" if known else "technical",
                )
            )

        # connections
        conns: list[ConnectionInfo] = []
        layout = base.layout
        for index in range(len(connection_blob) // 12):
            direction, offset, grp, num = struct.unpack_from(
                "<B3xiBB2x", connection_blob, index * 12
            )
            name = K.CONNECTION_NAMES.get(direction)
            if name is None:
                raise MapReadError("connections_invalid", f"connection direction {direction}")
            conns.append(
                ConnectionInfo(
                    direction=name,
                    offset=offset,
                    dest_group=grp,
                    dest_number=num,
                    dest_name=map_name(grp, num),
                    knowledge="observed"
                    if self._edge_in_view(name, layout, player, policy)
                    else "technical",
                )
            )

        objects = self._merge_objects(obj_blob, live_objects, flag_set, here, in_view, base)
        return base.model_copy(
            update={
                "warps": tuple(warps),
                "coord_events": tuple(coords),
                "bg_events": tuple(bgs),
                "connections": tuple(conns),
                "objects": tuple(objects),
                "view": policy.view,
                "seen_tiles": len(policy.seen),
                "strength_active": flag_set(K.FLAG_SYS_USE_STRENGTH),
            }
        )

    @staticmethod
    def _edge_in_view(
        name: str, layout, player: PlayerPlacement | None, policy: DiscoveryPolicy
    ) -> bool:
        if player is None:
            return False
        v = policy.view
        if name == "N":
            return player.y - v.up <= 0
        if name == "S":
            return player.y + v.down >= layout.height - 1
        if name == "W":
            return player.x - v.left <= 0
        if name == "E":
            return player.x + v.right >= layout.width - 1
        return False

    def _merge_objects(
        self, obj_blob, live_objects, flag_set, here, in_view, base
    ) -> list[MapObject]:
        """Templates (ROM) first, each merged with its live slot; live-only objects after."""
        live_by_key: dict[tuple[int, int, int], LiveObject] = {}
        for live in live_objects:
            live_by_key[(live.local_id, live.map_group, live.map_number)] = live
        used: set[tuple[int, int, int]] = set()
        merged: list[MapObject] = []
        for index in range(len(obj_blob) // 0x18):
            fields = struct.unpack_from("<BBBxhhBBBxHHIH2x", obj_blob, index * 0x18)
            local_id, gfx, kind, x, y, b8, movement, rng = fields[:8]
            trainer_type, trainer_range, script, flag_id = fields[8:]
            key = (local_id, here[0], here[1])
            live = live_by_key.get(key)
            if live is not None:
                used.add(key)
            clone_of = None
            if kind == K.OBJ_KIND_CLONE:
                # union.clone: targetLocalId @8, targetMapNum u16 @0xC, targetMapGroup u16 @0xE
                clone_of = (b8, trainer_range, trainer_type)  # (local id, group, number)
                role = "clone"
                elevation, movement, rng, trainer_type, trainer_range = 0, 0, 0, 0, 0
            else:
                elevation = b8
                role = _role(gfx, movement, trainer_type)
            flag_state = flag_set(flag_id) if flag_id else False
            spawned_static = not flag_state if flag_id else True
            obstacle = role in FIELD_OBSTACLE_ROLES
            cx, cy = (live.x, live.y) if live else (x, y)
            merged.append(
                MapObject(
                    local_id=local_id,
                    graphics_id=live.graphics_id if live else gfx,
                    template_graphics_id=gfx,
                    role=role,
                    x=cx,
                    y=cy,
                    elevation=live.elevation if live else elevation,
                    movement_type=movement,
                    movement_type_name=K.MOVEMENT_TYPE_NAMES.get(movement),
                    range_x=rng & 0x0F,
                    range_y=rng >> 4,
                    trainer_type=trainer_type,
                    trainer_range=trainer_range,
                    script_ptr=script or None,
                    hidden_flag_id=flag_id,
                    hidden_flag_set=flag_state if (flag_id and flag_state is not None) else None,
                    source="template+live" if live else "template",
                    live=live,
                    blocks_tile=bool(live) or (obstacle and spawned_static),
                    clone_of=clone_of,
                    knowledge="observed"
                    if (live is not None or in_view(x, y)) and in_view(cx, cy)
                    else "technical",
                )
            )
        for key, live in live_by_key.items():
            if key in used:
                continue
            role = _role(live.graphics_id, live.movement_type, 0)
            merged.append(
                MapObject(
                    local_id=live.local_id,
                    graphics_id=live.graphics_id,
                    role=role,
                    x=live.x,
                    y=live.y,
                    elevation=live.elevation,
                    movement_type=live.movement_type,
                    movement_type_name=K.MOVEMENT_TYPE_NAMES.get(live.movement_type),
                    range_x=live.range_x,
                    range_y=live.range_y,
                    trainer_type=0,
                    trainer_range=0,
                    script_ptr=None,
                    hidden_flag_id=0,
                    hidden_flag_set=None,
                    source="live",
                    live=live,
                    blocks_tile=True,
                    knowledge="observed" if in_view(live.x, live.y) else "technical",
                )
            )
        return merged


def _role(gfx: int, movement: int, trainer_type: int) -> str:
    if gfx == K.OBJ_EVENT_GFX_ITEM_BALL:
        return "item_ball"
    if gfx == K.OBJ_EVENT_GFX_CUT_TREE:
        return "cut_tree"
    if gfx == K.OBJ_EVENT_GFX_ROCK_SMASH_ROCK:
        return "rock_smash"
    if gfx == K.OBJ_EVENT_GFX_PUSHABLE_BOULDER:
        return "boulder"
    if gfx == K.OBJ_EVENT_GFX_FOSSIL:
        return "fossil"
    if gfx == K.OBJ_EVENT_GFX_SNORLAX:
        return "snorlax"
    if movement == K.MOVEMENT_TYPE_BERRY_TREE_GROWTH:
        return "berry_tree"
    if trainer_type in (
        K.TRAINER_TYPE_NORMAL,
        K.TRAINER_TYPE_SEE_ALL_DIRECTIONS,
        K.TRAINER_TYPE_BURIED,
    ):
        return "trainer"
    return "npc"


class _SaveFacts:
    """Event flags and game vars from SaveBlock1 bytes; ``None`` where they are not readable."""

    def __init__(self, flags_raw: bytes, vars_raw: bytes) -> None:
        self._flags = flags_raw
        self._vars = vars_raw

    def __call__(self, flag_id: int) -> bool | None:
        index = flag_id // 8
        if flag_id < 0 or index >= len(self._flags):
            return None
        return bool(self._flags[index] & (1 << (flag_id % 8)))

    def var(self, var_id: int) -> int | None:
        index = (var_id - 0x4000) * 2
        if index < 0 or index + 2 > len(self._vars):
            return None
        return int.from_bytes(self._vars[index : index + 2], "little")
