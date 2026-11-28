"""A field swap option says whether it brings a below-goal member to the lead slot."""

from test_field_start_menu_entry import _snap
from test_party_swap_species_labels import _members, _swap

from pokegent.actions.field import builder as B
from pokegent.options.registry import BuildContext
from pokegent.state.scenes import SceneResult

SPECIES = (25, 1, 1, 1, 6, 6)


def _facts(levels, objective_levels):
    party = tuple(
        type(m)(**{**vars(m), "level": level})
        for m, level in zip(_members(SPECIES), levels, strict=True)
    )
    snapshot = _snap(members=6).model_copy(update={"party": party})
    options = B.FieldBuilder(field_menu=True).build(
        BuildContext(
            snapshot=snapshot,
            scene=SceneResult(scene_id="field.free", confidence=1),
            goal_id=None,
            objective_levels=objective_levels,
        )
    ).options
    key = "brings_training_target_to_lead"
    return {slot: _swap(options, slot).facts.get(key) for slot in (1, 2, 5)}


def test_weak_member_swapped_in_for_a_strong_lead_is_a_training_target():
    facts = _facts((60, 50, 56, 56, 56, 40), ((None, None, 55),))
    assert facts == {1: True, 2: False, 5: True}


def test_false_when_the_lead_is_also_below_goal():
    assert set(_facts((50, 50, 56, 56, 56, 40), ((None, None, 55),)).values()) == {False}


def test_absent_without_a_level_goal():
    assert _facts((60, 50, 56, 56, 56, 40), ()) == {1: None, 2: None, 5: None}
