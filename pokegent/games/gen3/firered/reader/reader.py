"""FireRed state reader over a :class:`MemoryPort` (spec 4.2 / 4.3, WP1a).

``FireRedReader.read_snapshot()`` returns a :class:`StateSnapshot`. It never raises for bad
game memory: any violated guard turns into ``health.valid == False`` plus ``read_errors``, and an
invalid snapshot carries no data (consumers must check ``snapshot.usable``).

Reads
-----
One bulk ``read_range`` per memory region, issued back to back (the caller guarantees the
emulator does not advance in between, spec 4.1):

* IWRAM  : ``gSaveBlock1Ptr`` / ``gSaveBlock2Ptr`` (one span)
* SB1    : ``SaveBlock1[0 : vars_end]`` via the *pointer* (the blocks move by up to 124 bytes
           on every ``SetSaveBlocksPointers``; no fixed address is ever used)
* SB2    : ``SaveBlock2[0 : size]`` via the pointer
* EWRAM  : live party (``gPlayerPartyCount`` .. end of ``gPlayerParty``)
* EWRAM  : ``gObjectEvents`` + ``gPlayerAvatar`` (facing, movement mode), optional
* IWRAM  : the pointer span again, to detect a pointer change during the snapshot

Party source: ``SaveBlock1.playerPartyCount/playerParty`` (0x34/0x38) is only the *saved copy*
(``SavePlayerParty``, src/load_save.c). The live party is ``gPlayerParty``; the snapshot uses the
live one. ``party_source="saveblock"`` selects the saved copy (used for conformance checks).

Guards (any violation => invalid snapshot): pointer outside EWRAM, unaligned, or not leaving
room for the struct; party count > 6; party mon with bad checksum / bad egg / invalid fields;
bag item id or quantity out of range; money/coins out of range; position or map outside sane
bounds; avatar index out of range; save-block pointer changed during the snapshot.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Literal

from pokegent.games.gen3.firered.reader import ui_symbols as U
from pokegent.games.gen3.firered.reader.memory_port import (
    EWRAM_END,
    EWRAM_START,
    MemoryPort,
    MemoryReadError,
)
from pokegent.games.gen3.firered.reader.mon import (
    MonDecodeError,
    decode_party_mon,
    is_empty_slot,
)
from pokegent.games.gen3.firered.reader.schemas import (
    BagItem,
    BagPocket,
    FlagSet,
    Health,
    PartyMember,
    Player,
    PlayTime,
    Pokedex,
    ReadError,
    StateSnapshot,
    VarTable,
    WarpData,
)
from pokegent.games.gen3.firered.reader.storage import (
    BOX_CAPACITY,
    BOX_COUNT,
    G_STORAGE_BOX_OPTION_OFFSET,
    G_STORAGE_DEPOSIT_BOX_CURSOR_OFFSET,
    G_STORAGE_MOVING_MON_OFFSET,
    S_STORAGE_CURSOR_AREA_ADDRESS,
    S_STORAGE_CURSOR_POSITION_ADDRESS,
    S_STORAGE_IS_MON_BEING_MOVED_ADDRESS,
    S_STORAGE_MOVE_STATE_SIZE,
    S_STORAGE_MOVING_MON_ORIG_BOX_ADDRESS,
    S_STORAGE_MOVING_MON_ORIG_POSITION_ADDRESS,
    STORAGE_FROM_SAVE_BLOCK1,
    STORAGE_SIZE,
    PcItem,
    PokemonStorage,
    decode_storage,
)
from pokegent.games.gen3.firered.rom import constants as C
from pokegent.games.gen3.firered.rom.rom_data import ProfileError, RomProfile
from pokegent.games.gen3.firered.rom.text import decode_gen3_text

PartySource = Literal["live", "saveblock"]

_REQUIRED_SYMBOLS = (
    "gSaveBlock1Ptr",
    "gSaveBlock2Ptr",
    "gPlayerPartyCount",
    "gPlayerParty",
    "gMain",
    *U.PRE_FIELD_AVATAR_CALLBACKS,
)
_AVATAR_SYMBOLS = ("gObjectEvents", "gPlayerAvatar")
_MAX_SLOT_ERRORS_PER_POCKET = 3
_MAX_COORD = 0x3FF  # generous bound on s16 map coordinates; real maps are far smaller


@dataclass
class StorageDecodeCache:
    """Mutable decode cache owned by a long-lived live source, never by a snapshot."""

    raw: bytes | None = None
    decoded: PokemonStorage | None = None
    fields: dict[str, tuple[bytes, object]] = field(default_factory=dict)


class _Collector:
    def __init__(self) -> None:
        self.errors: list[ReadError] = []
        self.failed: set[str] = set()
        self.diagnostics: list[str] = []

    def add(self, subsystem: str, code: str, message: str, addr: int | None = None) -> None:
        self.errors.append(ReadError(subsystem=subsystem, code=code, message=message, addr=addr))
        self.failed.add(subsystem)


class FireRedReader:
    def __init__(
        self,
        port: MemoryPort,
        profile: RomProfile,
        *,
        run_id: str = "",
        party_source: PartySource = "live",
        start_seq: int = 0,
        storage_cache: StorageDecodeCache | None = None,
    ) -> None:
        if not profile.supported:
            raise ProfileError(f"profile {profile.profile_id} is not supported; refusing to read")
        for name in _REQUIRED_SYMBOLS:
            profile.symbol(name)  # raises ProfileError when absent
        self._port = port
        self._profile = profile
        self._run_id = run_id
        self._party_source: PartySource = party_source
        self._seq = start_seq
        self._has_avatar = all(profile.has_symbol(name) for name in _AVATAR_SYMBOLS)
        self._pre_field_avatar_callbacks = frozenset(
            profile.symbol(name).address | 1 for name in U.PRE_FIELD_AVATAR_CALLBACKS
        )
        # Storage is part of every coherent snapshot, but its 420 boxed-Pokemon
        # decode is unchanged across nearly all gameplay frames. Keep the last
        # raw payload so polling can reuse the validated immutable model.
        self._storage_cache = storage_cache if storage_cache is not None else StorageDecodeCache()

    @property
    def profile(self) -> RomProfile:
        return self._profile

    # -- public ------------------------------------------------------------------------

    def read_snapshot(
        self,
        *,
        emulator_frame: int | None = None,
        seq: int | None = None,
        defer_hash: bool = False,
    ) -> StateSnapshot:
        """Read one snapshot.

        ``seq`` overrides the reader's own counter (WP1c: a live source supplies
        ``frame + load_epoch`` so the number says *when* in emulator time the RAM was taken).
        The counter never goes backwards: after an explicit ``seq`` it continues above it.
        """
        if seq is None:
            seq = self._seq
            self._seq += 1
        else:
            if seq < 0:
                raise ValueError(f"seq must be >= 0, got {seq}")
            self._seq = max(self._seq, seq + 1)
        collector = _Collector()
        data = self._read_all(collector, emulator_frame)
        if collector.errors or data is None:
            if not collector.errors:
                collector.add("reader", "no_data", "reader produced no data")
            return self._invalid(seq, emulator_frame, collector, defer_hash=defer_hash)
        return self._valid(seq, emulator_frame, data, defer_hash=defer_hash)

    def check_rom_header(self) -> list[ReadError]:
        """Compare the ROM header visible in GBA memory (0x08000000) with the profile.

        Optional extra check for a live backend; a mismatch means the profile does not belong
        to the running game. Returns the list of problems (empty = header matches).
        """
        errors: list[ReadError] = []
        try:
            header = self._port.read_range(0x0800_0000, 0xC0)
        except MemoryReadError as exc:
            return [
                ReadError(
                    subsystem="rom", code="header_unreadable", message=str(exc), addr=exc.addr
                )
            ]
        code = header[0xAC:0xB0].decode("ascii", errors="replace")
        version = header[0xBC]
        if code != self._profile.rom.game_code or version != self._profile.rom.version:
            errors.append(
                ReadError(
                    subsystem="rom",
                    code="header_mismatch",
                    message=(
                        f"in-memory header {code!r} v{version} != profile "
                        f"{self._profile.rom.game_code!r} v{self._profile.rom.version}"
                    ),
                )
            )
        return errors

    # -- internals ---------------------------------------------------------------------

    def _invalid(
        self,
        seq: int,
        frame: int | None,
        collector: _Collector,
        *,
        defer_hash: bool = False,
    ) -> StateSnapshot:
        subsystems = {
            "save_blocks",
            "player",
            "party",
            "bag",
            "flags",
            "vars",
            "pokedex",
            "storage",
            "avatar",
        }
        confidence = {name: (0.0 if name in collector.failed else 1.0) for name in subsystems}
        if not self._has_avatar:
            confidence.pop("avatar")
        # A save-block failure poisons everything read through it.
        if "save_blocks" in collector.failed:
            for name in ("player", "bag", "flags", "vars", "pokedex"):
                confidence[name] = 0.0
        health = Health(valid=False, read_errors=tuple(collector.errors), confidence=confidence)
        if defer_hash:
            return StateSnapshot.model_construct(
                rom_profile=self._profile.profile_id,
                snapshot_seq=seq,
                emulator_frame=frame,
                state_hash="",
                health=health,
                diagnostics=tuple(collector.diagnostics),
                run_id=self._run_id,
            )
        return StateSnapshot.create(
            rom_profile=self._profile.profile_id,
            snapshot_seq=seq,
            health=health,
            run_id=self._run_id,
            emulator_frame=frame,
        )

    def _valid(
        self,
        seq: int,
        frame: int | None,
        data: _Parsed,
        *,
        defer_hash: bool = False,
    ) -> StateSnapshot:
        confidence = {
            name: 1.0
            for name in ("save_blocks", "player", "party", "bag", "flags", "vars", "pokedex")
        }
        confidence["storage"] = 1.0 if data.storage is not None else 0.0
        if self._has_avatar:
            # gPlayerAvatar exists before ObjectEvent is created (new-game naming/map hand-off).
            # Save/player data is still coherent then, but live movement facts are unavailable.
            confidence["avatar"] = 1.0 if data.player.movement is not None else 0.0
        health = Health(valid=True, confidence=confidence)
        if defer_hash:
            return StateSnapshot.model_construct(
                rom_profile=self._profile.profile_id,
                snapshot_seq=seq,
                emulator_frame=frame,
                state_hash="",
                health=health,
                diagnostics=tuple(data.diagnostics),
                run_id=self._run_id,
                player=data.player,
                party=data.party,
                bag=data.bag,
                flags=data.flags,
                vars=data.vars,
                registered_item=data.registered_item,
                pokedex=data.pokedex,
                dynamic_warp=data.dynamic_warp,
                storage=data.storage,
            )
        return StateSnapshot.create(
            rom_profile=self._profile.profile_id,
            snapshot_seq=seq,
            health=health,
            diagnostics=tuple(data.diagnostics),
            run_id=self._run_id,
            emulator_frame=frame,
            player=data.player,
            party=data.party,
            bag=data.bag,
            flags=data.flags,
            vars=data.vars,
            registered_item=data.registered_item,
            pokedex=data.pokedex,
            dynamic_warp=data.dynamic_warp,
            storage=data.storage,
        )

    def _pointer_span(self) -> tuple[int, int]:
        sb1 = self._profile.symbol("gSaveBlock1Ptr").address
        sb2 = self._profile.symbol("gSaveBlock2Ptr").address
        start = min(sb1, sb2)
        return start, max(sb1, sb2) + 4 - start

    def _read(self, collector: _Collector, subsystem: str, addr: int, size: int) -> bytes | None:
        try:
            return self._port.read_range(addr, size)
        except MemoryReadError as exc:
            collector.add(subsystem, "read_failed", str(exc), exc.addr)
            return None

    def _pointers(self, raw: bytes, base: int) -> tuple[int, int]:
        sb1 = self._profile.symbol("gSaveBlock1Ptr").address - base
        sb2 = self._profile.symbol("gSaveBlock2Ptr").address - base
        return struct.unpack_from("<I", raw, sb1)[0], struct.unpack_from("<I", raw, sb2)[0]

    def _check_pointer(self, collector: _Collector, name: str, ptr: int, size: int) -> bool:
        if ptr % 4:
            collector.add(
                "save_blocks", "pointer_unaligned", f"{name}=0x{ptr:08X} is not 4-byte aligned", ptr
            )
        elif not (EWRAM_START <= ptr and ptr + size <= EWRAM_END):
            collector.add(
                "save_blocks",
                "pointer_out_of_ewram",
                f"{name}=0x{ptr:08X} (+0x{size:X}) does not lie inside EWRAM",
                ptr,
            )
        else:
            return True
        return False

    def _read_all(self, collector: _Collector, frame: int | None) -> _Parsed | None:
        """Read a coherent save-block view, retrying one detected relocation in-place.

        SetSaveBlocksPointers can run between the initial pointer read and the final coherence
        check. Re-reading the complete view with the newly resolved pointers avoids parsing a
        mixed old/new block. This does not relax any parser guard and is intentionally bounded;
        frame-level transient settling remains the caller's responsibility.
        """
        for _attempt in range(2):
            attempt = _Collector()
            data = self._read_all_once(attempt, frame)
            if data is not None and not attempt.errors:
                collector.diagnostics.extend(attempt.diagnostics)
                return data
            if not attempt.errors or any(e.code != "pointer_changed" for e in attempt.errors):
                collector.errors.extend(attempt.errors)
                collector.failed.update(attempt.failed)
                collector.diagnostics.extend(attempt.diagnostics)
                return None
        collector.errors.extend(attempt.errors)
        collector.failed.update(attempt.failed)
        collector.diagnostics.extend(attempt.diagnostics)
        return None

    def _read_all_once(self, collector: _Collector, frame: int | None) -> _Parsed | None:
        profile = self._profile
        sb1_size = (
            profile.field("SaveBlock1", "vars").offset + profile.field("SaveBlock1", "vars").size
        )
        sb2_size = profile.struct_size("SaveBlock2")

        # 1. save-block pointers (IWRAM)
        span_base, span_size = self._pointer_span()
        pointer_bytes = self._read(collector, "save_blocks", span_base, span_size)
        if pointer_bytes is None:
            return None
        sb1_ptr, sb2_ptr = self._pointers(pointer_bytes, span_base)
        ok1 = self._check_pointer(collector, "gSaveBlock1Ptr", sb1_ptr, sb1_size)
        ok2 = self._check_pointer(collector, "gSaveBlock2Ptr", sb2_ptr, sb2_size)
        if not (ok1 and ok2):
            return None

        # 2. one bulk read per region
        sb1 = self._read(collector, "save_blocks", sb1_ptr, sb1_size)
        sb2 = self._read(collector, "save_blocks", sb2_ptr, sb2_size)
        if sb1 is None or sb2 is None:
            return None
        storage_pointer = self._storage_pointer()
        storage_box_option = self._storage_box_option(storage_pointer)
        try:
            storage_raw = self._port.read_range(sb1_ptr + STORAGE_FROM_SAVE_BLOCK1, STORAGE_SIZE)
            if storage_raw == self._storage_cache.raw and self._storage_cache.decoded is not None:
                storage = self._storage_cache.decoded
            else:
                storage = decode_storage(storage_raw)
                self._storage_cache.raw = storage_raw
                self._storage_cache.decoded = storage
            storage = replace(
                storage,
                options_button_mode=sb2[self._field("optionsButtonMode", "SaveBlock2")[0]],
            )
            storage = self._attach_storage_ui(storage, storage_pointer)
        except (MemoryReadError, ValueError):
            # Storage is an option sidecar. During copy/compact animations it can be transiently
            # unreadable; core party/save state remains usable and the postcondition waits for a
            # later coherent storage read.
            storage = None
        live_party = self._read_live_party(collector)
        callback2 = self._read_callback2(collector) if self._has_avatar else None
        avatar = self._read_avatar(collector) if self._has_avatar else None
        # 3. coherence: the pointers must not have moved while we were reading
        pointer_again = self._read(collector, "save_blocks", span_base, span_size)
        if collector.errors:
            return None
        assert sb1 is not None and sb2 is not None and pointer_again is not None
        if pointer_again != pointer_bytes:
            collector.add(
                "save_blocks",
                "pointer_changed",
                "save-block pointers changed during the snapshot (frame advanced or blocks moved)",
                span_base,
            )
            return None

        def cached(name: str, key: bytes, decode: Callable[[], Any]) -> Any:
            prior = self._storage_cache.fields.get(name)
            if prior is not None and prior[0] == key:
                return prior[1]
            before = len(collector.errors)
            value = decode()
            if len(collector.errors) == before:
                self._storage_cache.fields[name] = (key, value)
            return value

        player = self._parse_player(collector, sb1, sb2, avatar, callback2)
        allow_empty = storage_box_option == 1
        if self._party_source == "live":
            assert live_party is not None
            party_raw = live_party[1]
        else:
            count_off, _ = self._field("playerPartyCount")
            party_off, party_size = self._field("playerParty")
            party_raw = sb1[count_off : party_off + party_size]
        storage_active = storage_box_option is not None
        party = cached(
            "party",
            bytes((allow_empty, storage_active)) + party_raw,
            lambda: self._parse_party(
                collector,
                sb1,
                live_party,
                allow_empty=allow_empty,
                storage_active=storage_active,
            ),
        )
        encryption_off, encryption_size = self._field("encryptionKey", "SaveBlock2")
        bag_key = sb2[encryption_off : encryption_off + encryption_size] + b"".join(
            sb1[offset : offset + size]
            for _, field_name, _ in C.BAG_POCKETS
            for offset, size in (self._field(field_name),)
        )
        bag = cached("bag", bag_key, lambda: self._parse_bag(collector, sb1, sb2))
        if storage is not None:
            storage = replace(storage, pc_items=self._parse_pc_items(collector, sb1))
        flags_off, flags_size = self._field("flags")
        flags = cached(
            "flags",
            sb1[flags_off : flags_off + flags_size],
            lambda: self._parse_flags(collector, sb1),
        )
        vars_off, vars_size = self._field("vars")
        variables = cached(
            "vars",
            sb1[vars_off : vars_off + vars_size],
            lambda: self._parse_vars(collector, sb1),
        )
        owned_off, owned_size = self._field("pokedexOwned", "SaveBlock2")
        seen_off, seen_size = self._field("pokedexSeen", "SaveBlock2")
        pokedex_key = sb2[owned_off : owned_off + owned_size] + sb2[seen_off : seen_off + seen_size]
        pokedex = cached("pokedex", pokedex_key, lambda: self._parse_pokedex(collector, sb2))
        registered_offset, _ = self._field("registeredItem")
        registered_item = struct.unpack_from("<H", sb1, registered_offset)[0]
        dynamic_offset, _ = self._field("dynamicWarp")
        group, number, warp_id, _pad, x, y = struct.unpack_from("<bbbbhh", sb1, dynamic_offset)
        if (
            collector.errors
            or player is None
            or flags is None
            or variables is None
            or pokedex is None
        ):
            return None
        badges = tuple(
            number
            for number in range(1, C.NUM_BADGES + 1)
            if flags.is_set(C.FLAG_BADGE01_GET + number - 1)
        )
        player = player.model_copy(update={"badges": badges})
        return _Parsed(
            player=player,
            party=party,
            bag=bag,
            flags=flags,
            vars=variables,
            pokedex=pokedex,
            registered_item=registered_item,
            dynamic_warp=WarpData(map_group=group, map_number=number, warp_id=warp_id, x=x, y=y),
            storage=storage,
            diagnostics=tuple(collector.diagnostics),
        )

    def _attach_storage_ui(self, storage, pointer: int | None):
        """Best-effort live storage cursor facts; save contents stay valid if UI RAM is stale."""
        if pointer is None:
            return storage
        try:
            if not (
                EWRAM_START <= pointer
                and pointer + G_STORAGE_DEPOSIT_BOX_CURSOR_OFFSET + 1 <= EWRAM_END
            ):
                return storage
            raw = self._port.read_range(pointer, G_STORAGE_BOX_OPTION_OFFSET + 1)
            move_state = self._port.read_range(
                S_STORAGE_CURSOR_AREA_ADDRESS, S_STORAGE_MOVE_STATE_SIZE
            )
            if S_STORAGE_CURSOR_POSITION_ADDRESS != S_STORAGE_CURSOR_AREA_ADDRESS + 1:
                raise ValueError("storage cursor symbols are not adjacent")
            if S_STORAGE_IS_MON_BEING_MOVED_ADDRESS != S_STORAGE_CURSOR_AREA_ADDRESS + 2:
                raise ValueError("storage held flag is not adjacent to cursor symbols")
            if S_STORAGE_MOVING_MON_ORIG_BOX_ADDRESS != S_STORAGE_CURSOR_AREA_ADDRESS + 3:
                raise ValueError("storage held origin box is not adjacent to held flag")
            if S_STORAGE_MOVING_MON_ORIG_POSITION_ADDRESS != S_STORAGE_CURSOR_AREA_ADDRESS + 4:
                raise ValueError("storage held origin position is not adjacent to held origin box")
            deposit_box = self._port.read_range(pointer + G_STORAGE_DEPOSIT_BOX_CURSOR_OFFSET, 1)
            held_raw = self._port.read_range(
                pointer + G_STORAGE_MOVING_MON_OFFSET, C.PARTY_MON_SIZE
            )
            held = None
            is_holding = move_state[2]
            if is_holding not in (0, 1):
                raise ValueError(f"invalid storage held flag {is_holding}")
            if is_holding and not is_empty_slot(held_raw):
                try:
                    held = decode_party_mon(held_raw)
                except MonDecodeError:
                    held = None
            origin_kind = None
            origin_box = None
            origin_slot = None
            if is_holding:
                raw_origin_box = move_state[3]
                raw_origin_slot = move_state[4]
                if raw_origin_box == BOX_COUNT and raw_origin_slot < C.PARTY_SIZE:
                    origin_kind = "party"
                    origin_slot = raw_origin_slot
                elif raw_origin_box < BOX_COUNT and raw_origin_slot < BOX_CAPACITY:
                    origin_kind = "box"
                    origin_box = raw_origin_box
                    origin_slot = raw_origin_slot
                else:
                    raise ValueError(
                        f"invalid storage held origin ({raw_origin_box}, {raw_origin_slot})"
                    )
            return replace(
                storage,
                ui_state=raw[0],
                box_option=raw[G_STORAGE_BOX_OPTION_OFFSET],
                cursor_area=move_state[0],
                cursor_position=move_state[1],
                deposit_box_cursor=deposit_box[0] if deposit_box[0] < 14 else None,
                is_holding_mon=bool(is_holding),
                held_mon=held,
                held_origin_kind=origin_kind,
                held_origin_box=origin_box,
                held_origin_slot=origin_slot,
            )
        except (MemoryReadError, ValueError):
            return storage

    def _storage_pointer(self) -> int | None:
        if not self._profile.has_symbol("gStorage"):
            return None
        try:
            pointer = struct.unpack(
                "<I", self._port.read_range(self._profile.symbol("gStorage").address, 4)
            )[0]
            if not EWRAM_START <= pointer < EWRAM_END:
                return None
            return pointer
        except (MemoryReadError, ValueError):
            return None

    def _storage_box_option(self, pointer: int | None) -> int | None:
        if pointer is None:
            return None
        try:
            return self._port.read_range(pointer + G_STORAGE_BOX_OPTION_OFFSET, 1)[0]
        except (MemoryReadError, ValueError):
            return None

    def _field(self, name: str, struct_name: str = "SaveBlock1") -> tuple[int, int]:
        entry = self._profile.field(struct_name, name)
        return entry.offset, entry.size

    # -- live regions ------------------------------------------------------------------

    def _read_live_party(self, collector: _Collector) -> tuple[int, bytes] | None:
        if self._party_source != "live":
            return None
        count_sym = self._profile.symbol("gPlayerPartyCount")
        party_sym = self._profile.symbol("gPlayerParty")
        start = min(count_sym.address, party_sym.address)
        end = max(count_sym.address + 1, party_sym.address + C.PARTY_SIZE * C.PARTY_MON_SIZE)
        raw = self._read(collector, "party", start, end - start)
        return (start, raw) if raw is not None else None

    def _read_avatar(self, collector: _Collector) -> tuple[int, bytes] | None:
        events = self._profile.symbol("gObjectEvents")
        avatar = self._profile.symbol("gPlayerAvatar")
        start = min(events.address, avatar.address)
        end = max(
            events.address + C.OBJECT_EVENTS_COUNT * C.OBJECT_EVENT_SIZE,
            avatar.address + C.AVATAR_READ_SIZE,
        )
        raw = self._read(collector, "avatar", start, end - start)
        return (start, raw) if raw is not None else None

    def _read_callback2(self, collector: _Collector) -> int | None:
        raw = self._read(
            collector,
            "avatar",
            self._profile.symbol("gMain").address + U.MAIN_CALLBACK2,
            4,
        )
        return struct.unpack("<I", raw)[0] if raw is not None else None

    # -- parsers -----------------------------------------------------------------------

    def _parse_player(
        self,
        collector: _Collector,
        sb1: bytes,
        sb2: bytes,
        avatar: tuple[int, bytes] | None,
        callback2: int | None,
    ) -> Player | None:
        pos_off, _ = self._field("pos")
        loc_off, _ = self._field("location")
        layout_off, _ = self._field("mapLayoutId")
        money_off, _ = self._field("money")
        coins_off, _ = self._field("coins")
        key = struct.unpack_from("<I", sb2, self._field("encryptionKey", "SaveBlock2")[0])[0]

        x, y = struct.unpack_from("<hh", sb1, pos_off)
        group, number = sb1[loc_off], sb1[loc_off + 1]
        layout_id = struct.unpack_from("<H", sb1, layout_off)[0]
        money = struct.unpack_from("<I", sb1, money_off)[0] ^ key
        coins = (struct.unpack_from("<H", sb1, coins_off)[0] ^ key) & 0xFFFF

        ok = True
        if not 0 <= x <= _MAX_COORD or not 0 <= y <= _MAX_COORD:
            collector.add(
                "player", "position_out_of_range", f"position ({x}, {y}) outside 0..{_MAX_COORD}"
            )
            ok = False
        if not 0 <= money <= C.MAX_MONEY:
            collector.add(
                "player",
                "money_out_of_range",
                f"money {money} outside 0..{C.MAX_MONEY} (wrong key?)",
            )
            ok = False
        if not 0 <= coins <= C.MAX_COINS:
            collector.add("player", "coins_out_of_range", f"coins {coins} outside 0..{C.MAX_COINS}")
            ok = False

        name_off, name_size = self._field("playerName", "SaveBlock2")
        gender_off, _ = self._field("playerGender", "SaveBlock2")
        tid_off, _ = self._field("playerTrainerId", "SaveBlock2")
        tid, sid = struct.unpack_from("<HH", sb2, tid_off)
        gender_byte = sb2[gender_off]
        if gender_byte > 1:
            collector.add("player", "gender_invalid", f"player gender byte {gender_byte}")
            ok = False

        facing = movement = forced = None
        if avatar is not None:
            parsed = self._parse_avatar(collector, avatar, callback2)
            if parsed is None:
                ok = False
            else:
                facing, movement, forced = parsed
        if not ok:
            return None
        return Player(
            name=decode_gen3_text(sb2[name_off : name_off + name_size]),
            play_time=self._parse_play_time(sb2),
            gender="female" if gender_byte else "male",
            tid=tid,
            sid=sid,
            map_group=group,
            map_num=number,
            map_layout_id=layout_id,
            x=x,
            y=y,
            facing=facing,
            movement=movement,
            forced_movement=forced,
            money=money,
            coins=coins,
        )

    def _parse_play_time(self, sb2: bytes) -> PlayTime | None:
        # A display-only clock must not make otherwise legal state unusable.
        try:
            values = {}
            for unit, field in (
                ("hours", "playTimeHours"),
                ("minutes", "playTimeMinutes"),
                ("seconds", "playTimeSeconds"),
            ):
                offset, size = self._field(field, "SaveBlock2")
                raw = sb2[offset : offset + size]
                if len(raw) != size:
                    return None
                values[unit] = int.from_bytes(raw, "little")
            return PlayTime(**values)
        except (ProfileError, ValueError):
            return None

    def _parse_avatar(
        self, collector: _Collector, avatar: tuple[int, bytes], callback2: int | None
    ):
        base, raw = avatar
        events = self._profile.symbol("gObjectEvents").address - base
        av = self._profile.symbol("gPlayerAvatar").address - base
        flags = raw[av + C.AVATAR_FLAGS_OFFSET]
        index = raw[av + C.AVATAR_OBJECT_EVENT_ID_OFFSET]
        if index >= C.OBJECT_EVENTS_COUNT:
            collector.add(
                "avatar",
                "avatar_index_invalid",
                f"player object event index {index} >= {C.OBJECT_EVENTS_COUNT}",
            )
            return None
        obj = events + index * C.OBJECT_EVENT_SIZE
        if not raw[obj] & C.OBJ_EVENT_ACTIVE_BIT or not raw[obj + 2] & C.OBJ_EVENT_IS_PLAYER_BIT:
            # ObjectEvent is created by SpawnSpecialObjectEventParameterized during field setup;
            # the save blocks and gPlayerAvatar exist earlier in the new-game naming hand-off.
            # An indexed mismatch remains corruption when some active player object does exist.
            has_active_player = any(
                raw[event] & C.OBJ_EVENT_ACTIVE_BIT and raw[event + 2] & C.OBJ_EVENT_IS_PLAYER_BIT
                for event in range(
                    events,
                    events + C.OBJECT_EVENTS_COUNT * C.OBJECT_EVENT_SIZE,
                    C.OBJECT_EVENT_SIZE,
                )
            )
            if not has_active_player and callback2 in self._pre_field_avatar_callbacks:
                collector.diagnostics.append("avatar object event has not been created")
                return None, None, None
            collector.add(
                "avatar", "avatar_not_player", f"object event {index} is not the active player"
            )
            return None
        direction = raw[obj + C.OBJ_EVENT_FACING_BYTE] & 0x0F
        facing = C.FACING_BY_DIR.get(direction)  # None for DIR_NONE / diagonal values
        if flags & C.AVATAR_FLAG_SURFING:
            movement = "surfing"
        elif flags & C.AVATAR_FLAG_UNDERWATER:
            movement = "underwater"
        elif flags & C.AVATAR_FLAG_MACH_BIKE:
            movement = "mach_bike"
        elif flags & C.AVATAR_FLAG_ACRO_BIKE:
            movement = "acro_bike"
        else:
            movement = "walking"
        return facing, movement, bool(flags & C.AVATAR_FLAG_FORCED)

    def _parse_party(
        self,
        collector: _Collector,
        sb1: bytes,
        live: tuple[int, bytes] | None,
        *,
        allow_empty: bool = False,
        storage_active: bool = False,
    ) -> tuple[PartyMember, ...]:
        if self._party_source == "live":
            assert live is not None
            base, raw = live
            count_off = self._profile.symbol("gPlayerPartyCount").address - base
            party_off = self._profile.symbol("gPlayerParty").address - base
            buffer = raw
        else:
            count_off, _ = self._field("playerPartyCount")
            party_off, _ = self._field("playerParty")
            buffer = sb1
        count = buffer[count_off]
        if count > C.PARTY_SIZE:
            collector.add("party", "party_count_invalid", f"party count {count} > {C.PARTY_SIZE}")
            return ()
        members: list[PartyMember] = []
        # During PC withdraw, FireRed writes the new mon into gPlayerParty but refreshes
        # gPlayerPartyCount only when storage closes. Outside storage, slots beyond count are
        # stale garbage and must not be treated as party members.
        use_live_tail = self._party_source == "live" and storage_active
        slots = range(C.PARTY_SIZE) if use_live_tail else range(count)
        if use_live_tail and count < C.PARTY_SIZE:
            collector.diagnostics.append("party_slots_beyond_count_used")
        for slot in slots:
            start = party_off + slot * C.PARTY_MON_SIZE
            try:
                members.append(decode_party_mon(buffer[start : start + C.PARTY_MON_SIZE], slot))
            except MonDecodeError as exc:
                if exc.kind.value == "empty_slot" and (
                    allow_empty or (use_live_tail and slot >= count)
                ):
                    continue
                collector.add("party", f"mon_{exc.kind.value}", f"party slot {slot}: {exc.message}")
        return tuple(members)

    def _parse_bag(self, collector: _Collector, sb1: bytes, sb2: bytes) -> tuple[BagPocket, ...]:
        key16 = (
            struct.unpack_from("<I", sb2, self._field("encryptionKey", "SaveBlock2")[0])[0] & 0xFFFF
        )
        pockets: list[BagPocket] = []
        for name, field_name, capacity in C.BAG_POCKETS:
            offset, size = self._field(field_name)
            if size != capacity * 4:
                raise ProfileError(
                    f"profile field {field_name} size {size} != capacity {capacity} * 4"
                )
            items: list[BagItem] = []
            bad = 0
            for index in range(capacity):
                item_id, raw_qty = struct.unpack_from("<HH", sb1, offset + index * 4)
                quantity = raw_qty ^ key16
                if item_id == 0 and quantity == 0:
                    continue
                if (
                    item_id >= C.ITEMS_COUNT
                    or quantity > C.MAX_BAG_QUANTITY
                    or (item_id == 0) != (quantity == 0)
                ):
                    bad += 1
                    if bad <= _MAX_SLOT_ERRORS_PER_POCKET:
                        collector.add(
                            "bag",
                            "bag_slot_invalid",
                            f"{name}[{index}]: item {item_id}, quantity {quantity}",
                        )
                    continue
                items.append(BagItem(item_id=item_id, quantity=quantity))
            if bad > _MAX_SLOT_ERRORS_PER_POCKET:
                collector.add("bag", "bag_slot_invalid", f"{name}: {bad} invalid slots in total")
            pockets.append(BagPocket(pocket=name, capacity=capacity, items=tuple(items)))
        return tuple(pockets)

    def _parse_pc_items(self, collector: _Collector, sb1: bytes) -> tuple[PcItem, ...]:
        """Decode SaveBlock1.pcItems; FireRed deliberately XORs PC quantities with zero."""
        offset, size = self._field("pcItems")
        items: list[PcItem] = []
        for index in range(size // 4):
            item_id, quantity = struct.unpack_from("<HH", sb1, offset + index * 4)
            if item_id == 0 and quantity == 0:
                continue
            if (
                item_id >= C.ITEMS_COUNT
                or quantity > C.MAX_BAG_QUANTITY
                or (item_id == 0) != (quantity == 0)
            ):
                collector.add(
                    "bag",
                    "pc_item_slot_invalid",
                    f"pcItems[{index}]: item {item_id}, quantity {quantity}",
                )
                continue
            items.append(PcItem(item_id=item_id, quantity=quantity))
        return tuple(items)

    def _parse_flags(self, collector: _Collector, sb1: bytes) -> FlagSet | None:
        offset, size = self._field("flags")
        try:
            return FlagSet.from_bytes(sb1[offset : offset + size])
        except ValueError as exc:
            collector.add("flags", "flags_invalid", str(exc))
            return None

    def _parse_vars(self, collector: _Collector, sb1: bytes) -> VarTable | None:
        offset, size = self._field("vars")
        try:
            return VarTable.from_bytes(sb1[offset : offset + size])
        except ValueError as exc:
            collector.add("vars", "vars_invalid", str(exc))
            return None

    def _parse_pokedex(self, collector: _Collector, sb2: bytes) -> Pokedex | None:
        owned_off, owned_size = self._field("pokedexOwned", "SaveBlock2")
        seen_off, seen_size = self._field("pokedexSeen", "SaveBlock2")
        try:
            return Pokedex.from_bytes(
                sb2[owned_off : owned_off + owned_size], sb2[seen_off : seen_off + seen_size]
            )
        except ValueError as exc:
            collector.add("pokedex", "pokedex_invalid", str(exc))
            return None


class _Parsed:
    __slots__ = (
        "player",
        "party",
        "bag",
        "flags",
        "vars",
        "pokedex",
        "registered_item",
        "dynamic_warp",
        "storage",
        "diagnostics",
    )

    def __init__(
        self,
        *,
        player,
        party,
        bag,
        flags,
        vars,
        pokedex,
        registered_item,
        dynamic_warp,
        storage,
        diagnostics=(),
    ) -> None:  # noqa: A002
        self.player = player
        self.party = party
        self.bag = bag
        self.flags = flags
        self.vars = vars
        self.pokedex = pokedex
        self.registered_item = registered_item
        self.dynamic_warp = dynamic_warp
        self.storage = storage
        self.diagnostics = diagnostics
