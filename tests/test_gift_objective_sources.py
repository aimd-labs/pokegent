"""Gift and key-item sources resolve through resolve_collect_navigation (#413).

Seam: SimpleNamespace world stub returning the real PokemonGiftSource/CollectSource rows.
"""

from types import SimpleNamespace

import pytest

from pokegent.games.gen3.firered.maps.world import CollectSource, PokemonGiftSource
from pokegent.goals.models import Objective
from pokegent.goals.validate import ResolvedObjective, ResolvedTarget
from pokegent.navigation.objectives import resolve_collect_navigation

LAPRAS, POKE_FLUTE = 131, 350


def _gift(map_key=(1, 10), obj=3, status="available", gate_flags=()) -> PokemonGiftSource:
    return PokemonGiftSource(map_key, "Gift Map", obj, LAPRAS, "LAPRAS", 25, 0x20,
                             gate_flags, (), (), None, None, False, status)


def _resolve(kind, target, **world):
    objective = Objective(objective_id="gift", revision=1, kind=kind)
    resolved = ResolvedObjective(objective=objective, target=target, probes=())
    return resolve_collect_navigation(SimpleNamespace(), resolved, SimpleNamespace(**world))[0]


def _pokemon(sources):
    return _resolve("collect", ResolvedTarget(species=LAPRAS),
                    pokemon_gift_sources=lambda snapshot, species: tuple(sources))


@pytest.mark.xfail(strict=True, reason="defect: the wild branch returns before the gift scan")
def test_gift_only_species_reaches_gift_scan_when_world_has_wild_table() -> None:
    resolved = _resolve("collect", ResolvedTarget(species=LAPRAS),
                        wild_encounter_sources=lambda species: (),
                        tables=SimpleNamespace(species_name=lambda s: "LAPRAS"),
                        pokemon_gift_sources=lambda snapshot, species: (_gift(),))
    assert resolved.navigation_target is not None


def test_single_gift_source_resolves_to_its_npc() -> None:
    target = _pokemon([_gift()]).navigation_target
    assert (target.map_key, target.npc, target.species) == ((1, 10), 3, LAPRAS)


def test_two_gift_sources_of_one_species_are_not_picked() -> None:
    resolved = _pokemon([_gift(obj=3), _gift(obj=4)])
    assert resolved.navigation_target is None
    assert resolved.navigation_reason == "Pokemon gift has 2 routable ROM targets"


def test_gated_gift_source_is_not_offered_as_ready() -> None:
    resolved = _pokemon([_gift(status="locked", gate_flags=(0x801,))])
    assert resolved.navigation_target is None
    assert resolved.navigation_reason == "Pokemon gift has 0 routable ROM targets"


def test_gated_source_does_not_hide_the_ready_twin() -> None:
    assert _pokemon([_gift(obj=3), _gift(obj=4, status="locked")]).navigation_target.npc == 3


def test_additem_key_item_source_resolves_through_collect_sources() -> None:
    source = CollectSource((3, 20), "Flute Town", 7, "npc", 4, 5, POKE_FLUTE, 0x30, "available")
    resolved = _resolve("collect", ResolvedTarget(item=POKE_FLUTE),
                        collect_sources=lambda snapshot, item: (source,))
    target = resolved.navigation_target
    assert (target.map_key, target.npc, target.item) == ((3, 20), 7, POKE_FLUTE)
