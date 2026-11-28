from types import SimpleNamespace

from pokegent.actions.menu.shop import builder as shop_builder
from pokegent.actions.menu.shop.builder import ShopBuilder

ITEM = SimpleNamespace(item_id=13, name="Potion", price=300)


def snapshot(owned):
    pocket = SimpleNamespace(pocket="items", items=(SimpleNamespace(item_id=13, quantity=owned),))
    player = SimpleNamespace(map_group=3, map_num=4, money=9000)
    return SimpleNamespace(player=player, bag=(pocket,), ui=None, usable=True)


def builder(monkeypatch, quantity):
    monkeypatch.setattr(shop_builder.E, "selected_item", lambda snap: 13)
    monkeypatch.setattr(shop_builder.E, "quantity", lambda snap: quantity)
    return ShopBuilder(SimpleNamespace(tables=SimpleNamespace(item=lambda i: ITEM)))


def test_buy_confirm_reports_goal_progress_only_for_the_objective_item(monkeypatch):
    goal = {"objective_item": 13, "objective_quantity": 5}
    context = builder(monkeypatch, 3).buy_confirm(snapshot(2), **goal).context
    assert (context["objective_owned"], context["objective_target"]) == (2, 5)
    assert context["objective_after"] == 5 and context["objective_goal_met_after"] is True
    short = builder(monkeypatch, 1).buy_confirm(snapshot(2), **goal).context
    assert short["objective_goal_met_after"] is False
    shop = builder(monkeypatch, 3)
    assert "objective_after" not in shop.buy_confirm(snapshot(2)).context
    other = shop.buy_confirm(snapshot(2), objective_item=4, objective_quantity=5)
    assert "objective_after" not in other.context


def test_buy_quantity_flags_quantities_above_the_remaining_count(monkeypatch):
    shop = builder(monkeypatch, 1)
    draft = shop.buy_quantity(snapshot(2), objective_item=13, objective_quantity=5)
    flags = {o.facts["quantity"]: o.facts.get("exceeds_goal") for o in draft.options[:-1]}
    assert flags[3] is None and flags[5] is True
