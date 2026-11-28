"""Live objective resolution shared by field and scripted navigation scenes."""

from __future__ import annotations

from dataclasses import replace

from pokegent.games.gen3.firered.maps import map_names
from pokegent.goals.models import probe_leaves
from pokegent.goals.validate import ResolvedObjective, ResolvedTarget

_SUPPORTED_WILD_METHODS = frozenset({"land", "water"})

# Static legendaries have no wild-table rows but a fixed ROM map (pret FIRERED keys).
_STATIC_SOURCES: dict[int, tuple[tuple[int, int], str]] = {
    144: ((1, 87), "Seafoam Islands B4F"),  # Articuno
    145: ((1, 95), "Power Plant"),  # Zapdos
    146: ((1, 101), "Mt. Ember Summit"),  # Moltres
    150: ((1, 74), "Cerulean Cave B1F"),  # Mewtwo
}


def resolve_collect_navigation(snapshot, resolved: ResolvedObjective | None, world):
    """Resolve item and Pokemon acquisition objectives from the shared ROM index."""
    if world is None or resolved is None:
        return resolved, ()
    is_collect_item = (
        resolved.objective.kind == "collect"
        and resolved.target.item is not None
        and resolved.target.map_key is None
    )
    dex_probe = next(
        (probe for probe in probe_leaves(resolved.objective.success_probes)
         if probe.kind == "dex_owned"), None
    )
    species = (
        resolved.target.species
        if resolved.objective.kind in {"collect", "dex"}
        else None
    )
    if dex_probe is not None:
        species = dex_probe.species
    static_source = _STATIC_SOURCES.get(species) if species is not None else None
    if static_source is not None:
        (group, number), map_name = static_source
        target = ResolvedTarget(group=group, number=number, map_name=map_name, species=species)
        return replace(resolved, navigation_target=target, navigation_reason=None), ()
    is_wild = (
        species is not None
        and resolved.objective.kind in {"collect", "dex"}
        and hasattr(world, "wild_encounter_sources")
    )
    if is_wild:
        sources = world.wild_encounter_sources(species)
        if resolved.target.map_key is not None:
            sources = tuple(
                (map_key, methods)
                for map_key, methods in sources
                if map_key == resolved.target.map_key
            )
        supported_sources = tuple(
            (map_key, methods & _SUPPORTED_WILD_METHODS)
            for map_key, methods in sources
            if methods & _SUPPORTED_WILD_METHODS
        )
        maps = frozenset(map_key for map_key, _methods in supported_sources)
        if maps:
            return replace(
                resolved,
                navigation_map_keys=maps,
                navigation_map_methods=tuple(
                    (map_key, methods) for map_key, methods in supported_sources
                ),
                navigation_reason=None,
            ), ()
        name = world.tables.species_name(species)
        unsupported = tuple(
            (map_key, methods - _SUPPORTED_WILD_METHODS)
            for map_key, methods in sources
            if methods - _SUPPORTED_WILD_METHODS
        )
        if unsupported:
            methods = ", ".join(sorted({method for _, rows in unsupported for method in rows}))
            locations = ", ".join(
                map_names.map_name(*map_key) or str(map_key) for map_key, _ in unsupported
            )
            return replace(
                resolved,
                navigation_map_keys=frozenset(),
                navigation_reason=(
                    f"{name} ({species}) only via {methods} on {locations}: not supported"
                ),
            ), ()
        reason = (
            f"no ROM wild encounter table contains {name} ({species})"
            if resolved.target.map_key is None
            else f"{name} ({species}) is not in the ROM wild table for {resolved.target.map_name}"
        )
        if resolved.target.map_key is not None or not hasattr(world, "pokemon_gift_sources"):
            return replace(resolved, navigation_map_keys=frozenset(), navigation_reason=reason), ()
        # No wild table row at all: a gift-only species falls through to the gift scan below.
    is_pokemon = (
        species is not None
        and hasattr(world, "pokemon_gift_sources")
        and resolved.target.map_key is None
    )
    if not is_collect_item and not is_pokemon:
        return resolved, ()
    if is_pokemon:
        sources = world.pokemon_gift_sources(snapshot, species)
        routable = tuple(source for source in sources if source.status not in {"used", "locked"})
        targets = {(source.map_key, source.object_id): source for source in routable}
        if len(targets) == 1:
            source = next(iter(targets.values()))
            target = ResolvedTarget(
                group=source.map_key[0],
                number=source.map_key[1],
                map_name=source.map_name,
                npc=source.object_id,
                species=source.species,
            )
            return replace(resolved, navigation_target=target, navigation_reason=None), sources
        reason = (
            f"Pokemon gift has {len(targets)} routable ROM targets"
            if sources
            else "no remaining ROM source for Pokemon gift"
        )
        return replace(resolved, navigation_target=None, navigation_reason=reason), sources
    sources = world.collect_sources(snapshot, resolved.target.item)
    if len(sources) == 1:
        source = sources[0]
        locked_to_spawner = source.status == "locked" and source.spawned_by is not None
        target_map = (
            source.spawner_map_key or source.map_key if locked_to_spawner else source.map_key
        )
        target = ResolvedTarget(
            group=target_map[0],
            number=target_map[1],
            map_name=(
                source.spawner_map_name or source.map_name if locked_to_spawner else source.map_name
            ),
            x=source.x if source.kind == "item_ball" and source.status != "locked" else None,
            y=source.y if source.kind == "item_ball" and source.status != "locked" else None,
            npc=(
                source.spawned_by
                if locked_to_spawner
                else source.object_id
                if source.kind == "npc"
                else None
            ),
            item=resolved.target.item,
        )
        return replace(resolved, navigation_target=target, navigation_reason=None), sources
    reason = (
        f"collect item has {len(sources)} remaining ROM sources"
        if sources
        else "no remaining ROM source for collect item"
    )
    return replace(resolved, navigation_target=None, navigation_reason=reason), sources


__all__ = ["resolve_collect_navigation"]
