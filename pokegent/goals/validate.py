"""Validation and name resolution of objectives: every rejection is a typed, machine-readable
reason.

agent may hallucinate a map, a person or a probe (spec 9.3 "agent hallucinated target"); the game
answers with a list of :class:`Reason` and does not move. Resolution maps agent's names to ids with
the pret map-name table; an optional :class:`MapDirectory` adds the deep checks (the warp index and
the person exist on that map).

Reason codes (stable): ``schema_invalid``, ``unknown_map``, ``missing_target``,
``unreachable_target_type``, ``unknown_warp``, ``unknown_npc``, ``out_of_bounds``, ``bad_probe``,
``bad_event``, ``expired``, ``stale_revision``, ``unreadable_file``, ``file_too_large``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Any, Protocol

from pydantic import ValidationError

from pokegent.games.gen3.firered.maps import map_names
from pokegent.games.gen3.firered.rom import constants as C
from pokegent.goals.models import (
    AnyOfProbe,
    BadgeProbe,
    DexCountProbe,
    DexOwnedProbe,
    FlagProbe,
    ItemProbe,
    MapArrival,
    MoneyAtLeastProbe,
    MonLevel,
    Objective,
    PartyHealthy,
    PartyKnowsMove,
    PartyLevel,
    PartySpecies,
    Target,
    TrainerDefeated,
    VarProbe,
)

#: pret ``TRAINER_FLAGS_START``: the flag of trainer ``n`` is ``0x500 + n`` (decompile value; the
#: flag array itself is verified by the reader, this offset is not verified on this ROM)
TRAINER_FLAGS_START = 0x500


@dataclass(frozen=True)
class Reason:
    code: str
    field: str = ""
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "field": self.field, "detail": self.detail}


@dataclass(frozen=True)
class MapFacts:
    """What the deep check needs to know about one map (from the ROM, see ``RomMapDirectory``)."""

    width: int
    height: int
    warp_count: int
    #: local ids of the objects that have a script (the persons one can talk to or fight)
    npc_ids: frozenset[int]
    #: local ids of "trainer type" objects (sight-line trainers); gym leaders and the Elite Four are
    #: NOT among them: their scripts start the battle after a dialog
    trainer_ids: frozenset[int] = frozenset()


class MapDirectory(Protocol):
    def describe(self, group: int, number: int) -> MapFacts | None: ...

    def resolve_item(self, name: str) -> int | None: ...

    def leader_for_badge(self, badge: int) -> tuple[tuple[int, int], int] | None: ...


@dataclass(frozen=True)
class ResolvedTarget:
    group: int | None = None
    number: int | None = None
    map_name: str | None = None
    warp: int | None = None
    x: int | None = None
    y: int | None = None
    npc: int | None = None
    item: int | None = None
    quantity: int | None = None
    species: int | None = None
    count: int | None = None

    @property
    def map_key(self) -> tuple[int, int] | None:
        if self.group is None or self.number is None:
            return None
        return (self.group, self.number)


@dataclass(frozen=True)
class ResolvedObjective:
    """The objective with every name turned into ids, and the probes the tracker evaluates."""

    objective: Objective
    target: ResolvedTarget
    probes: tuple[ResolvedProbe, ...]
    #: ROM-derived navigation destination for a reach objective with a badge probe.
    navigation_target: ResolvedTarget | None = None
    navigation_reason: str | None = None
    #: ROM-derived maps where a wild target can be encountered.
    navigation_map_keys: frozenset[tuple[int, int]] = frozenset()
    navigation_map_methods: tuple[tuple[tuple[int, int], frozenset[str]], ...] = ()


@dataclass(frozen=True)
class ResolvedProbe:
    kind: str
    flag: int | None = None
    expected: bool = True
    var: int | None = None
    value: int | None = None
    map_key: tuple[int, int] | None = None
    min_fraction: float = 1.0
    min_level: int | None = None
    personality: int | None = None
    slot: int | None = None
    move_id: int | None = None
    item: int | None = None
    min_quantity: int = 1
    amount: int | None = None
    species: int | None = None
    min_count: int | None = None
    x: int | None = None
    y: int | None = None
    children: tuple[ResolvedProbe, ...] = ()
    derived: bool = False  # added from the target, not written by agent

    def label(self) -> str:
        if self.kind == "any_of":
            return "any of (" + "; ".join(child.label() for child in self.children) + ")"
        if self.kind == "at_tile":
            return f"standing on ({self.x}, {self.y}) of map {self.map_key}"
        if self.kind == "map_arrival":
            return f"arrived in map {self.map_key}"
        if self.flag is not None:
            return f"flag 0x{self.flag:X} {'set' if self.expected else 'clear'}"
        if self.var is not None:
            op = ">=" if self.kind == "var_at_least" else "=="
            return f"var 0x{self.var:X} {op} {self.value}"
        if self.kind == "party_healthy":
            return f"party at {self.min_fraction:.0%} health"
        if self.kind == "party_healed":
            return "party fully healed (HP, status and PP)"
        if self.kind == "party_level":
            return f"party members at least level {self.min_level}"
        if self.kind == "mon_level":
            identity = (
                f"personality 0x{self.personality:08X}"
                if self.personality is not None
                else f"party slot {self.slot}"
            )
            return f"{identity} at least level {self.min_level}"
        if self.kind == "party_species":
            return f"party contains species {self.species}"
        if self.kind == "party_knows_move":
            return f"a party member knows move {self.move_id}"
        if self.kind == "item":
            return f"bag holds item {self.item} x{self.min_quantity}"
        if self.kind == "money_at_least":
            return f"money at least {self.amount}"
        if self.kind == "dex_owned":
            return f"Pokedex caught species {self.species}"
        if self.kind == "dex_count":
            return f"Pokedex caught count at least {self.min_count}"
        return self.kind


@dataclass(frozen=True)
class Validation:
    ok: bool
    reasons: tuple[Reason, ...] = ()
    resolved: ResolvedObjective | None = None
    #: the parsed objective even when semantic checks failed (schema errors give ``None``)
    objective: Objective | None = None

    def as_dicts(self) -> list[dict[str, str]]:
        return [r.as_dict() for r in self.reasons]


# -- map names -----------------------------------------------------------------------------------

_NORM = re.compile(r"[^a-z0-9]")


def normalize_map_name(name: str) -> str:
    text = name.strip().lower()
    if text.startswith("map_"):
        text = text[4:]
    return _NORM.sub("", text)


@lru_cache(maxsize=1)
def _name_index() -> dict[str, tuple[int, int]]:
    index: dict[str, tuple[int, int]] = {}
    for group, size in enumerate(map_names.group_sizes()):
        for number in range(size):
            name = map_names.map_name(group, number)
            if name:
                index.setdefault(normalize_map_name(name), (group, number))
    return index


def resolve_map(name: str) -> tuple[int, int] | None:
    """``(group, number)`` of a pret map name; case, ``MAP_`` prefix and separators are ignored."""
    return _name_index().get(normalize_map_name(name))


def normalize_item_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.casefold())


# -- validation ------------------------------------------------------------------------------------

_ALLOWED_TARGET_FIELDS: dict[str, frozenset[str]] = {
    "reach": frozenset({"map", "warp", "x", "y"}),
    "talk": frozenset({"map", "npc"}),
    "fight": frozenset({"map", "npc"}),
    "collect": frozenset({"map", "item", "quantity", "species"}),
    "shop": frozenset({"map", "item", "quantity"}),
    "heal": frozenset({"map"}),
    "dex": frozenset({"map", "species", "count"}),
    "train": frozenset({"map"}),
    "hold": frozenset(),
    "team-prep": frozenset(),
}


def parse(data: Mapping[str, Any] | Objective) -> tuple[Objective | None, tuple[Reason, ...]]:
    """Schema validation only: the objective, or ``schema_invalid`` reasons (one per problem)."""
    if isinstance(data, Objective):
        return data, ()
    try:
        return Objective.model_validate(data), ()
    except ValidationError as exc:
        reasons = tuple(
            Reason(
                "schema_invalid",
                ".".join(str(p) for p in err["loc"]),
                f"{err['type']}: {err['msg']}"[:300],
            )
            for err in exc.errors()
        )
        return None, reasons


def validate(
    data: Mapping[str, Any] | Objective,
    *,
    now: datetime | None = None,
    directory: MapDirectory | None = None,
) -> Validation:
    """Schema, names, target shape, probes, events and (optionally) deep map facts."""
    objective, reasons = parse(data)
    if objective is None:
        return Validation(False, reasons)
    found: list[Reason] = []
    target = _check_target(objective, directory, found)
    probes = _check_probes(objective, target, directory, found)
    if now is not None and objective.expires_at is not None and objective.expires_at <= now:
        found.append(Reason("expired", "expires_at", "the objective is already expired"))
    if found:
        return Validation(False, tuple(found), None, objective)
    navigation_target: ResolvedTarget | None = None
    navigation_reason: str | None = None
    badge = next(
        (probe.badge for probe in objective.success_probes if isinstance(probe, BadgeProbe)),
        None,
    )
    if objective.kind == "reach" and badge is not None:
        leader = getattr(directory, "leader_for_badge", None) if directory is not None else None
        found_leader = leader(badge) if callable(leader) else None
        if found_leader is None:
            navigation_reason = f"no ROM gym leader mapping for badge {badge}"
        else:
            (group, number), local_id = found_leader
            navigation_target = ResolvedTarget(
                group=group,
                number=number,
                map_name=map_names.map_name(group, number),
                npc=local_id,
            )
    return Validation(
        True,
        (),
        ResolvedObjective(objective, target, probes, navigation_target, navigation_reason),
        objective,
    )


def _check_target(
    objective: Objective, directory: MapDirectory | None, out: list[Reason]
) -> ResolvedTarget:
    t: Target = objective.target
    kind = objective.kind
    present = {
        name
        for name in ("map", "warp", "x", "y", "npc", "item", "quantity", "species", "count")
        if getattr(t, name) is not None
    }
    extra = present - _ALLOWED_TARGET_FIELDS[kind]
    for name in sorted(extra):
        out.append(
            Reason(
                "unreachable_target_type",
                f"target.{name}",
                f"a {kind!r} objective cannot target {name!r}",
            )
        )
    if kind == "reach":
        if t.map is None:
            out.append(Reason("missing_target", "target.map", "reach needs a map"))
        if t.warp is not None and t.x is not None:
            out.append(
                Reason("unreachable_target_type", "target", "give a warp OR coordinates, not both")
            )
    elif kind in ("talk", "fight"):
        if t.map is None:
            out.append(Reason("missing_target", "target.map", f"{kind} needs a map"))
        if t.npc is None:
            out.append(Reason("missing_target", "target.npc", f"{kind} needs an npc local id"))
    elif kind in ("collect", "shop"):
        if kind == "collect" and (t.item is None) == (t.species is None):
            out.append(
                Reason("missing_target", "target", "collect needs exactly one of item or species")
            )
        elif kind == "shop" and t.item is None:
            out.append(Reason("missing_target", "target.item", f"{kind} needs an item id"))
    elif kind == "dex":
        if (t.species is None) == (t.count is None):
            out.append(
                Reason(
                    "missing_target",
                    "target",
                    "dex needs exactly one of species or caught count",
                )
            )

    key: tuple[int, int] | None = None
    if t.map is not None:
        key = resolve_map(t.map)
        if key is None:
            out.append(Reason("unknown_map", "target.map", f"no map named {t.map!r}"))
    facts = directory.describe(*key) if (directory is not None and key is not None) else None
    if key is not None and directory is not None and facts is None:
        out.append(Reason("unknown_map", "target.map", f"map {t.map!r} is not readable"))
    item = t.item
    if isinstance(item, str):
        resolver = getattr(directory, "resolve_item", None) if directory is not None else None
        try:
            item = resolver(item) if callable(resolver) else None
        except Exception as exc:
            code = getattr(exc, "code", "rom_tables_unavailable")
            out.append(Reason(code, "target.item", str(exc) or "ROM item tables unavailable"))
            item = None
        if item is None:
            if not any(
                r.code == "rom_tables_unavailable" and r.field == "target.item" for r in out
            ):
                out.append(Reason("unknown_item", "target.item", f"no ROM item named {t.item!r}"))
    if facts is not None:
        if t.warp is not None and t.warp >= facts.warp_count:
            out.append(Reason("unknown_warp", "target.warp", f"map has {facts.warp_count} warps"))
        if t.x is not None and t.y is not None:
            if not (0 <= t.x < facts.width and 0 <= t.y < facts.height):
                out.append(
                    Reason(
                        "out_of_bounds",
                        "target",
                        f"({t.x}, {t.y}) is outside the {facts.width}x{facts.height} map",
                    )
                )
        if t.npc is not None and t.npc not in facts.npc_ids:
            out.append(Reason("unknown_npc", "target.npc", f"no person {t.npc} on that map"))
    name = map_names.map_name(*key) if key is not None else None
    return ResolvedTarget(
        group=key[0] if key else None,
        number=key[1] if key else None,
        map_name=name,
        warp=t.warp,
        x=t.x,
        y=t.y,
        npc=t.npc,
        item=item if isinstance(item, int) else None,
        quantity=t.quantity,
        species=t.species,
        count=t.count,
    )


def _check_probes(
    objective: Objective,
    target: ResolvedTarget,
    directory: MapDirectory | None,
    out: list[Reason],
) -> tuple[ResolvedProbe, ...]:
    probes: list[ResolvedProbe] = []
    for index, probe in enumerate(objective.success_probes):
        where = f"success_probes[{index}]"
        if isinstance(probe, AnyOfProbe):
            child_reasons: list[Reason] = []
            children = _check_probes(
                objective.model_copy(update={"success_probes": probe.children}),
                target, directory, child_reasons,
            )
            out.extend(
                Reason(reason.code, f"{where}.children" + reason.field[len("success_probes"):],
                       reason.detail)
                for reason in child_reasons
            )
            probes.append(ResolvedProbe("any_of", children=children))
        elif isinstance(probe, MapArrival):
            key = resolve_map(probe.map)
            if key is None:
                out.append(Reason("unknown_map", where, f"no map named {probe.map!r}"))
                continue
            probes.append(ResolvedProbe("map_arrival", map_key=key))
        elif isinstance(probe, FlagProbe):
            probes.append(ResolvedProbe("flag", flag=probe.flag, expected=probe.expected))
        elif isinstance(probe, VarProbe):
            probes.append(ResolvedProbe(probe.kind, var=probe.var, value=probe.value))
        elif isinstance(probe, BadgeProbe):
            probes.append(ResolvedProbe("flag", flag=C.FLAG_BADGE01_GET + probe.badge - 1))
        elif isinstance(probe, TrainerDefeated):
            probes.append(ResolvedProbe("flag", flag=TRAINER_FLAGS_START + probe.trainer_id))
        elif isinstance(probe, PartyHealthy):
            probes.append(ResolvedProbe("party_healthy", min_fraction=probe.min_fraction))
        elif isinstance(probe, PartyLevel):
            probes.append(ResolvedProbe("party_level", min_level=probe.min_level))
        elif isinstance(probe, MonLevel):
            probes.append(
                ResolvedProbe(
                    "mon_level",
                    min_level=probe.min_level,
                    personality=probe.personality,
                    slot=probe.slot,
                )
            )
        elif isinstance(probe, PartySpecies):
            probes.append(ResolvedProbe("party_species", species=probe.species))
        elif isinstance(probe, PartyKnowsMove):
            probes.append(ResolvedProbe("party_knows_move", move_id=probe.move_id))
        elif isinstance(probe, ItemProbe):
            probes.append(ResolvedProbe("item", item=probe.item, min_quantity=probe.quantity))
        elif isinstance(probe, MoneyAtLeastProbe):
            probes.append(ResolvedProbe("money_at_least", amount=probe.amount))
        elif isinstance(probe, DexOwnedProbe):
            probes.append(ResolvedProbe("dex_owned", species=probe.species))
        elif isinstance(probe, DexCountProbe):
            probes.append(ResolvedProbe("dex_count", min_count=probe.min))
        else:  # GameClear
            probes.append(ResolvedProbe("flag", flag=C.FLAG_SYS_GAME_CLEAR))
    kind = objective.kind
    if not probes:
        if kind == "reach" and target.warp is not None:
            out.append(
                Reason(
                    "bad_probe",
                    "success_probes",
                    "a warp leaves the map: give a map_arrival probe for the destination",
                )
            )
        elif kind == "reach" and target.x is not None and target.map_key is not None:
            probes.append(
                ResolvedProbe(
                    "at_tile", map_key=target.map_key, x=target.x, y=target.y, derived=True
                )
            )
        elif kind == "reach" and target.map_key is not None:
            probes.append(ResolvedProbe("map_arrival", map_key=target.map_key, derived=True))
        elif kind == "heal":
            probes.append(ResolvedProbe("party_healed", derived=True))
        elif kind in ("collect", "shop") and target.item is not None:
            probes.append(
                ResolvedProbe(
                    "item",
                    item=target.item,
                    min_quantity=target.quantity or 1,
                    derived=True,
                )
            )
        elif kind == "collect" and target.species is not None:
            probes.append(ResolvedProbe("dex_owned", species=target.species, derived=True))
        elif kind == "dex" and target.species is not None:
            probes.append(ResolvedProbe("dex_owned", species=target.species, derived=True))
        elif kind == "dex" and target.count is not None:
            probes.append(ResolvedProbe("dex_count", min_count=target.count, derived=True))
        elif kind == "train":
            out.append(
                Reason(
                    "bad_probe",
                    "success_probes",
                    "train needs a party_level or party_healthy success probe",
                )
            )
        elif kind == "team-prep":
            if not any(probe.kind == "party_species" for probe in objective.success_probes):
                out.append(
                    Reason(
                        "bad_probe",
                        "success_probes",
                        "team-prep needs a party_species success probe",
                    )
                )
        elif kind in ("talk", "fight"):
            out.append(
                Reason(
                    "bad_probe",
                    "success_probes",
                    f"{kind} needs a flag, var or trainer probe so completion can be verified",
                )
            )
    return tuple(probes)
