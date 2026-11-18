"""Pure field-menu legality and compact facts for party item preparation."""

from __future__ import annotations

from typing import Any

from pokegent.games.gen3 import mechanics_ids as ids
from pokegent.games.gen3.firered.rom.move_mappings import HM_MOVE_IDS
from pokegent.options.models import Legality, Option, Risk

ITEM_EFFECTS = {
    ids.ITEM_POTION: ("hp", 20),
    ids.ITEM_FULL_RESTORE: ("full_restore", 999),
    ids.ITEM_SUPER_POTION: ("hp", 50),
    ids.ITEM_HYPER_POTION: ("hp", 120),
    ids.ITEM_MAX_POTION: ("hp", 999),
    ids.ITEM_REVIVE: ("revive", 50),
    ids.ITEM_MAX_REVIVE: ("revive", 100),
    ids.ITEM_FULL_HEAL: ("status", 0),
    ids.ITEM_BURN_HEAL: ("status", 0),
    ids.ITEM_ICE_HEAL: ("status", 0),
    ids.ITEM_AWAKENING: ("status", 0),
    ids.ITEM_PARALYZE_HEAL: ("status", 0),
    ids.ITEM_ETHER: ("pp", 10),
    ids.ITEM_MAX_ETHER: ("pp", 10),
    ids.ITEM_ELIXIR: ("pp", 10),
    ids.ITEM_MAX_ELIXIR: ("pp", 10),
    ids.ITEM_RARE_CANDY: ("level", 1),
    ids.ITEM_HP_UP: ("vitamin", 0),
    ids.ITEM_PROTEIN: ("vitamin", 0),
    ids.ITEM_IRON: ("vitamin", 0),
    ids.ITEM_CARBOS: ("vitamin", 0),
    ids.ITEM_CALCIUM: ("vitamin", 0),
    ids.ITEM_ZINC: ("vitamin", 0),
    ids.ITEM_MOON_STONE: ("evolution", 0),
    ids.ITEM_FIRE_STONE: ("evolution", 0),
    ids.ITEM_THUNDER_STONE: ("evolution", 0),
    ids.ITEM_WATER_STONE: ("evolution", 0),
    ids.ITEM_LEAF_STONE: ("evolution", 0),
}


def quantity(snapshot: Any, item_id: int) -> int:
    return sum(i.quantity for p in snapshot.bag for i in p.items if i.item_id == item_id)


def reason_for(item_id: int, mon: Any, tables: Any = None) -> tuple[bool, str, dict[str, Any]]:
    kind, amount = ITEM_EFFECTS[item_id]
    if kind == "evolution":
        species = getattr(mon, "species", None)
        evolution = (
            tables.evolution_for_item(species, item_id)
            if tables is not None and isinstance(species, int)
            else None
        )
        if evolution is None:
            return False, "this Pokemon does not evolve with this stone", {"kind": kind}
        before = tables.species_name(species)
        after = tables.species_name(evolution.target_species)
        return (
            True,
            "",
            {
                "kind": kind,
                "evolution": {"from": before, "to": after},
                "fact": f"evolves {before} into {after}",
                "irreversible": True,
                "target_species": evolution.target_species,
            },
        )
    hp = int(getattr(mon, "hp", 0))
    max_hp = int(getattr(mon, "max_hp", 0))
    status = int(getattr(mon, "status", 0))
    if kind in {"hp", "full_restore"}:
        if hp <= 0:
            return False, "fainted; use Revive", {}
        if hp >= max_hp:
            return False, "full HP", {}
        restored = min(amount, max_hp - hp)
        return (
            True,
            "",
            {
                "hp_restored": restored,
                "target_hp": hp,
                "target_max_hp": max_hp,
                "hp_after": hp + restored,
                "kind": kind,
            },
        )
    if kind == "revive":
        if hp > 0:
            return False, "not fainted", {}
        restored = max_hp * amount // 100
        return (
            True,
            "",
            {
                "hp_restored": restored,
                "target_hp": 0,
                "target_max_hp": max_hp,
                "hp_after": restored,
                "kind": kind,
            },
        )
    if kind == "status":
        if not status:
            return False, "no status condition", {}
        return True, "", {"cures_current": ["status"], "kind": kind}
    if kind == "vitamin":
        return False, "vitamin target stat/EV facts are not readable", {"kind": kind}
    if kind == "pp":
        return False, "PP target facts are not readable in the field snapshot", {"kind": kind}
    if kind == "level":
        return False, "Rare Candy field use is not yet ROM-proven", {"kind": kind}
    return False, "unsupported item effect", {}


def item_options(snapshot: Any, *, allow_heal: bool, tables: Any = None) -> list[Option]:
    out: list[Option] = []
    for pocket in snapshot.bag:
        for entry in pocket.items:
            if entry.item_id not in ITEM_EFFECTS or entry.quantity <= 0:
                continue
            for mon in snapshot.party:
                useful, reason, facts = reason_for(entry.item_id, mon, tables)
                facts = {
                    **facts,
                    "item_id": entry.item_id,
                    "quantity": entry.quantity,
                    "party_slot": mon.slot,
                }
                if not allow_heal and ITEM_EFFECTS[entry.item_id][0] in {
                    "hp",
                    "full_restore",
                    "revive",
                    "status",
                }:
                    useful, reason = False, "heal objective does not authorize field supplies"
                facts["would_have_effect"] = useful
                if not useful:
                    facts["reason"] = reason
                out.append(
                    Option(
                        id=f"use:{entry.item_id}:{mon.slot}",
                        label=f"Use item {entry.item_id} on party slot {mon.slot + 1}",
                        description=("Use the verified item on this Pokemon" if useful else reason),
                        legality=Legality.LEGAL if useful else Legality.UNAVAILABLE,
                        facts=facts,
                        executor_ref="field.use_item" if useful else None,
                        executor_args=(
                            {"item_id": entry.item_id, "party_slot": mon.slot} if useful else {}
                        ),
                        risk=Risk.NORMAL,
                        postcondition_ref=(
                            "field.evolution_started"
                            if useful and ITEM_EFFECTS[entry.item_id][0] == "evolution"
                            else "field.item_used"
                            if useful
                            else None
                        ),
                        verb="USE_ITEM" if useful else None,
                        expected_postcondition=(
                            "Evolution scene starts or the party species changes."
                            if useful and ITEM_EFFECTS[entry.item_id][0] == "evolution"
                            else "Party RAM shows the intended HP/status change."
                            if useful
                            else None
                        ),
                    )
                )
    return out


def tm_options(snapshot: Any, tables: Any) -> list[Option]:
    """Offer every ROM-compatible TM/HM target and legal replacement choice."""
    if tables is None:
        return []
    out = []
    for pocket in snapshot.bag:
        for entry in pocket.items:
            if not 289 <= entry.item_id <= 346 or entry.quantity <= 0:
                continue
            try:
                move_id = tables.tmhm_move(entry.item_id)
                if type(move_id) is not int or move_id <= 0:
                    continue
                new = tables.move(move_id)
            except Exception:
                continue
            for mon in snapshot.party:
                if any(m.move_id == move_id for m in mon.moves):
                    continue
                if tables.can_learn_tmhm(mon.species, entry.item_id) is not True:
                    continue
                has_free_slot = any(m.move_id == 0 for m in mon.moves)
                choices = tuple(
                    {
                        "move_slot": slot,
                        "move_id": move.move_id,
                        "move_name": tables.move(move.move_id).name.title(),
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
                candidates = [choice for choice in choices if choice["can_forget"]]
                if not has_free_slot and not candidates:
                    continue
                default_forget_move = (
                    0
                    if has_free_slot
                    else int(
                        min(
                            candidates, key=lambda choice: tables.move(int(choice["move_id"])).power
                        )["move_id"]
                    )
                )
                replacements = (None,) if has_free_slot else tuple(candidates)
                for replacement in replacements:
                    forget_move = 0 if replacement is None else int(replacement["move_id"])
                    machine = "HM" if entry.item_id >= 339 else "TM"
                    number = entry.item_id - (338 if machine == "HM" else 288)
                    suffix = (
                        ""
                        if replacement is None or forget_move == default_forget_move
                        else f":forget:{forget_move}"
                    )
                    outcome = (
                        f"learns {new.name.title()} in an empty move slot"
                        if replacement is None
                        else f"learns {new.name.title()} and forgets {replacement['move_name']}"
                    )
                    facts = {
                        "tm_item": entry.item_id,
                        "move_id": move_id,
                        "new_power": new.power,
                        "new_type": new.type,
                        "forget_move": forget_move,
                        "default_forget_move": default_forget_move,
                        "forget_choices": choices,
                        "uses_free_slot": has_free_slot,
                        "quantity": entry.quantity,
                        "compatible": True,
                        "hm_moves_cannot_be_forgotten": True,
                        "learned_move_is_hm": machine == "HM",
                        "outcome": outcome,
                    }
                    out.append(
                        Option(
                            id=f"teach_tm:{entry.item_id}:{mon.slot}{suffix}",
                            label=f"Teach {machine}{number:02d} to party slot {mon.slot + 1}",
                            description=outcome,
                            legality=Legality.LEGAL,
                            facts=facts,
                            executor_ref="field.teach_tm",
                            executor_args={
                                "item_id": entry.item_id,
                                "party_slot": mon.slot,
                                "forget_move": forget_move,
                                "move_id": move_id,
                            },
                            risk=Risk.NORMAL,
                            postcondition_ref="field.tm_taught",
                            verb="TEACH_MOVE",
                        )
                    )
    return out
