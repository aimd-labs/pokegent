from types import SimpleNamespace

from pokegent.actions.field.builder import (
    _clerk_goal_facts,
    _shop_clerk_local_id,
    _shop_target_is_local,
)
from pokegent.goals.models import Objective, Target
from pokegent.goals.validate import ResolvedObjective, ResolvedTarget
from pokegent.navigation.serving import serves_goal
from pokegent.options.models import fingerprint
from pokegent.options.registry import BuildContext, BuilderRegistry, FrameDraft


def test_shop_clerk_matches_named_item_using_resolved_rom_id() -> None:
    objective = Objective(
        objective_id="buy_hyper_potion",
        revision=1,
        kind="shop",
        target=Target(item="Hyper Potion", quantity=2, map="IndigoPlateau_PokemonCenter_1F"),
    )
    resolved = ResolvedObjective(
        objective=objective,
        target=ResolvedTarget(group=23, number=1, item=42),
        probes=(),
    )

    facts = {
        "target": {"kind": "npc", "map": [23, 1], "local_id": 1},
        "role": "clerk",
        "mart_inventory": [{"item_id": 42, "name": "Hyper Potion"}],
    }

    assert serves_goal(facts, resolved) is True


def test_shop_unmet_reason_survives_builder_registry_context() -> None:
    class DraftBuilder:
        def build(self, _ctx):
            return FrameDraft(
                options=(),
                state_fingerprint=fingerprint("field.free"),
                context={
                    "objective": "Buy map IndigoPlateau_PokemonCenter_1F, item Hyper Potion x1",
                    "objective_unmet_reason": (
                        "Hyper Potion is not sold on IndigoPlateau_PokemonCenter_1F "
                        "(stock: Ultra Ball, Max Potion)"
                    ),
                },
            )

    registry = BuilderRegistry()
    registry.register_scenes(("field.free",), DraftBuilder())
    frame = registry.build(
        BuildContext(
            snapshot=SimpleNamespace(run_id="test", snapshot_seq=1),
            scene=SimpleNamespace(scene_id="field.free", confidence=1.0),
            objective="Buy map IndigoPlateau_PokemonCenter_1F, item Hyper Potion x1",
        ),
        "decision-1",
    )

    assert frame is not None
    assert "unmet: Hyper Potion is not sold on IndigoPlateau_PokemonCenter_1F" in frame.context[
        "objective"
    ]


def test_shop_target_map_gate_excludes_remote_clerk_route_step() -> None:
    objective = Objective(
        objective_id="buy_max_potion",
        revision=1,
        kind="shop",
        target=Target(item=20, map="CeladonCity_Mart"),
    )
    resolved = ResolvedObjective(
        objective=objective,
        target=ResolvedTarget(group=3, number=5, item=20),
        probes=(),
    )

    assert _shop_target_is_local(resolved, (3, 5)) is True
    assert _shop_target_is_local(resolved, (13, 0)) is False


def test_shop_clerk_selection_uses_matching_mart_inventory() -> None:
    state = SimpleNamespace(
        objects=[
            SimpleNamespace(local_id=1, graphics_id=0x44),
            SimpleNamespace(local_id=2, graphics_id=0x44),
        ]
    )
    view = SimpleNamespace(
        marts=(
            (1, (SimpleNamespace(item_id=19),)),
            (2, (SimpleNamespace(item_id=20),)),
        )
    )

    assert _shop_clerk_local_id(state, view, 20) == 2


def _clerk_snapshot(money, owned):
    items = [SimpleNamespace(item_id=20, quantity=owned)] if owned else []
    return SimpleNamespace(bag=[SimpleNamespace(items=items)], player=SimpleNamespace(money=money))


def test_clerk_goal_facts_price_and_affordability() -> None:
    rows = [{"item_id": 20, "name": "Max Potion", "price": 2500}]
    got = _clerk_goal_facts(rows, 0, 3, _clerk_snapshot(5000, 1))
    assert got == {
        "goal_item_price": 2500,
        "goal_item_remaining": 2,
        "money_sufficient": True,
    }
    short = _clerk_goal_facts(rows, 0, 3, _clerk_snapshot(4999, 1))
    assert short["money_sufficient"] is False
    done = _clerk_goal_facts(rows, 0, 1, _clerk_snapshot(0, 1))
    assert done["goal_item_remaining"] == 0 and done["money_sufficient"] is True


def test_clerk_goal_facts_unknown_stays_absent() -> None:
    assert _clerk_goal_facts([{"item_id": 20, "price": None}], 0, 1, _clerk_snapshot(1, 0)) == {}
    rows = [{"item_id": 20, "price": 100}]
    assert _clerk_goal_facts(rows, 0, 2, SimpleNamespace(bag=[], player=None)) == {
        "goal_item_price": 100
    }
