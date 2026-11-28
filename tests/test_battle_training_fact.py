from types import SimpleNamespace as NS

import pytest

from pokegent.actions.battle import builder as B
from pokegent.options.models import Legality, Option
from pokegent.options.registry import BuildContext


def _opt(oid):
    return Option(id=oid, label=oid, description="d.", legality=Legality.UNAVAILABLE, verb="V")


def _frame(monkeypatch, kind, objective_kind):
    b = B.BattleBuilder()
    for name, ids in (("_move_options", ["move:0"]), ("_switch_options", ["switch:1"])):
        monkeypatch.setattr(b, name, lambda *a, _i=ids, **k: [_opt(i) for i in _i])
    monkeypatch.setattr(b, "_item_options", lambda *a: [])
    monkeypatch.setattr(b, "_ball_options", lambda *a: [])
    monkeypatch.setattr(b, "_escape_option", lambda v: _opt("escape") if kind == "wild" else None)
    monkeypatch.setattr(b, "_context", lambda v, extra=None: dict(extra or {}))
    monkeypatch.setattr(b, "_fp", lambda *a, **k: "fp")
    monkeypatch.setattr(B, "foe_facts", lambda *a: {})
    party = tuple(
        NS(level=lv, fainted=lv == 5, is_egg=lv == 3, hp_percent=9, slot=lv, name="m")
        for lv in (12, 5, 3, 14)
    )
    view = NS(kind=kind, opponent=NS(species=1, hp_percent=100), tables=None, party=party)
    kw = {"objective_kind": objective_kind, "objective_levels": ((None, None, 20),)}
    ctx = BuildContext(scene=NS(scene_id="s"), snapshot=NS(pokedex=None), objective=None, **kw)
    return b._action(ctx, view, "p")


def test_train_wild_frame_reports_training_and_escape_cancels_it(monkeypatch):
    draft = _frame(monkeypatch, "wild", "train")
    fact = draft.context["training_in_battle"]
    assert fact["lowest_party_level"] == 12
    assert fact["target_levels"] == [{"personality": None, "slot": None, "level": 20}]
    escape = draft.options[-1]
    assert [o.id for o in draft.options] == ["move:0", "switch:1", "escape"]
    assert escape.facts["cancels_training"] is True
    assert escape.description.endswith("; escaping gives no experience toward the train objective")


@pytest.mark.parametrize(("kind", "obj"), [("wild", None), ("wild", "heal"), ("trainer", "train")])
def test_no_training_fact_otherwise(monkeypatch, kind, obj):
    draft = _frame(monkeypatch, kind, obj)
    assert "training_in_battle" not in draft.context
    assert all("cancels_training" not in o.facts for o in draft.options)
