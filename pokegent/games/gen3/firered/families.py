"""Game-owned registration of FireRed's families (#139).

The default runner built the family configuration inline: optional-family filtering, the
dialog prompt policy over the FireRed menu sources/tables, the system-flow options and the
field walk limit.  This module keeps that construction next to the FireRed composition it
describes; it calls the existing :func:`register_all` once and returns its unchanged
:class:`FamilyBundle` (no second registry or parallel bundle).
"""

from __future__ import annotations

from typing import Any

from pokegent.actions import (
    OPTIONAL_FAMILIES,
    FamilyBundle,
    FamilyConfig,
    Registries,
    register_all,
)
from pokegent.games.gen3.firered.composition import FireRedMenuServices


def register_families(
    registries: Registries,
    *,
    config: Any,
    menu_services: FireRedMenuServices,
    facts: Any = None,
    objectives: Any = None,
    graph: Any = None,
    navigation: Any = None,
    external_control: bool = False,
    rom_path: Any = None,
) -> FamilyBundle:
    """Register FireRed's core and optional families exactly as the default runner did.

    ``config`` is duck-typed (``map_mode``, ``unknown_prompts``, ``new_game``, ``external``) so
    the game module does not import the app; ``menu_services`` is the FireRed menu composition.
    """
    from pokegent.actions.menu.dialog import PromptPolicy

    # The field family cannot work without the map block: with map_mode "off" it is not registered
    # (a field.free claim without a map would only be a bounded wait that starves the executors'
    # post-step budgets of the other families).
    optional = tuple(
        name for name in OPTIONAL_FAMILIES if not (name == "field" and config.map_mode == "off")
    )
    return register_all(
        registries,
        FamilyConfig(
            optional=optional,
            prompt_policy=PromptPolicy(  # type: ignore[arg-type]
                unknown_prompts=config.unknown_prompts,
                menu_entries=menu_services.menu_entries,
                tables=menu_services.tables,
            ),
            facts=facts,
            objectives=objectives,
            graph=graph,
            navigation=navigation,
            options={
                "system_flow": {
                    "new_game": config.new_game.__dict__,
                    "menus": menu_services.menus,
                    "rom_path": str(rom_path) if rom_path is not None else None,
                },
                "field": {
                    "walk_step_limit": (
                        int(config.external.get("walk_step_limit", 8))
                        if external_control
                        else None
                    ),
                },
            },
        ),
    )
