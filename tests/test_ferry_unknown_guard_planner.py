"""Planner-level proof: a ferry whose guard cannot be evaluated is never a usable route."""

from dataclasses import replace
from types import SimpleNamespace

from pokegent.games.gen3.firered.rom.script_effects import Guard
from pokegent.navigation.adapter import NavigationAdapter
from pokegent.navigation.capabilities import CapabilityState
from pokegent.navigation.graph import FerryRoute, NavigationGraph
from pokegent.navigation.models import NavigationEdge, NavigationNode, NavigationTarget
from pokegent.navigation.planner import NavigationPlanner

START = NavigationNode(1, 2, 3, 4, 0)
DEST = NavigationNode(5, 6, 7, 8, 0)
UNKNOWN = Guard("flag", 0x123, "eq", True)
KNOWN = Guard("flag", 0x456, "eq", True)


class _FerryGraph:
    """Real ferry-edge construction behind the planner's graph surface."""

    def __init__(self, guards, extra=()):
        self._route = FerryRoute(START, DEST, tuple(guards), 1)
        self._extra = extra
        self.maps = {}

    def edges_from(self, node, capabilities, *, live=None, include_unmet=False):
        edge = NavigationGraph.__new__(NavigationGraph)._ferry_edge(self._route, capabilities)
        edges = [edge] if edge is not None and node == START else []
        edges += [e for e in self._extra if e.source == node]
        return tuple(e for e in edges if include_unmet or e.available)

    state = lambda self, _key: None  # noqa: E731
    stateful_flags_toward = lambda self, *_args: frozenset()  # noqa: E731


def _plan(graph, capabilities):
    return NavigationPlanner(graph).plan(START, NavigationTarget(5, 6), capabilities)


def test_only_path_through_unknown_ferry_guard_is_unreachable_and_named():
    plan = _plan(_FerryGraph([UNKNOWN]), CapabilityState(flags_known=False))

    assert not plan.reachable
    assert plan.unmet == ("unknown requirement (flag 0x123)",)


def test_parallel_proven_path_beats_the_unknown_ferry():
    walk = NavigationEdge("walk", START, DEST, "walk", 50)
    plan = _plan(_FerryGraph([UNKNOWN], (walk,)), CapabilityState(flags_known=False))

    assert plan.reachable and [edge.stable_key for edge in plan.route] == ["walk"]


def test_menu_never_marks_an_unmet_ferry_as_the_planned_destination():
    plan = _plan(_FerryGraph([UNKNOWN]), CapabilityState(flags_known=False))
    menu = (("menu_id", 1), ("menu_pc", 9), ("menu_index", 0))
    edge = replace(plan.route[0], executor_target=plan.route[0].executor_target + menu)
    plan = replace(plan, route=(edge,))
    snap = SimpleNamespace(player=SimpleNamespace(map_group=1, map_num=2),
                           ui=SimpleNamespace(script=SimpleNamespace(script_ptr=9)))
    fake, rows = SimpleNamespace(plan=lambda *_args: plan), (("a", "A"),)
    assert NavigationAdapter.ferry_menu_entries(fake, snap, None, 1, rows, menu_id=1) == rows


def test_proven_ferry_is_still_planned():
    plan = _plan(_FerryGraph([KNOWN]), CapabilityState(flags=frozenset({0x456})))

    assert plan.reachable and plan.route[0].kind == "ferry"
    assert plan.unmet == ()
