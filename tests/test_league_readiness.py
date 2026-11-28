from test_field_start_menu_entry import _snap
from test_party_swap_species_labels import _members

from pokegent.actions.field import builder as B
from pokegent.games.gen3.firered.reader.schemas import BagItem, BagPocket
from pokegent.options.registry import BuildContext
from pokegent.state.scenes import SceneResult


def _draft(levels, name, items=(), goals=()):
    party = tuple(type(m)(**{**vars(m), "level": v}) for m, v in zip(_members((25, 1, 6)), levels))
    snap = _snap(members=3)
    ident = snap.map.identity.model_copy(update={"name": name})
    bag = (BagPocket(pocket="items", capacity=42, items=items),)
    snap = snap.model_copy(
        update={"party": party, "bag": bag, "map": snap.map.model_copy(update={"identity": ident})}
    )
    scene = SceneResult(scene_id="field.free", confidence=1)
    return B.FieldBuilder(field_menu=True).build(
        BuildContext(snapshot=snap, scene=scene, goal_id=None, objective_levels=goals)
    )


def test_fact_on_league_maps_with_default_and_objective_target():
    fact = _draft((44, 47, 42), "IndigoPlateau_Exterior").context["league_readiness"]
    assert (fact["min_level"], fact["avg_level"], fact["target_level"]) == (42, 44.3, 55)
    assert fact["target_source"] == "default" and len(fact["below_target"]) == 3
    goal = _draft((44, 47, 42), "PokemonLeague_LoreleisRoom", goals=((None, None, 46),))
    assert goal.context["league_readiness"]["target_level"] == 46


def test_stock_counts_absent_elsewhere_and_options_unchanged():
    items = tuple(BagItem(item_id=i, quantity=q) for i, q in ((13, 3), (22, 2), (19, 1), (24, 4)))
    fact = _draft((44, 47, 42), "IndigoPlateau_Exterior", items).context["league_readiness"]
    assert (fact["heal_items"], fact["full_restores"], fact["revives"]) == (6, 1, 4)
    elsewhere = _draft((44, 47, 42), "DemoMap")
    assert "league_readiness" not in elsewhere.context
    league = _draft((44, 47, 42), "IndigoPlateau_Exterior")
    assert [o.id for o in league.options] == [o.id for o in elsewhere.options]
    assert "league_readiness" not in _draft((), "IndigoPlateau_Exterior").context
