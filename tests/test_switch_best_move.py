"""Synthetic (no ROM) switch-option best_move fact and its rendering."""

from types import SimpleNamespace as NS

from pokegent.games.gen3.mechanics import facts as F
from pokegent.games.gen3.mechanics.views import BattlerView, FieldView, MoveSlotView
from pokegent.options.render import render_facts

# move_id -> (name, power, effectiveness, damage_model)
_MOVES = {
    1: ("SURF", 95, 2.0, "formula"),
    2: ("TACKLE", 35, 1.0, "formula"),
    3: ("WATER GUN", 40, 2.0, "formula"),
    4: ("GROWL", 0, 1.0, "none"),
}


class _Tables:
    def species_name(self, _species):
        return "SQUIRTLE"

    def species(self, _species):
        return NS(types=(11, 11))

    def move(self, move_id):
        return NS(name=_MOVES[move_id][0], power=_MOVES[move_id][1])


def _stub_estimate(_att, _def, move_id, _tables, _field):
    _, power, eff, model = _MOVES[move_id]
    return NS(effectiveness=eff, damage_model=model, power=power)


def _facts(monkeypatch, moves, *, hp=20, opponent=True, egg=False):
    monkeypatch.setattr(F, "estimate_damage", _stub_estimate)
    cand = BattlerView(
        species=7, level=5, hp=hp, max_hp=20, is_egg=egg,
        moves=tuple(MoveSlotView(move_id=m) for m in moves),
    )  # fmt: skip
    foe = BattlerView(species=4, level=5, hp=20, max_hp=20, is_player_side=False)
    active = BattlerView(species=1, level=5, hp=20, max_hp=20, position=0)
    return F.switch_option_facts(
        active, cand, foe if opponent else None, _Tables(), FieldView(), forced=True
    )


def test_best_move_picks_highest_effectiveness_then_power(monkeypatch):
    f = _facts(monkeypatch, [2, 3, 1, 4])
    assert f["best_move"] == {"name": "Surf", "effectiveness": 2.0}


def test_best_move_absent_without_moves_opponent_or_for_fainted_and_egg(monkeypatch):
    assert "best_move" not in _facts(monkeypatch, [])
    assert "best_move" not in _facts(monkeypatch, [1], opponent=False)
    assert "best_move" not in _facts(monkeypatch, [1], hp=0)
    assert "best_move" not in _facts(monkeypatch, [1], egg=True)
    assert "best_move" not in _facts(monkeypatch, [4])  # no damaging move


def test_render_switch_best_move_phrase():
    text = render_facts(
        {"species": "Squirtle", "best_move": {"name": "Surf", "effectiveness": 4.0}}
    )
    assert "best move Surf: super effective (x4)" in text
    neutral = render_facts({"species": "S", "best_move": {"name": "Tackle", "effectiveness": 1.0}})
    assert "best move Tackle: neutral" in neutral
