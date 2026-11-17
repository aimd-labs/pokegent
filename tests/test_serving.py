import json

import pytest

from pokegent.games.gen3.firered.maps import map_names
from pokegent.goals.models import MapArrival, Objective, Target
from pokegent.goals.validate import ResolvedObjective, ResolvedProbe, ResolvedTarget, _name_index
from pokegent.navigation.adapter import NavigationAdapter
from pokegent.navigation.models import (
    NavigationNode,
    NavigationPlan,
    NavigationTarget,
)
from pokegent.navigation.serving import serves_goal
from pokegent.options.models import Legality, Option


@pytest.fixture(autouse=True)
def synthetic_map_index(tmp_path, monkeypatch):
    """These two invented rooms exercise routing without game-derived map labels."""
    path = tmp_path / "demo-index.json"
    path.write_text(json.dumps({
        "group_order": ["Demo"], "groups": {"Demo": ["DemoArena", "DemoExit"]}
    }))
    monkeypatch.setenv("POKEGENT_MAP_GROUPS", str(path))
    _name_index.cache_clear()
    yield
    _name_index.cache_clear()
    map_names._load.cache_clear()


def _objective() -> Objective:
    return Objective(
        objective_id="fight_demo",
        revision=1,
        kind="fight",
        target=Target(map="DemoArena", npc=1),
        success_probes=(MapArrival(map="DemoExit"),),
    )


def test_defeated_fight_target_is_not_a_route_step() -> None:
    objective = _objective()
    facts = {
        "target": {
            "kind": "npc",
            "map": (0, 0),
            "local_id": 1,
        },
        "trainer_defeated": True,
    }

    assert serves_goal(facts, objective) is False


def test_warp_to_unmet_map_arrival_probe_is_the_route_step() -> None:
    objective = _objective()
    facts = {
        "target": {
            "kind": "warp",
            "map": (0, 0),
            "dest": (0, 1),
            "index": 1,
        },
    }

    assert serves_goal(facts, objective) is True


def test_adapter_routes_defeated_fight_to_probe_warp() -> None:
    objective = _objective()
    resolved = ResolvedObjective(
        objective=objective,
        target=ResolvedTarget(group=0, number=0, npc=1),
        probes=(ResolvedProbe("map_arrival", map_key=(0, 1)),),
    )
    npc = Option(
        id="talk:1",
        label="Talk to a person",
        legality=Legality.LEGAL,
        executor_ref="field.talk_to",
        facts={
            "target": {"kind": "npc", "map": (0, 0), "local_id": 1},
            "trainer_defeated": True,
        },
    )
    warp = Option(
        id="warp:0:0:1",
        label="Go through the door",
        legality=Legality.LEGAL,
        executor_ref="field.warp",
        facts={"target": {"kind": "warp", "dest": (0, 1), "index": 1}},
    )
    plan = NavigationPlan(
        start=NavigationNode(0, 0, 10, 10),
        target=NavigationTarget(0, 0, local_id=1),
        cost=0,
    )

    assert NavigationAdapter._current_map_step([npc, warp], plan, resolved) is warp
