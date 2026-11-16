"""Complete battle option adapter for every local FireRed battle controller."""

from __future__ import annotations

from pokegent.actions.battle import refs as R
from pokegent.actions.battle.builder import BattleBuilder
from pokegent.actions.battle.domain import BattleDomainState
from pokegent.actions.battle.reader import battle_of
from pokegent.actions.battle.view import make_view
from pokegent.games.gen3 import mechanics_constants as K
from pokegent.options.models import Legality, Option, fingerprint
from pokegent.options.registry import BuildContext, FrameDraft


class CompleteBattleBuilder(BattleBuilder):
    """All local FireRed battle controllers on the unified state contract."""

    def build(self, ctx: BuildContext) -> FrameDraft:
        scene = ctx.scene.scene_id
        if scene not in _SPECIAL_SCENES:
            return super().build(ctx)
        draft = self._build_special(ctx)
        block = battle_of(ctx.snapshot)
        if block is None or not block.active or not block.valid:
            return draft
        try:
            view = make_view(ctx.snapshot, block)
        except ValueError:
            return draft
        return self._add_trainer_facts(view, draft)

    def _build_special(self, ctx: BuildContext) -> FrameDraft:
        scene = ctx.scene.scene_id
        block = battle_of(ctx.snapshot)
        if block is None or not block.active or not block.valid:
            return super().build(ctx)
        domain = BattleDomainState.from_snapshot(
            ctx.snapshot,
            scene_id=scene,
            phase=str(ctx.scene.evidence.get("phase", "")),
            ready=not ctx.scene.busy,
        )
        try:
            view = make_view(ctx.snapshot, block)
        except ValueError:
            return super().build(ctx)
        inner = str(ctx.scene.evidence.get("inner_scene", ""))
        if inner in {"battle.message", "battle.ended"}:
            return self._message(ctx, view, domain.decision.phase)
        if scene == "battle.safari":
            return self._safari(ctx, view, domain)
        if scene == "battle.ghost":
            return self._ghost(ctx, view, domain)
        if scene == "battle.double_turn":
            return self._double(ctx, view, domain)
        if scene in {"battle.double_target", "battle.choose_target"}:
            return self._targets(ctx, view, domain)
        # Special controllers (including Oak's first battle) are only an outer scene label.  The
        # inner battle UI still owns the actual input: use the same text/menu constructors as every
        # other battle instead of inventing a zero-input tutorial action.
        inner_handlers = {
            "battle.action": self._action,
            "battle.moves_ui": self._moves_ui,
            "battle.party": self._party,
            "battle.forced_switch": self._forced_switch,
            "battle.item_target": self._item_target,
            "battle.bag": self._bag,
            "battle.level_up": self._level_up,
            "battle.switch_offer": self._switch_offer,
            "battle.faint": self._faint,
            "battle.learn_move": self._learn_move,
            "battle.choose_target": self._choose_target,
        }
        handler = inner_handlers.get(inner)
        if handler is not None:
            return handler(ctx, view, domain.decision.phase)
        return super().build(ctx)

    def _safari(self, ctx, view, domain: BattleDomainState) -> FrameDraft:
        block = domain.block
        catch = self._provider("safari_facts", view)
        catch.pop("safari_catch_factor", None)
        catch.pop("safari_escape_factor", None)
        catch.pop("quantity", None)
        balls = {"balls_left": block.safari_balls} if block.safari_balls is not None else {}
        ball_facts = {**catch, **balls}
        tactic_facts = {
            key: value for key, value in ball_facts.items() if key != "catch_chance_pct"
        }
        balls_known = block.safari_balls is not None
        options = (
            Option(
                id="safari_ball",
                label="Throw a Safari Ball",
                description="Throw one Safari Ball; the catch can fail and the Pokemon may flee.",
                legality=(
                    Legality.UNAVAILABLE
                    if block.safari_balls == 0
                    else Legality.CONDITIONAL
                    if balls_known
                    else Legality.UNKNOWN
                ),
                facts={**ball_facts, "action": "ball", "uses_turn": True},
                executor_ref=R.SAFARI_ACTION if balls_known and block.safari_balls != 0 else None,
                executor_args={"entry": 0},
                postcondition_ref=R.PC_SAFARI if balls_known and block.safari_balls != 0 else None,
                verb="SAFARI_BALL",
            ),
            Option(
                id="safari_bait",
                label="Throw bait",
                description=(
                    "Bait lowers the catch factor and makes fleeing less likely for a few turns."
                ),
                legality=Legality.CONDITIONAL,
                facts={**tactic_facts, "action": "bait", "uses_turn": True},
                executor_ref=R.SAFARI_ACTION,
                executor_args={"entry": 1},
                postcondition_ref=R.PC_SAFARI,
                verb="SAFARI_BAIT",
            ),
            Option(
                id="safari_rock",
                label="Throw a rock",
                description=(
                    "A rock raises the catch factor and makes fleeing more likely for a few turns."
                ),
                legality=Legality.CONDITIONAL,
                facts={**tactic_facts, "action": "rock", "uses_turn": True},
                executor_ref=R.SAFARI_ACTION,
                executor_args={"entry": 2},
                postcondition_ref=R.PC_SAFARI,
                verb="SAFARI_ROCK",
            ),
            Option(
                id="safari_run",
                label="Run away",
                description="Leave the Safari encounter immediately.",
                legality=Legality.LEGAL,
                facts={**tactic_facts, "action": "run", "escape_chance_pct": 100},
                executor_ref=R.SAFARI_ACTION,
                executor_args={"entry": 3},
                postcondition_ref=R.PC_SAFARI,
                verb="SAFARI_RUN",
            ),
        )
        return FrameDraft(
            options=options,
            state_fingerprint=fingerprint(
                ctx.scene.scene_id, domain=domain.model_dump(mode="json")
            ),
            context=self._context(view, {"question": "What will the player throw?"}),
        )

    def _ghost(self, ctx, view, domain: BattleDomainState) -> FrameDraft:
        diagnostics = tuple(
            option.model_copy(
                update={
                    "legality": Legality.UNAVAILABLE,
                    "executor_ref": None,
                    "postcondition_ref": None,
                    "facts": {
                        **option.facts,
                        "reason": "the unidentified ghost cannot be targeted",
                    },
                    "description": "The unidentified ghost makes the player too scared to move.",
                }
            )
            for option in self._move_options(view, verb="USE_MOVE", prefix="Use")
        )
        escape = Option(
            id="escape",
            label="Run from the unidentified ghost",
            description=(
                "FireRed guarantees escape from a ghost battle before the Silph Scope identifies "
                "it."
            ),
            legality=Legality.LEGAL,
            facts={"guaranteed": True, "escape_chance_pct": 100, "reason": "unidentified_ghost"},
            executor_ref=R.ESCAPE,
            postcondition_ref=R.PC_ESCAPE,
            verb="ATTEMPT_ESCAPE",
        )
        return FrameDraft(
            options=(escape, *diagnostics),
            state_fingerprint=fingerprint(ctx.scene.scene_id, flags=domain.block.type_flags),
            context=self._context(view, {"silph_scope": False}),
        )

    def _double(self, ctx, view, domain: BattleDomainState) -> FrameDraft:
        inner = str(ctx.scene.evidence.get("inner_scene", "battle.action"))
        handlers = {
            "battle.action": self._action,
            "battle.moves_ui": self._moves_ui,
            "battle.party": self._party,
            "battle.forced_switch": self._forced_switch,
            "battle.item_target": self._item_target,
            "battle.bag": self._bag,
        }
        draft = handlers.get(inner, self._action)(ctx, view, domain.decision.phase)
        return FrameDraft(
            options=draft.options,
            state_fingerprint=fingerprint(
                ctx.scene.scene_id,
                battler=domain.decision.battler,
                turn=[domain.block.turn, domain.block.turn_marker],
            ),
            context={
                **draft.context,
                "decision_battler": domain.decision.battler,
            },
        )

    def _targets(self, ctx, view, domain: BattleDomainState) -> FrameDraft:
        options = []
        chooser = domain.decision.battler
        chosen = domain.block.battler_at(chooser) if chooser is not None else None
        move = chosen.chosen_move if chosen is not None else 0
        move_info = domain.block.lookups.moves.get(move)
        target_mask = move_info.target if move_info is not None else 0
        allow_self = bool(target_mask & (K.MOVE_TARGET_USER_OR_SELECTED | K.MOVE_TARGET_USER))
        allow_ally = bool(target_mask & K.MOVE_TARGET_FOES_AND_ALLY)
        for battler in domain.block.battlers:
            if not battler.present:
                continue
            if battler.battler == chooser and not allow_self:
                continue
            if battler.side == "player" and battler.battler != chooser and not allow_ally:
                continue
            if battler.side == "opponent" and target_mask & K.MOVE_TARGET_USER:
                continue
            side = "your" if battler.side == "player" else "the foe's"
            name = domain.block.lookups.species.get(battler.species, f"species {battler.species}")
            options.append(
                Option(
                    id=f"target:{battler.battler}",
                    label=f"Target {side} {name}",
                    description=(f"Choose {side} {name}, HP {battler.hp_percent}%."),
                    legality=Legality.LEGAL,
                    facts={
                        "side": battler.side,
                        "species": battler.species,
                        "level": battler.level,
                        "hp_percent": battler.hp_percent,
                    },
                    executor_ref=R.CHOOSE_TARGET,
                    executor_args={"battler": battler.battler},
                    postcondition_ref=R.PC_TARGET,
                    verb="TARGET_BATTLER",
                )
            )
        return FrameDraft(
            options=tuple(options),
            state_fingerprint=fingerprint(
                ctx.scene.scene_id,
                chooser=domain.decision.battler,
                targets=[option.id for option in options],
            ),
            context=self._context(view, {"question": "Which Pokemon should the move target?"}),
        )


_SPECIAL_SCENES = frozenset(
    {
        "battle.double_turn",
        "battle.double_target",
        "battle.choose_target",
        "battle.safari",
        "battle.ghost",
        "battle.tutorial_scripted",
    }
)


__all__ = ["CompleteBattleBuilder"]
