from pokegent.goals.shop_feasibility import (
    Access,
    Feasibility,
    InventoryCoverage,
    ShopFacts,
    ShopRequest,
    assess_shop,
)


def facts(**changes):
    values = dict(
        map_key="PewterMart",
        coverage=InventoryCoverage.COMPLETE,
        item_ids=frozenset({1}),
        money=100,
        access=Access.ALLOWED,
        item_price=10,
    )
    values.update(changes)
    return ShopFacts(**values)


def test_supported_positive_case_is_satisfiable():
    assert (
        assess_shop(ShopRequest("PewterMart", 1, 2, owned_quantity=0), facts()).verdict
        == Feasibility.SATISFIABLE
    )


def test_already_owned_quantity_satisfies_without_purchase_event():
    assert (
        assess_shop(
            ShopRequest("PewterMart", 1, 2, owned_quantity=2), facts(item_ids=frozenset())
        ).verdict
        == Feasibility.SATISFIABLE
    )


def test_proven_absence_is_unsatisfiable_only_for_bound_map():
    req = ShopRequest("PewterMart", 1, 1, owned_quantity=0)
    assert assess_shop(req, facts(item_ids=frozenset())).verdict == Feasibility.UNSATISFIABLE
    assert (
        assess_shop(req, facts(map_key="ViridianMart", item_ids=frozenset())).verdict
        == Feasibility.UNKNOWN
    )
    assert (
        assess_shop(ShopRequest(None, 1, 1, owned_quantity=0), facts(item_ids=frozenset())).verdict
        == Feasibility.UNKNOWN
    )


def test_incomplete_inventory_and_unknown_bag_stay_unknown():
    req = ShopRequest("PewterMart", 1, 2, owned_quantity=0)
    assert (
        assess_shop(req, facts(coverage=InventoryCoverage.PARTIAL)).verdict == Feasibility.UNKNOWN
    )
    assert (
        assess_shop(req, facts(coverage=InventoryCoverage.UNKNOWN)).verdict == Feasibility.UNKNOWN
    )
    assert (
        assess_shop(ShopRequest("PewterMart", 1, 2, owned_quantity=None), facts()).verdict
        == Feasibility.UNKNOWN
    )


def test_known_missing_money_or_access_is_blocked_when_item_is_stocked():
    req = ShopRequest("PewterMart", 1, 1, owned_quantity=0)
    assert assess_shop(req, facts(money=0)).verdict == Feasibility.BLOCKED
    assert assess_shop(req, facts(access=Access.DENIED)).verdict == Feasibility.BLOCKED
    assert (
        assess_shop(req, facts(money=None, item_price=None, access=Access.UNKNOWN)).verdict
        == Feasibility.UNKNOWN
    )


def test_malformed_access_never_proves_sat():
    req = ShopRequest("PewterMart", 1, 1, owned_quantity=0)
    for access in (None, "allowed", object()):
        assert assess_shop(req, facts(access=access)).verdict == Feasibility.UNKNOWN


def test_known_money_blocker_precedes_unknown_access():
    req = ShopRequest("PewterMart", 1, 2, owned_quantity=0)
    assert assess_shop(req, facts(money=0, access=Access.UNKNOWN)).verdict == Feasibility.BLOCKED


def test_invalid_quantities_and_money_facts_stay_unknown():
    for quantity, owned in ((0, 0), (-1, 0), (True, 0), (1, -1), (1, True)):
        req = ShopRequest("PewterMart", 1, quantity, owned_quantity=owned)
        assert assess_shop(req, facts()).verdict == Feasibility.UNKNOWN
    for money, price in ((-1, 1), (float("nan"), 1), (1, float("inf")), (True, 1)):
        req = ShopRequest("PewterMart", 1, 1, owned_quantity=0)
        assert assess_shop(req, facts(money=money, item_price=price)).verdict == Feasibility.UNKNOWN
