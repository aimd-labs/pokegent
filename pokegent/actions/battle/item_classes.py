"""Hand-written battle item applicability rules.

Which bag items the battle family offers, and what each class of them needs to change anything,
comes from generated ROM data: the battle handler an item runs and its effect bytes. The shipped
YAML contains only the mechanics interpretation of those traits and applicability rules; it has
no item ids, names, addresses, or extracted rows.

``classes_of`` answers "which classes is this item in" (an item can be in several: Full Restore is
``hp`` and ``cure``); the rules themselves are evaluated by :mod:`legality`, which owns verdicts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from pokegent.actions.battle import state as S

PATH_ENV = "POKEGENT_BATTLE_ITEM_CLASSES"
TARGETS = frozenset({"party_member", "active", "field"})
RULES = frozenset(
    {
        "hurt",
        "has_status",
        "fainted",
        "boost_possible",
        "wild_only",
        "someone_asleep",
        "unsupported",
    }  # fmt: skip
)
TRAITS = frozenset({"heals_hp", "revive", "cures", "heal_pp", "stat_boost", "focus_energy",
                    "guard_spec", "sacred_ash"})  # fmt: skip


class ItemClassError(ValueError):
    """The class table is malformed (fail closed: no item would be offered on a guess)."""


@dataclass(frozen=True)
class ItemClass:
    id: str
    label: str
    handler: str
    target: str
    rule: str
    requires: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()
    #: reason code -> sentence (the game rule that makes a use change nothing)
    no_effect: dict[str, str] = field(default_factory=dict)
    unsupported: str = ""
    consequence: str = ""
    #: an item option exists for this class (a ball is a ``ball:*`` option instead; the key item
    #: class ``wake`` is switched off for the rollout, see the table)
    offer: bool = True

    @property
    def offered(self) -> bool:
        return self.offer


@dataclass(frozen=True)
class ItemClassTable:
    classes: tuple[ItemClass, ...]
    provenance: dict

    def get(self, class_id: str) -> ItemClass:
        for cls in self.classes:
            if cls.id == class_id:
                return cls
        raise KeyError(class_id)

    def classes_of(self, info: S.ItemInfo | None) -> tuple[ItemClass, ...]:
        """The classes of an item: its handler role, narrowed by the effect-byte traits."""
        if info is None or info.role is None or info.role == "none":
            return ()
        out = []
        for cls in self.classes:
            if cls.handler != info.role:
                continue
            if (cls.requires or cls.excludes) and info.effect is None:
                continue  # effect bytes unreadable: the trait selectors cannot match
            if any(not _has(info.effect, trait) for trait in cls.requires):
                continue
            if any(_has(info.effect, trait) for trait in cls.excludes):
                continue
            out.append(cls)
        return tuple(out)


def _has(effect: S.ItemEffectInfo | None, trait: str) -> bool:
    if effect is None:
        return False
    value = getattr(effect, trait)
    return bool(value)


def default_path() -> Path:
    """``$POKEGENT_BATTLE_ITEM_CLASSES`` or ``<repo>/references/battle_item_classes.yaml``."""
    override = os.environ.get(PATH_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "references" / "battle_item_classes.yaml"


def parse(document: dict) -> ItemClassTable:
    if document.get("schema_version") != 1:
        raise ItemClassError("battle_item_classes.yaml: schema_version must be 1")
    classes = []
    for class_id, spec in dict(document["classes"]).items():
        cls = ItemClass(
            id=str(class_id),
            label=str(spec["label"]),
            handler=str(spec["handler"]),
            target=str(spec["target"]),
            rule=str(spec["rule"]),
            requires=tuple(spec.get("requires", ())),
            excludes=tuple(spec.get("excludes", ())),
            no_effect=dict(spec.get("no_effect", {})),
            unsupported=str(spec.get("unsupported", "")),
            consequence=str(spec.get("consequence", "")),
            offer=bool(spec.get("offer", True)),
        )
        if not cls.handler:
            raise ItemClassError(f"class {cls.id}: handler is required")
        if cls.target not in TARGETS:
            raise ItemClassError(f"class {cls.id}: unknown target {cls.target!r}")
        if cls.rule not in RULES:
            raise ItemClassError(f"class {cls.id}: unknown rule {cls.rule!r}")
        for trait in (*cls.requires, *cls.excludes):
            if trait not in TRAITS:
                raise ItemClassError(f"class {cls.id}: unknown trait {trait!r}")
        if cls.rule == "unsupported" and not cls.unsupported:
            raise ItemClassError(f"class {cls.id}: an unsupported class needs its reason")
        classes.append(cls)
    return ItemClassTable(tuple(classes), dict(document.get("provenance", {})))


@lru_cache(maxsize=4)
def _load(path: str) -> ItemClassTable:
    try:
        with open(path, encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        raise ItemClassError(f"cannot read {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ItemClassError(f"{path}: not a mapping")
    return parse(document)


def load(path: str | os.PathLike[str] | None = None) -> ItemClassTable:
    return _load(str(path or default_path()))
