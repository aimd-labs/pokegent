"""Static legendary objectives resolve to an explicit navigation target (#413)."""

from types import SimpleNamespace

from pokegent.goals.models import Objective
from pokegent.goals.validate import ResolvedObjective, ResolvedTarget
from pokegent.navigation.capabilities import from_snapshot
from pokegent.navigation.objectives import resolve_collect_navigation
from pokegent.navigation.regions import RegionGraph


def _resolved(species: int) -> ResolvedObjective:
    objective = Objective(objective_id="static_legendary", revision=1, kind="dex")
    return ResolvedObjective(
        objective=objective, target=ResolvedTarget(species=species), probes=()
    )


def _world() -> SimpleNamespace:
    return SimpleNamespace(wild_encounter_sources=lambda species: (),
                           tables=SimpleNamespace(species_name=lambda s: "LEGENDARY"))


def test_zapdos_static_objective_resolves_to_power_plant_target() -> None:
    resolved, _sources = resolve_collect_navigation(SimpleNamespace(), _resolved(145), _world())
    assert resolved.navigation_target is not None
    assert resolved.navigation_target.map_key == (1, 95)
    assert resolved.navigation_target.map_name == "Power Plant"


def test_articuno_static_objective_resolves_to_seafoam_islands_b4f() -> None:
    resolved, _sources = resolve_collect_navigation(SimpleNamespace(), _resolved(144), _world())
    assert resolved.navigation_target is not None
    assert resolved.navigation_target.map_key == (1, 87)


def _surf_gate_graph() -> RegionGraph:
    return RegionGraph({"maps": {
        "3:44": {"connections": [{"dest": "3:3", "pairs": [{"from": 0, "to": 0}]}]},
        "3:3": {"connections": [{"dest": "3:28", "pairs": [{"from": 0, "to": 0}]}]},
        "3:28": {"trans": [{"from": 0, "to": 1, "needs": ["surf"], "cost": 4, "kind": "surf"}],
                 "connections": [{"dest": "1:95", "pairs": [{"from": 1, "to": 0}]}]},
        "1:95": {}}})


def _snapshot(*, surf: bool) -> SimpleNamespace:
    move = SimpleNamespace(move_id=57 if surf else 33, pp=5)
    mon = SimpleNamespace(species=25, moves=(move,))
    return SimpleNamespace(
        usable=True, player=SimpleNamespace(badges=(5,)), party=(mon,), bag=(),
        flags=None, vars=None, ui=None, map=None,
    )


def test_surf_gate_route25_to_power_plant_not_dropped_with_surf() -> None:
    graph = _surf_gate_graph()
    no_surf = from_snapshot(_snapshot(surf=False))
    assert "surf" not in no_surf.capabilities
    assert graph.route((3, 44, 0), (1, 95), no_surf.capabilities) is None
    assert graph.missing_for((3, 44, 0), (1, 95), no_surf.capabilities) == ("surf",)
    with_surf = from_snapshot(_snapshot(surf=True))
    assert with_surf.has("surf")
    assert graph.route((3, 44, 0), (1, 95), with_surf.capabilities) is not None
    assert graph.missing_for((3, 44, 0), (1, 95), with_surf.capabilities) == ()
