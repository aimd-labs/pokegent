from test_field_start_menu_entry import _snap
from test_party_swap_species_labels import _members

from pokegent.actions.field import builder as B
from pokegent.games.gen3.firered.reader.schemas import BagItem, BagPocket, MonMove
from pokegent.options.registry import BuildContext
from pokegent.state.scenes import SceneResult


def _tables(amounts):
    from types import SimpleNamespace as NS

    def item(item_id):
        if item_id not in amounts:
            return NS(effect=None)
        full = amounts[item_id] == "full"
        return NS(effect=NS(heal_hp=None if full else amounts[item_id], heal_hp_full=full))

    return NS(
        item=item,
        species_name=lambda s: f"s{s}",
        move_data=lambda m: NS(power=0 if m == 45 else 40),  # 45 is a status move
    )


def _ctx(hps, items=(), egg=None, pocket_name="items", tables=None, pps=None):
    fields = [
        {"hp": h, "max_hp": 100 if h >= 0 else 0, "is_egg": i == egg, "status": 0}
        for i, h in enumerate(hps)
    ]
    for f, pp in zip(fields, pps or ()):
        f["moves"] = (
            MonMove(move_id=33, pp=pp),
            MonMove(move_id=45, pp=5),
            MonMove(move_id=0, pp=0),
        )
    party = tuple(type(m)(**{**vars(m), **f}) for m, f in zip(_members((25, 1, 6)), fields))
    pocket = BagPocket(
        pocket=pocket_name, capacity=42, items=tuple(BagItem(item_id=i, quantity=1) for i in items)
    )
    snap = _snap(members=3).model_copy(update={"party": party, "bag": (pocket,)})
    ctx = BuildContext(snapshot=snap, scene=SceneResult(scene_id="field.free", confidence=1))
    return B.FieldBuilder(field_menu=True, tables=tables).build(ctx)


def _advice(*a, **k):
    return _ctx(*a, **k).context.get("heal_advice")


def test_members_without_readable_hp_are_skipped_not_raised():
    from types import SimpleNamespace as NS

    from pokegent.actions.field.league import heal_advice

    assert heal_advice(NS(party=(NS(species=25, level=5),), bag=())) is None
    assert heal_advice(NS(party=(NS(hp=5, max_hp=100),))) is not None  # no bag: stock unknown = 0


def test_center_when_low_or_fainted_and_not_covered():
    fact = _advice((30, 90, 80))
    assert fact["party_hp_pct"] == {"lowest": 30, "average": 66}
    assert (fact["suggest"], fact["heal_items"], fact["revives"]) == ("heal_at_center", 0, 0)
    assert _advice((0, 90, 80), (13,))["suggest"] == "heal_at_center"


def test_berries_pocket_oran_and_sitrus_count_as_heal_stock():
    for berry in (139, 142):
        fact = _advice((30, 90, 80), (berry,), pocket_name="berries")
        assert (fact["suggest"], fact["heal_items"]) == ("use_items", 1)


def test_items_ok_absent_unreadable_skipped_options_unchanged():
    assert _advice((0, 90, 80), (13, 24))["suggest"] == "use_items"
    assert _advice((55, 58, 59), (13,))["suggest"] == "use_items"
    assert _advice((90, 80, 100)) is None is _advice((-1, 90, 100)) is _advice((90, 90, 5), egg=2)
    assert [o.id for o in _ctx((30, 90, 80)).options] == [o.id for o in _ctx((90,) * 3).options]


def test_items_hp_and_missing_hp_with_tables():
    fact = _advice((30, 90, 80), (13, 13), tables=_tables({13: 20}))
    assert (fact["items_hp"], fact["missing_hp"]) == (40, 100)
    assert fact["suggest"] == "heal_at_center"  # 40 < 100 and lowest member is under 40%
    assert (
        _advice((30, 90, 80), (13, 13, 13, 13, 13, 13), tables=_tables({13: 20}))["items_hp"] == 100
    )
    assert _advice((30, 90, 80), (19,), tables=_tables({19: "full"}))["items_hp"] == 100
    fact = _advice((55, 58, 59), (13,), tables=_tables({13: 20}))
    assert (fact["suggest"], fact["items_hp"], fact["missing_hp"]) == ("use_items", 20, 128)


def test_hp_amounts_omitted_without_tables_or_readable_effect():
    for tables in (None, _tables({})):
        fact = _advice((30, 90, 80), (13, 13), tables=tables)
        assert "items_hp" not in fact and "missing_hp" not in fact
    assert _advice((55, 58, 59), (13,), tables=_tables({}))["suggest"] == "use_items"


def test_pp_exhausted_counts_members_whose_damaging_moves_are_all_empty():
    t = _tables({})
    fact = _advice((90, 90, 90), tables=t, pps=(5, 0, 5))  # status move PP is ignored
    assert fact["pp_exhausted"] == {"members_out_of_pp": 1, "lead_out_of_pp": False}
    assert "suggest" not in fact  # one of three: below half, lead fine
    assert _advice((90, 90, 90), tables=t, pps=(0, 5, 5))["suggest"] == "heal_at_center"
    assert _advice((90, 90, 90), tables=t, pps=(5, 0, 0))["suggest"] == "heal_at_center"  # 2 of 3
    assert _advice((90, 90, 90), tables=t, pps=(5, 5, 5)) is None


def test_pp_unreadable_or_without_tables_keeps_todays_advice():
    assert _advice((90, 90, 90), pps=(0, 0, 0)) is None
    bare = _tables({}).__class__(species_name=str)  # tables without move_data
    assert _advice((90, 90, 90), tables=bare, pps=(0, 0, 0)) is None
    assert "pp_exhausted" not in _advice((30, 90, 80), tables=_tables({}), pps=(5, 5, 5))
