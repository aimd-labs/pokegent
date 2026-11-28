"""Option builder of the battle family (spec 5.1, 5.2, 6.D): every option, every legality, English
facts.

One :class:`BattleBuilder` serves all ``battle.*`` scene ids (``register_family("battle", ...)``).
It
is a pure function of the snapshot: names and ROM rows come from ``BattleBlock.lookups`` (captured
with
the snapshot), never from the ROM, so a frame replays from state alone.

Semantic options (spec 6.D, "semantic macros instead of separate FIGHT/BAG/POKEMON/RUN decisions")
exist in ``battle.action``:

=========================  =====================================================================
``move:<slot>``            USE_MOVE; also ``move:struggle`` when every move is unusable
``switch:<slot>``          SWITCH_TO (fainted, egg, active, trapped: UNAVAILABLE with a reason)
``item:<id>:<slot>``       USE_BATTLE_ITEM (``slot`` -1 = no target); strategically bad stays LEGAL
``ball:<id>``              THROW_BALL (wild only; CONDITIONAL: a catch can fail)
``escape``                 ATTEMPT_ESCAPE (wild only; a trainer battle does not even list it)
=========================  =====================================================================

Facts come from a :class:`MoveFactsProvider`; any failure degrades to :class:`BasicFacts` (facts are
advice, legality never depends on them). UNKNOWN inputs give UNKNOWN options (diagnostics), never
LEGAL.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from pokegent.actions.battle import legality as L
from pokegent.actions.battle import refs as R
from pokegent.actions.battle import state as S
from pokegent.actions.battle.catch_facts import (
    add_ball_facts,
    foe_facts,
)
from pokegent.actions.battle.item_domain import BattleItemDomain, RomBattleItemDomain
from pokegent.actions.battle.reader import battle_of
from pokegent.actions.battle.view import (
    BasicFacts,
    BattleView,
    MoveFactsProvider,
    make_view,
)
from pokegent.actions.menu.system_flow.moves import HM_MOVE_IDS
from pokegent.games.gen3.mechanics_constants import is_physical_type
from pokegent.options.models import Legality, Option, Risk, fingerprint
from pokegent.options.registry import BuildContext, FrameDraft


def jsonify(value: Any) -> Any:
    """Make a provider's facts JSON-like (str keys; no tuples/sets/NaN)."""
    if isinstance(value, dict):
        return {str(k): jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonify(v) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return str(value)


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


NATURE_NAMES = (
    "Hardy",
    "Lonely",
    "Brave",
    "Adamant",
    "Naughty",
    "Bold",
    "Docile",
    "Relaxed",
    "Impish",
    "Lax",
    "Timid",
    "Hasty",
    "Serious",
    "Jolly",
    "Naive",
    "Modest",
    "Mild",
    "Quiet",
    "Bashful",
    "Rash",
    "Calm",
    "Gentle",
    "Sassy",
    "Careful",
    "Quirky",
)


def nature_from_personality(personality: int) -> str:
    """Gen-3 nature is the personality value modulo 25."""
    return NATURE_NAMES[personality % 25]


def gender_from_personality(personality: int, gender_ratio: int | None) -> str:
    """Gen-3 gender uses the low PID byte against the species' ROM ratio."""
    if gender_ratio == 0:
        return "male"
    if gender_ratio == 254:
        return "female"
    if gender_ratio == 255:
        return "genderless"
    if isinstance(gender_ratio, int):
        return "female" if (personality & 0xFF) < gender_ratio else "male"
    return "unknown gender"


class BattleBuilder:
    def __init__(
        self,
        facts: MoveFactsProvider | None = None,
        items: BattleItemDomain | None = None,
    ) -> None:
        self._facts: MoveFactsProvider = facts or BasicFacts()
        self._basic = BasicFacts()
        self._items = items or RomBattleItemDomain()

    # -- entry -------------------------------------------------------------------------------------

    def build(self, ctx: BuildContext) -> FrameDraft:
        scene = ctx.scene.scene_id
        block = battle_of(ctx.snapshot)
        if block is None or not block.valid or not block.active:
            return self._diag(
                scene,
                "unknown:battle_state",
                Legality.UNKNOWN,
                "Battle state unreadable",
                "The battle RAM block is missing or invalid; no battle option can be proven legal.",
                {"reason": "battle block missing or invalid"},
            )
        evidence = ctx.scene.evidence
        phase = str(evidence.get("phase", ""))
        if not block.supported or scene in _UNSUPPORTED_SCENES:
            return self._unsupported(scene, block)
        try:
            view = make_view(ctx.snapshot, block)
        except ValueError as exc:
            return self._diag(
                scene,
                "unknown:battle_state",
                Legality.UNKNOWN,
                "Battle state unreadable",
                str(exc),
                {"reason": str(exc)},
            )
        handler = _HANDLERS.get(scene)
        if handler is None:
            return self._diag(
                scene,
                "unknown:scene",
                Legality.UNKNOWN,
                "No options for this battle phase",
                f"The battle family has no option set for {scene}.",
                {"scene": scene},
            )
        return self._add_trainer_facts(view, handler(self, ctx, view, phase))

    @staticmethod
    def _add_trainer_facts(view: BattleView, draft: FrameDraft) -> FrameDraft:
        if view.kind == "trainer":
            trainer = view.block.opponent_trainer
            trainer_facts = {
                "trainer_id": trainer.trainer_id if trainer is not None else None,
                "class_name": trainer.class_name if trainer is not None else "unknown",
                "name": trainer.name if trainer is not None else "unknown",
            }
            draft = replace(
                draft,
                context={**draft.context, "opponent_trainer": trainer_facts},
                options=tuple(
                    option.model_copy(
                        update={"facts": {**option.facts, "opponent_trainer": trainer_facts}}
                    )
                    for option in draft.options
                ),
                all_options=tuple(
                    option.model_copy(
                        update={"facts": {**option.facts, "opponent_trainer": trainer_facts}}
                    )
                    for option in draft.all_options
                ),
            )
        return draft

    # -- helpers -----------------------------------------------------------------------------------

    def _provider(self, name: str, view: BattleView, *args: Any) -> dict:
        for provider in (self._facts, self._basic):
            try:
                out = getattr(provider, name)(view, *args)
            except Exception:  # facts are advice; a broken provider must not take the frame down
                continue
            if isinstance(out, dict):
                return jsonify(out)
        return {}

    def _context(self, view: BattleView, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        me, foe = view.player, view.opponent
        theirs = f"{foe.name} Lv{foe.level} HP {foe.hp_percent}%"
        if foe.status:
            theirs += " (" + ", ".join(foe.status) + ")"
        out: dict[str, Any] = {
            "battle": view.kind,
            "turns_played": view.block.turn,
            "opposing_pokemon": theirs,
        }
        if view.tables is not None:
            try:
                out["opposing_types"] = [
                    view.tables.type_name(value)
                    for value in dict.fromkeys(view.tables.species(foe.species).types)
                ]
            except (AttributeError, KeyError, IndexError, TypeError, ValueError):
                pass
        if view.kind != "safari":
            mine = f"{me.name} Lv{me.level} HP {me.hp}/{me.max_hp}"
            if me.status:
                mine += " (" + ", ".join(me.status) + ")"
            out["your_pokemon"] = mine
            if me.stat_stages:
                out["your_stat_changes"] = ", ".join(
                    f"{k} {v:+d}" for k, v in me.stat_stages.items()
                )
        if foe.stat_stages:
            out["opposing_stat_changes"] = ", ".join(
                f"{k} {v:+d}" for k, v in foe.stat_stages.items()
            )
        if view.kind != "safari" and me.volatile:
            out["your_conditions"] = ", ".join(me.volatile)
        if view.weather:
            out["weather"] = ", ".join(view.weather)
        if view.is_trainer and view.enemy_party_alive is not None:
            out["opposing_pokemon_remaining"] = view.enemy_party_alive
        alive = [m for m in view.party if not m.fainted and not m.is_egg]
        out["your_party_able_to_battle"] = len(alive)
        if extra:
            out.update(extra)
        return out

    def _fp(self, scene: str, view: BattleView, **extra: Any):
        me = view.block.battler_at(view.player.battler)
        foe = view.block.battler_at(view.opponent.battler)
        assert me is not None and foe is not None
        return fingerprint(
            scene,
            kind=view.kind,
            turn=[view.block.turn, view.block.turn_marker],
            me=[
                me.species,
                me.personality,
                me.hp,
                me.max_hp,
                me.status1,
                me.status2,
                [[m.move_id, m.pp] for m in me.moves],
                me.disable.disabled_move,
                me.disable.taunt_timer,
                me.disable.encore_timer,
                me.choiced_move,
                me.item,
            ],
            foe=[foe.species, foe.level, foe.hp_percent, foe.status1],
            party=[[m.slot, m.personality, m.hp, list(m.status), m.is_egg] for m in view.party],
            bag=[[k, v] for k, v in sorted(view.bag.items())],
            **extra,
        )

    @staticmethod
    def _diag(
        scene: str, option_id: str, legality: Legality, label: str, description: str, facts: dict
    ) -> FrameDraft:
        option = Option(
            id=option_id,
            label=label,
            description=description,
            legality=legality,
            facts=facts,
        )
        return FrameDraft(
            options=(option,),
            state_fingerprint=fingerprint(scene, diag=option_id, why=str(facts.get("reason", ""))),
            context={"battle": "unreadable"},
        )

    def _unsupported(self, scene: str, block: S.BattleBlock) -> FrameDraft:
        kind = block.kind if not block.supported else "scene"
        what = {
            "double": "double or multi battle",
            "safari": "Safari Zone battle",
            "ghost": "Pokemon Tower ghost battle",
            "tutorial": "scripted tutorial battle",
            "link": "link battle",
            "other_unsupported": "battle tower or special-rule battle",
        }.get(kind, "this battle phase")
        option = Option(
            id=f"unsupported:{kind}",
            label=f"Unsupported: {what}",
            description=f"The agent does not play a {what}; the run must pause and escalate.",
            legality=Legality.UNSUPPORTED,
            facts={"battle_kind": kind, "reason": block.kind_reason},
        )
        return FrameDraft(
            options=(option,),
            state_fingerprint=fingerprint(scene, unsupported=kind, flags=block.type_flags),
            context={"battle": kind},
        )

    # -- battle.action -----------------------------------------------------------------------------

    @staticmethod
    def _heading_to_heal(ctx: BuildContext) -> bool:
        """Whether this battle interrupts a live route to a Pokemon Center."""
        return ctx.objective_kind == "heal"

    @staticmethod
    def _lowest_party(view: BattleView) -> tuple[str, int] | None:
        party = [mon for mon in view.party if not mon.fainted and not mon.is_egg]
        if not party:
            return None
        lowest = min(party, key=lambda mon: (mon.hp_percent, mon.slot))
        return lowest.name, lowest.hp_percent

    @staticmethod
    def _escape_summary(facts: dict[str, Any]) -> str:
        chance = facts.get("escape_chance_pct")
        if isinstance(chance, (list, tuple)) and chance:
            values = [value for value in chance if isinstance(value, (int, float))]
            chance_text = (
                (f"{min(values):g}-{max(values):g}%" if len(set(values)) > 1 else f"{values[0]:g}%")
                if values
                else "unknown"
            )
        elif isinstance(chance, (int, float)):
            chance_text = f"{chance:g}%"
        else:
            chance_text = "unknown"
        return chance_text

    @staticmethod
    def _escape_is_low(facts: Mapping[str, Any]) -> bool:
        chance = facts.get("escape_chance_pct")
        values = (
            [value for value in chance if isinstance(value, (int, float))]
            if isinstance(chance, (list, tuple))
            else [chance]
            if isinstance(chance, (int, float))
            else []
        )
        return bool(values) and max(values) < 50

    @staticmethod
    def _active_party_slot(view: BattleView) -> int | None:
        return next((mon.slot for mon in view.party if mon.active), None)

    def _healing_option(self, option: Option, active_slot: int | None) -> bool:
        if option.facts.get("item_class") not in {"hp", "cure"}:
            return False
        target_slot = option.facts.get("target_slot")
        if target_slot == active_slot:
            return True
        target_hp = option.facts.get("target_hp")
        target_max_hp = option.facts.get("target_max_hp")
        return (
            isinstance(target_slot, int)
            and target_slot != active_slot
            and (
                (
                    isinstance(target_hp, (int, float))
                    and isinstance(target_max_hp, (int, float))
                    and target_hp < target_max_hp
                )
                or bool(option.facts.get("target_status"))
            )
        )

    def _action(self, ctx: BuildContext, view: BattleView, phase: str) -> FrameDraft:
        heading_to_heal = view.kind == "wild" and self._heading_to_heal(ctx)
        options: list[Option] = []
        options += self._move_options(view, verb="USE_MOVE", prefix="Use")
        options += self._switch_options(view, forced=False)
        options += self._item_options(view)
        options += self._ball_options(view)
        esc = self._escape_option(view)
        if esc is not None:
            if heading_to_heal:
                lowest = self._lowest_party(view)
                if lowest is not None:
                    name, hp_percent = lowest
                    summary = self._escape_summary(dict(esc.facts))
                    reason = (
                        f"Heading to heal (lowest: {name} {hp_percent}%); fleeing avoids further "
                        f"damage; escape chance ~{summary}"
                    )
                    esc = esc.model_copy(
                        update={
                            "description": reason + ".",
                            "facts": {**esc.facts, "heal_escape_reason": reason},
                        }
                    )
            options.append(esc)
        escape = [option for option in options if option.id == "escape"]
        non_escape = [option for option in options if option.id != "escape"]
        options = non_escape + escape
        extra = foe_facts(view, ctx.snapshot, ctx.objective) if view.kind == "wild" else {}
        extra["foe_hp_percent"] = view.opponent.hp_percent
        extra["foe_species"] = view.opponent.species
        matchup = self._party_matchup(options)
        if matchup:
            extra["party_matchup"] = matchup
        if view.kind == "wild" and view.tables is not None:
            try:
                extra["foe_types"] = [
                    view.tables.type_name(value)
                    for value in dict.fromkeys(view.tables.species(view.opponent.species).types)
                ]
            except (AttributeError, KeyError, IndexError, TypeError, ValueError):
                pass
        if view.kind == "wild":
            for index, option in enumerate(options):
                if not option.id.startswith("ball:"):
                    continue
                options[index] = option.model_copy(update={"facts": {**option.facts, **extra}})
        notes: list[str] = []
        threat = None
        cleaned: list[Option] = []
        for option in options:
            facts = dict(option.facts)
            if threat is None and isinstance(facts.get("foe_threat"), dict):
                threat = facts["foe_threat"]
            for note in facts.pop("ability_effects", ()):
                normalized = str(note).removeprefix("possible ")
                if all(str(existing).removeprefix("possible ") != normalized for existing in notes):
                    notes.append(note)
            facts.pop("foe_threat", None)
            cleaned.append(option.model_copy(update={"facts": facts}))
        options = cleaned
        if heading_to_heal:
            threat_can_ko = isinstance(threat, dict) and threat.get("can_ko") in ("yes", "likely")
            updated: list[Option] = []
            for option in options:
                if option.id.startswith("move:"):
                    facts = dict(option.facts)
                    if threat_can_ko:
                        facts["heal_faint_warning"] = "active Pokemon would faint this turn"
                    if isinstance(threat, dict) and threat.get("can_ko") != "no":
                        facts["heal_risk"] = "risky: you are heading to heal"
                    option = option.model_copy(update={"facts": facts})
                updated.append(option)
            options = updated
            # Healing is a strategic objective, not a game rule. Keep every legal battle
            # action chooseable so the decider can still fight when escape is unavailable.
            escape = [option for option in options if option.id == "escape"]
            options = escape + [option for option in options if option.id != "escape"]
            hidden = ()
        else:
            hidden = ()
        owned = getattr(getattr(ctx.snapshot, "pokedex", None), "owned", None)
        dex = view.tables.national_dex(view.opponent.species) if view.tables is not None else None
        seen = getattr(getattr(ctx.snapshot, "pokedex", None), "seen", None)
        party_foe_facts = {
            "foe_species": view.opponent.species,
            "foe_new_to_pokedex": None if owned is None or dex is None else dex not in owned,
            "foe_seen_in_pokedex": None if dex is None or seen is None else dex in seen,
        }
        options = [
            option.model_copy(update={"facts": {**option.facts, **party_foe_facts}})
            if option.id.startswith("ball:")
            else option
            for option in options
        ]
        if threat is not None:
            extra["foe_threat"] = threat
        if notes:
            extra["battle_ability_notes"] = notes
        return FrameDraft(
            options=tuple(options),
            state_fingerprint=self._fp(ctx.scene.scene_id, view),
            context=self._context(view, extra),
            all_options=hidden,
        )

    def _move_options(self, view: BattleView, *, verb: str, prefix: str) -> list[Option]:
        out: list[Option] = []
        verdicts, struggle = L.move_verdicts(view)
        me = view.block.battler_at(view.player.battler)
        assert me is not None
        for slot, verdict in enumerate(verdicts):
            if verdict is None:
                continue
            slot_move = me.moves[slot]
            facts = self._provider("move_facts", view, slot)
            facts.setdefault("slot", slot)
            facts.setdefault("move_id", slot_move.move_id)
            facts.setdefault("pp", slot_move.pp)
            facts.update(verdict.facts)
            name = str(facts.get("name") or f"move {slot_move.move_id}")
            legality = verdict.legality
            accuracy = facts.get("accuracy")
            if legality is Legality.LEGAL and isinstance(accuracy, int) and 0 < accuracy < 100:
                legality = Legality.CONDITIONAL  # it can miss: legal input, uncertain outcome
            if verdict.reason:
                facts["unavailable_reason"] = verdict.reason
            out.append(
                Option(
                    id=f"move:{slot}",
                    label=f"{prefix} {name}",
                    description=self._move_text(name, facts, verdict.reason),
                    legality=legality,
                    facts=facts,
                    executor_ref=R.USE_MOVE if legality.chooseable else None,
                    executor_args={
                        "slot": slot,
                        "move_id": slot_move.move_id,
                        **({"battler": me.battler} if me.battler else {}),
                    },
                    postcondition_ref=R.PC_MOVE if legality.chooseable else None,
                    verb=verb,
                )
            )
        if struggle:
            out.append(
                Option(
                    id="move:struggle",
                    label=f"{prefix} Struggle",
                    description=(
                        "Every move is unusable, so the game forces Struggle: it hurts the user "
                        "by recoil."
                    ),
                    legality=Legality.LEGAL,
                    facts={"struggle": True},
                    executor_ref=R.USE_STRUGGLE,
                    postcondition_ref=R.PC_MOVE,
                    verb=verb,
                )
            )
        return out

    @staticmethod
    def _move_text(name: str, facts: dict, reason: str | None) -> str:
        bits = []
        if facts.get("type"):
            bits.append(str(facts["type"]).lower())
        if facts.get("category"):
            bits.append(str(facts["category"]))
        if isinstance(facts.get("power"), int) and facts["power"]:
            bits.append(f"power {facts['power']}")
        if isinstance(facts.get("accuracy"), int) and facts["accuracy"]:
            bits.append(f"accuracy {facts['accuracy']}")
        if "pp" in facts:
            pp = f"PP {facts['pp']}"
            if "max_pp" in facts:
                pp += f"/{facts['max_pp']}"
            bits.append(pp)
        text = f"{name}" + (f" ({', '.join(bits)})" if bits else "")
        if reason:
            text += f"; not selectable: {reason.replace('_', ' ')}"
        return _clip(text, 580)

    def _switch_options(
        self,
        view: BattleView,
        *,
        forced: bool,
    ) -> list[Option]:
        out: list[Option] = []
        verb = "SEND_OUT" if forced else "SWITCH_TO"
        word = "Send out" if forced else "Switch to"
        for mon in view.party:
            verdict = L.switch_verdict(view, mon.slot, forced=forced)
            facts = self._provider("switch_facts", view, mon.slot)
            facts.setdefault("slot", mon.slot)
            facts.update(verdict.facts)
            if verdict.reason:
                facts["unavailable_reason"] = verdict.reason
            desc = f"{mon.name} Lv{mon.level}, HP {mon.hp}/{mon.max_hp}"
            if mon.status:
                desc += " (" + ", ".join(mon.status) + ")"
            if verdict.reason:
                desc += f"; not selectable: {verdict.reason.replace('_', ' ')}"
            out.append(
                Option(
                    id=("send_out" if forced else "switch") + f":{mon.slot}",
                    label=f"{word} {mon.name}",
                    description=_clip(desc, 580),
                    legality=verdict.legality,
                    facts=facts,
                    executor_ref=(R.SEND_OUT if forced else R.SWITCH_TO)
                    if verdict.chooseable
                    else None,
                    executor_args={"slot": mon.slot, "personality": mon.personality},
                    postcondition_ref=(R.PC_SENDOUT if forced else R.PC_SWITCH)
                    if verdict.chooseable
                    else None,
                    verb=verb,
                )
            )
        return out

    @staticmethod
    def _party_matchup(options: list[Option]) -> list[dict[str, Any]] | None:
        """Top 3 selectable switch candidates vs the foe, from the switch options' own facts."""
        rows = []
        for o in options:
            f = o.facts
            if not o.id.startswith("switch:") or o.legality not in (
                Legality.LEGAL,
                Legality.CONDITIONAL,
            ):
                continue
            best, stab, hp = f.get("best_move"), f.get("foe_stab_vs_candidate"), f.get("hp_pct")
            if f.get("species") and best and stab and hp is not None:
                rows.append(
                    {
                        "species": f["species"],
                        "best_move": best,
                        "foe_stab_vs_it": stab,
                        "hp_pct": hp,
                    }
                )
        rows.sort(
            key=lambda r: (
                -r["best_move"]["effectiveness"],
                r["foe_stab_vs_it"]["effectiveness"],
                -r["hp_pct"],
            )
        )
        return rows[:3] or None

    def _switch_offer_facts(self, view: BattleView) -> tuple[dict[str, Any], dict[str, Any]]:
        """Public counter facts for the trainer's incoming Pokemon prompt.

        The prompt is before the incoming mon has acted, so both the keep and replacement
        calculations use an unopposed first action.  If the provider cannot calculate mechanics,
        returning empty facts preserves the prompt's legality and deterministic fallback.
        """
        message = view.block.message or ""
        match = re.search(r"about to use\s+([A-Za-z0-9' .-]+?)(?:[.!?]|$)", message, re.I)
        if not match or not getattr(self._facts, "tables", None):
            return {}, {}
        raw = match.group(1).strip().upper()
        tables = self._facts.tables
        species_id = next((s.species_id for s in tables.all_species if s.name.upper() == raw), None)
        if species_id is None:
            return {}, {}
        from pokegent.games.gen3.mechanics.race_facts import race_facts
        from pokegent.games.gen3.mechanics.views import BattlerView

        incoming = BattlerView(
            species=species_id,
            level=view.opponent.level,
            hp=None,
            max_hp=None,
            hp_pct=100.0,
            stats=None,
            types=tables.species(species_id).types,
            is_player_side=False,
            name=tables.species_name(species_id),
        )
        ours = self._facts.player_battler(view)
        field = self._facts.field_view(view)
        keep = race_facts(ours, incoming, tables, field, option="attack")
        candidates = []
        for mon in view.party:
            if mon.active or mon.fainted or mon.is_egg:
                continue
            candidate = self._facts.bench_battler(view, mon)
            facts = self._facts.switch_facts(view, mon.slot)
            facts["incoming_foe"] = tables.species_name(species_id)
            facts["incoming_foe_types"] = [
                tables.type_name(t) for t in tables.species(species_id).types
            ]
            facts.update(
                race_facts(
                    ours, incoming, tables, field, option="switch", switch_in=candidate, forced=True
                )
            )
            candidates.append(facts)
        return {"incoming_foe": tables.species_name(species_id), **keep}, {
            "incoming_foe": tables.species_name(species_id),
            "switch_candidates": candidates,
        }

    def _item_options(self, view: BattleView) -> list[Option]:
        out: list[Option] = []
        for item_id in sorted(view.bag):
            for target, verdict in self._items.targets(view, item_id):
                facts = self._provider("item_facts", view, item_id, target)
                facts.setdefault("item_id", item_id)
                facts.setdefault("target_slot", target)
                facts.update(verdict.facts)
                if verdict.reason:
                    facts["unavailable_reason"] = verdict.reason
                name = str(
                    facts.get("name")
                    or (view.tables.item_name(item_id) if view.tables else None)
                    or f"item {item_id}"
                )
                mon = view.mon(target) if target >= 0 else None
                label = f"Use {name}" + (f" on {mon.name}" if mon else "")
                desc = label
                if mon:
                    desc += f" (HP {mon.hp}/{mon.max_hp}"
                    desc += (", " + ", ".join(mon.status)) if mon.status else ""
                    desc += ")"
                left = view.bag.get(item_id, 0)
                desc += "; the last one" if left == 1 else f"; {left} left"
                if verdict.reason:
                    desc += f"; not selectable: {verdict.reason.replace('_', ' ')}"
                chooseable = verdict.chooseable
                out.append(
                    Option(
                        id=f"item:{item_id}:{target}",
                        label=_clip(label, 110),
                        description=_clip(desc, 580),
                        legality=verdict.legality,
                        facts=facts,
                        executor_ref=R.USE_ITEM if chooseable else None,
                        executor_args={
                            "item_id": item_id,
                            "target_slot": target,
                            "pocket": L.MENU_POCKET.get(view.bag_pocket.get(item_id, ""), 0),
                        },
                        postcondition_ref=R.PC_ITEM if chooseable else None,
                        verb="USE_BATTLE_ITEM",
                    )
                )
        return out

    def _ball_options(self, view: BattleView) -> list[Option]:
        out: list[Option] = []
        pending: list[tuple[Option, dict[str, Any]]] = []
        for item_id in sorted(view.bag):
            info = view.block.lookups.items.get(item_id)
            if view.bag_pocket.get(item_id) != "poke_balls" and not (info and info.role == "ball"):
                continue
            verdict = L.ball_verdict(view, item_id)
            if verdict.reason == "trainer_battle":
                continue  # a trainer battle never offers a capture
            facts = self._provider("ball_facts", view, item_id)
            facts.setdefault("item_id", item_id)
            facts.update(verdict.facts)
            if verdict.reason:
                facts["unavailable_reason"] = verdict.reason
            name = str(facts.get("name") or f"item {item_id}")
            option = Option(
                id=f"ball:{item_id}",
                label=f"Throw {name}",
                description=_clip(
                    f"Throw a {name} at the wild {view.opponent.name} "
                    f"(Lv{view.opponent.level}, HP {view.opponent.hp_percent}%); "
                    f"{view.bag.get(item_id, 0)} left. A catch can fail.",
                    580,
                ),
                legality=verdict.legality,
                facts=facts,
                executor_ref=R.THROW_BALL if verdict.chooseable else None,
                executor_args={"item_id": item_id},
                postcondition_ref=R.PC_BALL if verdict.chooseable else None,
                verb="THROW_BALL",
            )
            pending.append((option, facts))
        all_facts = [f for _, f in pending]
        for option, facts in pending:
            add_ball_facts(facts, all_facts)
            out.append(option.model_copy(update={"facts": facts}))
        return out

    def _escape_option(self, view: BattleView) -> Option | None:
        if view.is_trainer:
            return None  # never offered in a trainer battle
        verdict = L.escape_verdict(view)
        facts = self._provider("escape_facts", view)
        facts.update(verdict.facts)
        if verdict.reason:
            facts["unavailable_reason"] = verdict.reason
        return Option(
            id="escape",
            label="Run away",
            description="Try to flee from the wild battle; the attempt can fail.",
            legality=verdict.legality,
            facts=facts,
            executor_ref=R.ESCAPE if verdict.chooseable else None,
            postcondition_ref=R.PC_ESCAPE if verdict.chooseable else None,
            verb="ATTEMPT_ESCAPE",
        )

    # -- battle.moves_ui ---------------------------------------------------------------------------

    def _moves_ui(self, ctx: BuildContext, view: BattleView, phase: str) -> FrameDraft:
        options = self._move_options(view, verb="SELECT_MOVE", prefix="Select")
        options.append(self._back_option("Back to the action menu"))
        return FrameDraft(
            options=tuple(options),
            state_fingerprint=self._fp(ctx.scene.scene_id, view),
            context=self._context(view, {"menu": "move list"}),
        )

    @staticmethod
    def _back_option(text: str, verb: str | None = "BACK") -> Option:
        return Option(
            id="back",
            label=text,
            description=f"{text} (press B).",
            legality=Legality.LEGAL,
            executor_ref=R.BACK,
            postcondition_ref=R.PC_LEFT,
            verb=verb,
            facts={"escape": True},
        )

    # -- party screens -----------------------------------------------------------------------------

    def _party(self, ctx: BuildContext, view: BattleView, phase: str) -> FrameDraft:
        options = self._switch_options(view, forced=False)
        options.append(self._back_option("Leave the party screen"))
        return FrameDraft(
            options=tuple(options),
            state_fingerprint=self._fp(ctx.scene.scene_id, view, phase=phase),
            context=self._context(view, {"menu": "party screen", "party_phase": phase}),
        )

    def _forced_switch(self, ctx: BuildContext, view: BattleView, phase: str) -> FrameDraft:
        options = self._switch_options(view, forced=True)
        return FrameDraft(
            options=tuple(options),
            state_fingerprint=self._fp(ctx.scene.scene_id, view, phase=phase),
            context=self._context(view, {"menu": "choose the next Pokemon", "party_phase": phase}),
        )

    def _item_target(self, ctx: BuildContext, view: BattleView, phase: str) -> FrameDraft:
        return FrameDraft(
            options=(self._back_option("Leave the item target screen", verb=None),),
            state_fingerprint=self._fp(ctx.scene.scene_id, view, phase=phase),
            context=self._context(view, {"menu": "item target"}),
        )

    def _bag(self, ctx: BuildContext, view: BattleView, phase: str) -> FrameDraft:
        return FrameDraft(
            options=(self._back_option("Close the bag", verb="CANCEL"),),
            state_fingerprint=self._fp(ctx.scene.scene_id, view, phase=phase),
            context=self._context(view, {"menu": "bag"}),
        )

    # -- text, level-up, ending --------------------------------------------------------------------

    def _message(self, ctx: BuildContext, view: BattleView, phase: str) -> FrameDraft:
        return self._single(
            ctx,
            view,
            "advance_text",
            "Continue",
            "Advance the battle text (press A).",
            R.ADVANCE_TEXT,
            R.PC_TEXT,
            "ADVANCE_TEXT",
            phase,
        )

    def _ended(self, ctx: BuildContext, view: BattleView, phase: str) -> FrameDraft:
        if phase == "nickname":
            option = Option(
                id="decline_nickname",
                label="Do not give a nickname",
                description="Answer NO to the nickname offer for the caught Pokemon.",
                legality=Legality.LEGAL,
                semantic_prompt="Do you want to give a nickname to the caught Pokemon?",
                effect_description="The Pokemon keeps its species name.",
                executor_ref=R.ANSWER,
                executor_args={"box": R.PROMPT_NICKNAME, "cursor": 1},
                postcondition_ref=R.PC_ANSWER,
                verb="ACKNOWLEDGE",
            )
            return FrameDraft(
                options=(option,),
                state_fingerprint=self._fp(ctx.scene.scene_id, view, phase=phase),
                context=self._context(view, {"question": view.block.message}),
            )
        return self._single(
            ctx,
            view,
            "acknowledge",
            "Continue",
            "Advance the end-of-battle text (press A).",
            R.ADVANCE_TEXT,
            R.PC_TEXT,
            "ACKNOWLEDGE",
            phase,
        )

    def _level_up(self, ctx: BuildContext, view: BattleView, phase: str) -> FrameDraft:
        return self._single(
            ctx,
            view,
            "acknowledge",
            "Continue",
            "Close the level-up stat box (press A).",
            R.ACKNOWLEDGE,
            R.PC_LEVELUP,
            "ACKNOWLEDGE",
            phase,
        )

    def _single(
        self, ctx, view, option_id, label, description, executor, post, verb, phase
    ) -> FrameDraft:
        option = Option(
            id=option_id,
            label=label,
            description=description,
            legality=Legality.LEGAL,
            executor_ref=executor,
            postcondition_ref=post,
            verb=verb,
        )
        ui = ctx.snapshot.ui
        printers = (
            [[p.window_id, p.state, p.current_char_ptr] for p in ui.text.printers] if ui else []
        )
        return FrameDraft(
            options=(option,),
            state_fingerprint=self._fp(
                ctx.scene.scene_id,
                view,
                phase=phase,
                printers=printers,
                text=view.block.message,
                lvl=view.block.lvlup_state,
                outcome=view.block.outcome,
            ),
            context=self._context(
                view, {"last_text": view.block.message} if view.block.message else None
            ),
        )

    # -- prompts -----------------------------------------------------------------------------------

    def _switch_offer(self, ctx: BuildContext, view: BattleView, phase: str) -> FrameDraft:
        question = view.block.message or "Do you want to change Pokemon?"
        keep_facts, accept_facts = self._switch_offer_facts(view)
        keep = Option(
            id="keep_current",
            label="Keep the current Pokemon",
            description="Answer NO: stay with the Pokemon on the field and let the battle go on.",
            legality=Legality.LEGAL,
            semantic_prompt=question,
            effect_description="No switch; the opponent's next Pokemon comes out.",
            executor_ref=R.ANSWER,
            executor_args={"box": R.PROMPT_SWITCH_OFFER, "cursor": 1},
            postcondition_ref=R.PC_ANSWER,
            verb="KEEP_CURRENT",
            facts=keep_facts,
        )
        accept = Option(
            id="accept_switch",
            label="Switch Pokemon",
            description=(
                "Answer YES: open the party screen and choose which Pokemon to send in before the "
                "opponent's next one arrives."
            ),
            legality=Legality.LEGAL,
            semantic_prompt=question,
            effect_description="Opens the party screen; the next screen offers the switch targets.",
            executor_ref=R.ANSWER,
            executor_args={"box": R.PROMPT_SWITCH_OFFER, "cursor": 0},
            postcondition_ref=R.PC_ANSWER,
            verb="ACCEPT_SWITCH",
            facts=accept_facts,
        )
        return FrameDraft(
            options=(keep, accept),
            state_fingerprint=self._fp(
                ctx.scene.scene_id, view, text=view.block.message, op=view.block.script_op
            ),
            context=self._context(view, {"question": view.block.message}),
        )

    def _faint(self, ctx: BuildContext, view: BattleView, phase: str) -> FrameDraft:
        question = view.block.message or "Use next Pokemon?"
        nxt = Option(
            id="send_out_next",
            label="Choose the next Pokemon",
            description="Answer YES: open the party screen to send out another Pokemon.",
            legality=Legality.LEGAL,
            semantic_prompt=question,
            effect_description="Opens the party screen; sending out is mandatory there.",
            executor_ref=R.ANSWER,
            executor_args={"box": R.PROMPT_USE_NEXT, "cursor": 0},
            postcondition_ref=R.PC_ANSWER,
            verb="SEND_OUT",
        )
        flee = Option(
            id="flee",
            label="Run away instead",
            description="Answer NO: try to flee from the wild battle; the attempt can fail.",
            legality=Legality.CONDITIONAL if view.kind == "wild" else Legality.UNAVAILABLE,
            semantic_prompt=question,
            effect_description="A flee attempt; if it fails the party screen opens anyway.",
            executor_ref=R.ANSWER if view.kind == "wild" else None,
            executor_args={"box": R.PROMPT_USE_NEXT, "cursor": 1},
            postcondition_ref=R.PC_ANSWER if view.kind == "wild" else None,
            verb=None,
        )
        return FrameDraft(
            options=(nxt, flee),
            state_fingerprint=self._fp(ctx.scene.scene_id, view, text=view.block.message),
            context=self._context(view, {"question": view.block.message}),
        )

    def _learn_move(self, ctx: BuildContext, view: BattleView, phase: str) -> FrameDraft:
        block = view.block
        hidden: tuple[Option, ...] = ()
        learner = (
            ctx.snapshot.party[block.learn_move_party_id]
            if 0 <= block.learn_move_party_id < len(ctx.snapshot.party)
            else None
        )
        move_name = (
            block.lookups.moves[block.move_to_learn].name
            if block.move_to_learn in block.lookups.moves
            else None
        )
        if phase == "learn_move":
            new_fact = self._learn_move_fact(
                block,
                block.move_to_learn,
                learner_species=learner.species if learner is not None else None,
            )
            known_moves = [
                self._learn_move_fact(block, known.move_id, learner_species=learner.species)
                for known in (learner.moves if learner is not None else ())
                if known.move_id
            ]
            move_facts = {
                "new_move": new_fact,
                "known_moves": known_moves,
                "consequence": {
                    "decline_move": "gives up the new move, keeps current moves",
                    "learn_move": "opens the move replacement choice",
                },
            }
            decline = Option(
                id="decline_move",
                label=f"Do not forget a move for {move_name or 'the new move'}",
                description="Answer NO: keep all current moves; the new move is declined and "
                "the game asks whether to stop trying.",
                legality=Legality.LEGAL,
                semantic_prompt=block.message or "Delete a move to make room for a new move?",
                effect_description="The new move is not learned (a second question confirms).",
                executor_ref=R.ANSWER,
                executor_args={"box": R.PROMPT_LEARN_MOVE, "cursor": 1},
                postcondition_ref=R.PC_ANSWER,
                facts=move_facts,
            )
            learn = Option(
                id="learn_move",
                label=f"Forget a move to learn {move_name or 'the new move'}",
                description="Answer YES: open the summary screen and choose the move to replace.",
                legality=Legality.LEGAL,
                semantic_prompt=block.message or "Delete a move to make room for a new move?",
                effect_description=(
                    "Opens the four known moves; no move is lost until one is chosen."
                ),
                executor_ref=R.ANSWER,
                executor_args={"box": R.PROMPT_LEARN_MOVE, "cursor": 0},
                postcondition_ref=R.PC_ANSWER,
                verb="REPLACE_MOVE",
                facts=move_facts,
            )
            options = (decline, learn)
        elif phase == "forget_move":
            return self._forget_move(ctx, view)
        else:
            stop = Option(
                id="stop_learning",
                label=f"Stop trying to learn {move_name or 'the new move'}",
                description="Answer YES: the new move is not learned and the known moves stay.",
                legality=Legality.LEGAL,
                semantic_prompt=block.message or "Stop trying to teach a new move?",
                effect_description=(
                    f"stop_learning: gives up {move_name or 'the new move'}, keeps current moves."
                ),
                executor_ref=R.ANSWER,
                executor_args={"box": R.PROMPT_STOP_LEARNING, "cursor": 0},
                postcondition_ref=R.PC_ANSWER,
                facts={
                    "consequence": (
                        f"stop_learning: gives up {move_name or 'the new move'}, "
                        "keeps current moves"
                    )
                },
            )
            resume = Option(
                id="continue_learning",
                label="Keep trying to learn the move",
                description="Answer NO: continue_learning asks again whether to replace a move.",
                legality=Legality.LEGAL,
                executor_ref=R.ANSWER,
                executor_args={"box": R.PROMPT_STOP_LEARNING, "cursor": 1},
                postcondition_ref=R.PC_ANSWER,
                facts={"consequence": "continue_learning: asks again whether to replace a move"},
            )
            options = (stop, resume)
        return FrameDraft(
            options=options,
            state_fingerprint=self._fp(
                ctx.scene.scene_id, view, phase=phase, text=block.message, op=block.script_op
            ),
            context=self._context(view, {"question": block.message}),
            all_options=hidden if phase != "learn_move" else (),
        )

    @staticmethod
    def _learn_move_fact(
        block: S.BattleBlock,
        move_id: int,
        *,
        learner_species: int | None = None,
        tables: Any = None,
    ) -> dict[str, Any]:
        info = block.lookups.moves.get(move_id)
        if info is None and tables is not None:
            data = tables.move_data(move_id)
            name = tables.move_name(move_id)
            if data is not None and name:
                info = type(
                    "MoveInfo",
                    (),
                    {
                        "name": name,
                        "type": data.type,
                        "power": data.power,
                        "accuracy": data.accuracy,
                        "pp": data.pp,
                        "priority": data.priority,
                        "effect": data.effect,
                        "secondary_chance": data.secondary_chance,
                    },
                )()
        if info is None:
            return {"move_id": move_id, "name": f"move {move_id}"}
        # A zero-power move has no physical/special category.  Gen-3's split is by type only for
        # damaging moves, and the learner (not whichever mon is currently active) supplies STAB.
        category = (
            None if info.power == 0 else ("physical" if is_physical_type(info.type) else "special")
        )
        learner_types = block.lookups.species_types.get(learner_species or 0, ())
        return {
            "move_id": move_id,
            "name": info.name,
            "type_id": info.type,
            "power": info.power,
            "accuracy": info.accuracy,
            "pp": info.pp,
            "priority": info.priority,
            "effect": info.effect,
            "effect_chance": info.secondary_chance,
            "category": category,
            "stab": bool(category and info.type in learner_types),
        }

    def _forget_move(self, ctx: BuildContext, view: BattleView) -> FrameDraft:
        block = view.block
        party_id = block.learn_move_party_id
        member = ctx.snapshot.party[party_id] if party_id < len(ctx.snapshot.party) else None
        new_fact = self._learn_move_fact(
            block,
            block.move_to_learn,
            learner_species=member.species if member is not None else None,
            tables=view.tables,
        )
        options: list[Option] = [
            Option(
                id="give_up_move",
                label=f"Give up learning {new_fact['name']}",
                description="Press B without replacing a move; the stop-learning question follows.",
                legality=Legality.LEGAL,
                executor_ref=R.BACK,
                postcondition_ref=R.PC_LEFT,
                semantic_prompt="Which move should be forgotten?",
                facts={"new_move": new_fact, "escape": True},
            )
        ]
        if member is not None:
            for slot, known in enumerate(member.moves):
                if not known.move_id:
                    continue
                old_fact = self._learn_move_fact(
                    block, known.move_id, learner_species=member.species, tables=view.tables
                )
                is_hm = known.move_id in HM_MOVE_IDS
                options.append(
                    Option(
                        id=f"forget_move:{slot}",
                        label=f"Forget {old_fact['name']} for {new_fact['name']}",
                        description=(
                            "HM moves cannot be forgotten."
                            if is_hm
                            else f"Permanently replace slot {slot + 1} with {new_fact['name']}."
                        ),
                        legality=Legality.UNAVAILABLE if is_hm else Legality.CONDITIONAL,
                        executor_ref=None if is_hm else R.FORGET_MOVE,
                        executor_args=(
                            {}
                            if is_hm
                            else {
                                "slot": slot,
                                "old_move": known.move_id,
                                "new_move": block.move_to_learn,
                                "party_id": party_id,
                            }
                        ),
                        postcondition_ref=None if is_hm else R.PC_MOVE_REPLACED,
                        risk=Risk.HIGH,
                        verb="REPLACE_MOVE",
                        semantic_prompt="Which move should be forgotten?",
                        effect_description=(
                            f"{old_fact['name']} is lost and {new_fact['name']} is learned."
                        ),
                        irreversible=not is_hm,
                        facts={
                            "slot": slot,
                            "forgets": old_fact,
                            "learns": new_fact,
                            **({"unavailable_reason": "hm_move"} if is_hm else {}),
                        },
                    )
                )
        return FrameDraft(
            options=tuple(options),
            state_fingerprint=self._fp(
                ctx.scene.scene_id,
                view,
                phase="forget_move",
                party_id=party_id,
                new_move=block.move_to_learn,
            ),
            context=self._context(view, {"question": "Which move should be forgotten?"}),
        )

    def _choose_target(self, ctx: BuildContext, view: BattleView, phase: str) -> FrameDraft:
        return self._diag(
            ctx.scene.scene_id,
            "unknown:target_menu",
            Legality.UNKNOWN,
            "Target menu",
            "A target menu only exists in double battles, which are not supported.",
            {"reason": "target menu in a single battle"},
        )


UNSUPPORTED_SCENES = _UNSUPPORTED_SCENES = frozenset(
    {
        "battle.link",
    }
)

_HANDLERS = {
    "battle.action": BattleBuilder._action,
    "battle.moves_ui": BattleBuilder._moves_ui,
    "battle.party": BattleBuilder._party,
    "battle.forced_switch": BattleBuilder._forced_switch,
    "battle.item_target": BattleBuilder._item_target,
    "battle.bag": BattleBuilder._bag,
    "battle.message": BattleBuilder._message,
    "battle.ended": BattleBuilder._ended,
    "battle.level_up": BattleBuilder._level_up,
    "battle.switch_offer": BattleBuilder._switch_offer,
    "battle.faint": BattleBuilder._faint,
    "battle.learn_move": BattleBuilder._learn_move,
    "battle.choose_target": BattleBuilder._choose_target,
}
