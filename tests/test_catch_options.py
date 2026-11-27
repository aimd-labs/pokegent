"""#411: catch options expose ball facts for wild battles only."""

from types import SimpleNamespace as NS

from pokegent.actions.battle.builder import BattleBuilder
from pokegent.actions.battle.lookups import Lookups

BALLS = {9: 5, 4: 3}  # poke ball, great ball
DEX = 25


def _tables(species):
    return Lookups(
        NS(
            species_national_dex={species: DEX},
            species={},
            species_types={},
            species_gender_ratio={},
            species_base_stats={},
            abilities={},
            moves={},
            items={},
        )
    )


def _view(kind, is_trainer, species, owned, seen):
    return NS(
        kind=kind,
        is_trainer=is_trainer,
        weather=(),
        bag={**BALLS, 20: 3},  # 20 = potion, not a ball
        bag_pocket={9: "poke_balls", 4: "poke_balls", 20: "potions"},
        opponent=NS(name="Synthetic", species=species, level=5, hp_percent=50, status=()),
        block=NS(active=True, lookups=NS(items={})),
        party=(),
        tables=_tables(species),
    )


def _frame(monkeypatch, view, snapshot):
    builder = BattleBuilder()
    for method in ("_move_options", "_switch_options", "_item_options"):
        monkeypatch.setattr(builder, method, lambda *a, **kw: [])
    monkeypatch.setattr(builder, "_escape_option", lambda *a: None)
    monkeypatch.setattr(builder, "_context", lambda *a: {})
    monkeypatch.setattr(builder, "_fp", lambda *a: "synthetic")
    ctx = NS(snapshot=snapshot, objective=None, objective_kind=None,
             scene=NS(scene_id="battle.action"))
    return builder._action(ctx, view, "")


def test_wild_battle_offers_ball_per_bag_ball_with_new_facts(monkeypatch):
    snap = NS(pokedex=NS(owned=(), seen=()))
    view = _view("wild", False, 1, (), ())
    frame = _frame(monkeypatch, view, snap)
    balls = [o for o in frame.options if o.id.startswith("ball:")]
    assert [o.id for o in balls] == [f"ball:{i}" for i in sorted(BALLS)]
    assert balls[0].facts["foe_new_to_pokedex"] is True
    assert balls[0].facts["foe_seen_in_pokedex"] is False


def test_wild_battle_owned_species_marks_ball_seen_and_not_new(monkeypatch):
    snap = NS(pokedex=NS(owned=(DEX,), seen=(DEX,)))
    view = _view("wild", False, 1, (DEX,), (DEX,))
    ball = next(o for o in _frame(monkeypatch, view, snap).options
                if o.id.startswith("ball:"))
    assert ball.facts["foe_new_to_pokedex"] is False
    assert ball.facts["foe_seen_in_pokedex"] is True


def test_trainer_battle_offers_no_ball_option(monkeypatch):
    snap = NS(pokedex=NS(owned=(), seen=()))
    frame = _frame(monkeypatch, _view("trainer", True, 1, (), ()), snap)
    assert not any(o.id.startswith("ball:") for o in frame.options)
