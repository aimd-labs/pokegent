"""An Egg in the party is reported as an Egg, not as a battle-ready member of its species."""

from types import SimpleNamespace

from pokegent.state.briefing import build_briefing


def _mon(*, is_egg, species=175):
    move = SimpleNamespace(move_id=1, pp=35)
    return SimpleNamespace(
        species=species, level=5, nickname="", moves=[move], hp=20, max_hp=20, status=0,
        held_item=0, is_egg=is_egg,
    )


def _team(*mons):
    snapshot = SimpleNamespace(party=list(mons), player=None, pokedex=None, storage=None, bag=())
    return build_briefing(snapshot, trim=False)["team"]


def test_egg_member_is_marked_and_carries_no_battle_facts():
    egg, normal = _team(_mon(is_egg=True), _mon(is_egg=False, species=25))
    assert egg["egg"] is True
    assert egg["moves"] == []
    assert egg["types"] == []
    assert "hp" not in egg
    assert "egg" not in normal
    assert normal["moves"] and normal["hp"] == [20, 20]


def test_unknown_egg_flag_is_not_treated_as_egg():
    mon = _mon(is_egg=None)
    del mon.is_egg
    (member,) = _team(mon)
    assert "egg" not in member
    assert member["moves"]
