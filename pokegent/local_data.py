"""Validate optional user-supplied data without distributing game content.

Configure paths before starting the process. Schemas retain the runtime's existing file formats;
provenance annotations are accepted as extra metadata. Invalid supplied data always fails closed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

LOCAL_DATA_ENV = "POKEGENT_LOCAL_DATA_DIR"
_CHECKOUT = Path(__file__).resolve().parents[1]
Number = Annotated[int, Field(ge=0)]
Byte = Annotated[int, Field(ge=0, le=255)]
Pair = Annotated[list[Number], Field(min_length=2, max_length=2)]
MapKey = Annotated[list[Byte], Field(min_length=2, max_length=2)]
Text = Annotated[str, Field(min_length=1)]
Direction = Literal["up", "down", "left", "right"]


class LocalDataError(RuntimeError):
    """A local data file is unavailable or invalid; its values are never logged."""


class _Schema(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")


class MapGroups(_Schema):
    group_order: Annotated[list[Text], Field(min_length=1, max_length=256)]
    groups: dict[Text, Annotated[list[Text], Field(min_length=1, max_length=256)]]

    @model_validator(mode="after")
    def coherent(self):
        if len(set(self.group_order)) != len(self.group_order):
            raise ValueError("duplicate group")
        if set(self.group_order) != set(self.groups):
            raise ValueError("group order does not match groups")
        names = [name.casefold() for values in self.groups.values() for name in values]
        if len(names) != len(set(names)):
            raise ValueError("ambiguous map label")
        return self


class QuizQuestion(_Schema):
    number: Annotated[int, Field(ge=1)]
    prompt: Text
    answer_yes: bool
    door_flag: int | str

    @model_validator(mode="after")
    def valid_flag(self):
        flag = int(self.door_flag, 0) if isinstance(self.door_flag, str) else self.door_flag
        if not 0 <= flag <= 0xFFFF:
            raise ValueError("flag outside range")
        return self


class Quiz(_Schema):
    questions: Annotated[list[QuizQuestion], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_questions(self):
        if len({q.number for q in self.questions}) != len(self.questions):
            raise ValueError("duplicate question number")
        return self


class Floor(_Schema):
    code: Text
    label: Text
    map: MapKey
    place: Text
    arrival: Pair | None = None


class Elevator(_Schema):
    scene: Literal["dialog.elevator", "menu.elevator"]
    map: MapKey
    menu: Literal["multichoice", "list"]
    panels: Annotated[list[Pair], Field(min_length=1)]
    exit_mat: Pair
    exit_push: Direction
    floors: Annotated[list[Floor], Field(min_length=1, max_length=12)]
    cancel: Byte
    requires: Literal["lift_key"] | None = None
    menu_id: Byte | None = None
    total: Annotated[int, Field(ge=1)] | None = None
    shown: Annotated[int, Field(ge=1)] | None = None

    @model_validator(mode="after")
    def valid_menu(self):
        if self.menu == "list" and (
            self.total != len(self.floors) or self.shown is None or self.shown > self.total
        ):
            raise ValueError("invalid list dimensions")
        expected = "menu.elevator" if self.menu == "list" else "dialog.elevator"
        if self.scene != expected:
            raise ValueError("scene does not match menu")
        return self


class Elevators(_Schema):
    elevators: dict[Text, Elevator]

    @model_validator(mode="after")
    def unique_maps(self):
        if not self.elevators or len({tuple(e.map) for e in self.elevators.values()}) != len(
            self.elevators
        ):
            raise ValueError("empty or ambiguous elevators")
        return self


Gate = Annotated[list[Pair], Field(min_length=1)]


class Statue(_Schema):
    id: Text
    floor: Text
    map: MapKey
    statue: Pair
    stand: Pair
    facing: Literal["N", "S", "E", "W"]
    opens: Annotated[list[Gate], Field(min_length=1)]
    closes: Annotated[list[Gate], Field(min_length=1)]


class SecretKey(_Schema):
    map: MapKey
    tile: Pair
    hidden_item_flag: Number


class Mansion(_Schema):
    switch_flag: Number
    maps: dict[Text, MapKey]
    secret_key: SecretKey
    statues: Annotated[list[Statue], Field(min_length=1)]

    @model_validator(mode="after")
    def coherent_statues(self):
        if len({s.id for s in self.statues}) != len(self.statues):
            raise ValueError("duplicate statue")
        if any(self.maps.get(s.floor) != s.map for s in self.statues):
            raise ValueError("statue floor does not match map")
        return self


class Puzzles(_Schema):
    mansion: Mansion


class SurfMove(_Schema):
    name: Text
    move_id: Number


class SurfBadge(_Schema):
    number: Number
    name: Text
    flag: Number


class SurfPrompt(_Schema):
    text: Text


class SurfStart(_Schema):
    player_elevation: Number
    front_tile: Text
    result: Text
    water_behaviors: dict[str, Any]


class SurfDismount(_Schema):
    land_elevation: Number
    water_elevation: Number | None = None
    rule: Text


class SurfCapability(_Schema):
    move: SurfMove
    badge: SurfBadge
    prompt: SurfPrompt


class Surf(_Schema):
    capability: SurfCapability
    start_rule: SurfStart
    dismount_rule: SurfDismount


class PromptRule(_Schema):
    kind: Literal[
        "heal", "field_move", "poke_flute", "fossil_choice", "secret_switch", "quiz",
        "destructive", "item_receive", "unknown", "unreadable", "pc_storage_error",
    ]
    patterns: Annotated[list[Text], Field(min_length=1)]
    default_yes: bool | None = None
    field_move: Text | None = None
    irreversible: bool = False
    wrong_answer_cost: Text | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PromptRules(_Schema):
    rules: list[PromptRule]


class FlyDestination(_Schema):
    mapsec: Number
    name: Text
    map_key: MapKey
    landing: Pair
    visited_flag: Number
    cell: Pair


class FlyDestinations(_Schema):
    destinations: Annotated[list[FlyDestination], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_destinations(self):
        for attr in ("mapsec", "name", "map_key"):
            values = [str(getattr(d, attr)) for d in self.destinations]
            if len(set(values)) != len(values):
                raise ValueError("duplicate destination")
        return self


SCHEMAS = {
    "pret_map_groups.json": MapGroups,
    "cinnabar_quiz.yaml": Quiz,
    "elevators.yaml": Elevators,
    "field_surf.yaml": Surf,
    "puzzles.yaml": Puzzles,
    "prompt_rules.yaml": PromptRules,
    "fly_destinations.json": FlyDestinations,
}


def local_path(name: str) -> Path | None:
    """Return an external path, never a fallback inside the checkout."""
    if name not in SCHEMAS:
        raise ValueError("unknown local data file")
    selected = os.environ.get("POKEGENT_MAP_GROUPS") if name == "pret_map_groups.json" else None
    directory = os.environ.get(LOCAL_DATA_ENV)
    if not selected and not directory:
        return None
    path = Path(selected).expanduser() if selected else Path(directory).expanduser() / name
    path = path.resolve()
    if path.is_relative_to(_CHECKOUT):
        raise LocalDataError(
            "Local game data must stay outside the checkout; see docs/local-data.md"
        )
    return path


def read_data(name: str, *, path: Path | None = None, optional: bool = False) -> dict | None:
    source = path if path is not None else local_path(name)
    if source is None or not source.is_file():
        if optional:
            return None
        raise LocalDataError(
            f"Local {name} data excluded or unavailable. Set {LOCAL_DATA_ENV} "
            "(or POKEGENT_MAP_GROUPS for the map index); see docs/local-data.md"
        )
    if source.resolve().is_relative_to(_CHECKOUT):
        raise LocalDataError("Local game data must stay outside the checkout")
    try:
        raw = source.read_text(encoding="utf-8")
        data = json.loads(raw) if name.endswith(".json") else yaml.safe_load(raw)
        SCHEMAS[name].model_validate(data)
    except (OSError, ValueError, TypeError, yaml.YAMLError, ValidationError):
        raise LocalDataError(f"Invalid local {name}: schema validation failed") from None
    return data
