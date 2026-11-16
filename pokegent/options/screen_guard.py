"""Generic legality for menu entries whose target screen is not family-owned."""

from __future__ import annotations

from collections.abc import Mapping

from pokegent.options.models import Legality, Option


def guard_option(
    option: Option,
    *,
    current_scene: str,
    families: frozenset[str],
    scenes: frozenset[str],
) -> Option:
    target = option.facts.get("target_screen")
    if not isinstance(target, Mapping):
        return option
    target_scene = str(target.get("scene") or "")
    family = str(target.get("family") or "")
    contexts = target.get("supported_contexts", ())
    # A recovery detector is not an owning family: registering a foreign screen
    # must never make an option into that screen chooseable.
    supported = (
        family in families
        or current_scene in contexts
        or (target_scene in scenes and not bool(target.get("recovery_only")))
    )
    if supported or option.legality is not Legality.LEGAL:
        return option
    reason = f"no supported scene for {option.id}"
    facts = dict(option.facts)
    facts["reason"] = reason
    return option.model_copy(
        update={
            "legality": Legality.UNAVAILABLE,
            "executor_ref": None,
            "executor_args": {},
            "postcondition_ref": None,
            "verb": None,
            "action_contract": None,
            "facts": facts,
            "description": reason,
        }
    )
