"""The single item option builder: catalog facts + current RAM -> legal item/target choices."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pokegent.actions.menu.system_flow.moves import HM_MOVE_IDS
from pokegent.games.gen3 import mechanics_constants as K
from pokegent.games.gen3.firered.maps.map_schema import snapshot_map_identity
from pokegent.games.gen3.firered.rom.item_catalog import (
    EffectClass,
    ItemCatalog,
    ItemRecord,
    ItemTarget,
)
from pokegent.options.models import Legality, Option, Risk

ItemContext = Literal["field", "battle"]

_STATUS_BITS = (
    (K.STATUS1_SLEEP_MASK, K.STATUS_SLEEP),
    (K.STATUS1_POISON, K.STATUS_POISON),
    (K.STATUS1_TOXIC_POISON, K.STATUS_POISON),
    (K.STATUS1_BURN, K.STATUS_BURN),
    (K.STATUS1_FREEZE, K.STATUS_FREEZE),
    (K.STATUS1_PARALYSIS, K.STATUS_PARALYSIS),
)

_FACING_STEP = {"N": (0, -1), "S": (0, 1), "W": (-1, 0), "E": (1, 0)}


@dataclass(frozen=True)
class ItemVerdict:
    legality: Legality
    reason: str = ""
    facts: dict[str, Any] | None = None
    move_slot: int | None = None

    @property
    def chooseable(self) -> bool:
        return self.legality.chooseable


def _quantity(snapshot: Any, item_id: int) -> int:
    return sum(
        entry.quantity
        for pocket in getattr(snapshot, "bag", ())
        for entry in pocket.items
        if entry.item_id == item_id
    )


def _name(catalog: ItemCatalog, mon: Any) -> str:
    nickname = str(getattr(mon, "nickname", "")).strip()
    return nickname or catalog.tables.species_name(mon.species).title()


def _statuses(value: int) -> set[str]:
    return {name for mask, name in _STATUS_BITS if value & mask}


def _max_pp(catalog: ItemCatalog, mon: Any, slot: int) -> int:
    move = mon.moves[slot]
    base = catalog.tables.move(move.move_id).pp
    ups = (int(mon.pp_bonuses) >> (slot * 2)) & 3
    return base + base * 20 * ups // 100


class ItemOptionBuilder:
    """Build one option/diagnostic per bag item and RAM-valid target combination."""

    def __init__(self, catalog: ItemCatalog) -> None:
        self.catalog = catalog

    def build(
        self,
        snapshot: Any,
        *,
        context: ItemContext = "field",
        battle_view: Any | None = None,
        summarize_teaching: bool = True,
    ) -> list[Option]:
        out: list[Option] = []
        for pocket in getattr(snapshot, "bag", ()):
            for entry in pocket.items:
                if entry.quantity <= 0:
                    continue
                record = self.catalog.item(entry.item_id)
                if not getattr(record.capability, context):
                    continue
                out.extend(
                    self._record_options(
                        snapshot,
                        record,
                        entry.quantity,
                        context,
                        battle_view,
                        summarize_teaching=summarize_teaching,
                    )
                )
        return out

    def _record_options(
        self,
        snapshot: Any,
        record: ItemRecord,
        quantity: int,
        context: ItemContext,
        battle_view: Any | None,
        *,
        summarize_teaching: bool = False,
    ) -> list[Option]:
        if EffectClass.TEACH_MOVE in record.capability.effects:
            options = [
                option
                for mon in snapshot.party
                for option in self._teach_move_options(record, quantity, context, mon)
            ]
            if summarize_teaching and options:
                return [self._teach_summary(record, quantity, options, context)]
            return options
        target = record.capability.target
        if target is ItemTarget.NONE:
            verdict = self._without_target(snapshot, record, context, battle_view)
            return [self._option(record, quantity, verdict, context)]
        if target is ItemTarget.MOVE_SLOT and EffectClass.PP in record.capability.effects:
            out: list[Option] = []
            effect = record.data.effect
            assert effect is not None
            for mon in snapshot.party:
                if effect.heal_pp_all_moves:
                    verdict = self._for_mon(record, mon, context)
                    out.append(self._option(record, quantity, verdict, context, mon=mon))
                else:
                    for move_slot, move in enumerate(mon.moves):
                        if not move.move_id:
                            continue
                        verdict = self._for_pp_move(record, mon, move_slot)
                        out.append(
                            self._option(
                                record,
                                quantity,
                                verdict,
                                context,
                                mon=mon,
                                move_slot=move_slot,
                            )
                        )
            return out
        return [
            self._option(
                record,
                quantity,
                self._for_mon(record, mon, context),
                context,
                mon=mon,
            )
            for mon in snapshot.party
        ]

    def teach_target_options(
        self, snapshot: Any, item_id: int, *, context: ItemContext = "field"
    ) -> list[Option]:
        """Rebuild legal target/replacement choices for a TM/HM party screen."""
        record = self.catalog.item(item_id)
        if EffectClass.TEACH_MOVE not in record.capability.effects:
            return []
        quantity = _quantity(snapshot, item_id)
        if quantity <= 0:
            return []
        return [
            option
            for mon in snapshot.party
            for option in self._teach_move_options(record, quantity, context, mon)
        ]

    def _teach_summary(
        self,
        record: ItemRecord,
        quantity: int,
        options: list[Option],
        context: ItemContext,
    ) -> Option:
        assert record.move_id is not None
        compatible = tuple(dict.fromkeys(str(option.facts["target_name"]) for option in options))
        facts = self._base_facts(record, quantity, context)
        facts.update(
            {
                "compatible": compatible,
                "compatible_count": len(compatible),
                "move_id": record.move_id,
                "move_name": self.catalog.tables.move(record.move_id).name.title(),
                "hm_moves_cannot_be_forgotten": True,
                "learned_move_is_hm": record.item_id >= 339,
                "would_have_effect": True,
                "prerequisites": "at least one compatible Pokemon",
                "outcome": "opens the party target and move-replacement choices",
                "target_screen": {
                    "scene": "party.list",
                    "callback": "CB2_UpdatePartyMenu",
                    "family": "start_party",
                },
            }
        )
        names = ", ".join(compatible)
        return Option(
            id=f"teach:{record.item_id}",
            label=(
                f"Teach {record.name.title()} "
                f"({self.catalog.tables.move(record.move_id).name.title()}) - compatible: {names}"
            ),
            description="Open the TM/HM party selection screen.",
            legality=Legality.LEGAL,
            facts=facts,
            executor_ref="field.begin_teach_tm",
            executor_args={"item_id": record.item_id, "move_id": record.move_id},
            postcondition_ref="field.teach_target_ready",
            verb="TEACH_MOVE",
            risk=Risk.LOW,
        )

    def _teach_move_options(
        self,
        record: ItemRecord,
        quantity: int,
        context: ItemContext,
        mon: Any,
    ) -> list[Option]:
        """Expose every legal replacement as a separate agent choice."""
        assert record.move_id is not None
        base = {
            "target_hp": int(mon.hp),
            "target_max_hp": int(mon.max_hp),
            "party_slot": int(mon.slot),
        }
        if any(move.move_id == record.move_id for move in mon.moves):
            return []
        if self.catalog.can_learn(record.item_id, mon.species) is not True:
            return []
        verdicts = self._compatible_teach_verdicts(record, mon, base)
        return [self._option(record, quantity, verdict, context, mon=mon) for verdict in verdicts]

    def _compatible_teach_verdicts(
        self, record: ItemRecord, mon: Any, base: dict[str, Any]
    ) -> list[ItemVerdict]:
        assert record.move_id is not None
        new_move = self.catalog.tables.move(record.move_id)
        free = next((slot for slot, move in enumerate(mon.moves) if move.move_id == 0), None)
        forget_choices = tuple(
            {
                "move_slot": slot,
                "move_id": move.move_id,
                "move_name": self.catalog.tables.move(move.move_id).name.title(),
                "can_forget": move.move_id not in HM_MOVE_IDS,
                **(
                    {}
                    if move.move_id not in HM_MOVE_IDS
                    else {"reason": "HM moves cannot be forgotten later"}
                ),
            }
            for slot, move in enumerate(mon.moves)
            if move.move_id
        )
        candidates = [choice for choice in forget_choices if choice["can_forget"]]
        if free is None and not candidates:
            return []

        current_types = {
            self.catalog.tables.move(move.move_id).type for move in mon.moves if move.move_id
        }
        default_forget_move = (
            0
            if free is not None
            else int(
                min(
                    candidates,
                    key=lambda choice: self.catalog.tables.move(int(choice["move_id"])).power,
                )["move_id"]
            )
        )
        replacements = (None,) if free is not None else tuple(candidates)
        verdicts: list[ItemVerdict] = []
        for replacement in replacements:
            old_power = (
                0
                if replacement is None
                else self.catalog.tables.move(int(replacement["move_id"])).power
            )
            forget_name = None if replacement is None else str(replacement["move_name"])
            facts = {
                **base,
                "move_name": new_move.name.title(),
                "move_power": new_move.power,
                "move_type": new_move.type,
                "upgrade": bool(
                    (replacement is None and new_move.power > 0)
                    or new_move.power > old_power
                    or (new_move.power > 0 and new_move.type not in current_types)
                ),
                "compatible": True,
                "uses_free_slot": replacement is None,
                "forget_move": 0 if replacement is None else int(replacement["move_id"]),
                "default_forget_move": default_forget_move,
                "forget_move_name": forget_name,
                "forget_choices": forget_choices,
                "hm_moves_cannot_be_forgotten": True,
                "learned_move_is_hm": record.item_id >= 339,
                "outcome": (
                    f"learns {new_move.name.title()} in an empty move slot"
                    if replacement is None
                    else f"learns {new_move.name.title()} and forgets {forget_name}"
                ),
                "irreversible": True,
            }
            verdicts.append(ItemVerdict(Legality.LEGAL, facts=facts))
        return verdicts

    def _base_facts(
        self, record: ItemRecord, quantity: int, context: ItemContext
    ) -> dict[str, Any]:
        effects = [effect.value for effect in record.capability.effects]
        return {
            "domain": "item",
            "item_id": record.item_id,
            "item_name": record.name.title(),
            "quantity": quantity,
            "context": context,
            "effect_class": effects[0] if effects else EffectClass.NONE.value,
            "effect_classes": effects,
            "consumable": record.capability.consumable,
            "cost": "uses one" if record.capability.consumable else "reusable",
        }

    def _for_mon(self, record: ItemRecord, mon: Any, context: ItemContext) -> ItemVerdict:
        effects = record.capability.effects
        facts: dict[str, Any] = {
            "target_hp": int(mon.hp),
            "target_max_hp": int(mon.max_hp),
            "party_slot": int(mon.slot),
        }
        effect = record.data.effect
        changed = False
        reasons: list[str] = []

        if EffectClass.EVOLVE in effects:
            target = self.catalog.evolution(record.item_id, mon.species)
            if target is None:
                return ItemVerdict(
                    Legality.UNAVAILABLE, "this Pokemon does not evolve with this item", facts
                )
            before = self.catalog.tables.species_name(mon.species).title()
            after = self.catalog.tables.species_name(target).title()
            facts.update(
                {
                    "target_species": target,
                    "evolution": {"from": before, "to": after},
                    "outcome": f"evolves {before} into {after}",
                    "irreversible": True,
                }
            )
            return ItemVerdict(Legality.LEGAL, facts=facts)

        if effect is None or record.capability.field_action in {"sacred_ash", "mail"}:
            return ItemVerdict(
                Legality.UNSUPPORTED,
                f"{record.capability.field_action or 'item'} follow-up is not switched over yet",
                facts,
            )
        if EffectClass.REVIVE in effects:
            if mon.hp:
                reasons.append("not fainted")
            else:
                restored = max(1, mon.max_hp // 2) if effect.heal_hp_half else mon.max_hp
                facts.update({"hp_restored": restored, "hp_after": restored})
                changed = True
        if EffectClass.HEAL_HP in effects:
            if mon.hp <= 0:
                reasons.append("fainted; use a revive")
            elif mon.hp >= mon.max_hp:
                reasons.append("full HP")
            else:
                amount = mon.max_hp - mon.hp if effect.heal_hp_full else int(effect.heal_hp or 0)
                restored = min(amount, mon.max_hp - mon.hp)
                facts.update({"hp_restored": restored, "hp_after": mon.hp + restored})
                changed = True
        if EffectClass.CURE_STATUS in effects:
            current = _statuses(int(mon.status))
            cured = sorted(current & set(effect.cures))
            facts["cures"] = sorted(effect.cures)
            if cured:
                facts["cures_current"] = cured
                changed = True
            else:
                reasons.append("no status this item cures")
        if EffectClass.PP in effects and effect.heal_pp_all_moves:
            restored = []
            for slot, move in enumerate(mon.moves):
                if not move.move_id:
                    continue
                missing = max(0, _max_pp(self.catalog, mon, slot) - move.pp)
                if missing:
                    restored.append(
                        {
                            "move": self.catalog.tables.move_name(move.move_id).title(),
                            "pp": min(missing, effect.heal_pp or missing),
                        }
                    )
            if restored:
                facts["pp_restored"] = restored
                changed = True
            else:
                reasons.append("no move is missing PP")
        if EffectClass.STAT_BOOST in effects:
            verdict = self._stat_boost(record, mon, facts)
            if verdict.chooseable:
                changed = True
                facts.update(verdict.facts or {})
            else:
                reasons.append(verdict.reason)

        if changed:
            return ItemVerdict(Legality.LEGAL, facts=facts)
        return ItemVerdict(Legality.UNAVAILABLE, " and ".join(dict.fromkeys(reasons)), facts)

    def _for_pp_move(self, record: ItemRecord, mon: Any, slot: int) -> ItemVerdict:
        move = mon.moves[slot]
        maximum = _max_pp(self.catalog, mon, slot)
        facts = {
            "party_slot": mon.slot,
            "move_slot": slot,
            "move_name": self.catalog.tables.move_name(move.move_id).title(),
            "pp_before": move.pp,
            "pp_max": maximum,
        }
        if move.pp >= maximum:
            return ItemVerdict(Legality.UNAVAILABLE, "move has full PP", facts, slot)
        amount = record.data.effect.heal_pp
        restored = maximum - move.pp if amount == 0x7F else min(int(amount or 0), maximum - move.pp)
        facts.update(
            {
                "pp_restored": restored,
                "pp_after": move.pp + restored,
                "outcome": f"restores {restored} PP to {facts['move_name']}",
            }
        )
        # The party-to-move submenu has no stable adapter contract yet. Keep the ROM-derived
        # target visible in diagnostics instead of offering an executor that could hang there.
        # TODO(item-domain): switch this verdict to LEGAL with the generic move-slot menu adapter.
        return ItemVerdict(
            Legality.UNSUPPORTED,
            "move-slot item execution is not switched over yet",
            facts,
            slot,
        )

    def _stat_boost(self, record: ItemRecord, mon: Any, facts: dict[str, Any]) -> ItemVerdict:
        effect = record.data.effect
        assert effect is not None
        if effect.level_up:
            if mon.level >= 100:
                return ItemVerdict(Legality.UNAVAILABLE, "already level 100")
            return ItemVerdict(
                Legality.LEGAL,
                facts={
                    "level_before": mon.level,
                    "level_after": mon.level + 1,
                    "irreversible": True,
                },
            )
        if effect.ev_gains:
            total = sum(
                int(getattr(mon.evs, name))
                for name in ("hp", "attack", "defense", "speed", "sp_attack", "sp_defense")
            )
            changes = []
            for stat, amount in effect.ev_gains:
                before = int(getattr(mon.evs, stat))
                gain = max(0, min(amount, 100 - before, 510 - total))
                if gain:
                    changes.append({"stat": stat, "before": before, "after": before + gain})
            if not changes:
                return ItemVerdict(Legality.UNAVAILABLE, "vitamin EV cap reached")
            return ItemVerdict(
                Legality.LEGAL,
                facts={"stat_changes": changes, "irreversible": True},
            )
        if effect.pp_up or effect.pp_max:
            return ItemVerdict(Legality.UNSUPPORTED, "PP Up needs a move-slot target")
        if record.capability.battle and effect.stat_boost:
            return ItemVerdict(Legality.LEGAL, facts={"stat_changes": list(effect.stat_boost)})
        return ItemVerdict(Legality.UNAVAILABLE, "no readable stat change")

    def _without_target(
        self,
        snapshot: Any,
        record: ItemRecord,
        context: ItemContext,
        battle_view: Any | None,
    ) -> ItemVerdict:
        action = (
            record.capability.field_action
            if context == "field"
            else record.capability.battle_action
        )
        facts: dict[str, Any] = {"field_action": action}
        if context == "field" and action == "bicycle":
            state = getattr(snapshot, "map", None)
            if state is None or state.player is None:
                return ItemVerdict(Legality.UNKNOWN, "map state is unreadable", facts)
            if not state.player.biking and not state.identity.biking_allowed:
                return ItemVerdict(
                    Legality.UNAVAILABLE, "Bicycle is not allowed on this map", facts
                )
            mounted = bool(state.player.biking)
            facts.update(
                {
                    "mode": "biking" if mounted else "on_foot",
                    "item_id": record.item_id,
                    "registered_item": getattr(snapshot, "registered_item", 0),
                    "uses_select": getattr(snapshot, "registered_item", 0) == record.item_id,
                    "serves_goal": True,
                    "outcome": "dismounts the Bicycle" if mounted else "rides the Bicycle",
                }
            )
            return ItemVerdict(Legality.LEGAL, facts=facts)
        if context == "field" and action == "repel":
            facts["outcome"] = "starts or extends Repel's encounter protection"
            return ItemVerdict(Legality.LEGAL, facts=facts)
        if context == "field" and action == "rod":
            state = getattr(snapshot, "map", None)
            player = None if state is None else state.player
            step = None if player is None else _FACING_STEP.get(player.facing)
            if state is None or player is None or step is None:
                return ItemVerdict(Legality.UNKNOWN, "player facing is unreadable", facts)
            front = state.tile(player.avatar_x + step[0], player.avatar_y + step[1])
            facts["facing_tile"] = {
                "x": front.x,
                "y": front.y,
                "terrain": front.terrain,
                "surfable": front.surfable,
            }
            if not front.surfable:
                return ItemVerdict(Legality.UNAVAILABLE, "not facing fishable water", facts)
            facts["outcome"] = "casts the fishing rod into the facing water tile"
            return ItemVerdict(Legality.LEGAL, facts=facts)
        if context == "field" and action == "escape_rope":
            identity = snapshot_map_identity(snapshot)
            if identity is None:
                return ItemVerdict(Legality.UNKNOWN, "map state is unreadable", facts)
            if not identity.allow_escaping:
                return ItemVerdict(Legality.UNAVAILABLE, "Escape Rope cannot be used here", facts)
            facts.update(
                {
                    "outcome": "returns to the nearest dungeon entrance",
                }
            )
            return ItemVerdict(Legality.LEGAL, facts=facts)
        if context == "battle" and EffectClass.ESCAPE in record.capability.effects:
            if battle_view is None:
                return ItemVerdict(Legality.UNKNOWN, "battle state is unreadable", facts)
            if battle_view.is_trainer:
                return ItemVerdict(Legality.UNAVAILABLE, "cannot escape a trainer battle", facts)
            facts["outcome"] = "escapes from the wild battle"
            return ItemVerdict(Legality.LEGAL, facts=facts)
        if context == "battle" and record.capability.battle_action == "stat_booster":
            facts["outcome"] = "raises the active Pokemon's battle stat"
            return ItemVerdict(Legality.LEGAL, facts=facts)
        return ItemVerdict(
            Legality.UNSUPPORTED,
            f"{action or 'item'} needs RAM context not yet modeled",
            facts,
        )

    def _option(
        self,
        record: ItemRecord,
        quantity: int,
        verdict: ItemVerdict,
        context: ItemContext,
        *,
        mon: Any | None = None,
        move_slot: int | None = None,
    ) -> Option:
        facts = self._base_facts(record, quantity, context)
        facts.update(verdict.facts or {})
        facts["would_have_effect"] = verdict.chooseable
        if mon is not None:
            facts["target_name"] = _name(self.catalog, mon)
        if verdict.reason:
            facts["reason"] = verdict.reason
            facts["prerequisites"] = verdict.reason
        else:
            facts["prerequisites"] = "met"
        facts.setdefault("outcome", self._outcome(record, facts))
        target_id = "none" if mon is None else f"party:{mon.slot}"
        if move_slot is not None:
            target_id += f":move:{move_slot}"
        option_id = f"item:{record.item_id}:{target_id}"
        label = f"Use {record.name.title()}"
        if mon is not None:
            label += f" on {_name(self.catalog, mon)}"
        if move_slot is not None:
            label += f"'s {facts.get('move_name', f'move {move_slot + 1}')}"
        irreversible = bool(facts.get("irreversible")) and verdict.chooseable
        executor_args: dict[str, Any] = {}
        if verdict.chooseable:
            executor_args = {
                "item_id": record.item_id,
                "party_slot": int(mon.slot) if mon is not None else -1,
                "move_slot": move_slot if move_slot is not None else -1,
                "move_id": record.move_id or 0,
                "forget_move": int(facts.get("forget_move") or 0),
            }
        teach_move = context == "field" and EffectClass.TEACH_MOVE in record.capability.effects
        if teach_move:
            # ``move_slot`` belongs to PP-restoration options, not TM/HM teaching.  Keep the
            # generic item facts broad, but pass only the verified teach executor's contract.
            executor_args.pop("move_slot", None)
            if mon is not None and not facts.get("uses_free_slot", True):
                if facts["forget_move"] != facts["default_forget_move"]:
                    option_id += f":forget:{int(facts['forget_move'])}"
                label = (
                    f"Teach {record.name.title()} to {_name(self.catalog, mon)}; "
                    f"forget {facts['forget_move_name']}"
                )
            elif mon is not None:
                label = f"Teach {record.name.title()} to {_name(self.catalog, mon)}"
        executor_ref = (
            "field.teach_tm"
            if teach_move and verdict.chooseable
            else "item.use"
            if verdict.chooseable
            else None
        )
        postcondition_ref = "item.applied" if verdict.chooseable else None
        verb = (
            "TEACH_MOVE"
            if teach_move and verdict.chooseable
            else "USE_ITEM"
            if verdict.chooseable
            else None
        )
        outcome = str(facts["outcome"])
        if (
            context == "field"
            and record.capability.field_action == "escape_rope"
            and verdict.chooseable
        ):
            # The verifier waits through the transition/script hand-off, then accepts either
            # the expected map change or the consumed Rope.  Keep a real postcondition so a
            # scripted transition cannot be reported as a successful use prematurely.
            facts["postcondition"] = "map changed or Escape Rope quantity decreased"
        if context == "field" and record.capability.field_action == "bicycle" and "mode" in facts:
            mounted = facts.get("mode") == "biking"
            facts = {
                key: facts[key]
                for key in (
                    "mode",
                    "serves_goal",
                    "item_id",
                    "registered_item",
                    "uses_select",
                )
            }
            option_id = "bike_off" if mounted else "bike_on"
            label = "Dismount the Bicycle" if mounted else "Ride the Bicycle"
            executor_ref = "field.toggle_bike" if verdict.chooseable else None
            executor_args = {}
            postcondition_ref = "field.bike_off" if mounted else "field.bike_on"
            verb = "DISMOUNT_BICYCLE" if mounted else "RIDE_BICYCLE"
        return Option(
            id=option_id,
            label=label,
            description=(outcome if verdict.chooseable else f"Unavailable: {verdict.reason}"),
            legality=verdict.legality,
            facts=facts,
            executor_ref=executor_ref,
            executor_args=executor_args,
            risk=Risk.HIGH if irreversible else Risk.NORMAL,
            postcondition_ref=postcondition_ref if verdict.chooseable else None,
            verb=verb,
            semantic_prompt=(
                f"Use {record.name.title()} on this target?" if irreversible else None
            ),
            effect_description=(str(facts["outcome"]) if irreversible else None),
            irreversible=irreversible,
            expected_postcondition=(
                None
                if (
                    context == "field"
                    and record.capability.field_action == "bicycle"
                    and "mode" in facts
                )
                else outcome
                if verdict.chooseable
                else None
            ),
        )

    @staticmethod
    def _outcome(record: ItemRecord, facts: dict[str, Any]) -> str:
        target = facts.get("target_name", "the target")
        if "hp_after" in facts:
            return f"changes {target}'s HP to {facts['hp_after']}/{facts['target_max_hp']}"
        if facts.get("cures_current"):
            return f"cures {target}'s {', '.join(facts['cures_current'])}"
        if facts.get("stat_changes"):
            return f"permanently changes {target}'s stats"
        return f"applies {record.name.title()} to {target}"
