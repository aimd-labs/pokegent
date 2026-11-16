from types import SimpleNamespace

from pokegent.actions.field.macros import party_item_use_verified


def snapshot(*, hp: int, quantity: int, status: int = 0):
    mon = SimpleNamespace(slot=0, hp=hp, status=status, species=131)
    item = SimpleNamespace(item_id=20, quantity=quantity)
    pocket = SimpleNamespace(items=(item,))
    return SimpleNamespace(party=(mon,), bag=(pocket,))


def test_party_item_use_requires_effect_and_consumption():
    before = snapshot(hp=46, quantity=1)
    assert party_item_use_verified(before, snapshot(hp=260, quantity=0), 20, 0, "hp")
    assert not party_item_use_verified(before, snapshot(hp=46, quantity=0), 20, 0, "hp")
    assert not party_item_use_verified(before, snapshot(hp=260, quantity=1), 20, 0, "hp")


def test_party_item_use_verifies_status_effects():
    before = snapshot(hp=260, quantity=1)
    assert party_item_use_verified(before, snapshot(hp=260, quantity=0, status=1), 20, 0, "status")
    assert not party_item_use_verified(before, snapshot(hp=260, quantity=0), 20, 0, "status")
