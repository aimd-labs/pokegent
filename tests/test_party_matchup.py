from pokegent.actions.battle import refs as R
from pokegent.actions.battle.builder import BattleBuilder
from pokegent.options.models import Legality, Option


def _opt(slot, species, eff, stab, hp, legal=True, move=True):
    facts = {"species": species, "hp_pct": hp, "foe_stab_vs_candidate": stab}
    if move:
        facts["best_move"] = {"name": "Surf", "effectiveness": eff}
    return Option(
        id=f"switch:{slot}",
        label=f"Switch to {species}",
        description="",
        legality=Legality.LEGAL if legal else Legality.UNAVAILABLE,
        facts=facts,
        executor_ref=R.SWITCH_TO if legal else None,
        postcondition_ref=R.PC_SWITCH if legal else None,
        verb="SWITCH_TO",
    )


def _stab(eff):
    return {"effectiveness": eff, "foe_type": "Fire"}


def test_ranked_by_best_move_then_foe_stab_then_hp_capped_at_three():
    options = [
        _opt(1, "A", 1.0, _stab(1.0), 50),
        _opt(2, "B", 2.0, _stab(2.0), 90),
        _opt(3, "C", 2.0, _stab(0.5), 10),
        _opt(4, "D", 2.0, _stab(0.5), 80),
        _opt(5, "E", 0.5, _stab(1.0), 100),
    ]
    got = BattleBuilder._party_matchup(options)
    assert [m["species"] for m in got] == ["D", "C", "B"]
    assert got[0] == {
        "species": "D",
        "best_move": {"name": "Surf", "effectiveness": 2.0},
        "foe_stab_vs_it": _stab(0.5),
        "hp_pct": 80,
    }


def test_matchup_absent_without_legal_candidate_or_facts():
    assert BattleBuilder._party_matchup([]) is None
    assert BattleBuilder._party_matchup([_opt(1, "A", 2.0, _stab(1.0), 50, legal=False)]) is None
    assert BattleBuilder._party_matchup([_opt(1, "A", 2.0, _stab(1.0), 50, move=False)]) is None
