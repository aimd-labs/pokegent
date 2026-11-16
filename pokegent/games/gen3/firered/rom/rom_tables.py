"""Game tables read from the ROM through a ``MemoryPort``.

Nothing here is hard-coded game data: moves, species, items, the type chart, item effects and
names are decoded from the ROM bytes. What the code contains is the *layout* of each table
(entry size, field offsets), taken from the pret/pokefirered decompile at the commit pinned in
``source-lock.md`` and verified against the real ROM
(``historical local validation``):

==================  =====  ==================================================================
table               size   decompile source
==================  =====  ==================================================================
gBattleMoves        12     ``struct BattleMove`` (include/pokemon.h): 9 u8 fields. The ROM entry
                           is padded to 12 bytes (the 2004-era compiler rounds struct sizes up
                           to a multiple of 4), found by scanning the ROM, not assumed.
gSpeciesInfo        28     ``struct SpeciesInfo`` (include/pokemon.h): 26 bytes + 2 padding
gItems              44     ``struct Item`` (include/item.h)
gItemEffectTable    4      pointer per item id ``ITEM_POTION..ITEM_ENIGMA_BERRY``
gTypeEffectiveness  336    3-byte triples ``{atk, def, mult}``, markers 0xFE / 0xFF
gMoveNames          13     ``MOVE_NAME_LENGTH + 1`` Gen-3 text
gSpeciesNames       11     ``POKEMON_NAME_LENGTH + 1``
gAbilityNames       13     ``ABILITY_NAME_LENGTH + 1``
gTypeNames          7      ``TYPE_NAME_LENGTH + 1``
gTMHMLearnsets      8      one bit per TM01..HM08, indexed by species id
gTMHMMoves          116    58 u16 move ids, indexed by TM01..TM50 then HM01..HM08
==================  =====  ==================================================================

Base addresses come from ``references/rom_profiles.yaml``: the profile's ``rom_tables`` section
(entries with ``source`` and ``verified_by``; kept apart from ``symbols`` because ROM-region
symbols there are function entry points). A table that is in neither place is a
``ProfileError``; an address is never guessed.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pokegent.games.gen3 import mechanics_constants as K
from pokegent.games.gen3.firered.rom import constants as C
from pokegent.games.gen3.firered.rom.text import decode_gen3_text

if TYPE_CHECKING:
    from pokegent.games.gen3.firered.reader.memory_port import MemoryPort
    from pokegent.games.gen3.firered.rom.rom_data import RomProfile

# -- layout constants (see the module docstring) --------------------------------------------
MOVE_ENTRY_SIZE = 12
SPECIES_ENTRY_SIZE = 28
ITEM_ENTRY_SIZE = 44
TYPE_CHART_SIZE = 336
MOVE_NAME_STRIDE = 13
SPECIES_NAME_STRIDE = 11
ABILITY_NAME_STRIDE = 13
TYPE_NAME_STRIDE = 7
TMHM_LEARNSET_ENTRY_SIZE = 8
TMHM_LEARNSET_COUNT = C.NUM_SPECIES
TMHM_MOVE_COUNT = 58
EVOLUTION_ENTRY_SIZE = 8
EVOLUTIONS_PER_SPECIES = 5
EVOLUTION_ITEM_METHOD = 7  # include/constants/evolution.h: EVO_ITEM
ABILITIES_COUNT = 78  # include/constants/abilities.h
ITEM_POTION = 13  # first item with an entry in gItemEffectTable (include/constants/items.h)
ITEM_ENIGMA_BERRY = 175  # last entry (LAST_BERRY_INDEX)
ITEM_EFFECT_COUNT = ITEM_ENIGMA_BERRY - ITEM_POTION + 1

#: symbol names in references/rom_profiles.yaml
SYMBOLS = (
    "gBattleMoves",
    "gSpeciesInfo",
    "gTypeEffectiveness",
    "gItems",
    "gItemEffectTable",
    "gAbilityNames",
    "gMoveNames",
    "gSpeciesNames",
    "gTypeNames",
    "gTMHMLearnsets",
    "gTMHMMoves",
    "gEvolutionTable",
)


class TablesError(Exception):
    """A table could not be read or failed a structural check."""


def rom_table_addresses(
    profile: RomProfile, path: str | os.PathLike[str] | None = None
) -> dict[str, int]:
    """Base address of every table in :data:`SYMBOLS` for ``profile``.

    A profile ``symbols`` entry wins (a profile built in code may register the tables there);
    otherwise the ``rom_tables`` section of the profile file is used. Missing -> ProfileError.
    """
    import yaml

    from pokegent.games.gen3.firered.rom.rom_data import ProfileError, default_profiles_path

    from_file: dict[str, dict] = {}
    missing = [name for name in SYMBOLS if not profile.has_symbol(name)]
    if missing:
        try:
            with open(path or default_profiles_path(), encoding="utf-8") as handle:
                doc = yaml.safe_load(handle)
            from_file = (doc.get("profiles", {}).get(profile.profile_id, {}) or {}).get(
                "rom_tables"
            ) or {}
        except (OSError, yaml.YAMLError, AttributeError) as exc:
            raise ProfileError(f"cannot read rom_tables for {profile.profile_id}: {exc}") from exc
    out: dict[str, int] = {}
    for name in SYMBOLS:
        if profile.has_symbol(name):
            out[name] = profile.symbol(name).address
            continue
        entry = from_file.get(name)
        if not entry or "address" not in entry or not entry.get("source"):
            raise ProfileError(
                f"profile {profile.profile_id} has no verified address for table {name!r}"
            )
        address = int(entry["address"])
        if not 0x0800_0000 <= address < 0x0A00_0000:
            raise ProfileError(f"table {name}: 0x{address:08X} is not a ROM address")
        out[name] = address
    return out


# -- data classes -----------------------------------------------------------------------------


@dataclass(frozen=True)
class MoveData:
    """``struct BattleMove`` (include/pokemon.h) plus the name."""

    move_id: int
    name: str
    effect: int
    power: int
    type: int
    accuracy: int  # 0 = never misses (the game's convention)
    pp: int
    secondary_chance: int
    target: int  # constants.MOVE_TARGET_*
    priority: int
    flags: int  # constants.FLAG_*

    @property
    def is_status(self) -> bool:
        """Zero base power: the damage formula is not used (status or fixed/variable move)."""
        return self.power == 0

    @property
    def is_physical(self) -> bool:
        return K.is_physical_type(self.type)

    @property
    def is_special(self) -> bool:
        return K.is_special_type(self.type)

    @property
    def makes_contact(self) -> bool:
        return bool(self.flags & K.FLAG_MAKES_CONTACT)

    @property
    def protect_affected(self) -> bool:
        return bool(self.flags & K.FLAG_PROTECT_AFFECTED)


@dataclass(frozen=True)
class SpeciesData:
    """``struct SpeciesInfo`` (include/pokemon.h)."""

    species_id: int
    name: str
    base_hp: int
    base_atk: int
    base_def: int
    base_spe: int
    base_spa: int
    base_spd: int
    types: tuple[int, int]
    catch_rate: int
    exp_yield: int
    ev_yield: tuple[int, int, int, int, int, int]  # hp, atk, def, spe, spa, spd
    item_common: int
    item_rare: int
    gender_ratio: int
    egg_cycles: int
    friendship: int
    growth_rate: int
    egg_groups: tuple[int, int]
    abilities: tuple[int, int]
    safari_flee_rate: int

    @property
    def base_stats(self) -> dict[str, int]:
        return {
            "hp": self.base_hp,
            "atk": self.base_atk,
            "def": self.base_def,
            "spe": self.base_spe,
            "spa": self.base_spa,
            "spd": self.base_spd,
        }


@dataclass(frozen=True)
class EvolutionData:
    """One ``struct Evolution`` record from ``gEvolutionTable``."""

    method: int
    parameter: int
    target_species: int


@dataclass(frozen=True)
class ItemEffect:
    """The battle-relevant decoding of ``gItemEffectTable[item]`` (src/pokemon.c
    ``PokemonUseItemEffects``; flags in include/constants/item_effects.h)."""

    heal_hp: int | None = None  # absolute HP restored; None = no fixed amount
    heal_hp_full: bool = False  # ITEM6_HEAL_HP_FULL
    heal_hp_half: bool = False  # ITEM6_HEAL_HP_HALF (Revive)
    revive: bool = False
    cures: frozenset[str] = frozenset()  # constants.STATUS_*, "confusion", "infatuation"
    heal_pp: int | None = None  # PP restored per move; 0x7F = full
    heal_pp_all_moves: bool = False  # Elixir family; False = one move (Ether, Leppa)
    stat_boost: tuple[tuple[str, int], ...] = ()  # (stat, stages): ("atk", 1) ... ("acc", 1)
    focus_energy: bool = False  # Dire Hit
    guard_spec: bool = False  # Mist for 5 turns
    sacred_ash: bool = False  # revives the whole party (field only)
    ev_gains: tuple[tuple[str, int], ...] = ()  # permanent EV gains (vitamins/EV berries)
    pp_up: bool = False
    pp_max: bool = False
    level_up: bool = False
    raw: bytes = b""

    @property
    def heals_hp(self) -> bool:
        return self.heal_hp is not None or self.heal_hp_full or self.heal_hp_half


@dataclass(frozen=True)
class ItemData:
    """``struct Item`` (include/item.h)."""

    item_id: int
    name: str
    price: int
    hold_effect: int
    hold_effect_param: int
    importance: int
    pocket: int  # include/constants/item.h POCKET_* (1 items, 2 key items, 3 balls, ...)
    item_type: int
    field_use_func: int
    battle_usage: int  # 0 = not usable in battle (src/item.c ItemId_GetBattleUsage)
    battle_use_func: int
    secondary_id: int
    effect: ItemEffect | None

    @property
    def battle_usable(self) -> bool:
        return self.battle_usage != 0

    @property
    def is_ball(self) -> bool:
        return self.pocket == POCKET_BALLS


POCKET_ITEMS = 1
POCKET_KEY_ITEMS = 2
POCKET_BALLS = 3
POCKET_TM_HM = 4
POCKET_BERRIES = 5


def parse_item_effect(raw: bytes) -> ItemEffect:
    """Decode one ``gItemEffectTable`` entry exactly as ``PokemonUseItemEffects`` walks it."""
    if len(raw) < 6:
        raise TablesError(f"item effect needs 6 header bytes, got {len(raw)}")
    b0, b1, b2, b3, b4, b5 = raw[:6]
    cures: set[str] = set()
    if b3 & 0x1:
        cures.add("confusion")
    if b3 & 0x2:
        cures.add(K.STATUS_PARALYSIS)
    if b3 & 0x4:
        cures.add(K.STATUS_FREEZE)
    if b3 & 0x8:
        cures.add(K.STATUS_BURN)
    if b3 & 0x10:
        cures.add(K.STATUS_POISON)  # also clears toxic (STATUS1_PSN_ANY | TOXIC_COUNTER)
    if b3 & 0x20:
        cures.add(K.STATUS_SLEEP)
    if b0 & 0x80:
        cures.add("infatuation")
    boosts: list[tuple[str, int]] = []
    if b0 & 0x0F:
        boosts.append(("atk", b0 & 0x0F))
    if b1 & 0xF0:
        boosts.append(("def", b1 >> 4))
    if b1 & 0x0F:
        boosts.append(("spe", b1 & 0x0F))
    if b2 & 0x0F:
        boosts.append(("spa", b2 & 0x0F))
    if b2 & 0xF0:
        boosts.append(("acc", b2 >> 4))

    # Arguments start at index 6 and are consumed in ITEM4 bit order (EV_HP, EV_ATK, HEAL_HP,
    # HEAL_PP): src/pokemon.c case 4 of PokemonUseItemEffects.
    idx = 6
    ev_gains: list[tuple[str, int]] = []
    # Permanent EV flags span bytes 4 and 5. Each flag consumes one argument before the
    # friendship arguments (PokemonUseItemEffects case 4/5).
    for enabled, stat in (
        (b4 & 0x01, "hp"),
        (b4 & 0x02, "attack"),
        (b5 & 0x01, "defense"),
        (b5 & 0x02, "speed"),
        (b5 & 0x04, "sp_defense"),
        (b5 & 0x08, "sp_attack"),
    ):
        if enabled:
            amount = raw[idx] if idx < len(raw) else 0
            idx += 1
            ev_gains.append((stat, amount))
    heal_hp: int | None = None
    full = half = False
    if b4 & 0x04:  # ITEM4_HEAL_HP
        amount = raw[idx] if idx < len(raw) else None
        idx += 1
        if amount == 0xFF:  # ITEM6_HEAL_HP_FULL
            full = True
        elif amount == 0xFE:  # ITEM6_HEAL_HP_HALF
            half = True
        elif amount is not None and amount != 0xFD:  # 0xFD = level-up HP (Rare Candy: not here)
            heal_hp = amount
    heal_pp: int | None = None
    pp_all = False
    if b4 & 0x08:  # ITEM4_HEAL_PP_ALL bit; with HEAL_PP_ONE (0x10) set it heals ONE move
        pp_all = not (b4 & 0x10)
        heal_pp = raw[idx] if idx < len(raw) else None
    return ItemEffect(
        heal_hp=heal_hp,
        heal_hp_full=full,
        heal_hp_half=half,
        revive=bool(b4 & 0x40),
        cures=frozenset(cures),
        heal_pp=heal_pp,
        heal_pp_all_moves=pp_all,
        stat_boost=tuple(boosts),
        focus_energy=bool(b0 & 0x30),
        guard_spec=bool(b3 & 0x80),
        sacred_ash=bool(b0 & 0x40),
        ev_gains=tuple(ev_gains),
        pp_up=bool(b4 & 0x20),
        pp_max=bool(b5 & 0x10),
        level_up=bool(b3 & 0x40),
        raw=bytes(raw),
    )


# -- type chart -------------------------------------------------------------------------------


class TypeChart:
    """``gTypeEffectiveness`` with its foresight and end markers (src/battle_main.c).

    The lookup replays ``Cmd_typecalc`` (src/battle_script_commands.c): entries are scanned in
    ROM order; the ``TYPE_FORESIGHT`` marker ends the scan when the defender is identified
    (Foresight / Odor Sleuth); entries after the marker are the ghost immunities that
    Foresight removes.
    """

    def __init__(self, raw: bytes) -> None:
        if len(raw) % 3 != 0:
            raise TablesError(f"type chart length {len(raw)} is not a multiple of 3")
        entries = tuple((raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw), 3))
        if not entries or entries[-1][0] != K.TYPE_ENDTABLE:
            raise TablesError("type chart does not end with the TYPE_ENDTABLE marker")
        self._entries = entries
        self.raw = bytes(raw)
        self.foresight_index = next(
            (i for i, e in enumerate(entries) if e[0] == K.TYPE_FORESIGHT), None
        )
        if self.foresight_index is None:
            raise TablesError("type chart has no TYPE_FORESIGHT marker")

    @property
    def entries(self) -> tuple[tuple[int, int, int], ...]:
        """Every triple in ROM order, markers included."""
        return self._entries

    def _matching(self, move_type: int, t1: int, t2: int, foresight: bool) -> list[int]:
        """Multipliers (x10 units) in the order ``Cmd_typecalc`` applies them."""
        out: list[int] = []
        for atk, dfn, mult in self._entries:
            if atk == K.TYPE_ENDTABLE:
                break
            if atk == K.TYPE_FORESIGHT:
                if foresight:
                    break
                continue
            if atk != move_type:
                continue
            if dfn == t1:
                out.append(mult)
            if dfn == t2 and t1 != t2:
                out.append(mult)
        return out

    def multiplier(
        self, move_type: int, def_type1: int, def_type2: int, *, foresight: bool = False
    ) -> float:
        """Net multiplier (0, 0.25, 0.5, 1, 2, 4). Mathematical product, no truncation."""
        value = 1.0
        for mult in self._matching(move_type, def_type1, def_type2, foresight):
            value *= mult / K.TYPE_MUL_NORMAL
        return value

    def apply(
        self,
        damage: int,
        move_type: int,
        def_type1: int,
        def_type2: int,
        *,
        foresight: bool = False,
    ) -> int:
        """Apply the chart to ``damage`` with the game's per-step integer truncation.

        ``ModulateDmgByType``: ``damage = damage * mult / 10``; a result of 0 becomes 1 unless
        the multiplier is 0 (src/battle_script_commands.c).
        """
        for mult in self._matching(move_type, def_type1, def_type2, foresight):
            damage = damage * mult // 10
            if damage == 0 and mult != 0:
                damage = 1
        return damage


# -- the tables -------------------------------------------------------------------------------


def _text(raw: bytes) -> str:
    return decode_gen3_text(raw)


def _tmhm_bit(item_id_or_move_id: int, tmhm_moves: tuple[int, ...]) -> int | None:
    if 289 <= item_id_or_move_id <= 346:
        return item_id_or_move_id - 289
    try:
        return tmhm_moves.index(item_id_or_move_id)
    except ValueError:
        return None


class GameTables:
    """Immutable decoded game data for one ROM build. Build it with :meth:`from_port`."""

    def __init__(
        self,
        *,
        moves: tuple[MoveData, ...],
        species: tuple[SpeciesData, ...],
        items: tuple[ItemData, ...],
        type_chart: TypeChart,
        ability_names: tuple[str, ...],
        type_names: tuple[str, ...],
        profile_id: str = "",
        tmhm_learnsets: tuple[bytes, ...] = (),
        tmhm_moves: tuple[int, ...] = (),
        evolutions: tuple[tuple[EvolutionData, ...], ...] = (),
    ) -> None:
        self._moves = moves
        self._species = species
        self._items = items
        self._type_chart = type_chart
        self._ability_names = ability_names
        self._type_names = type_names
        self.profile_id = profile_id
        self._tmhm_learnsets = tmhm_learnsets
        self._tmhm_moves = tmhm_moves
        self._evolutions = evolutions

    # -- construction -------------------------------------------------------------------------

    @classmethod
    def from_port(cls, port: MemoryPort, profile: RomProfile) -> GameTables:
        """Read every table through ``port``. A missing table address raises ``ProfileError``;
        malformed data raises :class:`TablesError`. Never guesses an address."""
        sym = rom_table_addresses(profile)

        raw_moves = port.read_range(sym["gBattleMoves"], C.MOVES_COUNT * MOVE_ENTRY_SIZE)
        move_names = _read_names(port, sym["gMoveNames"], C.MOVES_COUNT, MOVE_NAME_STRIDE)
        moves = tuple(
            _decode_move(i, move_names[i], raw_moves[i * MOVE_ENTRY_SIZE :][:MOVE_ENTRY_SIZE])
            for i in range(C.MOVES_COUNT)
        )

        n_species = C.NUM_SPECIES  # 412: ids 0..411 (SPECIES_EGG = 412 has no entry)
        raw_species = port.read_range(sym["gSpeciesInfo"], n_species * SPECIES_ENTRY_SIZE)
        species_names = _read_names(port, sym["gSpeciesNames"], n_species, SPECIES_NAME_STRIDE)
        species = tuple(
            _decode_species(
                i,
                species_names[i],
                raw_species[i * SPECIES_ENTRY_SIZE :][:SPECIES_ENTRY_SIZE],
            )
            for i in range(n_species)
        )

        raw_items = port.read_range(sym["gItems"], C.ITEMS_COUNT * ITEM_ENTRY_SIZE)
        effect_ptrs = struct.unpack(
            f"<{ITEM_EFFECT_COUNT}I",
            port.read_range(sym["gItemEffectTable"], ITEM_EFFECT_COUNT * 4),
        )
        items = tuple(
            _decode_item(
                i,
                raw_items[i * ITEM_ENTRY_SIZE :][:ITEM_ENTRY_SIZE],
                _read_item_effect(port, i, effect_ptrs),
            )
            for i in range(C.ITEMS_COUNT)
        )

        chart = TypeChart(port.read_range(sym["gTypeEffectiveness"], TYPE_CHART_SIZE))
        ability_names = _read_names(
            port, sym["gAbilityNames"], ABILITIES_COUNT, ABILITY_NAME_STRIDE
        )
        type_names = _read_names(port, sym["gTypeNames"], K.NUMBER_OF_MON_TYPES, TYPE_NAME_STRIDE)
        raw_learnsets = port.read_range(
            sym["gTMHMLearnsets"], TMHM_LEARNSET_COUNT * TMHM_LEARNSET_ENTRY_SIZE
        )
        tmhm_moves = struct.unpack(
            f"<{TMHM_MOVE_COUNT}H",
            port.read_range(sym["gTMHMMoves"], TMHM_MOVE_COUNT * 2),
        )
        raw_evolutions = port.read_range(
            sym["gEvolutionTable"],
            n_species * EVOLUTIONS_PER_SPECIES * EVOLUTION_ENTRY_SIZE,
        )
        evolutions = tuple(
            tuple(
                EvolutionData(*struct.unpack_from("<HHH", raw_evolutions, offset))
                for offset in range(
                    species_id * EVOLUTIONS_PER_SPECIES * EVOLUTION_ENTRY_SIZE,
                    (species_id + 1) * EVOLUTIONS_PER_SPECIES * EVOLUTION_ENTRY_SIZE,
                    EVOLUTION_ENTRY_SIZE,
                )
            )
            for species_id in range(n_species)
        )
        return cls(
            moves=moves,
            species=species,
            items=items,
            type_chart=chart,
            ability_names=ability_names,
            type_names=type_names,
            profile_id=profile.profile_id,
            tmhm_learnsets=tuple(
                raw_learnsets[i * TMHM_LEARNSET_ENTRY_SIZE : (i + 1) * TMHM_LEARNSET_ENTRY_SIZE]
                for i in range(TMHM_LEARNSET_COUNT)
            ),
            tmhm_moves=tuple(tmhm_moves),
            evolutions=evolutions,
        )

    @classmethod
    def from_rom_file(
        cls, path: str | os.PathLike[str] | None = None, profile_id: str | None = None
    ) -> GameTables:
        """Load the generated table cache for a verified user-owned ROM.

        The first call verifies and decodes the ROM into the private SHA-1-keyed cache.  Later
        calls do not rescan table bytes.  ``profile_id`` remains accepted for API compatibility
        and is checked against the generated document.
        """
        from pokegent.games.gen3.firered.rom.rom_cache import ensure

        cache = ensure(path)
        return cls.from_cache(cache, profile_id=profile_id)

    @classmethod
    def from_rom_image(cls, image: bytes, profile: RomProfile) -> GameTables:
        """Use the private cache for the configured ROM; retain synthetic-fixture decoding."""
        from pokegent.games.gen3.firered.rom.rom_data import ROM_PATH_ENV

        configured = os.environ.get(ROM_PATH_ENV)
        if configured:
            path = Path(configured).expanduser()
            try:
                if hashlib.sha1(image).digest() == hashlib.sha1(path.read_bytes()).digest():
                    return cls.from_rom_file(path, profile.profile_id)
            except OSError:
                pass
        return cls.from_port(RomImagePort(image), profile)

    @classmethod
    def from_cache(
        cls, directory: str | os.PathLike[str], *, profile_id: str | None = None
    ) -> GameTables:
        """Rehydrate immutable tables from ``runtime_tables.json`` in a verified cache."""
        cache_path = Path(directory) / "runtime_tables.json"
        try:
            document = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise TablesError(f"cannot read generated ROM tables {cache_path}: {exc}") from exc
        found_profile = str(document.get("profile", ""))
        if profile_id is not None and found_profile != profile_id:
            raise TablesError(
                f"generated ROM tables are for {found_profile!r}, expected {profile_id!r}"
            )

        def effect(value: dict | None) -> ItemEffect | None:
            if value is None:
                return None
            return ItemEffect(
                heal_hp=value["heal_hp"],
                heal_hp_full=bool(value["heal_hp_full"]),
                heal_hp_half=bool(value["heal_hp_half"]),
                revive=bool(value["revive"]),
                cures=frozenset(value["cures"]),
                heal_pp=value["heal_pp"],
                heal_pp_all_moves=bool(value["heal_pp_all_moves"]),
                stat_boost=tuple((str(name), int(stages)) for name, stages in value["stat_boost"]),
                focus_energy=bool(value["focus_energy"]),
                guard_spec=bool(value["guard_spec"]),
                sacred_ash=bool(value["sacred_ash"]),
                ev_gains=tuple((str(name), int(amount)) for name, amount in value["ev_gains"]),
                pp_up=bool(value["pp_up"]),
                pp_max=bool(value["pp_max"]),
                level_up=bool(value["level_up"]),
                raw=bytes(value["raw"]),
            )

        moves = tuple(MoveData(**row) for row in document["moves"])
        species = tuple(
            SpeciesData(
                **{
                    **row,
                    "types": tuple(row["types"]),
                    "ev_yield": tuple(row["ev_yield"]),
                    "egg_groups": tuple(row["egg_groups"]),
                    "abilities": tuple(row["abilities"]),
                }
            )
            for row in document["species"]
        )
        items = tuple(
            ItemData(**{**row, "effect": effect(row["effect"])}) for row in document["items"]
        )
        evolutions = tuple(
            tuple(EvolutionData(**entry) for entry in rows) for rows in document["evolutions"]
        )
        return cls(
            moves=moves,
            species=species,
            items=items,
            type_chart=TypeChart(bytes(document["type_chart"])),
            ability_names=tuple(document["ability_names"]),
            type_names=tuple(document["type_names"]),
            profile_id=found_profile,
            tmhm_learnsets=tuple(bytes.fromhex(row) for row in document["tmhm_learnsets"]),
            tmhm_moves=tuple(document["tmhm_moves"]),
            evolutions=evolutions,
        )

    def runtime_document(self) -> dict:
        """JSON-compatible, semantically complete representation for the private cache."""

        def item_effect(value: ItemEffect | None) -> dict | None:
            if value is None:
                return None
            return {
                "heal_hp": value.heal_hp,
                "heal_hp_full": value.heal_hp_full,
                "heal_hp_half": value.heal_hp_half,
                "revive": value.revive,
                "cures": sorted(value.cures),
                "heal_pp": value.heal_pp,
                "heal_pp_all_moves": value.heal_pp_all_moves,
                "stat_boost": [list(row) for row in value.stat_boost],
                "focus_energy": value.focus_energy,
                "guard_spec": value.guard_spec,
                "sacred_ash": value.sacred_ash,
                "ev_gains": [list(row) for row in value.ev_gains],
                "pp_up": value.pp_up,
                "pp_max": value.pp_max,
                "level_up": value.level_up,
                "raw": list(value.raw),
            }

        return {
            "profile": self.profile_id,
            "moves": [vars(row) for row in self._moves],
            "species": [vars(row) for row in self._species],
            "items": [{**vars(row), "effect": item_effect(row.effect)} for row in self._items],
            "type_chart": list(self._type_chart.raw),
            "ability_names": list(self._ability_names),
            "type_names": list(self._type_names),
            "tmhm_learnsets": [row.hex() for row in self._tmhm_learnsets],
            "tmhm_moves": list(self._tmhm_moves),
            "evolutions": [[vars(entry) for entry in rows] for rows in self._evolutions],
        }

    # -- lookups ------------------------------------------------------------------------------

    def move(self, move_id: int) -> MoveData:
        if not 0 <= move_id < len(self._moves):
            raise KeyError(f"move id {move_id} outside 0..{len(self._moves) - 1}")
        return self._moves[move_id]

    def species(self, species_id: int) -> SpeciesData:
        if not 0 <= species_id < len(self._species):
            raise KeyError(f"species id {species_id} outside 0..{len(self._species) - 1}")
        return self._species[species_id]

    def item(self, item_id: int) -> ItemData:
        if not 0 <= item_id < len(self._items):
            raise KeyError(f"item id {item_id} outside 0..{len(self._items) - 1}")
        return self._items[item_id]

    @property
    def type_chart(self) -> TypeChart:
        return self._type_chart

    @property
    def moves(self) -> tuple[MoveData, ...]:
        return self._moves

    @property
    def all_species(self) -> tuple[SpeciesData, ...]:
        return self._species

    @property
    def items(self) -> tuple[ItemData, ...]:
        return self._items

    # -- names (display only; never used in logic) ---------------------------------------------

    def move_name(self, move_id: int) -> str:
        return self.move(move_id).name

    def species_name(self, species_id: int) -> str:
        return self.species(species_id).name

    def ability_name(self, ability_id: int) -> str:
        if not 0 <= ability_id < len(self._ability_names):
            raise KeyError(f"ability id {ability_id} outside 0..{len(self._ability_names) - 1}")
        return self._ability_names[ability_id]

    def type_name(self, type_id: int) -> str:
        if not 0 <= type_id < len(self._type_names):
            raise KeyError(f"type id {type_id} outside 0..{len(self._type_names) - 1}")
        return K.TYPE_NAMES[type_id]  # readable English; the ROM abbreviates (FIGHT, ELECTR)

    def item_name(self, item_id: int) -> str:
        return self.item(item_id).name

    def can_learn_tmhm(self, species: int, item_id_or_move_id: int) -> bool | None:
        """Return the ROM's TM/HM compatibility bit, or ``None`` when it is unknown."""
        if not 0 <= species < len(self._tmhm_learnsets):
            return None
        bit = _tmhm_bit(item_id_or_move_id, self._tmhm_moves)
        if bit is None:
            return None
        row = self._tmhm_learnsets[species]
        return bool(row[bit // 8] & (1 << (bit % 8)))

    @property
    def tmhm_moves(self) -> tuple[int, ...]:
        """The ROM's TM01..TM50/HM01..HM08 move ids in bag-item order."""
        return self._tmhm_moves

    def tmhm_move(self, item_id: int) -> int | None:
        """Return the move taught by a TM/HM item, or ``None`` for any other item."""
        index = item_id - 289
        if not 0 <= index < len(self._tmhm_moves):
            return None
        return self._tmhm_moves[index]

    def evolutions_for(self, species: int) -> tuple[EvolutionData, ...]:
        if not 0 <= species < len(self._evolutions):
            return ()
        return self._evolutions[species]

    def evolution_for_item(self, species: int, item_id: int) -> EvolutionData | None:
        return next(
            (
                evolution
                for evolution in self.evolutions_for(species)
                if evolution.method == EVOLUTION_ITEM_METHOD
                and evolution.parameter == item_id
                and evolution.target_species
            ),
            None,
        )

    def rom_type_name(self, type_id: int) -> str:
        """The abbreviation the game prints (``FIGHT``, ``ELECTR``, ``PSYCHC``)."""
        return self._type_names[type_id]


# -- decoding helpers ---------------------------------------------------------------------------


def _read_names(port: MemoryPort, base: int, count: int, stride: int) -> tuple[str, ...]:
    raw = port.read_range(base, count * stride)
    return tuple(_text(raw[i * stride : (i + 1) * stride]) for i in range(count))


def _decode_move(move_id: int, name: str, raw: bytes) -> MoveData:
    effect, power, mtype, accuracy, pp, secondary, target, priority, flags = struct.unpack_from(
        "<BBBBBBBbB", raw, 0
    )
    return MoveData(
        move_id=move_id,
        name=name,
        effect=effect,
        power=power,
        type=mtype,
        accuracy=accuracy,
        pp=pp,
        secondary_chance=secondary,
        target=target,
        priority=priority,
        flags=flags,
    )


def _decode_species(species_id: int, name: str, raw: bytes) -> SpeciesData:
    (hp, atk, dfn, spe, spa, spd) = raw[0:6]
    ev_word = raw[0x0A] | (raw[0x0B] << 8)
    ev = tuple((ev_word >> (2 * k)) & 3 for k in range(6))  # HP, ATK, DEF, SPE, SPA, SPD
    common, rare = struct.unpack_from("<HH", raw, 0x0C)
    return SpeciesData(
        species_id=species_id,
        name=name,
        base_hp=hp,
        base_atk=atk,
        base_def=dfn,
        base_spe=spe,
        base_spa=spa,
        base_spd=spd,
        types=(raw[6], raw[7]),
        catch_rate=raw[8],
        exp_yield=raw[9],
        ev_yield=(ev[0], ev[1], ev[2], ev[3], ev[4], ev[5]),
        item_common=common,
        item_rare=rare,
        gender_ratio=raw[0x10],
        egg_cycles=raw[0x11],
        friendship=raw[0x12],
        growth_rate=raw[0x13],
        egg_groups=(raw[0x14], raw[0x15]),
        abilities=(raw[0x16], raw[0x17]),
        safari_flee_rate=raw[0x18],
    )


def _read_item_effect(
    port: MemoryPort, item_id: int, pointers: tuple[int, ...]
) -> ItemEffect | None:
    if not ITEM_POTION <= item_id <= ITEM_ENIGMA_BERRY:
        return None
    ptr = pointers[item_id - ITEM_POTION]
    if ptr == 0:
        return None
    if ptr < 0x0800_0000:
        raise TablesError(f"item {item_id}: effect pointer 0x{ptr:08X} is not a ROM address")
    # Entries are 6..10 bytes; read 10 and trim to what the header says it needs (the bytes
    # after the arguments belong to the next array and are never interpreted).
    return parse_item_effect(port.read_range(ptr, 10))


def _decode_item(item_id: int, raw: bytes, effect: ItemEffect | None) -> ItemData:
    # struct Item (include/item.h): name[14] @0, itemId u16 @14, price u16 @16, holdEffect @18,
    # holdEffectParam @19, description ptr @20, importance @24, registrability @25, pocket @26,
    # type @27, fieldUseFunc ptr @28, battleUsage @32, battleUseFunc ptr @36, secondaryId @40
    stored_id, price = struct.unpack_from("<HH", raw, 14)
    # Unused slots (ITEM_034 ... ) are filled with "?" names and itemId 0; every other entry
    # must store its own index, which pins the table base and stride.
    if stored_id not in (0, item_id):
        raise TablesError(f"gItems[{item_id}] stores itemId {stored_id}: wrong table base/stride")
    return ItemData(
        item_id=item_id,
        name=_text(raw[0:14]),
        price=price,
        hold_effect=raw[18],
        hold_effect_param=raw[19],
        importance=raw[24],
        pocket=raw[26],
        item_type=raw[27],
        field_use_func=struct.unpack_from("<I", raw, 28)[0],
        battle_usage=raw[32],
        battle_use_func=struct.unpack_from("<I", raw, 36)[0],
        secondary_id=struct.unpack_from("<I", raw, 40)[0],
        effect=effect,
    )


class RomImagePort:
    """Read-only ``MemoryPort`` over a ROM image held in memory (bus address 0x08000000+)."""

    def __init__(self, data: bytes) -> None:
        self._data = bytes(data)

    def _off(self, addr: int, size: int) -> int:
        off = addr & 0x01FF_FFFF
        if (addr >> 24) not in (0x08, 0x09) or off + size > len(self._data):
            from pokegent.games.gen3.firered.reader.memory_port import MemoryReadError

            raise MemoryReadError(addr, size, "outside the ROM image")
        return off

    def read_u8(self, addr: int) -> int:
        return self._data[self._off(addr, 1)]

    def read_u16(self, addr: int) -> int:
        off = self._off(addr, 2)
        return int.from_bytes(self._data[off : off + 2], "little")

    def read_u32(self, addr: int) -> int:
        off = self._off(addr, 4)
        return int.from_bytes(self._data[off : off + 4], "little")

    def read_range(self, addr: int, size: int) -> bytes:
        if size < 0:
            raise ValueError("size must be >= 0")
        off = self._off(addr, size)
        return self._data[off : off + size]
