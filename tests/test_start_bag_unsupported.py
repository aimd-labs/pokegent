"""START must not offer the direct BAG entry: no supported field-bag decision owner.

Synthetic RAM/UI only. The decoder still reports the real BAG row and its cursor
index; the START builder simply omits that one unsupported choice. Party GIVE keeps
its own internal bag transit and item argument.
"""

from test_field_start_menu_entry import START_MENU, _snap, _ui
from test_party_give_item_args import EXP_SHARE, _party_snapshot

from pokegent.actions.menu.start_party import refs as R
from pokegent.actions.menu.start_party.builder import StartPartyBuilder, _fingerprint
from pokegent.games.gen3.firered.reader.menu_decoders import decode_start_menu
from pokegent.games.gen3.firered.reader.schemas import FlagSet
from pokegent.games.gen3.firered.rom import constants as C
from pokegent.options.models import Legality
from pokegent.options.registry import BuildContext
from pokegent.state.scenes import SceneResult


def _start_snapshot(*, count=7):
    menu = _ui(start={**START_MENU, "num_items": count}, menu_type="start_menu")
    return _snap(ui=menu).model_copy(
        update={"flags": FlagSet(set_ids=(C.FLAG_SYS_POKEDEX_GET,))}
    )


def _start_draft(snapshot):
    return StartPartyBuilder().build(
        BuildContext(
            snapshot=snapshot,
            scene=SceneResult(scene_id=R.START, confidence=1),
            goal_id=None,
        )
    )


def test_start_bag_entry_is_not_offered_while_real_rows_stay_decoded():
    snapshot = _start_snapshot()
    model = decode_start_menu(snapshot)
    assert model is not None
    decoded = [entry.stable_key for entry in model.entries]
    assert decoded[2] == "start:bag"  # the real BAG row keeps its cursor index

    draft = _start_draft(snapshot)
    offered = [option.id for option in draft.options]
    assert "start:bag" not in offered
    # The remaining choices keep the decoded row order instead of renumbering.
    assert offered == [key for key in decoded if key != "start:bag"]
    # The fingerprint still covers every decoded row, BAG included.
    assert draft.state_fingerprint == _fingerprint(R.START, model, snapshot)


def test_start_pokemon_and_exit_keep_their_executor_contracts():
    snapshot = _start_snapshot()
    options = {option.id: option for option in _start_draft(snapshot).options}
    for key in ("start:pokemon", "start:exit"):
        option = options[key]
        assert option.legality is Legality.LEGAL
        assert option.executor_ref == R.REF_SELECT
        assert option.postcondition_ref == R.POST_EXPECTED
        assert option.action_contract is not None
        assert option.action_contract.cursor_key == key
    (pokemon_expected,) = options["start:pokemon"].action_contract.expected
    assert pokemon_expected.surface == R.PARTY_LIST
    (exit_expected,) = options["start:exit"].action_contract.expected
    assert exit_expected.kind == "menu_closed"
    assert exit_expected.surface == R.START


def test_field_bag_scene_has_no_supported_decision_frame():
    # Leaf-builder absence control only (manual scene): no model, so pause only.
    # Not a global detector result or a Chief callback/task reproduction.
    draft = StartPartyBuilder().build(
        BuildContext(
            snapshot=_start_snapshot(),
            scene=SceneResult(scene_id="bag.items", confidence=1),
            goal_id=None,
        )
    )
    assert [option.id for option in draft.options] == ["pause_and_escalate"]
    assert draft.options[0].legality is Legality.UNKNOWN


def test_party_give_still_offers_the_verified_bag_item():
    draft = StartPartyBuilder().build(
        BuildContext(
            snapshot=_party_snapshot(),
            scene=SceneResult(scene_id=R.PARTY_HELD_ITEM, confidence=1),
            goal_id=None,
        )
    )
    give = next(
        option
        for option in draft.options
        if option.action_contract is not None
        and option.action_contract.cursor_key == "party_action:give"
    )
    assert give.legality is Legality.LEGAL
    assert give.executor_args == {"item_id": EXP_SHARE}
    assert EXP_SHARE == 182
