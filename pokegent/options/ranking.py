"""Deterministic ranking for decider-visible field choices."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pokegent.options.models import Option, freeze

STATE_CHANGING_TARGETS = frozenset(
    {
        "warp",
        "edge",
        "fly",
        "surf",
        "landing",
        "ladder",
        "elevator",
        "door",
        "stairs",
        "npc",
        "item",
        "hidden_item",
        "uncollected_item",
        "key_item",
        "field_move",
        "boulder_plan",
        "obstacle",
    }
)

ESCAPE_IDS = frozenset(
    {
        "log_off",
        "cancel",
        "cancel_choice",
        "cancel_unknown_menu",
        "escape",
    }
)


def rank_field_options(
    scene_id: str,
    options: Sequence[Option],
    progression: Any,
    *,
    max_visible_options: int,
    reason_predicate: Callable[[Option, Any], bool] | None = None,
    priority_tiebreaker: Callable[[Option], tuple[object, ...]] | None = None,
) -> tuple[tuple[Option, ...], tuple[Option, ...]]:
    """Rank executable choices, merge identical grass, then apply the field count cap.

    The deliberate order is: route step; current RAM/ROM reason; untried
    state-changing target; untried reasonless target. Facing-only choices follow
    other reasonless choices. Original order breaks exact ties. Route steps,
    escape/log-off choices, and menu/storage surfaces are never cut.
    """
    chooseable = [option for option in options if option.chooseable]

    def target_kind(option: Option) -> object:
        target = option.facts.get("target")
        return target.get("kind") if isinstance(target, Mapping) else None

    def is_route(option: Option) -> bool:
        return (
            option.facts.get("route_step") is True
            or option.facts.get("next_route_step") is True
            or option.facts.get("route_relation") == "next route step"
        )

    def is_escape(option: Option) -> bool:
        return option.facts.get("escape") is True or option.id in ESCAPE_IDS

    def serves_objective(option: Option) -> bool:
        return option.facts.get("serves_goal") is True

    menu_surface = any(option.facts.get("menu") is not None for option in chooseable)
    if menu_surface:
        return tuple(chooseable), ()

    def has_fresh_reason(option: Option) -> bool:
        # An unread sign is not itself fresh-RAM evidence that reading it will change state.
        if target_kind(option) == "sign":
            return False
        if reason_predicate is not None:
            return bool(reason_predicate(option, progression))
        return option.facts.get("rank_fresh_reason") is True

    def travel_cost(option: Option) -> float:
        for name in ("cost", "steps", "distance_steps", "travel_cost"):
            value = option.facts.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        return float("inf")

    def grass_identity(option: Option) -> tuple[object, ...] | None:
        target = option.facts.get("target")
        if not isinstance(target, Mapping) or target.get("kind") not in {"grass", "encounter"}:
            return None
        return tuple(sorted((key, repr(value)) for key, value in target.items()))

    grass_groups: dict[tuple[object, ...], list[tuple[int, Option]]] = {}
    for index, option in enumerate(chooseable):
        identity = grass_identity(option)
        if identity is not None:
            grass_groups.setdefault(identity, []).append((index, option))
    merged_grass: list[tuple[Option, str]] = []
    grass_representatives: set[str] = set()
    for group in grass_groups.values():
        _index, representative = min(group, key=lambda row: (travel_cost(row[1]), row[0]))
        grass_representatives.add(representative.id)
        merged_grass.extend(
            (option, representative.id)
            for _position, option in group
            if option.id != representative.id
        )
    ranked_candidates = [
        option
        for option in chooseable
        if grass_identity(option) is None or option.id in grass_representatives
    ]

    def key(option: Option) -> tuple[object, ...]:
        facts = option.facts
        kind = target_kind(option)
        tier = (
            0
            if is_route(option)
            else 1
            if has_fresh_reason(option)
            else 2
            if kind in STATE_CHANGING_TARGETS
            else 3
        )
        return (
            tier,
            1 if "teach_move" in facts.get("effect_classes", ()) else 0,
            1 if tier == 3 and (kind == "face" or option.id.startswith("face:")) else 0,
            *(priority_tiebreaker(option) if priority_tiebreaker is not None else ()),
            facts.get("steps", 10**9)
            if isinstance(facts.get("steps"), (int, float))
            and not isinstance(facts.get("steps"), bool)
            else 10**9,
        )

    ranked = [
        option
        for _index, option in sorted(
            enumerate(ranked_candidates), key=lambda pair: (*key(pair[1]), pair[0])
        )
    ]
    if scene_id == "dialog.multichoice" or len(ranked) <= max_visible_options:
        visible, hidden = ranked, []
    else:
        protected = [
            option
            for option in ranked
            if is_route(option) or is_escape(option) or serves_objective(option)
        ]
        visible = ranked[:max_visible_options]
        visible_ids = {option.id for option in visible}
        for option in protected:
            if option.id in visible_ids:
                continue
            replacement = next(
                (
                    index
                    for index in range(len(visible) - 1, -1, -1)
                    if visible[index] not in protected
                ),
                None,
            )
            if replacement is None:
                visible.append(option)
            else:
                visible[replacement] = option
            visible_ids = {item.id for item in visible}
        hidden = [option for option in ranked if option.id not in visible_ids]

    def annotate(option: Option, *, capped: bool = False, merged_into: str | None = None) -> Option:
        facts = dict(option.facts)
        previously_capped = facts.pop("count_cap", None) is not None
        if previously_capped and str(facts.get("policy_note", "")).startswith("not shown above"):
            facts.pop("policy_note", None)
        facts["rank_fresh_reason"] = has_fresh_reason(option)
        facts.setdefault(
            "policy_note",
            "current RAM/ROM reason" if has_fresh_reason(option) else "no current reason",
        )
        if capped:
            facts["count_cap"] = max_visible_options
            facts["policy_note"] = f"not shown above count cap {max_visible_options}"
        elif merged_into is not None:
            facts["equivalent_grass"] = merged_into
            facts["policy_note"] = f"merged with nearer identical grass option {merged_into}"
        return option.model_copy(update={"facts": freeze(facts)})

    return (
        tuple(annotate(option) for option in visible),
        tuple(annotate(option, capped=True) for option in hidden)
        + tuple(
            annotate(option, merged_into=representative) for option, representative in merged_grass
        ),
    )
