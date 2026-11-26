"""Field party swaps name the current lead and target by species and 1-based slot."""

from test_field_start_menu_entry import _snap

from pokegent.actions.field import builder as B
from pokegent.options.registry import BuildContext
from pokegent.state.scenes import SceneResult

NAMES = {1: "BULBASAUR", 6: "CHARIZARD", 25: "RATICATE"}


class _Tables:
    def species_name(self, species):
        return NAMES[species]


def _members(species, nicknames=None):
    nicknames = nicknames if nicknames is not None else ("",) * len(species)
    base = _snap(members=len(species)).party
    return tuple(
        type(member)(
            **{
                **vars(member),
                "slot": slot,
                "personality": 0x100 + slot,
                "species": species_id,
                "nickname": nickname,
            }
        )
        for slot, (member, species_id, nickname) in enumerate(
            zip(base, species, nicknames, strict=True)
        )
    )


def _with_slots(party, order):
    return tuple(
        type(party[index])(**{**vars(party[index]), "slot": slot})
        for slot, index in enumerate(order)
    )


def _options(party, tables=None):
    snapshot = _snap(members=len(party)).model_copy(update={"party": party})
    return B.FieldBuilder(field_menu=True, tables=tables).build(
        BuildContext(
            snapshot=snapshot,
            scene=SceneResult(scene_id="field.free", confidence=1),
            goal_id=None,
        )
    ).options


def _swap(options, slot):
    return next(option for option in options if option.facts.get("party_slot") == slot)


def test_swap_names_lead_and_target_with_species_and_one_based_slots():
    option = _swap(_options(_members((25, 1, 1, 1, 6)), _Tables()), 4)
    assert option.label == "Swap RATICATE (slot 1) with CHARIZARD (slot 5)"
    assert "RATICATE (slot 1)" in option.description
    assert "CHARIZARD (slot 5)" in option.description
    assert option.executor_ref == "field.swap_party"
    assert option.executor_args == {"target_personality": 0x104}


def test_refreshed_party_relabels_swap_but_keeps_execution_binding():
    before_party = _members((25, 1, 1, 1, 6))
    after_party = _with_slots(before_party, (4, 1, 2, 3, 0))
    before = _swap(_options(before_party, _Tables()), 4)
    after = _swap(_options(after_party, _Tables()), 4)
    assert after.label == "Swap CHARIZARD (slot 1) with RATICATE (slot 5)"
    assert "CHARIZARD (slot 1)" in after.description
    assert "RATICATE (slot 5)" in after.description
    assert after.executor_ref == before.executor_ref == "field.swap_party"
    assert after.postcondition_ref == before.postcondition_ref
    assert before.id == "swap:00000104"
    assert after.id == "swap:00000100"
    assert before.executor_args == {"target_personality": 0x104}
    assert after.executor_args == {"target_personality": 0x100}
    assert before.action_contract.cursor_key == "party:00000104"
    assert after.action_contract.cursor_key == "party:00000100"


def test_no_table_fallback_uses_safe_names_and_keeps_other_menu_options():
    options = _options(
        _members((25, 1, 1, 1, 6), ("Sparky", "", "", "", "")), tables=None
    )
    option = _swap(options, 4)
    assert "Sparky (slot 1)" in option.label
    assert "species 6 (slot 5)" in option.label
    assert "Sparky (slot 1)" in option.description
    assert "species 6 (slot 5)" in option.description
    assert option.executor_ref == "field.swap_party"
    assert sum(candidate.id == "open_start" for candidate in options) == 1
