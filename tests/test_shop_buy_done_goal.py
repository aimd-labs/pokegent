from types import SimpleNamespace

from pokegent.actions.menu.shop.builder import ShopBuilder


def snapshot(owned):
    pocket = SimpleNamespace(
        pocket="items", items=(SimpleNamespace(item_id=13, quantity=owned),) if owned else ()
    )
    return SimpleNamespace(
        player=SimpleNamespace(map_group=3, map_num=4, money=900),
        bag=(pocket,),
        ui=None,
        usable=True,
    )


def done(owned, **kwargs):
    return ShopBuilder(None).buy_done(snapshot(owned), **kwargs)


def option(draft, option_id):
    return next(o for o in draft.options if o.id == option_id)


def test_buy_done_reports_goal_progress_and_points_to_quit_when_met():
    met = done(5, objective_item=13, objective_quantity=5)
    assert met.context["objective_goal_met"] is True
    assert option(met, "quit").facts["shop_serves_goal"] is True
    assert "goal met" in met.context["summary"]

    short = done(2, objective_item=13, objective_quantity=5)
    assert short.context["objective_goal_met"] is False
    assert short.context["objective_remaining"] == 3
    assert option(short, "continue_shopping").facts["shop_serves_goal"] is True
    assert "3 more" in short.context["summary"]


def test_buy_done_without_objective_is_unchanged():
    plain = done(2)
    assert "objective_goal_met" not in plain.context
    assert option(plain, "quit").facts == {}
