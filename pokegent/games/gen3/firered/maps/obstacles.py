"""ROM/RAM-backed field obstacles and the requirements that clear them.

An obstacle is a blocking object event with one game mechanic that can remove or arm it.  The
object's location, script pointer and hide flag come from the ROM map event; its live position and
hide state come from RAM.  This module owns the small FireRed mechanic registry so navigation,
field options and prompt execution cannot disagree about names or requirements.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pokegent.games.gen3.firered.maps.map_schema import MapObject
from pokegent.games.gen3.firered.rom.script_scan import (
    Cmd,
    UnknownOpcode,
    reachable,
)

Completion = Literal["object_gone", "strength_active", "object_gone_or_battle", "script_flag"]
RequirementKind = Literal["field_move", "key_item", "script_flag"]
SourceKind = Literal["object_event", "tile"]


@dataclass(frozen=True)
class ObstacleKind:
    role: str
    name: str
    option_prefix: str
    verb: str
    requirement_key: str
    requirement_kind: RequirementKind
    capability_kind: str
    completion: Completion
    label: str
    prompt_key: str
    irreversible: bool = False


@dataclass(frozen=True)
class FieldObstacle:
    """One obstacle object, including script-derived outcome facts where readable."""

    kind: ObstacleKind
    source: SourceKind
    local_id: int | None
    bg_index: int | None
    x: int
    y: int
    script_ptr: int | None
    hide_flag: int | None
    script_sets_hide_flag: bool
    script_starts_battle: bool
    requirement_label: str | None = None
    equivalent_requirements: tuple[str, ...] = ()
    triggerable: bool = False
    declinable: bool = False

    @property
    def option_id(self) -> str:
        if self.local_id is None:
            raise ValueError("tile obstacles do not have object option ids")
        return f"{self.kind.option_prefix}:{self.local_id}"


KINDS: dict[str, ObstacleKind] = {
    "cut_tree": ObstacleKind(
        "cut_tree",
        "tree",
        "cut",
        "CUT_TREE",
        "cut",
        "field_move",
        "cut",
        "object_gone",
        "Use Cut on the tree",
        "cut",
    ),
    "rock_smash": ObstacleKind(
        "rock_smash",
        "rock",
        "smash",
        "ROCK_SMASH",
        "rock_smash",
        "field_move",
        "smash",
        "object_gone",
        "Use Rock Smash on the rock",
        "rock smash",
    ),
    "boulder": ObstacleKind(
        "boulder",
        "boulder",
        "strength",
        "ACTIVATE_STRENGTH",
        "strength",
        "field_move",
        "strength",
        "strength_active",
        "Use Strength on the boulder",
        "strength",
    ),
    "snorlax": ObstacleKind(
        "snorlax",
        "sleeping Snorlax",
        "snorlax",
        "USE_KEY_ITEM",
        "poke_flute",
        "key_item",
        "snorlax",
        "object_gone_or_battle",
        "Use Poke Flute on the sleeping Snorlax",
        "poke_flute",
        irreversible=True,
    ),
}

# Field capabilities that are not represented by a blocking map object still use the same
# requirement registry as object obstacles.  Darkness is optional: navigation remains valid
# without this capability, while the field family may offer Flash as a convenience.
FIELD_MOVE_KINDS: dict[str, ObstacleKind] = {
    "flash": ObstacleKind(
        "darkness",
        "darkness",
        "use_flash",
        "USE_FLASH",
        "flash",
        "field_move",
        "flash",
        "script_flag",
        "Use Flash to light the cave",
        "flash",
    )
}

ROLES = frozenset(KINDS)


def kind_for_role(role: str) -> ObstacleKind | None:
    return KINDS.get(role)


def from_object(obj: MapObject, rom: bytes = b"") -> FieldObstacle | None:
    """Build an obstacle from one ROM object event overlaid with its current RAM state.

    Script decoding is deliberately best-effort.  Unsupported bytecode does not erase the
    map-event facts; it only leaves the two script-derived booleans false.
    """

    kind = kind_for_role(obj.role)
    if kind is None:
        return None
    commands = ()
    if rom and obj.script_ptr is not None:
        try:
            commands = tuple(reachable(rom, obj.script_ptr))
        except (UnknownOpcode, ValueError):
            commands = ()
    set_flags = {command.u16() for command in commands if command.name == "setflag"}
    return FieldObstacle(
        kind=kind,
        source="object_event",
        local_id=obj.local_id,
        bg_index=None,
        x=obj.x,
        y=obj.y,
        script_ptr=obj.script_ptr,
        hide_flag=obj.hidden_flag_id or None,
        script_sets_hide_flag=bool(obj.hidden_flag_id and obj.hidden_flag_id in set_flags),
        script_starts_battle=any(
            command.name in {"setwildbattle", "dowildbattle", "dotrainerbattle"}
            for command in commands
        ),
    )


def script_tile(
    *,
    x: int,
    y: int,
    bg_index: int,
    opened_flag: int,
    requirement_key: str,
    name: str,
) -> FieldObstacle:
    """Model a ROM-scripted blocking tile such as a Card Key door."""

    kind = ObstacleKind(
        role="script_gate",
        name=name,
        option_prefix="gate",
        verb="INTERACT_WITH",
        requirement_key=requirement_key,
        requirement_kind="script_flag",
        capability_kind=requirement_key,
        completion="script_flag",
        label=f"Use {requirement_key.replace('_', ' ').title()} on {name}",
        prompt_key=requirement_key,
    )
    return FieldObstacle(
        kind=kind,
        source="tile",
        local_id=None,
        bg_index=bg_index,
        x=x,
        y=y,
        script_ptr=None,
        hide_flag=opened_flag,
        script_sets_hide_flag=True,
        script_starts_battle=False,
    )


def script_gate(
    *,
    x: int,
    y: int,
    requirement_key: str,
    name: str,
    local_id: int | None = None,
    requirement_label: str | None = None,
    equivalent_requirements: tuple[str, ...] = (),
    triggerable: bool = False,
    declinable: bool = False,
) -> FieldObstacle:
    """Model an NPC/coordinate-event gate discovered from its ROM script.

    Unlike a field-move obstacle, a script gate does not have a fixed interaction
    mechanic.  Its first item/flag check is the ROM-backed fact that controls the
    passage, and the navigation graph turns the tile into a crossing edge.
    """

    kind = ObstacleKind(
        role="script_gate",
        name=name,
        option_prefix="gate",
        verb="INTERACT_WITH",
        requirement_key=requirement_key,
        requirement_kind=("key_item" if requirement_key.startswith("item:") else "script_flag"),
        capability_kind=requirement_key,
        completion="script_flag",
        label=f"Pass {name}",
        prompt_key=requirement_key,
    )
    return FieldObstacle(
        kind=kind,
        source="object_event" if local_id is not None else "tile",
        local_id=local_id,
        bg_index=None,
        x=x,
        y=y,
        script_ptr=None,
        hide_flag=None,
        script_sets_hide_flag=False,
        script_starts_battle=False,
        requirement_label=requirement_label,
        equivalent_requirements=equivalent_requirements,
        triggerable=triggerable,
        declinable=declinable,
    )


def gate_requirement(commands: tuple[Cmd, ...]) -> str | None:
    """Return the ROM fact used by a scripted passage check.

    Item checks are preferred because guard scripts commonly also inspect an
    auxiliary story flag.  If no item is checked, the first checked flag is the
    passage's explicit prerequisite.  Scripts without either check are ordinary
    conversations, not gates.
    """

    item = next(
        (
            command.u16()
            for command in commands
            if command.name == "checkitem" and len(command.operands) >= 2
        ),
        None,
    )
    if item is not None:
        return f"item:{item}"
    flag = next(
        (
            command.u16()
            for command in commands
            if command.name == "checkflag" and len(command.operands) >= 2
        ),
        None,
    )
    return f"flag:{flag}" if flag is not None else None


def option_key(role: str, local_id: int) -> str | None:
    kind = kind_for_role(role)
    return f"{kind.requirement_key}:{local_id}" if kind is not None else None


__all__ = [
    "Completion",
    "FieldObstacle",
    "KINDS",
    "FIELD_MOVE_KINDS",
    "ObstacleKind",
    "ROLES",
    "from_object",
    "kind_for_role",
    "option_key",
    "gate_requirement",
    "script_gate",
    "script_tile",
]
