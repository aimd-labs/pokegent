"""Read-evidence guards around the party GIVE swap prompt.

``UiEvidence.valid`` is only ``gMain.valid`` (``reader/ui.py``). Every other section is read
independently, and ``valid=False`` means *unknown*: its value fields are empty/None, never a
known False. The swap prompt authorizes a single YES only from a complete read, so an invalid
tasks/menu/field/script section must not be read as "no task / no window / no fade / no script".

Synthetic evidence only; it is not proof of live RAM or a live transfer.
"""

import pytest
from test_party_give_return_flow import (
    OLD_ITEM,
    _after_snapshot,
    _bag_pairs,
    _bag_scene,
    _bag_snapshot,
    _give_setup,
    _obs,
    _party_scene,
    _Perceiver,
    _run,
    _swap_prompt_snapshot,
)

from pokegent.actions._exec.models import ActionStatus
from pokegent.errors import ErrorCode


def _run_prompt(monkeypatch, mutate=None):
    registries, option, spec, before = _give_setup(monkeypatch, held_item=OLD_ITEM)
    bag = _bag_snapshot(held_item=OLD_ITEM, quantity=2)
    prompt = _swap_prompt_snapshot()
    if mutate is not None:
        mutate(prompt)
    after = _after_snapshot()
    after.bag = _bag_pairs((182, 1), (OLD_ITEM, 1))
    perceiver = _Perceiver(
        [
            _obs(before, _party_scene(before)),
            _obs(bag, _bag_scene(registries, bag)),
            _obs(bag, _bag_scene(registries, bag)),
            _obs(prompt, _party_scene(prompt)),
            _obs(prompt, _party_scene(prompt)),
            _obs(prompt, _party_scene(prompt)),
            _obs(after, _party_scene(after)),
        ],
        _obs(after, _party_scene(after)),
    )
    result, actor = _run(registries, option, spec, before, perceiver)
    return result, actor, prompt


def _invalid(section):
    def mutate(snapshot):
        getattr(snapshot.ui, section).valid = False

    return mutate


def _unknown_script_flags(snapshot):
    snapshot.ui.script.running = None
    snapshot.ui.script.controls_locked = None


@pytest.mark.parametrize(
    "mutate",
    [
        _invalid("tasks"),
        _invalid("menu"),
        _invalid("field"),
        _invalid("script"),
        _unknown_script_flags,
    ],
    ids=["tasks-invalid", "menu-invalid", "field-invalid", "script-invalid", "script-unknown"],
)
def test_invalid_nested_section_never_authorizes_swap_yes(monkeypatch, mutate):
    result, actor, prompt = _run_prompt(monkeypatch, mutate)

    # The top-level flag is still gMain's valid read: this is a nested-section failure only.
    assert prompt.ui.valid is True
    assert result.status is ActionStatus.INTERRUPTED
    assert result.error_code is ErrorCode.E_INTERRUPTED_SCENE
    assert result.verified_outcome is not True
    # Party GIVE confirmation + bag item row only; no YES and no verified transfer.
    assert tuple(actor.taps) == ("a", "a")


def test_valid_known_false_sections_still_authorize_one_yes(monkeypatch):
    result, actor, prompt = _run_prompt(monkeypatch)

    assert prompt.ui.valid is True
    assert prompt.ui.tasks.valid is True
    assert prompt.ui.menu.valid is True
    assert prompt.ui.field.valid is True
    assert prompt.ui.script.valid is True
    assert prompt.ui.script.running is False
    assert result.status is ActionStatus.APPLIED, result.reason
    assert result.verified_outcome is True
    # Party GIVE confirmation + bag item row + exactly one bound YES.
    assert tuple(actor.taps) == ("a", "a", "a")
