"""Names and move/item data read from the ROM image through the same atomic port as the RAM.

The atomic snapshot carries the ROM image, so a table row is read in the same coherent moment as the
battle state. The table addresses are family-local symbols (:mod:`symbols`, ``ROM_TABLES``) with the
structural scan that found them recorded next to each address. This module is deliberately small:
the Gen-3 mechanics tables (type chart, base stats, damage) belong to the mechanics library, the
facts here are only what an option label and the legality rules need (a name, a type, a power of 0
for Taunt, PP, an item's pocket and battle use).

Everything returns ``None`` when the port cannot back the read (ROM-free tests, unmapped range): a
missing name is "unknown", never an invented one.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass

from pokegent.actions.battle import state as S
from pokegent.actions.battle.symbols import rom_table
from pokegent.games.gen3.firered.reader.memory_port import MemoryPort, MemoryReadError
from pokegent.games.gen3.firered.rom.rom_data import ROM_PATH_ENV, RomProfile
from pokegent.games.gen3.firered.rom.rom_tables import ItemEffect, parse_item_effect
from pokegent.games.gen3.firered.rom.text import decode_gen3_text

TYPE_NAMES = (
    "NORMAL",
    "FIGHTING",
    "FLYING",
    "POISON",
    "GROUND",
    "ROCK",
    "BUG",
    "GHOST",
    "STEEL",
    "???",
    "FIRE",
    "WATER",
    "GRASS",
    "ELECTRIC",
    "PSYCHIC",
    "ICE",
    "DRAGON",
    "DARK",
)  # fmt: skip  (include/constants/pokemon.h TYPE_*; physical types are ids < 9)

#: move target bitmask (include/battle.h MOVE_TARGET_*)
MOVE_TARGET_SELECTED = 0
MOVE_TARGET_USER = 1 << 4

ITEM_POTION = 13  # gItemEffectTable starts here: index = item id - ITEM_POTION
ITEM_CHOICE_BAND = 186
ITEM_ENIGMA_BERRY = 175

POCKET_NAMES = {1: "items", 2: "key_items", 3: "poke_balls", 4: "tms_hms", 5: "berries"}


@dataclass(frozen=True)
class MoveData:
    move_id: int
    effect: int
    power: int
    type: int
    accuracy: int
    pp: int
    secondary_chance: int
    target: int
    priority: int
    flags: int

    @property
    def type_name(self) -> str:
        return TYPE_NAMES[self.type] if self.type < len(TYPE_NAMES) else "???"

    @property
    def is_physical(self) -> bool:
        return self.type < 9  # Gen-3 split by TYPE (spec 6.D): ids below MYSTERY are physical


@dataclass(frozen=True)
class ItemData:
    item_id: int
    name: str
    price: int
    hold_effect: int
    hold_effect_param: int
    pocket: int
    battle_usage: int
    battle_use_func: int  # raw function pointer; role comes from reference items

    @property
    def pocket_name(self) -> str:
        return POCKET_NAMES.get(self.pocket, f"pocket_{self.pocket}")


class RomTables:
    """Lazy, cached reader of the name/move/item tables of one ROM profile.

    Production consumers opt into the generated on-disk tables.  The default raw-port mode remains
    available for synthetic snapshots and keeps the existing test and extension contract intact.
    """

    # The ROM is constant per profile; rows are cached across snapshots.
    _cache: dict[tuple[str, str, int], object] = {}
    _generated: dict[str, object] = {}

    def __init__(
        self, port: MemoryPort, profile: RomProfile, *, use_generated: bool = False
    ) -> None:
        self._port = port
        self._profile = profile
        self._tables = self._generated.get(profile.profile_id) if use_generated else None
        if use_generated and self._tables is None:
            configured = os.environ.get(ROM_PATH_ENV)
            if configured:
                from pokegent.games.gen3.firered.rom.rom_tables import GameTables

                self._tables = GameTables.from_rom_file(profile_id=profile.profile_id)
                self._generated[profile.profile_id] = self._tables
            else:
                # Synthetic and ROM-free snapshots retain the raw-port path.
                self._tables = False

    # -- raw rows ---------------------------------------------------------------------------

    def _row(self, table: str, index: int, size: int | None = None) -> bytes | None:
        spec = rom_table(self._profile, table)
        if spec is None or not 0 <= index < spec.count:
            return None
        key = (self._profile.profile_id, table, index)
        hit = self._cache.get(key)
        if isinstance(hit, bytes):
            return hit
        try:
            raw = self._port.read_range(spec.address + index * spec.stride, size or spec.stride)
        except (MemoryReadError, ValueError):
            return None
        self._cache[key] = raw
        return raw

    def _name(self, table: str, index: int, length: int) -> str | None:
        raw = self._row(table, index)
        if raw is None:
            return None
        end = raw.find(b"\xff")
        text = decode_gen3_text(raw[: length if end < 0 else min(end, length)] + b"\xff")
        return text or None

    # -- names ------------------------------------------------------------------------------

    def move_name(self, move_id: int) -> str | None:
        if self._tables:
            try:
                return self._tables.move_name(move_id)
            except KeyError:
                return None
        return self._name("gMoveNames", move_id, 13)

    def species_name(self, species: int) -> str | None:
        if self._tables:
            try:
                return self._tables.species_name(species)
            except KeyError:
                return None
        return self._name("gSpeciesNames", species, 11)

    def species_types(self, species: int) -> tuple[int, int] | None:
        """The two ROM type bytes from ``gSpeciesInfo`` (Gen-3 keeps the split by type)."""
        if self._tables:
            try:
                return self._tables.species(species).types
            except KeyError:
                return None
        raw = self._row("gSpeciesInfo", species, 28)
        if raw is None or len(raw) < 8:
            return None
        return raw[6], raw[7]

    def species_gender_ratio(self, species: int) -> int | None:
        if self._tables:
            try:
                return self._tables.species(species).gender_ratio
            except KeyError:
                return None
        raw = self._row("gSpeciesInfo", species, 28)
        return raw[0x10] if raw is not None and len(raw) > 0x10 else None

    def species_base_stats(self, species: int) -> tuple[int, int, int, int, int, int] | None:
        if self._tables:
            try:
                row = self._tables.species(species)
                return (
                    row.base_hp,
                    row.base_atk,
                    row.base_def,
                    row.base_spe,
                    row.base_spa,
                    row.base_spd,
                )
            except KeyError:
                return None
        raw = self._row("gSpeciesInfo", species, 28)
        return tuple(raw[:6]) if raw is not None and len(raw) >= 6 else None

    def ability_name(self, ability: int) -> str | None:
        if self._tables:
            try:
                return self._tables.ability_name(ability)
            except KeyError:
                return None
        return self._name("gAbilityNames", ability, 13)

    def item_name(self, item_id: int) -> str | None:
        if self._tables:
            try:
                return self._tables.item_name(item_id)
            except KeyError:
                return None
        return self._name("gItems", item_id, 14)

    @staticmethod
    def type_name(type_id: int) -> str:
        return TYPE_NAMES[type_id] if 0 <= type_id < len(TYPE_NAMES) else "???"

    # -- rows with fields ---------------------------------------------------------------------

    def move_data(self, move_id: int) -> MoveData | None:
        if self._tables:
            try:
                row = self._tables.move(move_id)
                return MoveData(
                    row.move_id,
                    row.effect,
                    row.power,
                    row.type,
                    row.accuracy,
                    row.pp,
                    row.secondary_chance,
                    row.target,
                    row.priority,
                    row.flags,
                )
            except KeyError:
                return None
        raw = self._row("gBattleMoves", move_id)
        if raw is None or len(raw) < 12:
            return None
        effect, power, mtype, accuracy, pp, secondary, target, priority, flags = struct.unpack_from(
            "<BBBBBBBbB", raw, 0
        )
        return MoveData(
            move_id, effect, power, mtype, accuracy, pp, secondary, target, priority, flags
        )

    def item_data(self, item_id: int) -> ItemData | None:
        if self._tables:
            try:
                row = self._tables.item(item_id)
                return ItemData(
                    row.item_id,
                    row.name,
                    row.price,
                    row.hold_effect,
                    row.hold_effect_param,
                    row.pocket,
                    row.battle_usage,
                    row.battle_use_func,
                )
            except KeyError:
                return None
        raw = self._row("gItems", item_id)
        name = self.item_name(item_id)
        if raw is None or len(raw) < 44 or name is None:
            return None
        return ItemData(
            item_id=item_id,
            name=name,
            price=struct.unpack_from("<H", raw, 16)[0],
            hold_effect=raw[18],
            hold_effect_param=raw[19],
            pocket=raw[26],
            battle_usage=raw[32],
            battle_use_func=struct.unpack_from("<I", raw, 36)[0],
        )

    def item_battle_roles(self) -> dict[int, str]:
        """``{battle_use_func pointer: role}`` derived from complete verified ROM groups."""
        key = (self._profile.profile_id, "roles", 0)
        hit = self._cache.get(key)
        if isinstance(hit, dict):
            return hit
        groups: dict[int, list[tuple[ItemData, S.ItemEffectInfo | None]]] = {}
        for item_id in range(1, 375):
            data = self.item_data(item_id)
            if data is not None and data.battle_usage and data.battle_use_func:
                groups.setdefault(data.battle_use_func, []).append(
                    (data, self.item_effect(item_id))
                )
        roles: dict[int, str] = {}
        for pointer, rows in groups.items():
            data = [row[0] for row in rows]
            effects = [row[1] for row in rows if row[1] is not None]
            role = None
            if all(row.pocket == 3 for row in data):
                role = "ball"
            elif any(effect and effect.sacred_ash for effect in effects):
                role = "sacred_ash"
            elif any(
                effect and (effect.heals_hp or effect.revive or effect.cures) for effect in effects
            ):
                role = "medicine"
            elif any(effect and effect.heal_pp for effect in effects):
                role = "pp_restore"
            elif any(
                effect and (effect.stat_boost or effect.focus_energy or effect.guard_spec)
                for effect in effects
            ):
                role = "stat_booster"
            else:
                shape = (len(data), data[0].pocket, data[0].battle_usage)
                role = {
                    (2, 1, 2): "poke_doll",
                    (1, 2, 2): "poke_flute",
                    (1, 2, 3): "berry_pouch",
                }.get(shape)
            if role is not None:
                roles[pointer] = role
        if roles:
            self._cache[key] = roles
        return roles

    def item_battle_role(self, item_id: int) -> str | None:
        """``medicine`` / ``pp_restore`` / ``sacred_ash`` (need a party target), ``stat_booster`` /
        ``ball`` / ``poke_doll`` / ``poke_flute`` (immediate), ``none`` (not usable in battle), or
        ``None`` when the handler is not one of the reference handlers."""
        data = self.item_data(item_id)
        if data is None:
            return None
        if data.battle_usage == 0:
            return "none"
        return self.item_battle_roles().get(data.battle_use_func)

    def item_effect(self, item_id: int) -> S.ItemEffectInfo | None:
        """The decoded ``gItemEffectTable`` row of a medicine-like item (``None``: no row)."""
        if self._tables:
            try:
                effect = self._tables.item(item_id).effect
                return _effect_info(effect) if effect is not None else None
            except KeyError:
                return None
        spec = rom_table(self._profile, "gItemEffectTable")
        index = item_id - ITEM_POTION
        if spec is None or not 0 <= index < spec.count:
            return None
        key = (self._profile.profile_id, "effect", item_id)
        hit = self._cache.get(key)
        if isinstance(hit, S.ItemEffectInfo):
            return hit
        try:
            pointer = struct.unpack("<I", self._port.read_range(spec.address + index * 4, 4))[0]
            if not 0x0800_0000 <= pointer < 0x0A00_0000:
                return None  # no row behind this port (never cached: another ROM may have one)
            info = _effect_info(parse_item_effect(self._port.read_range(pointer, 10)))
        except (MemoryReadError, ValueError, struct.error):
            return None
        self._cache[key] = info
        return info

    @classmethod
    def clear_cache(cls) -> None:
        cls._cache.clear()
        cls._generated.clear()


def _effect_info(effect: ItemEffect) -> S.ItemEffectInfo:
    return S.ItemEffectInfo(
        heals_hp=effect.heals_hp,
        revive=effect.revive,
        cures=tuple(sorted(effect.cures)),
        heal_pp=effect.heal_pp is not None,
        stat_boost=effect.stat_boost,
        focus_energy=effect.focus_energy,
        guard_spec=effect.guard_spec,
        sacred_ash=effect.sacred_ash,
    )
