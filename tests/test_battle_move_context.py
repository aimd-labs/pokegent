"""Synthetic (no ROM): the battle frame context names the foe's types next to its species and HP."""

from types import SimpleNamespace as NS

from pokegent.actions.battle.builder import BattleBuilder

TYPE_NAMES = {0: "Normal", 2: "Flying", 10: "Fire"}


def _tables(types):
    return NS(
        species=lambda species_id: NS(types=types),
        type_name=lambda type_id: TYPE_NAMES[type_id],
    )


def _context(tables, kind="trainer"):
    view = NS(
        kind=kind,
        block=NS(turn=1),
        player=NS(
            name="Pidgeot", level=50, hp=30, max_hp=40, status=(), stat_stages={}, volatile=()
        ),
        opponent=NS(
            name="Charizard", species=6, level=54, hp_percent=80, status=(), stat_stages={}
        ),
        weather=(),
        is_trainer=kind == "trainer",
        enemy_party_alive=None,
        party=(),
        tables=tables,
    )
    return BattleBuilder()._context(view)


def test_context_lists_dual_foe_types():
    assert _context(_tables((10, 2)))["opposing_types"] == ["Fire", "Flying"]


def test_context_dedupes_a_single_type_species():
    assert _context(_tables((0, 0)))["opposing_types"] == ["Normal"]


def test_context_omits_types_without_tables():
    assert "opposing_types" not in _context(None)
