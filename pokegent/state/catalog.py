"""Scene catalog: the typed, validated view of ``references/scenes.yaml`` (spec chapter 6).

The YAML records the public scene interfaces in ``references/scenes.yaml``.
Nothing here decides whether a scene is *implemented*: the catalog is the specification of
which scene ids exist, which phase they belong to and which option vocabulary the spec lists
(spec 6.K.6: a mapping is a specification, not a coverage claim).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

SCENE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
VERB_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

Phase = Literal["P0", "P1", "P2"]
Section = Literal["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]

#: Runtime ids that are not rows of chapter 6 (spec 6.0 items 1 and 8).
SCENE_INVALID = "system.invalid"  # emulator/ROM/RAM inconsistent -> pause + diagnose
SCENE_UNKNOWN = "scene.unknown"  # unknown or ambiguous -> diagnostics, no input, escalate
RESERVED_SCENE_IDS = frozenset({SCENE_INVALID, SCENE_UNKNOWN})

#: Catalog scenes whose only safe answer is a controlled pause (spec 6.A, 6.0.1).
PAUSE_SCENE_IDS = frozenset({SCENE_INVALID, "system.unsupported_rom"})

ESCALATION_VERBS = frozenset({"PAUSE_AND_ESCALATE", "ESCALATE", "ESCALATE_OR_REPLAN"})


class SceneSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scene_id: str
    family: str
    section: Section
    section_title: str
    phases: tuple[Phase, ...]
    options: str
    verbs: tuple[str, ...]
    guards: str

    @model_validator(mode="after")
    def _well_formed(self) -> SceneSpec:
        if not SCENE_ID_RE.match(self.scene_id):
            raise ValueError(f"malformed scene id {self.scene_id!r}")
        if self.family != self.scene_id.split(".", 1)[0]:
            raise ValueError(f"{self.scene_id}: family {self.family!r} is not the id namespace")
        if not self.phases:
            raise ValueError(f"{self.scene_id}: no phase")
        bad = [v for v in self.verbs if not VERB_RE.match(v)]
        if bad:
            raise ValueError(f"{self.scene_id}: malformed verbs {bad}")
        return self

    @property
    def p0(self) -> bool:
        return "P0" in self.phases

    @property
    def unsupported(self) -> bool:
        """P2-only rows and rows whose vocabulary is ``UNSUPPORTED*`` (spec 0.3: P2 is formally
        UNSUPPORTED). ``system.unsupported_rom`` is a pause scene instead, see PAUSE_SCENE_IDS."""
        return self.phases == ("P2",) or any(v.startswith("UNSUPPORTED") for v in self.verbs)

    @property
    def escalation_only(self) -> bool:
        return bool(self.verbs) and all(v in ESCALATION_VERBS for v in self.verbs)

    @property
    def has_yes_no(self) -> bool:
        """Binary confirm prompts: scenes that need confirmation over stable snapshots."""
        return "YES" in self.verbs and "NO" in self.verbs


class SceneCatalog:
    """Immutable id -> :class:`SceneSpec` map with a few derived sets."""

    def __init__(self, scenes: Iterable[SceneSpec], *, version: int = 1, spec_sha256: str = ""):
        ordered = tuple(scenes)
        by_id: dict[str, SceneSpec] = {}
        for spec in ordered:
            if spec.scene_id in by_id:
                raise ValueError(f"duplicate scene id {spec.scene_id!r}")
            by_id[spec.scene_id] = spec
        self.version = version
        self.spec_sha256 = spec_sha256
        self.scenes = ordered
        self._by_id = by_id

    def __len__(self) -> int:
        return len(self.scenes)

    def __contains__(self, scene_id: object) -> bool:
        return scene_id in self._by_id

    def get(self, scene_id: str) -> SceneSpec | None:
        return self._by_id.get(scene_id)

    def require(self, scene_id: str) -> SceneSpec:
        try:
            return self._by_id[scene_id]
        except KeyError:
            raise KeyError(f"scene id {scene_id!r} is not in the scene catalog") from None

    def ids(self) -> tuple[str, ...]:
        return tuple(self._by_id)

    def p0_ids(self) -> frozenset[str]:
        return frozenset(s.scene_id for s in self.scenes if s.p0)

    def families(self) -> frozenset[str]:
        return frozenset(s.family for s in self.scenes)

    def unsupported_ids(self) -> frozenset[str]:
        return frozenset(s.scene_id for s in self.scenes if s.unsupported)

    def escalation_only_ids(self) -> frozenset[str]:
        return frozenset(s.scene_id for s in self.scenes if s.escalation_only)

    def confirm_ids(self) -> frozenset[str]:
        return frozenset(s.scene_id for s in self.scenes if s.has_yes_no)

    def verbs(self, scene_id: str) -> frozenset[str]:
        return frozenset(self.require(scene_id).verbs)

    def is_unsupported(self, scene_id: str) -> bool:
        spec = self._by_id.get(scene_id)
        return bool(spec and spec.unsupported)


def default_catalog_path() -> Path:
    """``$POKEGENT_SCENES`` or ``<repo>/references/scenes.yaml``."""
    override = os.environ.get("POKEGENT_SCENES")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "references" / "scenes.yaml"


def load_scene_catalog(path: str | os.PathLike[str] | None = None) -> SceneCatalog:
    """Parse and validate the catalog. Raises ``ValueError`` on any malformed or duplicate row."""
    source = Path(path) if path is not None else default_catalog_path()
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "scenes" not in data:
        raise ValueError(f"{source}: not a scene catalog (missing 'scenes')")
    specs = [SceneSpec(**row) for row in data["scenes"]]
    if data.get("scene_count") not in (None, len(specs)):
        raise ValueError(f"{source}: scene_count {data['scene_count']} != {len(specs)} rows")
    return SceneCatalog(
        specs,
        version=int(data.get("catalog_version", 1)),
        spec_sha256=str(data.get("spec_sha256", "")),
    )


@lru_cache(maxsize=1)
def default_catalog() -> SceneCatalog:
    return load_scene_catalog()
