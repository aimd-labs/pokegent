from types import SimpleNamespace

from pokegent.actions.menu.shop.builder import ShopBuilder
from pokegent.games.gen3.firered.maps.world import ShopOffer
from pokegent.options.models import Legality


class FakeWorld:
    def __init__(self, inventory):
        self.inventory = inventory
        self.tables = SimpleNamespace(item=self.item)

    @staticmethod
    def item(item_id):
        names = {4: "Escape Rope", 35: "TM35"}
        if item_id not in names:
            raise KeyError(item_id)
        return SimpleNamespace(
            item_id=item_id,
            name=names[item_id],
            price=550,
            pocket=1,
            effect=None,
            is_ball=False,
        )

    def shop_inventory(self, snapshot):
        return self.inventory


def snapshot(*, money=10_000):
    return SimpleNamespace(
        player=SimpleNamespace(map_group=3, map_num=4, money=money),
        bag=(),
        ui=None,
        usable=True,
    )


def offer(item_id):
    names = {4: "Escape Rope", 35: "TM35"}
    return ShopOffer(item_id=item_id, name=names[item_id], price=550, owned=0, need=None)


def test_shop_objective_feedback_controls_absent_present_unknown_and_no_objective():
    state = snapshot()

    no_objective = ShopBuilder(FakeWorld((offer(4),))).buy_list(state)
    assert "objective_unmet_reason" not in no_objective.context

    present = ShopBuilder(FakeWorld((offer(35), offer(4)))).buy_list(
        state, objective_item=35
    )
    target = next(option for option in present.options if option.id == "buy:35")
    assert target.facts["shop_serves_goal"] is True
    assert "objective_unmet_reason" not in present.context

    unaffordable = ShopBuilder(FakeWorld((offer(35),))).buy_list(
        snapshot(money=0), objective_item=35
    )
    assert unaffordable.context["objective_unmet_reason"] == (
        "cannot afford TM35: have 0, need 550"
    )

    absent = ShopBuilder(FakeWorld((offer(4),))).buy_list(state, objective_item=35)
    reason = absent.context["objective_unmet_reason"]
    assert "TM35" in reason
    assert "current mart" in reason
    assert "map 3/4" in reason
    unrelated = next(option for option in absent.options if option.id == "buy:4")
    assert unrelated.legality is Legality.LEGAL
    back = next(option for option in absent.options if option.id == "back")
    assert back.legality is Legality.LEGAL

    unknown = ShopBuilder(FakeWorld(None)).buy_list(state, objective_item=35)
    assert "objective_unmet_reason" not in unknown.context
    assert len(unknown.options) == 1
    assert unknown.options[0].legality is Legality.UNKNOWN
    assert unknown.options[0].id == "pause_and_escalate"
