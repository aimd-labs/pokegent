"""Scene ownership: who detects and who builds each catalog scene, conflicts and gaps (M2.5).

Spec 6.K says a scene is not covered because it is in the catalog. This module answers, for the
registries that were really filled, per catalog scene: which family's detector can claim it, which
family's builder produces its options, whether it is only waited out (BUSY), recognised but
UNSUPPORTED, or a GAP. Two rules are enforced, not just reported:

* **Unique ownership.** A scene id claimed by detectors of two different families, or built by two
  different families (an explicit scene registration of one family next to the namespace builder of
  another), is an :class:`OwnershipConflict`. ``register_all`` raises on it. Scenes that really are
  shared (a generic busy scene) must be listed explicitly in ``FamilyConfig.shared_scenes``.
* **No hidden gaps.** A P0-capable catalog scene (``SceneSpec.p0``) that no detector claims is
  listed as a gap, with the catalog's own unsupported rows and the escalation-only rows shown
  separately.

Claims come from the families themselves: a detector exposes ``claimed_scenes`` (a frozenset of
scene ids), a family declares ``busy`` (claimed, only waited out) and ``unsupported`` (claimed,
builder answers with an UNSUPPORTED diagnostic) scenes in its :class:`FamilyManifest`. A detector
without ``claimed_scenes`` is listed under ``undeclared_detectors`` and cannot be checked statically
(the registry's ``scene.unknown`` on ambiguity is then the runtime guard).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pokegent.actions.coverage import ALL_DISPOSITIONS
from pokegent.state.catalog import SceneCatalog


class OwnershipError(RuntimeError):
    """Raised by ``register_all`` when two families claim the same scene."""


@dataclass(frozen=True)
class FamilyManifest:
    """What one family registered and which scene ids that covers."""

    name: str
    detectors: tuple[str, ...] = ()  # detector names registered by this family
    detects: frozenset[str] = frozenset()  # scene ids its detectors may claim (declared)
    undeclared_detectors: tuple[str, ...] = ()  # detectors without ``claimed_scenes``
    builder_namespaces: frozenset[str] = frozenset()  # ``register_family`` keys
    builder_scenes: frozenset[str] = frozenset()  # ``register_scenes`` ids (win over a namespace)
    busy: frozenset[str] = frozenset()  # claimed, only waited out (no builder by design)
    unsupported: frozenset[str] = frozenset()  # claimed, builder answers UNSUPPORTED diagnostics
    executors: frozenset[str] = frozenset()
    verifiers: frozenset[str] = frozenset()
    #: explicit builder scenes that intentionally replace a generic namespace builder
    handoffs: frozenset[str] = frozenset()
    #: catalog scenes the family does not claim on purpose because their options or facts live
    #: inside another scene of the family (``battle.capture`` -> ``battle.action``): scene -> where
    folded: Mapping[str, str] = field(default_factory=dict)
    #: catalog scenes the family knows it does not cover (nobody claims them): scene -> why
    known_gaps: Mapping[str, str] = field(default_factory=dict)

    def builds(self, scene_id: str) -> bool:
        return scene_id in self.builder_scenes or scene_id.split(".", 1)[0] in (
            self.builder_namespaces
        )


@dataclass(frozen=True)
class OwnershipConflict:
    scene_id: str
    kind: str  # "detector" | "builder"
    owners: tuple[str, ...]

    def __str__(self) -> str:
        return f"{self.scene_id}: {self.kind} claimed by {' and '.join(self.owners)}"


def find_conflicts(
    manifests: Iterable[FamilyManifest], *, shared: frozenset[str] = frozenset()
) -> list[OwnershipConflict]:
    """Every scene id with detectors or builders of more than one family (except ``shared``)."""
    detect_owners: dict[str, set[str]] = defaultdict(set)
    explicit_builders: dict[str, set[str]] = defaultdict(set)
    namespace_owners: dict[str, set[str]] = defaultdict(set)
    for m in manifests:
        for scene_id in m.detects:
            detect_owners[scene_id].add(m.name)
        for scene_id in m.builder_scenes:
            explicit_builders[scene_id].add(m.name)
        for namespace in m.builder_namespaces:
            namespace_owners[namespace].add(m.name)
    handoffs = {scene for m in manifests for scene in m.handoffs}
    conflicts: list[OwnershipConflict] = []
    for scene_id, owners in sorted(detect_owners.items()):
        if len(owners) > 1 and scene_id not in shared:
            conflicts.append(OwnershipConflict(scene_id, "detector", tuple(sorted(owners))))
    scene_ids = set(explicit_builders) | {
        s for s in detect_owners if s.split(".", 1)[0] in namespace_owners
    }
    for scene_id in sorted(scene_ids):
        owners = set(explicit_builders.get(scene_id, ()))
        if scene_id not in handoffs:
            owners |= set(namespace_owners.get(scene_id.split(".", 1)[0], ()))
        if len(owners) > 1 and scene_id not in shared:
            conflicts.append(OwnershipConflict(scene_id, "builder", tuple(sorted(owners))))
    return conflicts


# -- the report ------------------------------------------------------------------------------------

#: row status values (ordered by "how much is there")
IMPLEMENTED = "implemented"  # detector + builder
BUSY = "busy"  # detector, waited out by the runner, no builder by design
RECOGNISED_UNSUPPORTED = "recognised_unsupported"  # detector + UNSUPPORTED diagnostic builder
ESCALATE = "escalate"  # detector claims, catalog vocabulary is escalation only
FOLDED = "folded"  # no own detector by design: handled inside another scene of the same family
NOT_NEEDED = "not_needed_for_e4"  # valid catalog row, outside the E4 objective
REAL_GAP = "REAL_GAP"  # decision point with no detector, builder, or bounded exit
NO_BUILDER = "detected_no_builder"  # claimed, not busy, nobody builds it: runner escalates
CATALOG_UNSUPPORTED = "catalog_unsupported"  # P2-only / UNSUPPORTED* row: no owner needed
GAP = "GAP"  # P0-capable, supported, nobody claims it
UNCLAIMED = "unclaimed"  # not P0 (P1/P2 later), nobody claims it


@dataclass(frozen=True)
class SceneRow:
    scene_id: str
    family: str  # catalog namespace
    section: str
    phases: tuple[str, ...]
    p0: bool
    detector_owner: str  # "" if nobody
    builder_owner: str  # "" if nobody
    status: str
    note: str = ""

    @property
    def owner(self) -> str:
        return self.detector_owner or self.builder_owner or "-"


@dataclass
class OwnershipReport:
    rows: list[SceneRow]
    conflicts: list[OwnershipConflict] = field(default_factory=list)
    unknown_claims: list[str] = field(
        default_factory=list
    )  # claimed ids that are not in the catalog
    undeclared_detectors: list[str] = field(default_factory=list)
    skipped_families: list[str] = field(default_factory=list)
    planned: dict[str, str] = field(default_factory=dict)  # namespace -> who is building it

    # -- queries -----------------------------------------------------------------------------

    def gaps(self) -> list[SceneRow]:
        """P0-capable scenes nobody can recognise, plus P0 scenes recognised but not buildable."""
        return [r for r in self.rows if r.p0 and r.status in (GAP, NO_BUILDER, REAL_GAP)]

    def gaps_by_family(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for r in self.gaps():
            counts[r.family] += 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def by_status(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for r in self.rows:
            counts[r.status] += 1
        return dict(sorted(counts.items()))

    def by_owner(self) -> dict[str, dict[str, int]]:
        owners: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for r in self.rows:
            if r.detector_owner:
                owners[r.detector_owner][r.status] += 1
        return {k: dict(sorted(v.items())) for k, v in sorted(owners.items())}

    def p0_totals(self) -> dict[str, int]:
        p0 = [r for r in self.rows if r.p0]
        covered = [
            r
            for r in p0
            if r.status in (IMPLEMENTED, BUSY, RECOGNISED_UNSUPPORTED, ESCALATE, FOLDED)
        ]
        return {
            "p0_scenes": len(p0),
            "covered": len(covered),
            "implemented": sum(r.status == IMPLEMENTED for r in p0),
            "busy": sum(r.status == BUSY for r in p0),
            "recognised_unsupported": sum(r.status == RECOGNISED_UNSUPPORTED for r in p0),
            "escalate": sum(r.status == ESCALATE for r in p0),
            "folded": sum(r.status == FOLDED for r in p0),
            "not_needed_for_e4": sum(r.status == NOT_NEEDED for r in p0),
            "real_gaps": sum(r.status == REAL_GAP for r in p0),
            "gaps": len(self.gaps()),
            "catalog_unsupported": sum(r.status == CATALOG_UNSUPPORTED for r in p0),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "totals": self.p0_totals(),
            "by_status": self.by_status(),
            "by_owner": self.by_owner(),
            "conflicts": [str(c) for c in self.conflicts],
            "unknown_claims": self.unknown_claims,
            "undeclared_detectors": self.undeclared_detectors,
            "skipped_families": self.skipped_families,
            "gaps": [r.scene_id for r in self.gaps()],
            "gaps_by_family": self.gaps_by_family(),
            "planned": self.planned,
            "rows": [
                {
                    "scene": r.scene_id,
                    "p0": r.p0,
                    "detector": r.detector_owner,
                    "builder": r.builder_owner,
                    "status": r.status,
                    "note": r.note,
                }
                for r in self.rows
            ],
        }

    # -- rendering ---------------------------------------------------------------------------

    def format_text(self, *, all_rows: bool = False) -> str:
        """Plain-text table. Default: every row that is not a plain P1/P2 'unclaimed' one, gaps
        first, so nothing P0 is hidden; ``all_rows=True`` prints all catalog rows."""
        rows = self.rows if all_rows else [r for r in self.rows if r.status != UNCLAIMED]
        order = {GAP: 0, NO_BUILDER: 1, REAL_GAP: 0}
        rows = sorted(rows, key=lambda r: (order.get(r.status, 2), r.scene_id))
        width = max((len(r.scene_id) for r in rows), default=10)
        lines = [
            f"{'scene':<{width}}  P0  {'detector':<8} {'builder':<8} status",
            f"{'-' * width}  --  {'-' * 8} {'-' * 8} {'-' * 22}",
        ]
        for r in rows:
            lines.append(
                f"{r.scene_id:<{width}}  {'P0' if r.p0 else '  '}  {r.detector_owner or '-':<8} "
                f"{r.builder_owner or '-':<8} {r.status}" + (f"  ({r.note})" if r.note else "")
            )
        t = self.p0_totals()
        lines += [
            "",
            f"P0 scenes: {t['p0_scenes']}  covered: {t['covered']} "
            f"(implemented {t['implemented']}, busy {t['busy']}, "
            f"recognised-unsupported {t['recognised_unsupported']}, escalate {t['escalate']}, "
            f"folded {t['folded']})  GAPS: {t['gaps']}  catalog-unsupported: "
            f"{t['catalog_unsupported']}",
            f"gaps by namespace: {self.gaps_by_family()}",
            f"by status: {self.by_status()}",
            f"by owner: {self.by_owner()}",
        ]
        if self.skipped_families:
            lines.append(f"optional families not installed: {self.skipped_families}")
        if self.conflicts:
            lines.append("CONFLICTS: " + "; ".join(str(c) for c in self.conflicts))
        if self.unknown_claims:
            lines.append(f"claimed but not in the catalog: {self.unknown_claims}")
        if self.undeclared_detectors:
            lines.append(f"detectors without claimed_scenes: {self.undeclared_detectors}")
        return "\n".join(lines)

    def format_markdown(self) -> str:
        t = self.p0_totals()
        out = [
            "| scene | P0 | detector | builder | status | note |",
            "|---|---|---|---|---|---|",
        ]
        order = {GAP: 0, NO_BUILDER: 1, REAL_GAP: 0}
        for r in sorted(self.rows, key=lambda r: (order.get(r.status, 2), r.scene_id)):
            if r.status == UNCLAIMED:
                continue
            out.append(
                f"| `{r.scene_id}` | {'P0' if r.p0 else ''} | {r.detector_owner or '-'} | "
                f"{r.builder_owner or '-'} | {r.status} | {r.note} |"
            )
        out += [
            "",
            f"P0 scenes {t['p0_scenes']}: implemented {t['implemented']}, busy {t['busy']}, "
            f"recognised-unsupported {t['recognised_unsupported']}, escalate {t['escalate']}, "
            f"folded {t['folded']}, not-needed-for-e4 {t['not_needed_for_e4']}, "
            f"**real gaps {t['real_gaps']}**, catalog-unsupported "
            f"{t['catalog_unsupported']}. Gaps by namespace: {self.gaps_by_family()}. "
            f"Rows with status `unclaimed` (P1/P2, nobody claims them) are omitted: "
            f"{sum(r.status == UNCLAIMED for r in self.rows)} of {len(self.rows)}.",
        ]
        return "\n".join(out)


def build_report(
    catalog: SceneCatalog,
    manifests: Iterable[FamilyManifest],
    *,
    shared: frozenset[str] = frozenset(),
    skipped: Iterable[str] = (),
    planned: Mapping[str, str] | None = None,
) -> OwnershipReport:
    """The per-scene ownership table for ``catalog`` over the registered families."""
    manifests = tuple(manifests)
    detector_owner: dict[str, str] = {}
    busy: set[str] = set()
    unsupported: dict[str, str] = {}
    explicit_builder: dict[str, str] = {}
    namespace_builder: dict[str, str] = {}
    folded: dict[str, tuple[str, str]] = {}
    known_gaps: dict[str, str] = {}
    planned = dict(planned or {})
    for m in manifests:
        for s, why in m.folded.items():
            folded.setdefault(s, (m.name, why))
        for s, why in m.known_gaps.items():
            known_gaps.setdefault(s, f"{m.name}: {why}")
        for s in sorted(m.detects):
            detector_owner.setdefault(s, m.name)
        busy |= m.busy
        for s in m.unsupported:
            unsupported.setdefault(s, m.name)
        for s in m.builder_scenes:
            explicit_builder.setdefault(s, m.name)
        for ns in m.builder_namespaces:
            namespace_builder.setdefault(ns, m.name)

    rows: list[SceneRow] = []
    for spec in catalog.scenes:
        sid = spec.scene_id
        det = detector_owner.get(sid, "")
        bld = explicit_builder.get(sid) or namespace_builder.get(spec.family, "")
        note = ""
        if spec.unsupported:
            status, note = CATALOG_UNSUPPORTED, "P2-only or UNSUPPORTED* row"
            if det:
                note += "; recognised anyway"
        elif not det and sid in folded:
            owner, why = folded[sid]
            status, note = FOLDED, why
            det = det or ""
        elif not det:
            disposition = ALL_DISPOSITIONS.get(sid)
            if disposition is not None:
                status, note = disposition.kind, disposition.note
            else:
                status = GAP if spec.p0 else UNCLAIMED
            if spec.escalation_only and disposition is None:
                note = "escalation-only row"
            if disposition is None and sid in known_gaps:
                note = known_gaps[sid]
            elif disposition is None and spec.family in planned:
                note = planned[spec.family]
        elif spec.escalation_only:
            status, note = ESCALATE, "catalog vocabulary is escalation only"
        elif sid in unsupported:
            status, note = RECOGNISED_UNSUPPORTED, "builder answers an UNSUPPORTED diagnostic"
        elif sid in busy:
            status, note = BUSY, "waited out by the runner"
        elif bld:
            status = IMPLEMENTED
        else:
            status, note = NO_BUILDER, "claimed, not busy, no builder: the runner escalates"
        if status == FOLDED and not bld:
            bld = folded[sid][0] if sid in folded else "coverage"
        if sid in shared and det:
            note = (note + "; " if note else "") + "shared by config"
        rows.append(
            SceneRow(sid, spec.family, spec.section, spec.phases, spec.p0, det, bld, status, note)
        )

    claimed: set[str] = set()
    for m in manifests:
        claimed |= set(m.detects) | set(m.builder_scenes)
    report = OwnershipReport(
        rows,
        find_conflicts(manifests, shared=shared),
        unknown_claims=sorted(s for s in claimed if s not in catalog),
        undeclared_detectors=sorted(d for m in manifests for d in m.undeclared_detectors),
        skipped_families=list(skipped),
        planned=planned,
    )
    return report
