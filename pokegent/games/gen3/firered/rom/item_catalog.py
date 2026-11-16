"""One ROM-backed catalog and capability model for every FireRed item.

The item table supplies identity, pocket, type, handlers, hold effect, importance and price. The
item-effect table supplies medicine, PP and stat effects; gTMHMMoves and gEvolutionTable supply the
two relationships that are not in ``struct Item`` itself.

The only overrides below name handlers. Function pointers carry no semantic name in the ROM, so
one reference row per distinct handler is the smallest auditable bridge from an address to a role.
All other items inherit the role by pointer equality. These are deliberately reference items, not
membership lists.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pokegent.games.gen3.firered.rom.rom_tables import (
    EVOLUTION_ITEM_METHOD,
    POCKET_BALLS,
    POCKET_KEY_ITEMS,
    POCKET_TM_HM,
    GameTables,
    ItemData,
)


class ItemTarget(str, Enum):
    NONE = "none"
    PARTY_MON = "party_mon"
    MOVE_SLOT = "move_slot"


class EffectClass(str, Enum):
    NONE = "none"
    HEAL_HP = "heal_hp"
    CURE_STATUS = "cure_status"
    REVIVE = "revive"
    PP = "pp"
    STAT_BOOST = "stat_boost"
    EVOLVE = "evolve"
    TEACH_MOVE = "teach_move"
    KEY_ITEM_TOGGLE = "key_item_toggle"
    CAPTURE = "capture"
    ESCAPE = "escape"
    GIVE_HELD_ITEM = "give_held_item"


# One row per distinct fieldUseFunc that has gameplay semantics. ``cannot_use`` is important: many
# inert/held/battle-only items point at that stub rather than NULL. Source: gItems pointer grouping,
# names checked against pret/pokefirered src/data/items.json and src/item_use.c.
FIELD_HANDLER_REFERENCES: dict[str, int] = {
    "cannot_use": 0,
    "medicine": 13,
    "pp_restore": 34,
    "encounter_flute": 42,
    "sacred_ash": 45,
    "rare_candy": 68,
    "pp_up": 69,
    "repel": 86,
    "escape_rope": 85,
    "evolution_stone": 93,
    "mail": 121,
    "enigma_berry": 175,
    "bicycle": 360,
    "coin_case": 260,
    "itemfinder": 261,
    "rod": 262,
    "poke_flute": 350,
    "town_map": 361,
    "vs_seeker": 362,
    "fame_checker": 363,
    "tm_case": 364,
    "berry_pouch": 365,
    "teachy_tv": 366,
    "powder_jar": 372,
}

# One row per battleUseFunc. This replaces the handler section of
# references/battle_item_classes.yaml; effect bytes distinguish hp/cure/revive within medicine.
BATTLE_HANDLER_REFERENCES: dict[str, int] = {
    "medicine": 13,
    "pp_restore": 34,
    "sacred_ash": 45,
    "stat_booster": 75,
    "ball": 4,
    "escape": 80,
    "poke_flute": 350,
    "berry_pouch": 365,
}

_IMMEDIATE_TARGET_ROLES = frozenset({"stat_booster", "escape", "poke_flute", "ball"})
_NONCONSUMING_FIELD_ROLES = frozenset(
    {
        "encounter_flute",
        "bicycle",
        "coin_case",
        "itemfinder",
        "rod",
        "poke_flute",
        "town_map",
        "vs_seeker",
        "fame_checker",
        "tm_case",
        "berry_pouch",
        "teachy_tv",
        "powder_jar",
    }
)


@dataclass(frozen=True)
class ItemCapability:
    field: bool
    battle: bool
    target: ItemTarget
    effects: tuple[EffectClass, ...]
    consumable: bool
    field_action: str | None = None
    battle_action: str | None = None

    @property
    def effect_class(self) -> EffectClass:
        return self.effects[0] if self.effects else EffectClass.NONE


@dataclass(frozen=True)
class ItemRecord:
    item_id: int
    name: str
    pocket: int
    item_type: int
    field_use_func: int
    battle_use_func: int
    hold_effect: int
    price: int
    importance: int
    move_id: int | None
    evolutions: tuple[tuple[int, int], ...]  # (source species, target species)
    capability: ItemCapability
    data: ItemData


def _roles_by_pointer(tables: GameTables, references: dict[str, int], attr: str) -> dict[int, str]:
    roles: dict[int, str] = {}
    for role, item_id in references.items():
        pointer = int(getattr(tables.item(item_id), attr))
        if pointer:
            roles.setdefault(pointer, role)
    return roles


class ItemCatalog:
    """Immutable capability record for every gItems row in one ROM build."""

    def __init__(self, tables: GameTables) -> None:
        self.tables = tables
        self._field_roles = _roles_by_pointer(tables, FIELD_HANDLER_REFERENCES, "field_use_func")
        self._battle_roles = _roles_by_pointer(tables, BATTLE_HANDLER_REFERENCES, "battle_use_func")
        evolutions: dict[int, list[tuple[int, int]]] = {}
        for species in range(len(tables.all_species)):
            for evolution in tables.evolutions_for(species):
                if (
                    evolution.method == EVOLUTION_ITEM_METHOD
                    and evolution.parameter
                    and evolution.target_species
                ):
                    evolutions.setdefault(evolution.parameter, []).append(
                        (species, evolution.target_species)
                    )
        self._records = tuple(
            self._record(item, tuple(evolutions.get(item.item_id, ()))) for item in tables.items
        )

    def _record(self, item: ItemData, evolutions: tuple[tuple[int, int], ...]) -> ItemRecord:
        move_id = self.tables.tmhm_move(item.item_id)
        field_role = self._field_roles.get(item.field_use_func)
        battle_role = self._battle_roles.get(item.battle_use_func)
        effects: list[EffectClass] = []
        effect = item.effect

        if move_id is not None:
            effects.append(EffectClass.TEACH_MOVE)
        if evolutions:
            effects.append(EffectClass.EVOLVE)
        if effect is not None:
            # Rare Candy shares low-level bits with revive/HP effects; its named field handler and
            # LEVEL_UP flag are the ROM proof that it is a permanent stat change instead.
            if effect.level_up or effect.ev_gains or effect.pp_up or effect.pp_max:
                effects.append(EffectClass.STAT_BOOST)
            else:
                if effect.revive:
                    effects.append(EffectClass.REVIVE)
                if effect.heals_hp and not effect.revive:
                    effects.append(EffectClass.HEAL_HP)
            if effect.cures:
                effects.append(EffectClass.CURE_STATUS)
            if effect.heal_pp is not None:
                effects.append(EffectClass.PP)
            if effect.stat_boost or effect.focus_energy or effect.guard_spec:
                effects.append(EffectClass.STAT_BOOST)
        if item.pocket == POCKET_BALLS:
            effects.append(EffectClass.CAPTURE)
        if battle_role == "escape":
            effects.append(EffectClass.ESCAPE)
        if field_role == "mail":
            effects.append(EffectClass.GIVE_HELD_ITEM)
        if not effects and field_role not in (None, "cannot_use"):
            effects.append(EffectClass.KEY_ITEM_TOGGLE)

        effects = list(dict.fromkeys(effects))
        field_usable = move_id is not None or field_role not in (None, "cannot_use")
        battle_usable = bool(item.battle_usage and battle_role is not None)
        if (
            move_id is not None
            or evolutions
            or field_role
            in {
                "medicine",
                "rare_candy",
                "sacred_ash",
                "mail",
            }
        ):
            target = ItemTarget.PARTY_MON
        elif EffectClass.PP in effects:
            target = ItemTarget.MOVE_SLOT
        elif battle_role and battle_role not in _IMMEDIATE_TARGET_ROLES:
            target = ItemTarget.PARTY_MON
        else:
            target = ItemTarget.NONE

        actionable = bool(effects) and (field_usable or battle_usable)
        consumable = bool(
            actionable
            and item.importance == 0
            and item.pocket != POCKET_KEY_ITEMS
            and field_role not in _NONCONSUMING_FIELD_ROLES
            and not (item.pocket == POCKET_TM_HM and item.item_id >= 339)
        )
        capability = ItemCapability(
            field=field_usable,
            battle=battle_usable,
            target=target,
            effects=tuple(effects),
            consumable=consumable,
            field_action=field_role,
            battle_action=battle_role,
        )
        return ItemRecord(
            item_id=item.item_id,
            name=item.name,
            pocket=item.pocket,
            item_type=item.item_type,
            field_use_func=item.field_use_func,
            battle_use_func=item.battle_use_func,
            hold_effect=item.hold_effect,
            price=item.price,
            importance=item.importance,
            move_id=move_id,
            evolutions=evolutions,
            capability=capability,
            data=item,
        )

    def item(self, item_id: int) -> ItemRecord:
        if not 0 <= item_id < len(self._records):
            raise KeyError(item_id)
        return self._records[item_id]

    @property
    def items(self) -> tuple[ItemRecord, ...]:
        return self._records

    def evolution(self, item_id: int, species: int) -> int | None:
        return dict(self.item(item_id).evolutions).get(species)

    def can_learn(self, item_id: int, species: int) -> bool | None:
        if self.item(item_id).move_id is None:
            return False
        return self.tables.can_learn_tmhm(species, item_id)
