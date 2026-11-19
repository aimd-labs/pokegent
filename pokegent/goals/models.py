"""The objective: what agent hands the game (spec 7.2, 9.2). Pure data, versioned, no I/O.

agent is the strategist; the code never invents a strategic choice (CONTEXT.md decision order:
agent plan > SYSTEM1 with facts > default). An :class:`Objective` is one atomic, verifiable thing:
reach a place, talk to someone, fight a trainer, heal, or hold (do nothing special). Names are
written the way agent thinks (pret map names, local ids, badge numbers); :mod:`.validate` resolves
them to ids and rejects the rest with a machine-readable reason.

Schema version ``objective/1``. Models are frozen and reject unknown fields: a typo in a key is a
rejected proposal, never a silently ignored one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

SCHEMA_VERSION = "objective/1"
FORBID_CLASSES = frozenset({"pc", "storage", "fly", "shop", "tm", "sell"})


OBJECTIVE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_MAP_WORDS = {
    "Pokemon": "Pokémon",
    "Loreleis": "Lorelei's",
    "Brunos": "Bruno's",
    "Agathas": "Agatha's",
    "Lances": "Lance's",
}
_MAP_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _readable_map(name: str) -> str:
    parts = []
    for part in name.split("_"):
        words = _MAP_CAMEL.sub(" ", part).split()
        parts.append(" ".join(_MAP_WORDS.get(word, word) for word in words))
    return ", ".join(parts)


Kind = Literal[
    "reach", "talk", "fight", "collect", "shop", "heal", "dex", "train", "hold", "team-prep"
]

#: probe kinds (``success_probes``)
PROBE_KINDS = (
    "map_arrival",
    "flag",
    "var",
    "var_at_least",
    "any_of",
    "badge",
    "trainer_defeated",
    "party_healthy",
    "party_level",
    "mon_level",
    "party_species",
    "game_clear",
    "item",
    "money_at_least",
    "party_knows_move",
    "dex_owned",
    "dex_count",
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Target(_Frozen):
    """Where/who the objective is about, in agent's vocabulary.

    ``map`` is a pret map name (``PewterCity``, ``MAP_PEWTER_CITY``, case/underscores ignored).
    ``warp`` is a warp index of that map, ``x``/``y`` a coordinate of it, ``npc`` an object's local
    id on it. ``reach`` takes a map and optionally ONE of warp / coordinates; ``talk`` and ``fight``
    need map + npc; ``heal`` takes an optional map (a specific healing spot); ``hold`` takes
    nothing.
    """

    map: str | None = Field(default=None, max_length=80)
    warp: int | None = Field(default=None, ge=0, le=255)
    x: int | None = Field(default=None, ge=0, le=1023)
    y: int | None = Field(default=None, ge=0, le=1023)
    npc: int | None = Field(default=None, ge=1, le=254)
    item: Annotated[int, Field(ge=1, le=374)] | Annotated[str, Field(max_length=80)] | None = None
    quantity: int | None = Field(default=None, ge=1, le=999)
    species: int | None = Field(default=None, ge=1, le=151)
    count: int | None = Field(default=None, ge=1, le=151)

    @model_validator(mode="after")
    def _coords_pair(self) -> Target:
        if (self.x is None) != (self.y is None):
            raise ValueError("x and y must be given together")
        return self


class MapArrival(_Frozen):
    kind: Literal["map_arrival"] = "map_arrival"
    map: str = Field(max_length=80)


class FlagProbe(_Frozen):
    kind: Literal["flag"] = "flag"
    flag: int = Field(ge=1, le=0x8FF)
    expected: bool = True


class VarProbe(_Frozen):
    kind: Literal["var"] = "var"
    var: int = Field(ge=0x4000, le=0x40FF)
    value: int = Field(ge=0, le=0xFFFF)


class VarAtLeastProbe(VarProbe):
    kind: Literal["var_at_least"] = "var_at_least"


class BadgeProbe(_Frozen):
    kind: Literal["badge"] = "badge"
    badge: int = Field(ge=1, le=8)


class TrainerDefeated(_Frozen):
    kind: Literal["trainer_defeated"] = "trainer_defeated"
    trainer_id: int = Field(ge=1, le=0x2FF)


class PartyHealthy(_Frozen):
    kind: Literal["party_healthy"] = "party_healthy"
    min_fraction: float = Field(default=1.0, gt=0.0, le=1.0)


class PartyLevel(_Frozen):
    kind: Literal["party_level"] = "party_level"
    min_level: int = Field(ge=1, le=100)


class MonLevel(_Frozen):
    kind: Literal["mon_level"] = "mon_level"
    personality: int | None = Field(default=None, ge=0, le=0xFFFF_FFFF)
    slot: int | None = Field(default=None, ge=0, le=5)
    min_level: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def _one_identity(self) -> MonLevel:
        if (self.personality is None) == (self.slot is None):
            raise ValueError("mon_level requires exactly one of personality or slot")
        return self


class PartySpecies(_Frozen):
    """The party contains the requested species (used by PC team-prep objectives)."""

    kind: Literal["party_species"] = "party_species"
    species: int = Field(ge=1, le=151)


class PartyKnowsMove(_Frozen):
    kind: Literal["party_knows_move"] = "party_knows_move"
    move_id: int = Field(ge=1, le=354)


class GameClear(_Frozen):
    kind: Literal["game_clear"] = "game_clear"


class ItemProbe(_Frozen):
    """The bag holds ``min_quantity`` or more of an item (summed over every pocket)."""

    kind: Literal["item"] = "item"
    item: int = Field(ge=1, le=374)
    min_quantity: int = Field(default=1, ge=1, le=999)
    # Short form used by objective files; min_quantity remains for compatibility.
    min: int | None = Field(default=None, ge=1, le=999)

    @model_validator(mode="after")
    def _one_count(self) -> ItemProbe:
        if self.min is not None and self.min_quantity != 1:
            raise ValueError("use min or min_quantity, not both")
        return self

    @property
    def quantity(self) -> int:
        return self.min if self.min is not None else self.min_quantity


class MoneyAtLeastProbe(_Frozen):
    kind: Literal["money_at_least"] = "money_at_least"
    amount: int = Field(ge=0, le=999999)


class DexOwnedProbe(_Frozen):
    kind: Literal["dex_owned"] = "dex_owned"
    species: int = Field(ge=1, le=151)


class DexCountProbe(_Frozen):
    kind: Literal["dex_count"] = "dex_count"
    min: int = Field(ge=0, le=151)


LeafProbe = Annotated[
    MapArrival
    | FlagProbe
    | VarProbe
    | VarAtLeastProbe
    | BadgeProbe
    | TrainerDefeated
    | PartyHealthy
    | PartyLevel
    | MonLevel
    | PartySpecies
    | PartyKnowsMove
    | GameClear
    | ItemProbe
    | MoneyAtLeastProbe
    | DexOwnedProbe
    | DexCountProbe,
    Field(discriminator="kind"),
]


class AnyOfProbe(_Frozen):
    kind: Literal["any_of"] = "any_of"
    children: tuple[LeafProbe, ...] = Field(min_length=1, max_length=8)


Probe = Annotated[LeafProbe | AnyOfProbe, Field(discriminator="kind")]


def probe_leaves(probes):
    """Authored or resolved leaves for guidance; never replace tracker conjunctions."""
    for probe in probes:
        if probe.kind == "any_of":
            yield from probe.children
        else:
            yield probe


class Objective(_Frozen):
    schema_version: Literal["objective/1"] = SCHEMA_VERSION  # type: ignore[assignment]
    objective_id: str = Field(min_length=1, max_length=48)
    revision: int = Field(ge=1)
    kind: Kind
    target: Target = Field(default_factory=Target)
    success_probes: tuple[Probe, ...] = ()
    next_review_on: tuple[Literal["stuck"], ...] = Field(default=(), max_length=1)
    rationale: str = Field(default="", max_length=600)
    forbid: tuple[str, ...] = Field(default_factory=tuple, max_length=6)
    set_by: str = Field(default="agent", min_length=1, max_length=40)
    #: ISO-8601 UTC instant after which the objective no longer applies (the game goes goal-less)
    expires_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_phase3_fields(cls, value: object) -> object:
        if isinstance(value, Mapping):
            removed = sorted(set(value) & {"instructions", "abort_if"})
            if removed:
                raise ValueError(
                    f"{', '.join(removed)} was removed in Phase 3; write objective decisions as "
                    "objectives/success_probes instead"
                )
        return value

    @model_serializer(mode="wrap")
    def _omit_empty_review(self, handler):
        data = handler(self)
        if not self.next_review_on:
            data.pop("next_review_on", None)  # Preserve pre-field history equality on restart.
        return data

    @field_validator("objective_id")
    @classmethod
    def _id_shape(cls, value: str) -> str:
        if not OBJECTIVE_ID_RE.match(value):
            raise ValueError("objective_id must match ^[a-z][a-z0-9_]{0,47}$")
        return value

    @field_validator("forbid")
    @classmethod
    def _forbid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(item.strip().lower() for item in value))
        unknown = sorted(set(normalized) - FORBID_CLASSES)
        if unknown:
            raise ValueError(f"forbid contains unknown executor class(es): {unknown}")
        return normalized

    @field_validator("forbid", mode="before")
    @classmethod
    def _forbid_is_list(cls, value):
        if isinstance(value, str):
            raise ValueError("forbid must be a list of executor classes, not a bare string")
        return value

    @field_validator("expires_at")
    @classmethod
    def _tz(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expires_at must carry a timezone (use ...Z)")
        return value

    # -- views ----------------------------------------------------------------------------------

    @property
    def goal_id(self) -> str:
        """The id builders and frames carry (``BuildContext.goal_id``): the semantic id only."""
        return self.objective_id

    def summary(self) -> str:
        """One English line for the model-visible context (never contains ids or pointers)."""
        t = self.target
        where = []
        if t.map:
            map_name = _readable_map(t.map) if self.set_by == "progression" else t.map
            where.append(f"map {map_name}")
        if t.warp is not None:
            where.append(f"warp {t.warp}")
        if t.x is not None and t.y is not None:
            where.append(f"tile ({t.x}, {t.y})")
        if t.npc is not None:
            where.append(f"person {t.npc}")
        if t.item is not None:
            where.append(f"item {t.item}" + (f" x{t.quantity}" if t.quantity else ""))
        if t.species is not None:
            where.append(f"species {t.species}")
        if t.count is not None:
            where.append(f"caught count {t.count}")
        text = {
            "reach": "Reach",
            "talk": "Talk to someone at",
            "fight": "Fight the trainer at",
            "collect": "Collect",
            "shop": "Buy",
            "heal": "Heal the team" + (" at" if where else ""),
            "dex": "Complete the Pokedex target for",
            "train": "Train the party" + (" on" if where else ""),
            "team-prep": "Prepare the team",
            "hold": "Hold: no particular objective",
        }[self.kind]
        if self.kind != "hold" and where:
            text += " " + ", ".join(where)
        if self.rationale:
            text += f". Why: {self.rationale}"
        return text


def default_objective_kind() -> Kind:
    """The objective the game runs on when no file exists: ``hold`` (no strategy is invented)."""
    return "hold"
