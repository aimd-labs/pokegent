"""Typed core snapshot for FireRed (spec 4.3), Pydantic v2.

This is the *minimal core*: health, player, party, bag, flags, vars. The spec's ``ui``,
``battle`` and ``game_events`` blocks are added by WP2/WP4 as optional fields. The ``map`` block
(M2.2, :mod:`pokegent.games.gen3.firered.maps.map_schema`) is such an additive optional field:
``None`` by
default, and ``state_hash`` only includes it when present, so every hash computed without it is
unchanged and ``schema_version`` stays ``1.0``.

Rules enforced here:

* Models are frozen and reject unknown fields.
* ``health.valid == False`` means "do not use this snapshot": every data field is then empty
  (player/flags/vars ``None``, party/bag empty). Consumers cannot read partial data by accident.
* ``state_hash`` is sha256 over a canonical JSON of the normalized game-relevant fields. It
  never includes wall-clock time, ``run_id``, ``snapshot_seq`` or ``emulator_frame``, so two
  snapshots of an unchanged game state hash equal.
* ``snapshot_seq`` is assigned by the reader and increases monotonically.

M2.1 adds the optional ``ui`` block
(:class:`~pokegent.games.gen3.firered.reader.ui_schemas.UiEvidence`). It is additive: a snapshot
with
``ui=None`` hashes exactly as before. With ``ui`` present, the hash also
covers ``ui.scene_digest()`` (stable scene facts only, no key presses or counters). ``ui`` may ride
on an invalid snapshot: the UI evidence is read independently of the party/bag checks, and it is
exactly what explains a transient ``party/mon_bad_checksum`` while a party screen is drawing.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from pokegent.games.gen3.firered.maps.map_schema import MapState
from pokegent.games.gen3.firered.reader.ui_schemas import UiEvidence
from pokegent.games.gen3.firered.rom import constants as C

SCHEMA_VERSION = "1.0"
_STATE_HASH_COMPUTED = object()

Facing = Literal["N", "S", "W", "E"]
Movement = Literal["walking", "mach_bike", "acro_bike", "surfing", "underwater"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# -- health ------------------------------------------------------------------------------


class ReadError(_Frozen):
    subsystem: str
    code: str
    message: str
    addr: int | None = None


class Health(_Frozen):
    valid: bool
    read_errors: tuple[ReadError, ...] = ()
    # Per perception subsystem: 1.0 = every check passed, 0.0 = failed. Absent = not configured.
    confidence: dict[str, float] = Field(default_factory=dict)
    # Unknown at this layer (needs UI/scene evidence, WP2).
    controls_locked: bool | None = None

    @model_validator(mode="after")
    def _valid_means_no_errors(self) -> Health:
        if self.valid and self.read_errors:
            raise ValueError("health.valid must be False when read_errors is not empty")
        if not self.valid and not self.read_errors:
            raise ValueError("an invalid health must explain itself with at least one read error")
        return self


# -- party -------------------------------------------------------------------------------


class StatBlock(_Frozen):
    hp: int
    attack: int
    defense: int
    speed: int
    sp_attack: int
    sp_defense: int


class MonMove(_Frozen):
    move_id: int
    pp: int


class BoxMon(_Frozen):
    """Everything stored in the 80-byte box struct (also the first 80 bytes of a party mon)."""

    personality: int
    ot_id: int
    nickname: str
    language: int
    ot_name: str
    markings: int
    species: int
    held_item: int
    experience: int
    pp_bonuses: int
    friendship: int
    moves: tuple[MonMove, ...]
    evs: StatBlock
    ivs: StatBlock
    is_egg: bool
    ability_num: int
    pokerus: int
    met_location: int
    met_level: int
    met_game: int
    pokeball: int
    ot_gender: int

    @property
    def tid(self) -> int:
        return self.ot_id & 0xFFFF

    @property
    def sid(self) -> int:
        return self.ot_id >> 16


class PartyMember(BoxMon):
    slot: int
    status: int
    level: int
    hp: int
    max_hp: int
    stats: StatBlock
    mail: int


# -- player, bag, flags, vars -------------------------------------------------------------


class PlayTime(_Frozen):
    hours: int = Field(ge=0, le=999)
    minutes: int = Field(ge=0, le=59)
    seconds: int = Field(ge=0, le=59)


class Player(_Frozen):
    name: str
    gender: Literal["male", "female"]
    tid: int
    sid: int
    map_group: int
    map_num: int
    map_layout_id: int
    x: int
    y: int
    # Facing/movement come from the live avatar; None when the profile has no avatar symbols.
    facing: Facing | None = None
    movement: Movement | None = None
    forced_movement: bool | None = None
    play_time: PlayTime | None = None
    money: int
    coins: int
    badges: tuple[int, ...] = ()  # badge numbers 1..8, from FLAG_BADGE01_GET..08


class BagItem(_Frozen):
    item_id: int
    quantity: int


class BagPocket(_Frozen):
    pocket: str
    capacity: int
    items: tuple[BagItem, ...]


class FlagSet(_Frozen):
    """All set event flags (ids are the decompile's FLAG_* values; 0 is never set)."""

    set_ids: tuple[int, ...] = ()

    @model_validator(mode="after")
    def _sorted_unique(self) -> FlagSet:
        ids = self.set_ids
        if list(ids) != sorted(set(ids)) or any(not 0 < i < C.FLAGS_COUNT for i in ids):
            raise ValueError("set_ids must be sorted, unique and within 1..FLAGS_COUNT-1")
        return self

    def is_set(self, flag_id: int) -> bool:
        if not 0 <= flag_id < C.FLAGS_COUNT:
            raise KeyError(f"flag id 0x{flag_id:X} is outside the saved flag range")
        return flag_id in self.set_ids

    @classmethod
    def from_bytes(cls, raw: bytes) -> FlagSet:
        if len(raw) != C.NUM_FLAG_BYTES:
            raise ValueError(f"expected {C.NUM_FLAG_BYTES} flag bytes, got {len(raw)}")
        ids = [
            index * 8 + bit
            for index, byte in enumerate(raw)
            for bit in range(8)
            if byte & (1 << bit) and index * 8 + bit != 0 and index * 8 + bit < C.FLAGS_COUNT
        ]
        return cls(set_ids=tuple(ids))


class VarTable(_Frozen):
    """Non-zero event variables as ``(var_id, value)`` pairs, var ids in VARS_START..VARS_END."""

    nonzero: tuple[tuple[int, int], ...] = ()

    @model_validator(mode="after")
    def _valid(self) -> VarTable:
        ids = [i for i, _ in self.nonzero]
        if ids != sorted(set(ids)) or any(not C.VARS_START <= i <= C.VARS_END for i in ids):
            raise ValueError("var ids must be sorted, unique and within VARS_START..VARS_END")
        if any(not 0 < v <= 0xFFFF for _, v in self.nonzero):
            raise ValueError("stored var values must be 1..65535 (zero is implicit)")
        return self

    def get(self, var_id: int) -> int:
        if not C.VARS_START <= var_id <= C.VARS_END:
            raise KeyError(f"var id 0x{var_id:X} is outside the saved var range")
        return dict(self.nonzero).get(var_id, 0)

    @classmethod
    def from_bytes(cls, raw: bytes) -> VarTable:
        if len(raw) != C.VARS_COUNT * 2:
            raise ValueError(f"expected {C.VARS_COUNT * 2} var bytes, got {len(raw)}")
        pairs = []
        for index in range(C.VARS_COUNT):
            value = int.from_bytes(raw[index * 2 : index * 2 + 2], "little")
            if value:
                pairs.append((C.VARS_START + index, value))
        return cls(nonzero=tuple(pairs))


class Pokedex(_Frozen):
    """National dex flags as stored in ``SaveBlock2.Pokedex`` (bit 0 = dex 1)."""

    owned: tuple[int, ...] = ()
    seen: tuple[int, ...] = ()

    @classmethod
    def from_bytes(cls, owned_raw: bytes, seen_raw: bytes) -> Pokedex:
        def decode(raw: bytes) -> tuple[int, ...]:
            if len(raw) != 52:
                raise ValueError(f"expected 52 dex bytes, got {len(raw)}")
            return tuple(i + 1 for i in range(C.NATIONAL_DEX_COUNT) if raw[i // 8] & (1 << (i % 8)))

        return cls(owned=decode(owned_raw), seen=decode(seen_raw))

    @property
    def owned_count(self) -> int:
        return len(self.owned)

    @property
    def seen_count(self) -> int:
        return len(self.seen)


class WarpData(_Frozen):
    """A live ``SaveBlock1`` WarpData destination."""

    map_group: int
    map_number: int
    warp_id: int
    x: int
    y: int


# -- snapshot ------------------------------------------------------------------------------


def compute_state_hash(
    *,
    rom_profile: str,
    health: Health,
    player: Player | None,
    party: tuple[PartyMember, ...],
    bag: tuple[BagPocket, ...],
    flags: FlagSet | None,
    vars: VarTable | None,
    dynamic_warp: WarpData | None = None,
    registered_item: int | None = None,
    pokedex: Pokedex | None = None,
    ui: UiEvidence | None = None,
    map: MapState | None = None,  # noqa: A002
) -> str:
    """sha256 over normalized game-relevant fields. Excludes run id, seq, frame and time.

    ``pokedex``, ``ui`` (M2.1) and ``map`` (M2.2) add a ``"pokedex"`` / ``"ui"`` / ``"map"`` key
    only when present, so hashes of snapshots without these blocks are identical to those of
    schema 1.0 before the blocks existed.
    """
    fragments = {
        "schema": json.dumps(SCHEMA_VERSION),
        "rom_profile": json.dumps(rom_profile, ensure_ascii=True),
        "valid": json.dumps(health.valid),
        "errors": json.dumps(
            sorted([e.subsystem, e.code] for e in health.read_errors),
            separators=(",", ":"),
        ),
        "player": _state_fragment(player),
        "party": _state_fragment(party),
        "bag": _state_fragment(bag),
        "flags": _state_fragment(flags.set_ids if flags else None),
        "vars": _state_fragment(vars.nonzero if vars else None),
    }
    # Live map facts are additive with the optional map block.  Keep the historical hash
    # byte-identical for core-only snapshots used by replay/persisted state compatibility.
    if dynamic_warp is not None and map is not None:
        fragments["dynamic_warp"] = _state_fragment(dynamic_warp)
    # Zero means no registered item and preserves the pre-registered-item hash recipe.
    if registered_item not in (None, 0):
        fragments["registered_item"] = json.dumps(registered_item)
    if pokedex is not None:  # like ui/map: a snapshot without the block hashes as it always did
        fragments["pokedex"] = _state_fragment(pokedex)
    if ui is not None:
        fragments["ui"] = json.dumps(ui.scene_digest())
    if map is not None:
        fragments["map"] = json.dumps(map.digest)
    canonical = (
        "{" + ",".join(json.dumps(key) + ":" + fragments[key] for key in sorted(fragments)) + "}"
    )
    return "sha256:" + hashlib.sha256(canonical.encode("ascii")).hexdigest()


@lru_cache(maxsize=4096)
def _state_fragment(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    elif isinstance(value, tuple):
        value = [
            item.model_dump(mode="json")
            if isinstance(item, BaseModel)
            else list(item)
            if isinstance(item, tuple)
            else item
            for item in value
        ]
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class StateSnapshot(_Frozen):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str = ""
    rom_profile: str
    snapshot_seq: int = Field(ge=0)
    emulator_frame: int | None = None
    state_hash: str
    health: Health
    diagnostics: tuple[str, ...] = ()
    player: Player | None = None
    party: tuple[PartyMember, ...] = ()
    bag: tuple[BagPocket, ...] = ()
    flags: FlagSet | None = None
    vars: VarTable | None = None
    dynamic_warp: WarpData | None = None
    registered_item: int | None = None
    pokedex: Pokedex | None = None
    # Save-block-adjacent PokemonStorage RAM.  It is deliberately excluded from the state hash:
    # storage options are a live sidecar, while core state identity remains backwards compatible.
    storage: object | None = Field(default=None, repr=False)
    ui: UiEvidence | None = None
    # Optional map block (spec 4.3 ``map``, M2.2): None until a map reader attaches one.
    map: MapState | None = None

    @field_validator("storage", mode="before")
    @classmethod
    def _decode_storage_json(cls, value):
        """Restore the typed sidecar when a snapshot is loaded from JSON."""
        if not isinstance(value, dict) or "boxes" not in value:
            return value
        from pokegent.games.gen3.firered.reader.storage import PcItem, PokemonStorage

        boxes = tuple(
            tuple(None if mon is None else BoxMon.model_validate(mon) for mon in box)
            for box in value["boxes"]
        )
        return PokemonStorage(
            current_box=int(value["current_box"]),
            boxes=boxes,
            names=tuple(value["names"]),
            wallpapers=tuple(value["wallpapers"]),
            options_button_mode=int(value.get("options_button_mode", 0)),
            ui_state=value.get("ui_state"),
            box_option=value.get("box_option"),
            cursor_area=value.get("cursor_area"),
            cursor_position=value.get("cursor_position"),
            deposit_box_cursor=value.get("deposit_box_cursor"),
            is_holding_mon=value.get("is_holding_mon"),
            held_mon=(
                None
                if value.get("held_mon") is None
                else PartyMember.model_validate(value["held_mon"])
            ),
            held_origin_kind=value.get("held_origin_kind"),
            held_origin_box=value.get("held_origin_box"),
            held_origin_slot=value.get("held_origin_slot"),
            pc_items=tuple(PcItem(**item) for item in value.get("pc_items", ())),
        )

    @model_validator(mode="after")
    def _consistent(self, info: ValidationInfo) -> StateSnapshot:
        if self.health.valid:
            if self.player is None or self.flags is None or self.vars is None:
                raise ValueError("a valid snapshot must carry player, flags and vars")
        elif (
            self.player
            or self.party
            or self.bag
            or self.flags
            or self.vars
            or self.registered_item is not None
            or self.pokedex
            or self.storage
            or self.map
        ):
            raise ValueError("an invalid snapshot must not carry data (consumers must not use it)")
        if (info.context or {}).get("state_hash_computed") is not _STATE_HASH_COMPUTED:
            expected = compute_state_hash(
                rom_profile=self.rom_profile,
                health=self.health,
                player=self.player,
                party=self.party,
                bag=self.bag,
                flags=self.flags,
                vars=self.vars,
                dynamic_warp=self.dynamic_warp,
                registered_item=self.registered_item,
                pokedex=self.pokedex,
                ui=self.ui,
                map=self.map,
            )
            if self.state_hash != expected:
                raise ValueError("state_hash does not match the snapshot content")
        return self

    @classmethod
    def create(
        cls,
        *,
        rom_profile: str,
        snapshot_seq: int,
        health: Health,
        diagnostics: tuple[str, ...] = (),
        run_id: str = "",
        emulator_frame: int | None = None,
        player: Player | None = None,
        party: tuple[PartyMember, ...] = (),
        bag: tuple[BagPocket, ...] = (),
        flags: FlagSet | None = None,
        vars: VarTable | None = None,
        dynamic_warp: WarpData | None = None,
        registered_item: int | None = None,
        pokedex: Pokedex | None = None,
        storage: object | None = None,
        ui: UiEvidence | None = None,
        map: MapState | None = None,  # noqa: A002
    ) -> StateSnapshot:
        state_hash = compute_state_hash(
            rom_profile=rom_profile,
            health=health,
            player=player,
            party=party,
            bag=bag,
            flags=flags,
            vars=vars,
            dynamic_warp=dynamic_warp,
            registered_item=registered_item,
            pokedex=pokedex,
            ui=ui,
            map=map,
        )
        return cls.model_validate(
            {
                "run_id": run_id,
                "rom_profile": rom_profile,
                "snapshot_seq": snapshot_seq,
                "emulator_frame": emulator_frame,
                "state_hash": state_hash,
                "health": health,
                "diagnostics": diagnostics,
                "player": player,
                "party": party,
                "bag": bag,
                "flags": flags,
                "vars": vars,
                "dynamic_warp": dynamic_warp,
                "registered_item": registered_item,
                "pokedex": pokedex,
                "storage": storage,
                "ui": ui,
                "map": map,
            },
            context={"state_hash_computed": _STATE_HASH_COMPUTED},
        )

    def with_ui(self, ui: UiEvidence | None) -> StateSnapshot:
        """A copy carrying ``ui`` (state_hash recomputed). Sets ``health.controls_locked`` too."""
        health = self.health
        if ui is not None and ui.controls_locked is not None:
            health = health.model_copy(update={"controls_locked": ui.controls_locked})
        return StateSnapshot.create(
            rom_profile=self.rom_profile,
            snapshot_seq=self.snapshot_seq,
            health=health,
            diagnostics=self.diagnostics,
            run_id=self.run_id,
            emulator_frame=self.emulator_frame,
            player=self.player,
            party=self.party,
            bag=self.bag,
            flags=self.flags,
            vars=self.vars,
            registered_item=self.registered_item,
            pokedex=self.pokedex,
            dynamic_warp=self.dynamic_warp,
            storage=self.storage,
            ui=ui,
            map=self.map,
        )

    def with_blocks(self, *, ui: UiEvidence | None, map_state: MapState | None) -> StateSnapshot:
        """Attach UI and map together, hashing the trusted decoded fields once.

        Composite live reads decode every block from one atomic RAM image.  Their core snapshot
        has already passed this model's validators, and both attached blocks are typed reader
        outputs, so rebuilding and re-hashing an intermediate UI-only snapshot adds no safety.
        """
        if not self.health.valid and map_state is not None:
            raise ValueError("an invalid snapshot cannot carry a map block")
        health = self.health
        updates: dict[str, object] = {}
        if ui is not None and ui.controls_locked is not None:
            updates["controls_locked"] = ui.controls_locked
        if self.health.valid:
            confidence = dict(health.confidence)
            confidence["map"] = 1.0 if map_state is not None else 0.0
            updates["confidence"] = confidence
        if updates:
            health = health.model_copy(update=updates)
        state_hash = compute_state_hash(
            rom_profile=self.rom_profile,
            health=health,
            player=self.player,
            party=self.party,
            bag=self.bag,
            flags=self.flags,
            vars=self.vars,
            registered_item=self.registered_item,
            pokedex=self.pokedex,
            dynamic_warp=self.dynamic_warp,
            ui=ui,
            map=map_state,
        )
        return self.model_copy(
            update={"state_hash": state_hash, "health": health, "ui": ui, "map": map_state}
        )

    def with_map(self, map_state: MapState | None) -> StateSnapshot:
        """Copy of this snapshot with the map block set (state_hash recomputed).

        ``None`` removes the block. On an invalid snapshot only ``None`` is allowed (an invalid
        snapshot carries no data). ``health.confidence['map']`` is set to 1.0 / 0.0 for a
        present / absent block of a valid snapshot.
        """
        if not self.health.valid:
            if map_state is not None:
                raise ValueError("an invalid snapshot cannot carry a map block")
            return self
        confidence = dict(self.health.confidence)
        confidence["map"] = 1.0 if map_state is not None else 0.0
        health = self.health.model_copy(update={"confidence": confidence})
        return StateSnapshot.create(
            rom_profile=self.rom_profile,
            snapshot_seq=self.snapshot_seq,
            health=health,
            run_id=self.run_id,
            emulator_frame=self.emulator_frame,
            player=self.player,
            party=self.party,
            bag=self.bag,
            flags=self.flags,
            vars=self.vars,
            registered_item=self.registered_item,
            pokedex=self.pokedex,
            dynamic_warp=self.dynamic_warp,
            storage=self.storage,
            ui=self.ui,
            map=map_state,
        )

    @property
    def usable(self) -> bool:
        """True only for a valid snapshot. Consumers must check this before using any field."""
        return self.health.valid
