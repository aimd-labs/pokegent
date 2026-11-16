"""Scene families and their one registration point for runtime initialization.

``register_all(registries, config)`` fills the four shared registries with every family:

* ``dialog``  (always): message boxes, yes/no, heal offer, multichoice, the generic busy scenes
  ``field.map_transition`` / ``field.scripted_movement`` and the system scenes ``system.whiteout`` /
  ``system.quest_log_playback`` (dialog OWNS these two; pass ``dialog_system_scenes=False`` only if
  another family takes them over). One exception: with the optional ``system_flow`` family
  registered and its option ``quest_log: skip`` (the default) dialog keeps the quest log DETECTOR
  and system_flow builds its options (a conditional skip; dialog's builder only reports it
  unsupported);
* ``battle``  (always): every ``battle.*`` scene;
* optional families (``field`` today): ``register_optional(name, ...)`` imports
  ``pokegent.actions.<name>`` if it exists and calls its ``register`` function. A missing
  package is skipped and reported (``FamilyBundle.skipped``); an import error INSIDE an installed
  package is a bug and propagates.

Scene ownership is unique: after the registrations the declared claims of all families are checked
(:mod:`pokegent.actions.ownership`) and :class:`~.ownership.OwnershipError` is raised if two
families claim the same scene id (``FamilyConfig.shared_scenes`` lists the deliberate exceptions).
``FamilyBundle.ownership(catalog)`` returns the coverage table over all catalog scenes, with the P0
gaps listed, never hidden.

Plugin contract for an optional family package ``pokegent.actions.<name>``
(additive, docs/contracts.md v1.1):

* ``register`` callable. Called with keyword arguments chosen by its parameter names:
  ``registries`` (the :class:`Registries`), ``detectors``, ``builders``, ``executors`` or
  ``executor_registry``, ``verifiers`` or ``verifier_registry``, ``config`` (the family's slot of
  ``FamilyConfig.options``), ``catalog``, and (v1.3) ``objectives`` / ``graph`` (the objective
  source and route graph of ``FamilyConfig``). Its return value is kept in ``FamilyBundle.plugins``.
* optional module attributes ``CLAIMED_SCENES`` (scene ids its detectors can claim), ``BUSY_SCENES``
  (claimed, only waited out) and ``UNSUPPORTED_SCENES`` (claimed, builder answers UNSUPPORTED), and
  ``CONFIRM_SCENE_IDS``. Detectors may expose ``claimed_scenes`` instead of the module attribute.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as _dc_field  # avoid colliding with the field subpackage
from typing import TYPE_CHECKING, Any

from pokegent.actions.ownership import (
    FamilyManifest,
    OwnershipError,
    OwnershipReport,
    build_report,
    find_conflicts,
)

if TYPE_CHECKING:  # pragma: no cover
    from pokegent.actions._exec.registry import ExecutorRegistry, VerifierRegistry
    from pokegent.actions.battle.builder import BattleBuilder
    from pokegent.actions.battle.view import MoveFactsProvider
    from pokegent.actions.menu.dialog import DialogFamily, PromptPolicy
    from pokegent.options.registry import BuilderRegistry
    from pokegent.state.catalog import SceneCatalog
    from pokegent.state.scenes import ConfirmPolicy, DetectorRegistry

#: namespaces somebody is building (shown as the note of their gap rows)
PLANNED_FAMILIES: Mapping[str, str] = {
    "field": "planned: field family (branch m2c-field)",
    "special": "planned: scripted puzzles and prompts",
    "system_flow": "planned: endgame, evolution, PC, shop",
    "shop": "planned: Poke Mart buy flow",
}
#: families registered by default, in order
CORE_FAMILIES: tuple[str, ...] = ("dialog", "battle")
#: families registered if their package exists
OPTIONAL_FAMILIES: tuple[str, ...] = (
    "field",
    "special",
    "system_flow",
    "shop",
    "start_party",
)

# Public family ids remain stable while menu families live under the menu namespace.
_FAMILY_MODULES: Mapping[str, str] = {
    "dialog": "pokegent.actions.menu.dialog",
    "system_flow": "pokegent.actions.menu.system_flow",
    "shop": "pokegent.actions.menu.shop",
    "start_party": "pokegent.actions.menu.start_party",
}


def _family_module(name: str) -> str:
    return _FAMILY_MODULES.get(name, f"pokegent.actions.{name}")


@dataclass
class Registries:
    """The four shared registries every family registers into."""

    detectors: DetectorRegistry
    builders: BuilderRegistry
    executors: ExecutorRegistry
    verifiers: VerifierRegistry

    @classmethod
    def new(cls, catalog: SceneCatalog | None = None) -> Registries:
        from pokegent.actions._exec.registry import ExecutorRegistry, VerifierRegistry
        from pokegent.options.registry import BuilderRegistry
        from pokegent.state.catalog import load_scene_catalog
        from pokegent.state.scenes import DetectorRegistry

        catalog = catalog if catalog is not None else load_scene_catalog()
        verifiers = VerifierRegistry()
        builders = BuilderRegistry()
        builders.bind_verifiers(verifiers)
        return cls(
            DetectorRegistry(catalog=catalog),
            builders,
            ExecutorRegistry(),
            verifiers,
        )


@dataclass(frozen=True)
class FamilyConfig:
    """What the families need from the caller. Everything has a safe default."""

    core: tuple[str, ...] = CORE_FAMILIES
    optional: tuple[str, ...] = OPTIONAL_FAMILIES
    #: dialog prompt policy (``PromptPolicy()`` if None): unknown prompts escalate by default
    prompt_policy: PromptPolicy | None = None
    #: battle move facts (mechanics adapter); None = the family's BasicFacts
    facts: MoveFactsProvider | None = None
    #: dialog owns system.whiteout and system.quest_log_playback; False hands them to someone else
    dialog_system_scenes: bool = True
    #: scene ids deliberately claimed by more than one family (default: none)
    shared_scenes: frozenset[str] = frozenset()
    #: per optional family: the value passed as ``config`` to its ``register``
    options: Mapping[str, Any] = _dc_field(default_factory=dict)
    #: the active objective as the field builder reads it, ``callable(goal_id) ->
    #: ResolvedObjective | None`` (``ObjectiveSession.objective_source()``); passed on to a
    #: family's ``register(objectives=)``
    objectives: Any = None
    #: an optional ``MapGraph`` for ``serves_goal`` route facts; passed to ``register(graph=)``
    graph: Any = None
    #: the ROM/RAM navigation adapter
    navigation: Any = None


@dataclass
class FamilyBundle:
    """The result of :func:`register_all`: handles, manifests, what was skipped."""

    registries: Registries
    config: FamilyConfig
    manifests: dict[str, FamilyManifest]
    dialog: DialogFamily | None = None
    battle: BattleBuilder | None = None
    plugins: dict[str, Any] = _dc_field(default_factory=dict)
    skipped: tuple[str, ...] = ()
    confirm_scene_ids: frozenset[str] = frozenset()

    @property
    def sidecar(self):
        """The dialog text sidecar (what ``CompositeSnapshotSource`` must fill), or None."""
        return self.dialog.sidecar if self.dialog is not None else None

    def confirm_policy(self, catalog: SceneCatalog | None = None, **kw: Any) -> ConfirmPolicy:
        """Scenes to confirm over stable snapshots: the catalog's YES+NO rows UNION every family's
        ``confirm_scene_ids`` (the heal offer's verbs are HEAL_TEAM/DECLINE_HEAL, so the catalog
        rule alone would not confirm it)."""
        from pokegent.state.catalog import load_scene_catalog
        from pokegent.state.scenes import ConfirmPolicy

        catalog = catalog if catalog is not None else self.registries.detectors.catalog
        if catalog is None:
            catalog = load_scene_catalog()
        return ConfirmPolicy(scene_ids=catalog.confirm_ids() | self.confirm_scene_ids, **kw)

    def ownership(self, catalog: SceneCatalog | None = None) -> OwnershipReport:
        from pokegent.state.catalog import load_scene_catalog

        catalog = catalog if catalog is not None else self.registries.detectors.catalog
        if catalog is None:
            catalog = load_scene_catalog()
        return build_report(
            catalog,
            self.manifests.values(),
            shared=self.config.shared_scenes,
            skipped=self.skipped,
            planned=PLANNED_FAMILIES,
        )


# -- recording what a family registered ------------------------------------------------------------


@dataclass(frozen=True)
class _Marks:
    detectors: tuple[Any, ...]
    families: frozenset[str]
    scenes: frozenset[str]
    executors: frozenset[str]
    verifiers: frozenset[str]


def _marks(r: Registries) -> _Marks:
    return _Marks(
        tuple(r.detectors.detectors),
        frozenset(r.builders.family_builders()),
        frozenset(r.builders.scene_builders()),
        r.executors.refs(),
        r.verifiers.refs(),
    )


def _manifest(
    name: str,
    before: _Marks,
    after: _Marks,
    *,
    busy: frozenset[str] = frozenset(),
    unsupported: frozenset[str] = frozenset(),
    declared: frozenset[str] = frozenset(),
    folded: Mapping[str, str] | None = None,
    known_gaps: Mapping[str, str] | None = None,
    handoffs: frozenset[str] = frozenset(),
) -> FamilyManifest:
    known = {id(d) for d in before.detectors}
    new = [d for d in after.detectors if id(d) not in known]
    detects: set[str] = set(declared)
    undeclared: list[str] = []
    for d in new:
        claims = getattr(d, "claimed_scenes", None)
        if claims is None:
            if not declared:
                undeclared.append(getattr(d, "name", type(d).__name__))
        else:
            detects |= set(claims)
    return FamilyManifest(
        name=name,
        detectors=tuple(getattr(d, "name", type(d).__name__) for d in new),
        detects=frozenset(detects),
        undeclared_detectors=tuple(undeclared),
        builder_namespaces=after.families - before.families,
        builder_scenes=after.scenes - before.scenes,
        busy=busy & frozenset(detects),
        unsupported=unsupported & frozenset(detects),
        executors=after.executors - before.executors,
        verifiers=after.verifiers - before.verifiers,
        folded=dict(folded or {}),
        known_gaps=dict(known_gaps or {}),
        handoffs=handoffs,
    )


# -- core families -------------------------------------------------------------------------


def _system_flow_builds_quest_log(cfg: FamilyConfig) -> bool:
    """True when the system_flow family is registered with ``quest_log: skip`` (its default): dialog
    keeps the DETECTOR of ``system.quest_log_playback`` and system_flow builds its options."""
    if "system_flow" not in cfg.optional:
        return False
    try:
        from pokegent.actions.menu.system_flow.policy import coerce_options
    except ModuleNotFoundError:
        return False
    return coerce_options(cfg.options.get("system_flow")).quest_log == "skip"


def _register_dialog(r: Registries, cfg: FamilyConfig, bundle: FamilyBundle) -> FamilyManifest:
    from pokegent.actions.menu.dialog import (
        UNSUPPORTED_RECOGNISED,
        UNSUPPORTED_UNCLAIMED,
        register,
    )
    from pokegent.actions.menu.dialog import evidence as E

    before = _marks(r)
    handed_over = cfg.dialog_system_scenes and _system_flow_builds_quest_log(cfg)
    family = register(
        detectors=r.detectors,
        builders=r.builders,
        executors=r.executors,
        verifiers=r.verifiers,
        policy=cfg.prompt_policy,
        system_scenes=cfg.dialog_system_scenes,
        quest_log_builder=not handed_over,
    )
    bundle.dialog = family
    bundle.confirm_scene_ids |= family.confirm_scene_ids
    return _manifest(
        "dialog",
        before,
        _marks(r),
        busy=frozenset({E.MAP_TRANSITION, E.SCRIPTED_MOVEMENT}),
        unsupported=frozenset(UNSUPPORTED_RECOGNISED) - ({E.QUEST_LOG} if handed_over else set()),
        declared=family.claimed_scenes,
        known_gaps=UNSUPPORTED_UNCLAIMED,
        handoffs=frozenset({"dialog.shop_prompt"}),
    )


def _register_battle(r: Registries, cfg: FamilyConfig, bundle: FamilyBundle) -> FamilyManifest:
    from pokegent.actions.battle.builder import UNSUPPORTED_SCENES
    from pokegent.actions.battle.detect import (
        BUSY_ONLY_SCENES,
        FOLDED_SCENES,
        KNOWN_GAPS,
    )
    from pokegent.actions.battle.register import register

    before = _marks(r)
    bundle.battle = register(r.detectors, r.builders, r.executors, r.verifiers, facts=cfg.facts)
    return _manifest(
        "battle",
        before,
        _marks(r),
        busy=BUSY_ONLY_SCENES,
        unsupported=frozenset(UNSUPPORTED_SCENES),
        folded=FOLDED_SCENES,
        known_gaps=KNOWN_GAPS,
    )


_CORE = {"dialog": _register_dialog, "battle": _register_battle}


# -- optional families ---------------------------------------------------------------------


def register_optional(
    name: str, registries: Registries, config: FamilyConfig | None = None
) -> tuple[FamilyManifest, Any] | None:
    """Register ``pokegent.actions.<name>`` if that package exists, else return ``None``.

    Only a missing package named exactly like the family is tolerated; any other import error
    (a broken dependency inside the family) propagates.
    """
    config = config or FamilyConfig()
    module_name = _family_module(name)
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return None
        raise
    register = getattr(module, "register", None)
    if register is None:
        try:  # some families keep it in a ``register`` submodule (battle does)
            register = importlib.import_module(f"{module_name}.register").register
        except ModuleNotFoundError as exc:
            if exc.name != f"{module_name}.register":
                raise
            raise AttributeError(f"{module_name} has no register()") from None
    available = {
        "registries": registries,
        "detectors": registries.detectors,
        "builders": registries.builders,
        "executors": registries.executors,
        "executor_registry": registries.executors,
        "verifiers": registries.verifiers,
        "verifier_registry": registries.verifiers,
        "catalog": registries.detectors.catalog,
        "config": config.options.get(name),
        "objectives": config.objectives,
        "graph": config.graph,
        "navigation": config.navigation,
        "prompt_policy": config.prompt_policy,
    }
    signature = inspect.signature(register)
    kwargs: dict[str, Any] = {}
    positional: list[Any] = []
    for pname, param in signature.parameters.items():
        if pname in available and param.kind is not inspect.Parameter.VAR_KEYWORD:
            if param.kind is inspect.Parameter.POSITIONAL_ONLY:
                positional.append(available[pname])
            else:
                kwargs[pname] = available[pname]
        elif param.default is inspect.Parameter.empty and param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            raise TypeError(
                f"{module_name}.register needs a parameter {pname!r} that register_optional "
                f"cannot supply (known: {sorted(available)})"
            )
    before = _marks(registries)
    handle = register(*positional, **kwargs)
    manifest = _manifest(
        name,
        before,
        _marks(registries),
        busy=frozenset(getattr(module, "BUSY_SCENES", ())),
        unsupported=frozenset(getattr(module, "UNSUPPORTED_SCENES", ())),
        declared=frozenset(getattr(module, "CLAIMED_SCENES", ())),
        folded=getattr(module, "FOLDED_SCENES", None),
        known_gaps=getattr(module, "KNOWN_GAPS", None),
        handoffs=frozenset(getattr(module, "HANDOFF_SCENES", ())),
    )
    return manifest, handle


def _hand_over_quest_log(r: Registries, cfg: FamilyConfig, bundle: FamilyBundle) -> None:
    """The other half of the hand-over (see :func:`_system_flow_builds_quest_log`): system_flow
    builds the options of the scene dialog detects, and its manifest says so."""
    if not (cfg.dialog_system_scenes and _system_flow_builds_quest_log(cfg)):
        return
    family = bundle.plugins.get("system_flow")
    if family is None:  # configured but not installed (cannot happen after the import check)
        return
    from dataclasses import replace

    from pokegent.actions.menu.dialog.evidence import QUEST_LOG

    r.builders.register_scenes([QUEST_LOG], family.builder)
    manifest = bundle.manifests["system_flow"]
    bundle.manifests["system_flow"] = replace(
        manifest, builder_scenes=manifest.builder_scenes | {QUEST_LOG}
    )


def register_all(registries: Registries, config: FamilyConfig | None = None) -> FamilyBundle:
    """Register every core and every installed optional family; enforce unique scene ownership."""
    config = config or FamilyConfig()
    # Callers may construct the four registries directly instead of using ``Registries.new``.
    # Bind here as well so every runtime family is fail-closed under postcondition validation.
    registries.builders.bind_verifiers(registries.verifiers)
    bundle = FamilyBundle(registries, config, {})
    for name in config.core:
        if name not in _CORE:
            raise ValueError(f"unknown core family {name!r} (known: {sorted(_CORE)})")
        bundle.manifests[name] = _CORE[name](registries, config, bundle)
    skipped: list[str] = []
    for name in config.optional:
        if name in bundle.manifests:
            continue
        found = register_optional(name, registries, config)
        if found is None:
            skipped.append(name)
            continue
        manifest, handle = found
        bundle.manifests[name] = manifest
        bundle.plugins[name] = handle
        module = importlib.import_module(_family_module(name))
        bundle.confirm_scene_ids |= frozenset(getattr(module, "CONFIRM_SCENE_IDS", ()))
    bundle.skipped = tuple(skipped)
    _hand_over_quest_log(registries, config, bundle)
    conflicts = find_conflicts(bundle.manifests.values(), shared=config.shared_scenes)
    if conflicts:
        raise OwnershipError(
            "scene ownership is not unique: " + "; ".join(str(c) for c in conflicts)
        )
    return bundle


__all__ = [
    "CORE_FAMILIES",
    "OPTIONAL_FAMILIES",
    "FamilyBundle",
    "FamilyConfig",
    "FamilyManifest",
    "OwnershipError",
    "Registries",
    "register_all",
    "register_optional",
]
