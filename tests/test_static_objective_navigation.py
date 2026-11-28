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


def test_moltres_static_objective_resolves_to_mt_ember_summit() -> None:
    resolved, _sources = resolve_collect_navigation(SimpleNamespace(), _resolved(146), _world())
    assert resolved.navigation_target is not None
    assert resolved.navigation_target.map_key == (1, 101)
    assert resolved.navigation_reason is None


def test_mewtwo_static_objective_resolves_to_cerulean_cave_b1f() -> None:
    resolved, _sources = resolve_collect_navigation(SimpleNamespace(), _resolved(150), _world())
    assert resolved.navigation_target is not None
    assert resolved.navigation_target.map_key == (1, 74)
    assert resolved.navigation_reason is None


def test_snorlax_static_objective_resolves_to_route_12() -> None:
    resolved, _sources = resolve_collect_navigation(SimpleNamespace(), _resolved(143), _world())
    assert resolved.navigation_target is not None
    assert resolved.navigation_target.map_key == (3, 30)
    assert resolved.navigation_target.map_name == "Route 12"
    assert resolved.navigation_reason is None


def _snorlax_world(hide_flags: dict[tuple[int, int], int]) -> SimpleNamespace:
    def read_static(group: int, number: int) -> SimpleNamespace:
        flag = hide_flags[(group, number)]
        obj = SimpleNamespace(role="snorlax", hidden_flag_id=flag)
        return SimpleNamespace(usable=True, state=SimpleNamespace(objects=(obj,)))

    world = _world()
    world.map_reader = SimpleNamespace(read_static=read_static)
    return world


def _flags(*set_ids: int) -> SimpleNamespace:
    return SimpleNamespace(flags=SimpleNamespace(is_set=lambda flag: flag in set_ids))


_HIDE = {(3, 30): 0x11, (3, 34): 0x22}


def test_snorlax_goes_to_route_12_while_neither_sleeper_is_gone() -> None:
    resolved, _ = resolve_collect_navigation(_flags(), _resolved(143), _snorlax_world(_HIDE))
    assert resolved.navigation_target.map_key == (3, 30)


def test_snorlax_goes_to_route_16_once_route_12_sleeper_is_gone() -> None:
    resolved, _ = resolve_collect_navigation(_flags(0x11), _resolved(143), _snorlax_world(_HIDE))
    assert resolved.navigation_target.map_key == (3, 34)
    assert resolved.navigation_target.map_name == "Route 16"


def test_snorlax_refuses_when_both_sleepers_are_gone() -> None:
    resolved, _ = resolve_collect_navigation(
        _flags(0x11, 0x22), _resolved(143), _snorlax_world(_HIDE)
    )
    assert resolved.navigation_target is None
    assert "no remaining static source" in resolved.navigation_reason


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
