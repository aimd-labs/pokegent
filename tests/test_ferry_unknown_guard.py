import pytest

from pokegent.games.gen3.firered.rom.script_effects import Guard
from pokegent.navigation.capabilities import CapabilityState
from pokegent.navigation.graph import FerryRoute, NavigationGraph
from pokegent.navigation.models import NavigationNode


def _edge(guard: Guard, capabilities: CapabilityState):
    route = FerryRoute(
        source=NavigationNode(1, 2, 3, 4, 0),
        destination=NavigationNode(5, 6, 7, 8, 0),
        guards=(guard,),
        selector=1,
    )
    return NavigationGraph.__new__(NavigationGraph)._ferry_edge(route, capabilities)


@pytest.mark.parametrize(
    ("guard", "capabilities", "label"),
    [
        (Guard("flag", 0x123, "eq", True), CapabilityState(flags_known=False), "flag 0x123"),
        (Guard("var", 0x4050, "eq", 2), CapabilityState(vars_known=False), "var 0x4050"),
        (Guard("var", 0x8004, "eq", 2), CapabilityState(), "var 0x8004"),
        (Guard("item", 13, "ge", 3), CapabilityState(), "item 13"),
    ],
)
def test_unevaluable_ferry_guard_keeps_edge_as_unmet_unknown(guard, capabilities, label):
    edge = _edge(guard, capabilities)

    assert edge is not None
    assert not edge.available
    assert [row.label for row in edge.requirements] == [f"unknown requirement ({label})"]


def test_evaluable_ferry_guard_is_unchanged():
    edge = _edge(Guard("flag", 0x123, "eq", True), CapabilityState(flags=frozenset({0x123})))

    assert edge is not None and edge.available
    assert edge.requirements[0].label == "flag 0x123 set"
