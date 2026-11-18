"""Authored TM/HM facts; no extracted game tables or game input required."""

from types import SimpleNamespace as NS

import pytest

from pokegent.games.gen3.firered.reader.menu_mappings import tm_options
from pokegent.games.gen3.firered.rom.move_mappings import HM_MOVE_IDS
from pokegent.options.models import Legality


class Tables:
    def __init__(self, mapping, compatible=True):
        self.mapping = mapping
        self.compatible = compatible
        self.lookups = []
        self.compatibility_checks = []
        self.moves = {
            value: NS(name=f"invented-{value}", power=power, type=3)
            for value, power in ((7, 80), (11, 90), (13, 40), (17, 10), (23, 60))
        }
        self.moves[min(HM_MOVE_IDS)] = NS(name="protected", power=1, type=3)

    def tmhm_move(self, item_id):
        self.lookups.append(item_id)
        return self.mapping.get(item_id)

    def can_learn_tmhm(self, species, item_id):
        self.compatibility_checks.append((species, item_id))
        return self.compatible

    def move(self, move_id):
        return self.moves[move_id]


def snapshot(item_id=289, moves=(13, 0, 0, 0), quantity=2):
    return NS(
        bag=(NS(items=(NS(item_id=item_id, quantity=quantity),)),),
        party=(NS(slot=2, species=5, moves=tuple(NS(move_id=m) for m in moves)),),
    )


@pytest.mark.parametrize("item_id,label", [(289, "TM01"), (338, "TM50"),
                                          (339, "HM01"), (346, "HM08")])
@pytest.mark.parametrize("move_id", [7, 11])
def test_rom_mapping_drives_facts_and_executor(item_id, label, move_id):
    tables = Tables({item_id: move_id})
    option, = tm_options(snapshot(item_id), tables)
    assert tables.lookups == [item_id]
    assert tables.compatibility_checks == [(5, item_id)]
    assert option.legality is Legality.LEGAL
    assert option.label == f"Teach {label} to party slot 3"
    assert option.id == f"teach_tm:{item_id}:2"
    assert option.facts["move_id"] == move_id
    assert option.facts["new_power"] == tables.moves[move_id].power
    assert option.facts["new_type"] == 3
    assert option.facts["uses_free_slot"] is True
    assert option.facts["quantity"] == 2
    assert option.facts["learned_move_is_hm"] is label.startswith("HM")
    assert f"Invented-{move_id}" in option.description
    assert option.executor_ref == "field.teach_tm"
    assert option.executor_args == {
        "item_id": item_id, "party_slot": 2, "forget_move": 0, "move_id": move_id,
    }


@pytest.mark.parametrize("move_id", [None, 0, -1, True, 7.0, "7", 999])
def test_missing_or_invalid_mapping_refuses(move_id):
    tables = Tables({289: move_id})
    assert tm_options(snapshot(), tables) == []
    assert tables.compatibility_checks == []


def test_missing_tables_mapping_and_lookup_errors_refuse():
    assert tm_options(snapshot(), None) == []
    assert tm_options(snapshot(), NS()) == []
    assert tm_options(snapshot(), Tables({})) == []

    def broken_lookup(_):
        raise ValueError("unreadable synthetic mapping")

    tables = Tables({})
    tables.tmhm_move = broken_lookup
    assert tm_options(snapshot(), tables) == []


@pytest.mark.parametrize("compatible", [False, None, 1])
def test_compatibility_requires_true(compatible):
    assert tm_options(snapshot(), Tables({289: 7}, compatible)) == []


def test_known_rom_move_is_filtered():
    tables = Tables({289: 7})
    assert tm_options(snapshot(moves=(7, 0, 0, 0)), tables) == []
    assert tables.compatibility_checks == []


def test_full_moves_keep_all_legal_replacements_and_protect_hm():
    hm = min(HM_MOVE_IDS)
    options = tm_options(snapshot(moves=(13, 17, 23, hm)), Tables({289: 7}))
    assert {o.facts["forget_move"] for o in options} == {13, 17, 23}
    for option in options:
        forget = option.facts["forget_move"]
        assert option.facts["default_forget_move"] == 17
        assert option.facts["uses_free_slot"] is False
        assert option.facts["hm_moves_cannot_be_forgotten"] is True
        assert option.executor_args["forget_move"] == forget
        assert option.id == "teach_tm:289:2" + ("" if forget == 17 else f":forget:{forget}")
        protected = next(c for c in option.facts["forget_choices"] if c["move_id"] == hm)
        assert protected["can_forget"] is False
        assert "HM" in protected["reason"]
    assert tm_options(snapshot(moves=(hm,) * 4), Tables({289: 7})) == []


@pytest.mark.parametrize("item_id,quantity", [(288, 1), (347, 1), (289, 0), (289, -1)])
def test_item_range_and_quantity_are_preserved(item_id, quantity):
    tables = Tables({item_id: 7})
    assert tm_options(snapshot(item_id, quantity=quantity), tables) == []
    assert tables.lookups == []
