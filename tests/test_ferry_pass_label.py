"""Ferry edges name the pass item a Seagallop guard needs (synthetic graph, no ROM)."""

from pokegent.games.gen3.firered.rom.script_effects import Guard
from pokegent.navigation.graph import FerryRoute, NavigationGraph
from pokegent.navigation.models import CapabilityState, NavigationNode


def _graph(item_names):
    graph = NavigationGraph.__new__(NavigationGraph)
    graph._item_names = item_names
    return graph


def _route(item_id):
    return FerryRoute(
        NavigationNode(3, 1, 5, 5),
        NavigationNode(1, 2, 3, 3),
        (Guard("item", item_id, "ge", 1),),
        selector=1,
        local_id=4,
    )


def test_ferry_edge_names_the_pass_item_it_needs():
    edge = _graph({372: "Tri-Pass"})._ferry_edge(_route(372), CapabilityState())
    assert edge is not None
    assert [(r.label, r.met) for r in edge.requirements] == [("Tri-Pass", False)]
    assert edge.requirements[0].key == "item:372"


def test_ferry_edge_keeps_the_id_label_for_an_unnamed_item():
    edge = _graph({})._ferry_edge(_route(372), CapabilityState())
    assert edge is not None
    assert edge.requirements[0].label == "item 372"
