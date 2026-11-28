"""Option builders: contract and registry (spec 3, 5.4 "builder_registry.build").

A builder turns one confirmed scene plus the facts of a snapshot into a :class:`FrameDraft`: all
options it knows (any legality), the decision-relevant ``state_fingerprint`` and the English
context. It never presses anything, never calls a model and never assigns frame identity: the
pipeline wraps the draft into a :class:`~pokegent.options.models.DecisionFrame`.

Builders are registered by scene *family* (the namespace of the scene id: ``battle``, ``dialog``,
``field`` ...) or by explicit scene id; an explicit id wins.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.options.models import DecisionFrame, Legality, Option
from pokegent.options.screen_guard import guard_option
from pokegent.state.scenes import SceneResult

# These are executor classes, not substring labels.  In particular, ``pc`` means opening or
# leaving the PC, while ``storage`` means moving a Pokemon/item through PC storage.
_FORBID_REFS = {
    "pc": frozenset(
        {
            "field.use_pc",
            "system_flow.pc_menu_choice",
            "system_flow.pc_player",
            "system_flow.leave_pc",
            "dialog.select_pc_choice",
        }
    ),
    "storage": frozenset({"system_flow.pc_storage"}),
    "fly": frozenset({"field.use_fly", "field.fly_map_resume", "field.fly_map_cancel"}),
    "shop": frozenset(
        {
            "shop.main_cursor",
            "shop.list_item",
            "shop.list_back",
            "shop.quantity",
            "shop.quantity_back",
            "shop.confirm",
            "shop.confirm_no",
            "shop.done_continue",
            "shop.done_quit",
        }
    ),
    "sell": frozenset(
        {
            "shop.sell_item",
            "shop.sell_qty",
            "shop.sell_yes",
            "shop.sell_no",
            "shop.sell_done_continue",
            "shop.sell_done_quit",
        }
    ),
    "tm": frozenset({"field.teach_tm", "field.begin_teach_tm", "field.finish_teach_tm"}),
}


@dataclass(frozen=True)
class BuildContext:
    snapshot: StateSnapshot
    scene: SceneResult
    run_id: str = ""
    goal_id: str | None = None
    goal_revision: int | None = None
    #: Most recent failed actions, newest last: ``{"option_id", "scene_id", "error_code"}``.
    recent_failed: tuple[Mapping[str, Any], ...] = ()
    #: Reserved for the world/navigation state (WP4). Builders must tolerate ``None``.
    world: Any = None
    #: The active objective as the model reads it (English, bounded; contracts v1.3). When set the
    #: registry writes it into the frame context under ``objective``, whatever the builder wrote.
    objective: str | None = None
    #: Machine-readable kind from the active objective. Builders must never infer this from text.
    objective_kind: str | None = None
    #: Resolved target facts used by menu families; absent for legacy/fixed goals.
    objective_item: int | None = None
    objective_quantity: int | None = None
    #: Level goals from the objective's probes: ``(personality, slot, min_level)``; both ``None``
    #: means the whole party. Lets party menus name who still needs training.
    objective_levels: tuple[tuple[int | None, int | None, int], ...] = ()
    forbid: tuple[str, ...] = ()


@dataclass(frozen=True)
class FrameDraft:
    """What a builder returns. Frame identity (decision id, run id, seq) is not its business."""

    options: tuple[Option, ...]
    state_fingerprint: str
    context: Mapping[str, Any] = field(default_factory=dict)
    deadline_ms: int = 1500
    #: Legal options removed by a situation filter. Kept out of the ordinary provider view.
    all_options: tuple[Option, ...] = ()


@runtime_checkable
class OptionBuilder(Protocol):
    def build(self, ctx: BuildContext) -> FrameDraft: ...


OBJECTIVE_CONTEXT_MAX = 400


def _bounded(text: str, limit: int = OBJECTIVE_CONTEXT_MAX) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def family_of(scene_id: str) -> str:
    return scene_id.split(".", 1)[0]


class BuilderRegistry:
    def __init__(self) -> None:
        self._by_family: dict[str, OptionBuilder] = {}
        self._by_scene: dict[str, OptionBuilder] = {}
        self._verifier_refs: Callable[[], frozenset[str]] | None = None

    def bind_verifiers(self, verifiers: Any) -> None:
        self._verifier_refs = verifiers.refs

    def register_family(self, family: str, builder: OptionBuilder) -> None:
        if family in self._by_family:
            raise ValueError(f"a builder for family {family!r} is already registered")
        self._by_family[family] = builder

    def register_scenes(self, scene_ids: Iterable[str], builder: OptionBuilder) -> None:
        ids = list(scene_ids)
        clash = [s for s in ids if s in self._by_scene]
        if clash:
            raise ValueError(f"builders already registered for scenes {clash}")
        for scene_id in ids:
            self._by_scene[scene_id] = builder

    def family_builders(self) -> Mapping[str, OptionBuilder]:
        """Read-only view: builder per scene family (namespace). Used by the ownership report."""
        return dict(self._by_family)

    def scene_builders(self) -> Mapping[str, OptionBuilder]:
        """Read-only view: builder per explicit scene id (these win over the family builder)."""
        return dict(self._by_scene)

    def for_scene(self, scene_id: str) -> OptionBuilder | None:
        return self._by_scene.get(scene_id) or self._by_family.get(family_of(scene_id))

    def families(self) -> frozenset[str]:
        return frozenset(self._by_family)

    def build(self, ctx: BuildContext, decision_id: str) -> DecisionFrame | None:
        """Frame for ``ctx.scene``, or ``None`` if no builder is registered for it."""
        builder = self.for_scene(ctx.scene.scene_id)
        if builder is None:
            return None
        draft = builder.build(ctx)
        if self._verifier_refs is None and any(
            option.postcondition_ref is not None for option in draft.options
        ):
            raise RuntimeError(
                "BuilderRegistry must be bound to a VerifierRegistry before validating "
                "postconditions"
            )
        options = tuple(self._validated(option) for option in draft.options)
        options = tuple(
            guard_option(
                option,
                current_scene=ctx.scene.scene_id,
                families=frozenset(self._by_family),
                scenes=frozenset(self._by_scene),
            )
            for option in options
        )
        options = tuple(self._apply_forbidden(option, ctx.forbid) for option in options)
        # Objective forbids constrain actions, never the navigation needed to leave the
        # current menu/dialog.  If that leaves only an escape, make the intent explicit to
        # the decider and put it first so a resumed run cannot stall in the forbidden context.
        escapes = tuple(option for option in options if self._is_escape(option))
        if (
            ctx.forbid
            and escapes
            and not any(option.chooseable and not self._is_escape(option) for option in options)
        ):
            escape_ids = {option.id for option in escapes if option.chooseable}
            options = tuple(
                option.model_copy(update={"facts": {**option.facts, "next_step": True}})
                if option.id in escape_ids
                else option
                for option in options
            )
            options = tuple(
                (
                    *[option for option in options if self._is_escape(option)],
                    *[option for option in options if not self._is_escape(option)],
                )
            )
        hidden = tuple(self._validated(option) for option in draft.all_options)
        forbidden_hidden = tuple(
            self._apply_forbidden(option, ctx.forbid)
            for option in hidden
            if self._is_forbidden(option, ctx.forbid)
        )
        all_options = tuple(
            option for option in hidden if not self._is_forbidden(option, ctx.forbid)
        )
        option_ids = {option.id for option in options if option.chooseable}
        all_options = tuple(
            option for option in all_options if option.id not in option_ids and option.chooseable
        )
        context = dict(draft.context)
        if ctx.objective:
            objective = _bounded(ctx.objective)
            unmet_reason = context.get("objective_unmet_reason")
            if isinstance(unmet_reason, str) and unmet_reason:
                objective = _bounded(f"{objective} (unmet: {unmet_reason})")
            context["objective"] = objective
        diagnostic_ids = {option.id for option in options}
        forbidden_hidden = tuple(
            option for option in forbidden_hidden if option.id not in diagnostic_ids
        )
        return DecisionFrame.from_options(
            (*options, *forbidden_hidden),
            decision_id=decision_id,
            run_id=ctx.run_id or ctx.snapshot.run_id,
            snapshot_seq=ctx.snapshot.snapshot_seq,
            scene_id=ctx.scene.scene_id,
            scene_confidence=ctx.scene.confidence,
            state_fingerprint=draft.state_fingerprint,
            goal_id=ctx.goal_id,
            goal_revision=ctx.goal_revision,
            deadline_ms=draft.deadline_ms,
            context=context,
            all_options=all_options,
        )

    @staticmethod
    def _is_forbidden(option: Option, forbidden: tuple[str, ...]) -> bool:
        if not forbidden:
            return False
        facts = option.facts
        target = facts.get("target") if isinstance(facts.get("target"), Mapping) else {}
        ref = (option.executor_ref or "").lower()
        verb = (option.verb or "").lower()
        menu = str(facts.get("menu") or "").lower()
        menu_kind = str(facts.get("menu_kind") or "").lower()
        scene = str(facts.get("scene_family") or "").lower()
        checks = {
            "pc": (
                ref in _FORBID_REFS["pc"]
                or menu == "pc_menu"
                or (target.get("kind") == "pc" and ref not in _FORBID_REFS["storage"])
            ),
            "storage": (
                ref in _FORBID_REFS["storage"]
                or menu_kind in {"storage", "item_storage"}
                or (target.get("kind") == "pc" and ref in _FORBID_REFS["storage"])
            ),
            "fly": (
                verb in {"fly_to", "use_fly"}
                or ref in _FORBID_REFS["fly"]
                or target.get("kind") == "fly"
            ),
            "shop": (
                scene == "shop"
                or menu_kind == "shop"
                or verb in {"buy", "buy_item"}
                or ref in _FORBID_REFS["shop"]
            ),
            "tm": (
                verb in {"teach_move", "teach_tm"}
                or ref in _FORBID_REFS["tm"]
                or facts.get("item_kind") == "tm"
            ),
            "sell": verb in {"sell", "sell_item"} or ref in _FORBID_REFS["sell"],
        }
        return any(checks.get(name, False) for name in forbidden)

    @staticmethod
    def _is_escape(option: Option) -> bool:
        """Whether the builder says this option exits the current menu/dialog."""
        return option.facts.get("escape") is True

    @classmethod
    def _apply_forbidden(cls, option: Option, forbidden: tuple[str, ...]) -> Option:
        if not cls._is_forbidden(option, forbidden) or cls._is_escape(option):
            return option
        facts = dict(option.facts)
        facts["reason"] = "forbidden by objective"
        return option.model_copy(
            update={
                "legality": Legality.UNAVAILABLE,
                "executor_ref": None,
                "executor_args": {},
                "postcondition_ref": None,
                "verb": None,
                "facts": facts,
            }
        )

    def _validated(self, option: Option) -> Option:
        ref = option.postcondition_ref
        if ref is None or self._verifier_refs is None or ref in self._verifier_refs():
            return option
        facts = dict(option.facts)
        facts["reason"] = f"postcondition verifier is not registered: {ref}"
        return option.model_copy(
            update={
                "legality": Legality.UNAVAILABLE,
                "facts": facts,
                "executor_ref": None,
                "executor_args": {},
                "postcondition_ref": None,
                "verb": None,
            }
        )


def option_target_key(option: Option) -> str:
    """Stable semantic target identity used across map hops and rebuilt frames."""
    target = option.facts.get("target")
    return repr(_freeze_key(target)) if isinstance(target, Mapping) else option.id


def relevant_state_signature(snapshot: Any) -> str:
    """Flags, badges, bag and party state that can make a blocked target relevant again."""
    player = getattr(snapshot, "player", None)
    value = (
        tuple(getattr(getattr(snapshot, "flags", None), "set_ids", ())),
        tuple(getattr(player, "badges", ())),
        tuple(
            (getattr(item, "item_id", None), getattr(item, "quantity", None))
            for pocket in getattr(snapshot, "bag", ())
            for item in getattr(pocket, "items", ())
        ),
        tuple(
            (
                getattr(member, "personality", None),
                getattr(member, "species", None),
                getattr(member, "level", None),
                getattr(member, "hp", None),
                getattr(member, "max_hp", None),
                getattr(member, "status", None),
            )
            for member in getattr(snapshot, "party", ())
        ),
    )
    return repr(value)


def _freeze_key(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _freeze_key(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_key(item) for item in value)
    return value
