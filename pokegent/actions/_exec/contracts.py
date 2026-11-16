"""Frozen, JSON-round-trippable contracts shared by future adapters.

These types describe observations only.  PR1 deliberately does not register an adapter or
change any existing family behavior.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pokegent.frozen import FrozenMap


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RamSelector(_Frozen):
    """A typed, symbolic RAM read used by VERIFY; it contains no live address."""

    path: tuple[str, ...] = Field(min_length=1, max_length=16)
    value_type: Literal["bool", "int", "text", "bytes", "mapping"] = "int"


class ExpectedState(_Frozen):
    """One generic RAM post-state predicate declared by an adapter action.

    Optional fields narrow the predicate.  For example, ``map_changed`` without a destination
    accepts any map transition, while ``map_group``/``map_number`` require that destination.
    """

    kind: Literal[
        "menu_closed",
        "menu_open_cursor",
        "item_count_changed",
        "party_move_set_changed",
        "party_order_changed",
        "held_item_changed",
        "map_changed",
    ]
    surface: str | None = Field(default=None, max_length=120)
    stable_key: str | None = Field(default=None, max_length=120)
    item_id: int | None = Field(default=None, ge=1, le=65535)
    held_item_id: int | None = Field(default=None, ge=1, le=65535)
    bag_item_id: int | None = Field(default=None, ge=1, le=65535)
    bag_delta: int | None = Field(default=None, ge=-999, le=999)
    delta: int | None = Field(default=None, ge=-999, le=999)
    personality: int | None = Field(default=None, ge=0, le=0xFFFF_FFFF)
    move_id: int | None = Field(default=None, ge=1, le=65535)
    map_group: int | None = Field(default=None, ge=0, le=255)
    map_number: int | None = Field(default=None, ge=0, le=255)

    @model_validator(mode="after")
    def _required_identity(self) -> ExpectedState:
        if self.kind == "menu_open_cursor" and self.stable_key is None:
            raise ValueError("menu_open_cursor requires stable_key")
        if self.kind == "item_count_changed" and self.item_id is None:
            raise ValueError("item_count_changed requires item_id")
        if self.kind == "held_item_changed" and self.personality is None:
            raise ValueError("held_item_changed requires personality")
        if (self.map_group is None) != (self.map_number is None):
            raise ValueError("map destination requires both map_group and map_number")
        return self


class ActionContract(_Frozen):
    """The four phases every effectful adapter action must be able to verify."""

    pre: tuple[RamSelector, ...] = ()
    progress: tuple[RamSelector, ...] = ()
    success: tuple[RamSelector, ...] = ()
    interrupt: tuple[RamSelector, ...] = ()
    cursor_key: str | None = Field(default=None, max_length=120)
    expected: tuple[ExpectedState, ...] = Field(default=(), max_length=8)
    #: Scenes which are an intended result of this particular action. The execution layer
    #: combines these with the executor's ordinary allowed scenes; they are not global scene
    #: exceptions, so an unrelated action still interrupts on the same scene.
    expected_outcome_scenes: tuple[str, ...] = Field(default=(), max_length=8)


class MenuEntry(_Frozen):
    stable_key: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=120)
    effect: str = Field(default="", max_length=240)
    enabled: bool = True
    #: RAM/decomp identity of the screen opened by selecting this entry.
    target_screen: FrozenMap = Field(default_factory=dict)


class MenuModel(_Frozen):
    surface: str = Field(min_length=1, max_length=120)
    entries: tuple[MenuEntry, ...] = Field(max_length=128)
    selected_key: str | None = Field(default=None, max_length=120)
    scroll: int = Field(default=0, ge=0)
    cancel: bool = False


class PromptChoice(_Frozen):
    """One stable answer in a prompt."""

    stable_key: str = Field(min_length=1, max_length=120)
    effect: str = Field(min_length=1, max_length=240)


class PromptModel(_Frozen):
    """A semantic view of a prompt; it never supplies execution inputs."""

    question: str = Field(min_length=1, max_length=400)
    kind: Literal["yes_no", "multichoice", "confirm"]
    choices: tuple[PromptChoice, ...] = Field(max_length=128)
    cursor_key: str = Field(default="cursor_unknown", max_length=120)
    reversible: bool = True
    requires_authorization: bool = False

    @property
    def prompt_text(self) -> str:
        """Readable alias for consumers that call the field a prompt rather than a question."""
        return self.question


class Interactable(_Frozen):
    stable_key: str = Field(min_length=1, max_length=120)
    kind: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=120)
    requirements: tuple[str, ...] = Field(max_length=32)
    effects: tuple[str, ...] = Field(max_length=32)
    expected_delta: str | None = Field(default=None, max_length=240)


class NavEdge(_Frozen):
    stable_key: str = Field(min_length=1, max_length=120)
    requirements: tuple[str, ...] = Field(max_length=32)
    cost: int = Field(ge=0)
    verifier: RamSelector | None = None
