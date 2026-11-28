"""Synthetic (no ROM) battle move options: provider facts survive _move_options.

The stub provider returns facts shaped like gen3 move_option_facts; the real damage estimate needs
ROM tables, so effectiveness values here are supplied, not computed.
"""

from types import SimpleNamespace as NS

from pokegent.actions.battle.builder import BattleBuilder

_ATTACK = {"type": "ICE", "category": "special", "power": 95, "accuracy": 100, "stab": True}
FACTS = {
    0: {**_ATTACK, "name": "Ice Beam", "effectiveness": 4.0, "max_pp": 16},
    1: {**_ATTACK, "name": "Thunderbolt", "effectiveness": 2.0, "stab": False, "max_pp": 24},
    2: {**_ATTACK, "name": "Hydro Pump", "effectiveness": 0.5, "accuracy": 80, "max_pp": 8},
    3: {"name": "Toxic", "category": "status", "power": 0, "accuracy": 85, "max_pp": 16},
}


def _options(pps=(10, 10, 10, 10), facts=FACTS):
    class Provider:
        def move_facts(self, view, slot):
            return dict(facts[slot])

    off = NS(disabled_move=0, taunt_timer=0, encore_timer=0, encored_move=0, disable_timer=0)
    me = NS(
        side="player",
        moves=[NS(move_id=100 + i, pp=pp) for i, pp in enumerate(pps)],
        disable=off,
        status2=0,
        status3=0,
        last_move=0,
        item=0,
        choiced_move=0,
        battler=0,
    )
    block = NS(battler_at=lambda b: me, player=me, battlers=[me], lookups=NS(items={}))
    view = NS(block=block, player=NS(battler=0), tables=None)
    out = BattleBuilder(facts=Provider())._move_options(view, verb="USE_MOVE", prefix="Use")
    return {o.id: o for o in out}


def test_effectiveness_and_stab_reach_option_facts():
    opts = _options()
    assert opts["move:0"].facts["effectiveness"] == 4.0 and opts["move:0"].facts["stab"] is True
    assert opts["move:1"].facts["effectiveness"] == 2.0 and opts["move:1"].facts["stab"] is False
    assert opts["move:2"].facts["effectiveness"] == 0.5


def test_immune_move_keeps_zero_effectiveness_and_no_effect_flag():
    immune = {**FACTS, 0: {**FACTS[0], "effectiveness": 0.0, "no_effect": True}}
    facts = _options(facts=immune)["move:0"].facts
    assert facts["effectiveness"] == 0.0 and facts["no_effect"] is True


def test_power_accuracy_and_pp_in_description_and_facts():
    opt = _options((7, 10, 10, 10))["move:0"]
    assert opt.facts["pp"] == 7 and opt.facts["max_pp"] == 16
    assert "power 95" in opt.description and "PP 7/16" in opt.description
    assert opt.legality.name == "LEGAL"  # accuracy 100 never misses by this rule


def test_zero_pp_move_is_unavailable_with_no_pp_reason():
    opt = _options((0, 10, 10, 10))["move:0"]
    assert opt.legality.name == "UNAVAILABLE" and opt.executor_ref is None
    assert opt.facts["unavailable_reason"] == "no_pp"
    assert "not selectable: no pp" in opt.description


def test_sub_100_accuracy_is_conditional_but_still_chooseable():
    opt = _options()["move:2"]
    assert opt.legality.name == "CONDITIONAL" and opt.executor_ref is not None
    assert "accuracy 80" in opt.description


def test_status_move_is_labelled_and_has_no_power_text():
    opt = _options()["move:3"]
    assert opt.facts["category"] == "status" and "status" in opt.description
    assert "power" not in opt.description
