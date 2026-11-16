"""Dict-backed name/move/item lookups with the :class:`RomTables` surface.

``BattleBlock.lookups`` is captured with the snapshot (see :mod:`reader`); :class:`Lookups`
exposes it
through the same method names the facts code uses on a live ``RomTables``, so builders, legality and
:class:`~pokegent.actions.battle.view.BasicFacts` never touch the ROM and replay identically.
"""

from __future__ import annotations

from pokegent.actions.battle.romtables import MoveData, RomTables
from pokegent.actions.battle.state import BattleLookups, ItemInfo, MoveInfo


class Lookups:
    def __init__(self, data: BattleLookups) -> None:
        self._d = data

    # names ------------------------------------------------------------------------------------
    def species_name(self, species: int) -> str | None:
        return self._d.species.get(species)

    def ability_name(self, ability: int) -> str | None:
        return self._d.abilities.get(ability)

    def move_name(self, move_id: int) -> str | None:
        info = self._d.moves.get(move_id)
        return info.name if info else None

    def item_name(self, item_id: int) -> str | None:
        info = self._d.items.get(item_id)
        return info.name if info else None

    @staticmethod
    def type_name(type_id: int) -> str:
        return RomTables.type_name(type_id)

    def species_gender_ratio(self, species: int) -> int | None:
        return self._d.species_gender_ratio.get(species)

    def species_base_stats(self, species: int) -> tuple[int, ...] | None:
        return self._d.species_base_stats.get(species)

    # rows -------------------------------------------------------------------------------------
    def move_data(self, move_id: int) -> MoveData | None:
        info = self._d.moves.get(move_id)
        if info is None:
            return None
        return MoveData(
            move_id=move_id,
            effect=info.effect,
            power=info.power,
            type=info.type,
            accuracy=info.accuracy,
            pp=info.pp,
            secondary_chance=info.secondary_chance,
            target=info.target,
            priority=info.priority,
            flags=0,
        )

    def item_info(self, item_id: int) -> ItemInfo | None:
        return self._d.items.get(item_id)

    def item_battle_role(self, item_id: int) -> str | None:
        info = self._d.items.get(item_id)
        return info.role if info else None


def collect_lookups(
    tables: RomTables,
    *,
    species: set[int],
    abilities: set[int],
    moves: set[int],
    items: set[int],
) -> BattleLookups:
    """Read the ROM rows for the mentioned ids (cached rows make repeated calls cheap)."""
    sp = {i: n for i in sorted(species) if i and (n := tables.species_name(i))}
    species_types = {
        i: types for i in sorted(species) if i and (types := tables.species_types(i)) is not None
    }
    species_gender_ratio = {
        i: ratio for i in sorted(species) if (ratio := tables.species_gender_ratio(i)) is not None
    }
    species_base_stats = {
        i: stats for i in sorted(species) if (stats := tables.species_base_stats(i)) is not None
    }
    ab = {i: n for i in sorted(abilities) if i and (n := tables.ability_name(i))}
    mv: dict[int, MoveInfo] = {}
    for i in sorted(moves):
        if not i:
            continue
        data = tables.move_data(i)
        mv[i] = MoveInfo(
            move_id=i,
            name=tables.move_name(i),
            type=data.type if data else 0,
            power=data.power if data else 0,
            accuracy=data.accuracy if data else 0,
            pp=data.pp if data else 0,
            priority=data.priority if data else 0,
            target=data.target if data else 0,
            effect=data.effect if data else 0,
            secondary_chance=data.secondary_chance if data else 0,
        )
    it: dict[int, ItemInfo] = {}
    for i in sorted(items):
        if not i:
            continue
        data = tables.item_data(i)
        if data is None:
            continue
        it[i] = ItemInfo(
            item_id=i,
            name=data.name,
            pocket=data.pocket,
            battle_usage=data.battle_usage,
            role=tables.item_battle_role(i),
            hold_effect=data.hold_effect,
            effect=tables.item_effect(i),
        )
    return BattleLookups(
        species=sp,
        species_types=species_types,
        species_gender_ratio=species_gender_ratio,
        species_base_stats=species_base_stats,
        abilities=ab,
        moves=mv,
        items=it,
    )
