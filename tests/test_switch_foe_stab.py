"""Synthetic (no ROM) switch-option foe_stab_vs_candidate fact and its rendering."""

from types import SimpleNamespace as NS

from pokegent.games.gen3.mechanics import facts as F
from pokegent.games.gen3.mechanics.views import BattlerView, FieldView
from pokegent.options.render import render_facts

_SCRIPT = {0: 1.0, 10: 2.0}  # foe move type (Normal, Fire) -> multiplier vs the candidate


class _Tables:
    type_chart = NS(multiplier=lambda mtype, _a, _b, **_k: _SCRIPT[mtype])

    def species_name(self, _species):
        return "SQUIRTLE"

    def species(self, species):
        return NS(types={7: (11, 11), 4: (0, 10)}[species])


def _facts(*, hp=20, opponent=True, egg=False, foe=4):
    cand = BattlerView(species=7, level=5, hp=hp, max_hp=20, is_egg=egg)
    foe_view = BattlerView(species=foe, level=5, hp=20, max_hp=20, is_player_side=False)
    active = BattlerView(species=1, level=5, hp=20, max_hp=20, position=0)
    return F.switch_option_facts(
        active, cand, foe_view if opponent else None, _Tables(), FieldView(), forced=True
    )


def test_worst_foe_type_against_candidate():
    assert _facts()["foe_stab_vs_candidate"] == {"effectiveness": 2.0, "foe_type": "Fire"}


def test_foe_stab_absent_when_unknown_or_ineligible():
    unreadable_foe = {"foe": 99}  # species table lookup raises KeyError: unknown, not a crash
    for kw in ({"opponent": False}, {"hp": 0}, {"egg": True}, {"foe": None}, unreadable_foe):
        assert "foe_stab_vs_candidate" not in _facts(**kw)


def test_render_foe_stab_phrase():
    fact = {"effectiveness": 2.0, "foe_type": "Fire"}
    text = render_facts({"species": "S", "foe_stab_vs_candidate": fact})
    assert "the foe's Fire type hits it x2" in text
