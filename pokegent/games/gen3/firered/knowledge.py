"""Default-off, read-only FireRed knowledge over the existing decoded ROM tables.

The caller supplies a trusted ``PluginSelection`` and the already-decoded immutable
``GameTables``. This module checks that both name the same FireRed profile, then answers only
native ``move``/``species``/``national_dex``/``item``/``ability``/``type``/``evolution``/``tmhm``
questions from those tables. Not a database, cache or plugin
registration or a verification claim: the game/profile/ROM labels are caller assertions, not
digest or live proof.
"""

from __future__ import annotations

from dataclasses import dataclass

from pokegent.games.gen3.firered.plugin import FIRERED_GAME
from pokegent.games.gen3.firered.rom.rom_tables import (
    EVOLUTIONS_PER_SPECIES,
    EvolutionData,
    GameTables,
    ItemData,
    ItemEffect,
    MoveData,
    SpeciesData,
)
from pokegent.games.plugin import PluginRefusal, PluginSelection, RefusalCode

SOURCE_LOCAL_ROM = "local_rom"
NATIVE_NAMESPACE = "native"
MAX_NAME_CHARS = 64
MAX_RECORD_VALUES = 8
_STATS = ("hp", "atk", "def", "spe", "spa", "spd")


@dataclass(frozen=True, slots=True)
class LocalRomRecord:
    """One detached local fact, or a typed unknown for a native id outside its table."""

    source_class: str
    game: str
    profile_id: str
    rom_sha1: str
    namespace: str
    record_kind: str
    record_id: int
    known: bool
    values: tuple[tuple[str, object], ...]


class FireRedKnowledge:
    """Native move/species/item facts from one already-decoded FireRed ``GameTables``."""

    def __init__(
        self,
        selection: PluginSelection,
        tables: GameTables,
        *,
        enabled: bool = False,
    ) -> None:
        if type(enabled) is not bool:
            raise TypeError("enabled must be exactly bool")
        if not isinstance(selection, PluginSelection):
            raise TypeError("selection must be a PluginSelection")
        if not isinstance(tables, GameTables):
            raise TypeError("tables must be GameTables")
        if not enabled:
            raise PluginRefusal(RefusalCode.DISABLED, "knowledge adapter is not explicitly enabled")

        descriptor = selection.descriptor
        if descriptor.game != FIRERED_GAME:
            raise PluginRefusal(
                RefusalCode.PROFILE_MISMATCH,
                f"selection game {descriptor.game!r} is not {FIRERED_GAME!r}",
            )
        profile_id = tables.profile_id
        if type(profile_id) is not str or not profile_id.strip():
            raise PluginRefusal(
                RefusalCode.PROFILE_MISMATCH, "tables have no non-blank selected profile"
            )
        if profile_id != descriptor.profile_id:
            raise PluginRefusal(
                RefusalCode.PROFILE_MISMATCH,
                f"tables profile {profile_id!r} != selection profile {descriptor.profile_id!r}",
            )
        self._selection = selection
        self._tables = tables

    def move(self, move_id: int) -> LocalRomRecord:
        move_id = _native_id(move_id, "move")
        try:
            data = self._tables.move(move_id)
        except KeyError:
            return self._record("move", move_id, known=False, values=())
        return self._record("move", move_id, known=True, values=_move_values(data))

    def species(self, species_id: int) -> LocalRomRecord:
        species_id = _native_id(species_id, "species")
        try:
            data = self._tables.species(species_id)
        except KeyError:
            return self._record("species", species_id, known=False, values=())
        return self._record("species", species_id, known=True, values=_species_values(data))

    def national_dex(self, species_id: int) -> LocalRomRecord:
        """National number for an existing native species row, from the supplied mapping."""
        species_id = _native_id(species_id, "species")
        try:
            self._tables.species(species_id)
        except KeyError:
            return self._record("national_dex", species_id, known=False, values=())
        number = self._tables.national_dex(species_id)
        if type(number) is not int or number <= 0:
            return self._record("national_dex", species_id, known=False, values=())
        return self._record(
            "national_dex",
            species_id,
            known=True,
            values=(("species_id", species_id), ("national_dex", number)),
        )

    def item(self, item_id: int) -> LocalRomRecord:
        item_id = _native_id(item_id, "item")
        try:
            data = self._tables.item(item_id)
        except KeyError:
            return self._record("item", item_id, known=False, values=())
        return self._record("item", item_id, known=True, values=_item_values(data))

    def item_restoration(self, item_id: int) -> LocalRomRecord:
        """Decoded restoration subset for one native bag item."""
        item_id = _native_id(item_id, "item")
        try:
            data = self._tables.item(item_id)
        except KeyError:
            return self._record("item_restoration", item_id, known=False, values=())
        if data.effect is None:
            return self._record("item_restoration", item_id, known=False, values=())
        return self._record(
            "item_restoration",
            item_id,
            known=True,
            values=_restoration_values(data.effect),
        )

    def ability(self, ability_id: int) -> LocalRomRecord:
        ability_id = _native_id(ability_id, "ability")
        try:
            data = self._tables.ability_name(ability_id)
        except KeyError:
            return self._record("ability", ability_id, known=False, values=())
        return self._record(
            "ability",
            ability_id,
            known=True,
            values=(("id", ability_id), ("name", _bounded_name(data, "ability"))),
        )

    def type(self, type_id: int) -> LocalRomRecord:
        if type(type_id) is not int or type_id < 0:
            raise ValueError("type id must be a non-negative plain int")
        try:
            data = self._tables.rom_type_name(type_id)
        except IndexError:
            return self._record("type", type_id, known=False, values=())
        return self._record(
            "type",
            type_id,
            known=True,
            values=(("id", type_id), ("name", _bounded_name(data, "type"))),
        )

    def evolutions(self, species_id: int) -> LocalRomRecord:
        species_id = _native_id(species_id, "species")
        try:
            self._tables.species(species_id)
        except KeyError:
            return self._record("evolution", species_id, known=False, values=())
        if not self._tables.has_evolution_data(species_id):
            return self._record("evolution", species_id, known=False, values=())
        row = self._tables.evolutions_for(species_id)
        if len(row) > EVOLUTIONS_PER_SPECIES:
            raise ValueError(
                f"evolution row for species {species_id} has {len(row)} slots; "
                f"at most {EVOLUTIONS_PER_SPECIES} are allowed"
            )
        return self._record(
            "evolution",
            species_id,
            known=True,
            values=(("evolutions", tuple(_evolution_triple(row_entry) for row_entry in row)),),
        )

    def tmhm(self, species_id: int, item_id: int) -> LocalRomRecord:
        """Native TM/HM compatibility: species index plus bag item id, not a move id."""
        species_id = _native_id(species_id, "species")
        item_id = _native_id(item_id, "item")
        try:
            self._tables.species(species_id)
            move_id = self._tables.tmhm_move(item_id)
            if type(move_id) is not int or move_id <= 0:
                return self._record("tmhm", species_id, known=False, values=())
            self._tables.move(move_id)
            can_learn = self._tables.can_learn_tmhm(species_id, item_id)
        except (KeyError, IndexError):
            return self._record("tmhm", species_id, known=False, values=())
        if can_learn is None:
            return self._record("tmhm", species_id, known=False, values=())
        return self._record(
            "tmhm",
            species_id,
            known=True,
            values=(
                ("item_id", item_id),
                ("move_id", move_id),
                ("can_learn", can_learn),
            ),
        )

    def _record(
        self,
        record_kind: str,
        record_id: int,
        *,
        known: bool,
        values: tuple[tuple[str, object], ...],
    ) -> LocalRomRecord:
        if len(values) > MAX_RECORD_VALUES:
            raise ValueError("record values exceed the bounded field count")
        descriptor = self._selection.descriptor
        return LocalRomRecord(
            source_class=SOURCE_LOCAL_ROM,
            game=descriptor.game,
            profile_id=descriptor.profile_id,
            rom_sha1=descriptor.rom_sha1,
            namespace=NATIVE_NAMESPACE,
            record_kind=record_kind,
            record_id=record_id,
            known=known,
            values=values,
        )


def _native_id(value: int, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} id must be a positive plain int")
    return value


def _bounded_name(value: str, kind: str = "move") -> str:
    if type(value) is not str or len(value) > MAX_NAME_CHARS:
        raise ValueError(f"{kind} name must be a string of at most {MAX_NAME_CHARS} characters")
    return value


def _evolution_triple(data: EvolutionData) -> tuple[int, int, int]:
    return (data.method, data.parameter, data.target_species)


def _move_values(data: MoveData) -> tuple[tuple[str, object], ...]:
    if data.power == 0:
        category = "zero_base_power"
    elif data.is_physical:
        category = "physical"
    elif data.is_special:
        category = "special"
    else:
        category = "unknown"
    return (
        ("id", data.move_id),
        ("name", _bounded_name(data.name)),
        ("power", data.power),
        ("type", data.type),
        ("accuracy", data.accuracy),
        ("pp", data.pp),
        ("category", category),
    )


def _species_values(data: SpeciesData) -> tuple[tuple[str, object], ...]:
    stats = data.base_stats
    return (
        ("types", tuple(data.types)),
        ("base_stats", tuple((name, stats[name]) for name in _STATS)),
        ("abilities", (data.abilities[0], data.abilities[1])),
    )


def _item_values(data: ItemData) -> tuple[tuple[str, object], ...]:
    return (
        ("id", data.item_id),
        ("name", _bounded_name(data.name, "item")),
        ("price", data.price),
        ("pocket", data.pocket),
        ("hold_effect", data.hold_effect),
        ("hold_effect_param", data.hold_effect_param),
        ("battle_usage", data.battle_usage),
    )


def _restoration_values(effect: ItemEffect) -> tuple[tuple[str, object], ...]:
    return (
        ("heal_hp", effect.heal_hp),
        ("heal_hp_full", effect.heal_hp_full),
        ("heal_hp_half", effect.heal_hp_half),
        ("revive", effect.revive),
        ("cures", tuple(sorted(effect.cures))),
        ("heal_pp", effect.heal_pp),
        ("heal_pp_all_moves", effect.heal_pp_all_moves),
        ("sacred_ash", effect.sacred_ash),
    )
