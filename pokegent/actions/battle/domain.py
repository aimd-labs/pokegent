"""The single RAM/ROM-backed state contract consumed by the battle option adapter.

``BattleBlock`` is intentionally close to the game's structs.  ``BattleDomainState`` is the
decision-facing projection: it joins that atomic battle read with the party, bag, field effects,
ROM lookup rows, and the exact controller which owns the current question.  Builders and
comparisons use this object instead of rediscovering battle shape with scene-specific branches.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pokegent.actions.battle.reader import battle_of
from pokegent.actions.battle.state import BattleBlock, BattleLookups, SideInfo
from pokegent.games.gen3.firered.reader.schemas import PartyMember, StateSnapshot

DecisionKind = Literal[
    "action",
    "move",
    "target",
    "party",
    "forced_switch",
    "bag",
    "item_target",
    "message",
    "switch_offer",
    "learn_move",
    "level_up",
    "ended",
    "busy",
    "unsupported_link",
    "unknown",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BagEntry(_Frozen):
    item_id: int
    quantity: int
    pocket: str


class DecisionPoint(_Frozen):
    kind: DecisionKind
    scene_id: str
    phase: str = ""
    ready: bool
    battler: int | None = None
    forced: bool = False


class BattleDomainState(_Frozen):
    """Complete battle input assembled from one atomic snapshot.

    The model contains no policy and does not hide diagnostics.  Opposing technical RAM remains in
    ``block`` under the existing discovery policy; model-facing views still redact it.
    """

    schema_version: Literal["battle-domain/1.0"] = "battle-domain/1.0"
    block: BattleBlock
    decision: DecisionPoint
    party: tuple[PartyMember, ...] = ()
    bag: tuple[BagEntry, ...] = ()
    player_battlers: tuple[int, ...] = ()
    opponent_battlers: tuple[int, ...] = ()
    active_party_slots: tuple[int, ...] = ()
    sides: tuple[SideInfo, SideInfo] | None = None
    lookups: BattleLookups = Field(default_factory=BattleLookups)

    @classmethod
    def from_snapshot(
        cls,
        snapshot: StateSnapshot,
        *,
        scene_id: str,
        phase: str = "",
        ready: bool = True,
    ) -> "BattleDomainState":
        block = battle_of(snapshot)
        if block is None or not block.active or not block.valid:
            raise ValueError("an active, valid BattleBlock is required")
        kind = _decision_kind(scene_id, block)
        party_slots = tuple(
            block.party_indexes[b.battler]
            for b in block.player_battlers
            if b.battler < len(block.party_indexes)
        )
        bag = tuple(
            BagEntry(item_id=item.item_id, quantity=item.quantity, pocket=pocket.pocket)
            for pocket in snapshot.bag
            for item in pocket.items
            if item.item_id and item.quantity > 0
        )
        return cls(
            block=block,
            decision=DecisionPoint(
                kind=kind,
                scene_id=scene_id,
                phase=phase,
                ready=ready,
                battler=block.decision_battler,
                forced=kind == "forced_switch",
            ),
            party=tuple(snapshot.party),
            bag=bag,
            player_battlers=tuple(b.battler for b in block.player_battlers),
            opponent_battlers=tuple(b.battler for b in block.opponent_battlers),
            active_party_slots=party_slots,
            sides=block.sides,
            lookups=block.lookups,
        )


def _decision_kind(scene_id: str, block: BattleBlock) -> DecisionKind:
    if block.kind == "link":
        return "unsupported_link"
    suffix = scene_id.removeprefix("battle.")
    aliases: dict[str, DecisionKind] = {
        "action": "action",
        "double_turn": "action",
        "safari": "action",
        "ghost": "action",
        "tutorial_scripted": "action",
        "moves_ui": "move",
        "choose_target": "target",
        "double_target": "target",
        "party": "party",
        "forced_switch": "forced_switch",
        "bag": "bag",
        "item_target": "item_target",
        "message": "message",
        "faint": "message",
        "switch_offer": "switch_offer",
        "learn_move": "learn_move",
        "level_up": "level_up",
        "ended": "ended",
        "entering": "busy",
        "animation": "busy",
    }
    return aliases.get(suffix, "unknown")


__all__ = ["BagEntry", "BattleDomainState", "DecisionPoint", "DecisionKind"]
