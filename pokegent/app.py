"""The runnable system: ``build_runner`` assembles it, ``run_offline`` runs a saved state.

``build_runner`` is the one place that wires reader, composite snapshot source, detectors, builders,
executor, decider, event log and escalation sink (M2.5). Everything it needs from the outside is
injected, so tests drive it with a fake emulator and the CLI with the real one::

    emulator actor ──► CompositeSnapshotSource ──► Perceiver ──► Runner ──► EventLog
         ▲              (core+ui+map+battle+text)      │  (transient-invalid retry)
         └────────────── Executor (re-validates on a fresh snapshot) ◄── decider

``run_offline`` is what ``pokegent run`` calls: ROM check, savestate load, router loop with a
bounded number of steps, ``events.jsonl`` + ``summary.json`` in the output directory, and an exit
code (see :data:`EXIT_CODES`). It never starts a server and never talks to agent. The only network
a run can make is the System One provider of ``--decider system1``, and that refuses non-loopback
endpoints unless the caller opts in (:func:`check_system1_endpoint`).
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from pokegent.actions import (
    OPTIONAL_FAMILIES,
    FamilyBundle,
    FamilyConfig,
    Registries,
    register_all,
)
from pokegent.actions._exec.executor import Executor
from pokegent.addons import AddonConfig, AddonConfigError, AddonStartError, AddonTaps
from pokegent.events import EventLog, EventSink, EventType, MemoryEventLog
from pokegent.games.gen3.firered.reader.snapshot_sources import (
    CompositeSnapshotSource,
    MapMode,
)
from pokegent.games.gen3.firered.rom.rom_data import RomProfile
from pokegent.goals import (
    ObjectiveSession,
    ObjectiveStore,
    PlanError,
    ScriptedObjectiveSource,
)
from pokegent.goals.validate import MapDirectory
from pokegent.history import DEFAULT_KEEP, MAX_KEEP
from pokegent.loop import (
    ON_OBJECTIVE_DONE,
    EscalationAction,
    EscalationKind,
    EscalationRequest,
    EscalationResponse,
    Runner,
    RunnerConfig,
    RunStatus,
    RunSummary,
    stop_message,
)
from pokegent.navigation import MapGraph
from pokegent.options.models import Decider, FirstLegalDecider, ForcedDecider
from pokegent.pipeline import Perceiver
from pokegent.state.catalog import SceneCatalog, load_scene_catalog
from pokegent.state.scenes import SceneCoverageReport

#: process exit codes of ``pokegent run``
EXIT_OK = 0  # the step budget (or deadline) was reached without a stop
EXIT_BLOCKED = 1  # SAFE_PAUSE (invalid snapshot, unsupported ROM) or an escalation answered STOP
EXIT_USAGE = 2  # bad arguments or configuration
EXIT_PAUSED = 3  # the run waits for its agent: ESCALATED, or no objective (WAITING_OBJECTIVE)
EXIT_ENVIRONMENT = 4  # ROM / savestate / emulator problem: nothing was played
EXIT_GRACEFUL_TIMEOUT = 124  # graceful-stop deadline expired after the final-state save attempt
EXIT_FORCED_INTERRUPT = 130  # a second SIGINT/SIGTERM forced the bounded save-and-exit path

EXIT_CODES = {
    "ok": EXIT_OK,
    "blocked": EXIT_BLOCKED,
    "usage": EXIT_USAGE,
    "paused": EXIT_PAUSED,
    "environment": EXIT_ENVIRONMENT,
    "graceful_timeout": EXIT_GRACEFUL_TIMEOUT,
    "forced_interrupt": EXIT_FORCED_INTERRUPT,
}
logger = logging.getLogger(__name__)

STUB_PORTS = range(19700, 19800)
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
#: Shown when ``addons`` (the System One package, a top-level package next to ``pokegent``)
#: cannot be imported: a venv installed before ``addons*`` joined the package list only maps
#: ``pokegent``, so the entry point works only from the repository root.
ADDON_MISSING_HINT = (
    "the System One addon is not importable ({error}); the installed package does not include "
    "'addons'. Reinstall it: uv pip install --python <venv>/bin/python -e . --no-deps "
    "(check: cd /tmp && <venv>/bin/python -c 'import addons.system_one')"
)

DECIDERS = ("agent", "system1", "hybrid", "first", "forced")
DECIDER_ALIASES = ("external",)
_external_alias_warned = False


class AppError(Exception):
    """A configuration or environment problem; ``exit_code`` is what the CLI returns."""

    def __init__(self, message: str, exit_code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class NewGameConfig:
    enabled: bool = False
    gender: str = "male"

    def __post_init__(self) -> None:
        if self.gender not in ("male", "female"):
            raise AppError("new_game.gender must be male|female")


# -- configuration ---------------------------------------------------------------------------------


RUN_ID_MAX = 64  # DecisionFrame.run_id
_RUN_ID_OK = re.compile(r"^[A-Za-z0-9._-]{1,%d}$" % RUN_ID_MAX)
_RUN_ID_BAD = re.compile(r"[^A-Za-z0-9._-]+")


def safe_run_id(name: str, suffix: str = "") -> str:
    """A valid run id from a file or directory name: odd characters become ``-``, and a name that
    would not fit ``RUN_ID_MAX`` is cut so ``suffix`` (a timestamp) always survives.

    A state file name over ~45 characters used to make ``DecisionFrame.run_id`` invalid, and the
    run ended in SAFE_PAUSE at step 1.
    """
    clean = _RUN_ID_BAD.sub("-", name).strip("-.") or "run"
    tail = f"-{suffix}" if suffix else ""
    head = clean[: RUN_ID_MAX - len(tail)].rstrip("-.") or "run"
    return head + tail


@dataclass(frozen=True)
class AppConfig:
    """Everything ``build_runner`` needs besides the live objects. Strict, JSON/YAML-able."""

    run_id: str = "run"
    max_steps: int = 200
    deadline_s: float | None = None
    graceful_stop_timeout_s: float = 10.0
    restart_file: str | None = None
    goal_id: str | None = None
    #: ``None`` derives from the decider: off for external control, forced for local/system1.
    autoplay: str | None = None
    #: External-control knobs (kept separate from the decider provider settings).
    external: Mapping[str, Any] = field(default_factory=lambda: {"walk_step_limit": 8})
    #: a finished objective at the next free-roaming decision: ``hold`` (default, stay alive and
    #: poll the file), ``pause`` (stop with ``WAITING_OBJECTIVE``, exit 3) or
    on_objective_done: str = "hold"
    on_objective_review: str = "continue"
    objective_stuck_after: int = 120
    decision_history: int = DEFAULT_KEEP
    #: headless/maintenance opt-out: a satisfied objective success probe does not stop the run
    #: (``run.ignore_objective``, CLI ``--ignore-objective``; default off)
    ignore_objective: bool = False
    dex_fallback: bool = False
    story_fallback: bool = False
    story_path: str | None = None
    #: savestate checkpoints: every ``checkpoint_every`` iterations (0 = off) a state is written to
    #: ``<out>/checkpoints/`` and the newest ``checkpoint_keep`` stay; ``save_final_state`` writes
    #: ``<out>/final.state`` when the run ends, whatever the reason (the next run's ``--state``)
    checkpoint_every: int = 0
    checkpoint_keep: int = 5
    save_final_state: bool = True
    #: ``off`` | ``field`` (not in battles) | ``always``: the snapshot source's map block
    map_mode: MapMode = "field"
    #: dialog: ``escalate`` (default) or ``offer`` prompts whose effect is unknown
    unknown_prompts: str = "escalate"
    #: ``mechanics`` (Gen-3 damage/PP/catch facts from the ROM tables) or ``basic``
    facts: str = "mechanics"
    #: Maximum reason-bearing alternatives shown in dense field frames.
    max_visible_options: int = 15
    #: ``guide`` uses ROM/observed battle data; ``fair_play`` uses only moves seen this battle.
    opponent_knowledge: str = "guide"
    #: extra :class:`RunnerConfig` fields (``no_progress_limit``, ``transient_invalid_frames``, ...)
    runner: Mapping[str, Any] = field(default_factory=dict)
    #: executor knobs
    busy_wait_frames: int = 600
    busy_wait_wall_s: float = 60.0
    #: the read-only run viewer alias (``docs/addons.md``); off unless it has a port
    viewer: AddonConfig = field(default_factory=AddonConfig)
    #: split addon surfaces; ``viewer`` remains a one-release combined alias
    stream: AddonConfig = field(default_factory=AddonConfig)
    dashboard: AddonConfig = field(default_factory=AddonConfig)
    new_game: NewGameConfig = field(default_factory=NewGameConfig)
    realtime_idle_while_deciding: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.realtime_idle_while_deciding, bool):
            raise AppError("emulator.realtime_idle_while_deciding must be a boolean")
        if not _RUN_ID_OK.match(self.run_id):
            raise AppError(
                f"run_id must be 1..{RUN_ID_MAX} characters of letters, digits, '.', '_' or '-' "
                f"(got {len(self.run_id)} characters); use --run-id or a shorter --out name"
            )
        if self.autoplay == "forced":
            raise AppError("autoplay=forced was removed: the decider owns action selection")
        if self.autoplay not in (None, "off"):
            raise AppError("autoplay must be off")
        if set(self.external) - {"walk_step_limit"}:
            raise AppError("unknown external settings")
        if (
            isinstance(self.external.get("walk_step_limit", 8), bool)
            or int(self.external.get("walk_step_limit", 8)) < 1
        ):
            raise AppError("external.walk_step_limit must be >= 1")
        if (isinstance(self.decision_history, bool)
                or not isinstance(self.decision_history, int)
                or not 0 <= self.decision_history <= MAX_KEEP):
            raise AppError(f"decision_history must be an integer from 0 to {MAX_KEEP}")
        if self.on_objective_review not in ("continue", "pause", "stop"):
            raise AppError("on_objective_review must be continue|pause|stop")
        if (isinstance(self.objective_stuck_after, bool)
                or not isinstance(self.objective_stuck_after, int)
                or self.objective_stuck_after < 1):
            raise AppError("objective_stuck_after must be an integer >= 1")
        if self.story_path is not None and (not isinstance(self.story_path, str)
                                            or not self.story_path):
            raise AppError("objectives.story_path must be a nonempty path string")
        if self.story_fallback and (self.dex_fallback or self.on_objective_done != "continue"):
            raise AppError("story fallback requires continue and cannot combine with dex")
        if self.on_objective_done not in ON_OBJECTIVE_DONE:
            raise AppError(f"on_objective_done must be one of {ON_OBJECTIVE_DONE}")
        if not isinstance(self.ignore_objective, bool):
            raise AppError("ignore_objective must be true or false")
        if (
            isinstance(self.checkpoint_every, bool)
            or not isinstance(self.checkpoint_every, int)
            or self.checkpoint_every < 0
            or isinstance(self.checkpoint_keep, bool)
            or not isinstance(self.checkpoint_keep, int)
            or not 1 <= self.checkpoint_keep <= 50
            or not isinstance(self.save_final_state, bool)
        ):
            raise AppError(
                "checkpoint_every must be an integer >= 0, checkpoint_keep 1..50, "
                "save_final_state true|false"
            )
        if self.map_mode not in ("off", "field", "always"):
            raise AppError("map_mode must be off|field|always")
        if self.unknown_prompts not in ("escalate", "offer"):
            raise AppError("unknown_prompts must be escalate|offer")
        if self.facts not in ("mechanics", "basic"):
            raise AppError("facts must be mechanics|basic")
        if (
            isinstance(self.max_visible_options, bool)
            or not isinstance(self.max_visible_options, int)
            or not 1 <= self.max_visible_options <= 50
        ):
            raise AppError("field.max_visible_options must be an integer from 1 to 50")
        if self.opponent_knowledge not in ("guide", "fair_play"):
            raise AppError("battle.opponent_knowledge must be guide|fair_play")
        if self.max_steps < 1:
            raise AppError("max_steps must be >= 1")
        if (
            isinstance(self.graceful_stop_timeout_s, bool)
            or not isinstance(self.graceful_stop_timeout_s, (int, float))
            or self.graceful_stop_timeout_s <= 0
        ):
            raise AppError("graceful_stop_timeout_s must be a positive number")
        if self.restart_file is not None and not isinstance(self.restart_file, str):
            raise AppError("restart_file must be a path string or null")
        runner_fields = set(RunnerConfig.__dataclass_fields__) - {
            "run_id",
            "goal_id",
            "on_objective_done",  # a top-level setting: run.on_objective_done
            "decision_history",
        }
        unknown = sorted(set(self.runner) - runner_fields)
        if unknown:
            raise AppError(f"unknown runner settings {unknown}")

    def runner_config(self) -> RunnerConfig:
        return RunnerConfig(
            run_id=self.run_id,
            goal_id=self.goal_id,
            max_steps=self.max_steps,
            deadline_s=self.deadline_s,
            on_objective_done=self.on_objective_done,
            decision_history=self.decision_history,
            ignore_objective=self.ignore_objective,
            autoplay=self.autoplay or "off",
            **{k: v for k, v in self.runner.items() if k not in ("max_steps", "deadline_s")},
        )


_SECTIONS = {
    "objectives": {"fallback"},
    "emulator": {"realtime_idle_while_deciding"},
    "schema_version": None,
    "autoplay": None,
    "external": {"walk_step_limit"},
    "run": {
        "max_steps",
        "deadline_s",
        "graceful_stop_timeout_s",
        "restart_file",
        "goal_id",
        "on_objective_done",
        "on_objective_review",
        "objective_stuck_after",
        "decision_history",
        "ignore_objective",
        "checkpoint_every",
        "checkpoint_keep",
        "save_final_state",
        "busy_wait_frames",
        "busy_wait_wall_s",
    },
    "families": {"map_mode", "unknown_prompts", "facts"},
    "field": {"max_visible_options"},
    "battle": {"opponent_knowledge"},
    "runner": None,
    "viewer": {
        "port",
        "public_only",
        "realtime",
        "static",
        "frame_hz",
        "fps",
        "linger_s",
    },
    "stream": {
        "enabled",
        "port",
        "public_only",
        "realtime",
        "static",
        "frame_hz",
        "fps",
        "linger_s",
    },
    "dashboard": {
        "enabled",
        "port",
        "public_only",
        "realtime",
        "static",
        "frame_hz",
        "fps",
        "linger_s",
    },
    "system_one": None,  # validated by addons.system_one.policy
    "new_game": {"enabled", "gender"},
}


def config_from_mapping(raw: Mapping[str, Any], *, run_id: str = "run") -> AppConfig:
    """``run:`` / ``families:`` / ``runner:`` sections of a run config file (strict)."""
    if "recovery" in raw:
        raise AppError(
            "recovery config was removed: the runner now reports no progress and stops for "
            "supervisor review"
        )
    if "progression" in raw:
        raise AppError(
            "progression config was removed: the hand-authored story graph and derived "
            "facts are no longer provided"
        )
    if raw.get("autoplay") == "forced":
        raise AppError("autoplay was removed: forced autoplay no longer exists")
    run_raw = raw.get("run")
    if isinstance(run_raw, Mapping):
        removed = sorted(
            set(run_raw)
            & {
                "instructions",
                "route_assist",
                "route_assist_repeat",
            }
        )
        if removed:
            raise AppError(f"removed config key(s): {removed}")
    runner_raw = raw.get("runner")
    if isinstance(runner_raw, Mapping):
        removed = sorted(
            set(runner_raw)
            & {"instructions", "route_assist", "route_assist_repeat", "review_events"}
        )
        if removed:
            raise AppError(f"removed config key(s): {removed}")
    unknown = sorted(set(raw) - set(_SECTIONS))
    if unknown:
        raise AppError(f"unknown config sections {unknown}; known: {sorted(_SECTIONS)}")
    if raw.get("schema_version", 1) != 1:
        raise AppError(f"unsupported schema_version {raw.get('schema_version')!r}")
    kw: dict[str, Any] = {"run_id": run_id}
    objectives = raw.get("objectives", {})
    if not isinstance(objectives, Mapping) or set(objectives) - {"fallback", "story_path"}:
        raise AppError("objectives must be a mapping with optional fallback and story_path")
    if objectives.get("fallback", "off") not in ("off", "dex", "story"):
        raise AppError("objectives.fallback must be off|dex|story")
    kw["dex_fallback"] = objectives.get("fallback", "off") == "dex"
    kw["story_fallback"] = objectives.get("fallback", "off") == "story"
    kw["story_path"] = objectives.get("story_path")
    if kw["story_path"] is not None and (not isinstance(kw["story_path"], str)
                                         or not kw["story_path"]):
        raise AppError("objectives.story_path must be a nonempty path string")
    if raw.get("autoplay") is not None:
        kw["autoplay"] = raw["autoplay"]
    external = raw.get("external") or {}
    if not isinstance(external, Mapping):
        raise AppError("external must be a mapping")
    bad_external = sorted(set(external) - _SECTIONS["external"])
    if bad_external:
        raise AppError(f"unknown keys in external: {bad_external}")
    kw["external"] = dict(external)
    for section in ("run", "families", "battle", "emulator"):
        body = raw.get(section) or {}
        if not isinstance(body, Mapping):
            raise AppError(f"{section} must be a mapping")
        bad = sorted(set(body) - _SECTIONS[section])
        if bad:
            raise AppError(f"unknown keys in {section}: {bad}")
        kw.update(body)
    field_options = raw.get("field") or {}
    if not isinstance(field_options, Mapping):
        raise AppError("field must be a mapping")
    bad_field = sorted(set(field_options) - _SECTIONS["field"])
    if bad_field:
        raise AppError(f"unknown field keys: {bad_field}")
    if "max_visible_options" in field_options:
        kw["max_visible_options"] = field_options["max_visible_options"]
    new_game = raw.get("new_game") or {}
    if not isinstance(new_game, Mapping):
        raise AppError("new_game must be a mapping")
    bad_new_game = sorted(set(new_game) - _SECTIONS["new_game"])
    if bad_new_game:
        raise AppError(f"unknown keys in new_game: {bad_new_game}")
    kw["new_game"] = NewGameConfig(**dict(new_game))
    runner = raw.get("runner") or {}
    if not isinstance(runner, Mapping):
        raise AppError("runner must be a mapping")
    kw["runner"] = dict(runner)
    viewer = raw.get("viewer") or {}
    if not isinstance(viewer, Mapping):
        raise AppError("viewer must be a mapping")
    bad = sorted(set(viewer) - _SECTIONS["viewer"])
    if bad:
        raise AppError(f"unknown keys in viewer: {bad}")
    kw["viewer"] = AddonConfig(**viewer)
    for addon in ("stream", "dashboard"):
        body = raw.get(addon) or {}
        if not isinstance(body, Mapping):
            raise AppError(f"{addon} must be a mapping")
        bad = sorted(set(body) - _SECTIONS[addon])
        if bad:
            raise AppError(f"unknown keys in {addon}: {bad}")
        addon_body = dict(body)
        enabled = addon_body.pop("enabled", None)
        if enabled is False:
            addon_body["port"] = None
        elif enabled is True and addon_body.get("port") is None:
            raise AppError(f"{addon}.port is required when {addon}.enabled is true")
        kw[addon] = AddonConfig(**addon_body)
    return AppConfig(**kw)


def load_config_file(path: str | os.PathLike[str] | None) -> dict[str, Any]:
    if path is None:
        new_path, old_path = Path("pokegent.yaml"), Path("pokejev.yaml")
        if new_path.is_file() and old_path.is_file():
            raise AppError("both pokegent.yaml and deprecated pokejev.yaml exist")
        if new_path.is_file():
            path = new_path
        elif old_path.is_file():
            print("deprecated: pokejev.yaml is an alias for pokegent.yaml", file=sys.stderr)
            path = old_path
    if path is None:
        return {}
    try:
        data = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AppError(f"cannot read config {path}: {type(exc).__name__}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise AppError("the config file must be a mapping")
    if "recovery" in data:
        raise AppError(
            "recovery config was removed: the runner now reports no progress and stops for "
            "supervisor review"
        )
    legacy_field = data.get("field")
    is_legacy_field = isinstance(legacy_field, dict) and set(legacy_field) <= {"menus", "teach_tm"}
    if (
        any(
            key in data
            for key in (
                "rom_path",
                "decider",
                "system1",
                "external",
                "hybrid",
                "battle",
                "output",
            )
        )
        or is_legacy_field
    ):
        from pokegent.user_config import load_user_config, to_legacy_config

        if "system1" in data and "system_one" in data:
            raise AppError("config has both system1 and system_one; keep one System One section")
        try:
            out = to_legacy_config(load_user_config(path))
        except ValueError as exc:
            raise AppError(str(exc)) from None
        # Sections the user-config format does not own (system_one, stream, dashboard, field, ...)
        # pass through unchanged instead of being dropped silently.
        for key, value in data.items():
            if key in _USER_CONFIG_KEYS:
                continue
            if key not in out:
                out[key] = value
            elif key == "runner" and isinstance(out["runner"], dict) and isinstance(value, dict):
                clash = sorted(set(value) & set(out["runner"]))
                if clash:
                    raise AppError(f"runner keys set twice (runner and system1): {clash}")
                out["runner"] = {**value, **out["runner"]}
            else:
                raise AppError(
                    f"config section {key!r} is set twice (directly and via system1 / SYSTEM1_* "
                    "environment); keep one"
                )
        return out
    return data


_USER_CONFIG_KEYS = frozenset(
    {
        "objectives",
        "rom_path",
        "decider",
        "system1",
        "external",
        "hybrid",
        "battle",
        "output",
        "autoplay",
        "field",
    }
)


# -- escalation sink -----------------------------------------------------------------------


class RecordingSink:
    """Keeps (bounded) every escalation request for ``summary.json``; answers a fixed action."""

    def __init__(
        self,
        action: str = "pause",
        keep: int = 200,
        review_action: str = "continue",
    ) -> None:
        self._action = EscalationAction(action)
        self._review_action = EscalationAction(review_action)
        self._keep = keep
        self.requests: list[EscalationRequest] = []
        self.total = 0

    def escalate(self, request: EscalationRequest) -> EscalationResponse:
        self.total += 1
        if len(self.requests) < self._keep:
            self.requests.append(request)
        action = (self._review_action if request.kind is EscalationKind.OBJECTIVE_REVIEW
                  else self._action)
        return EscalationResponse(action, f"recording sink: {action.value}")

    def as_dicts(self) -> list[dict[str, Any]]:
        out = []
        for r in self.requests:
            row: dict[str, Any] = {
                "step": r.step,
                "kind": r.kind.value,
                "scene": r.scene_id,
                "error_code": r.error_code.value if r.error_code else None,
            }
            out.append(row)
        return out


# -- assembling the system ----------------------------------------------------------------


@dataclass
class RunnerApp:
    """The assembled system. ``runner.run()`` plays; the rest is for inspection."""

    runner: Runner
    executor: Executor
    perceiver: Perceiver
    source: CompositeSnapshotSource
    bundle: FamilyBundle
    events: EventSink
    sink: RecordingSink
    coverage: SceneCoverageReport
    config: AppConfig
    #: the objective store + tracker + script behind the run (``None`` without ``objectives=``)
    objectives: ObjectiveSession | None = None

    def run(self) -> RunSummary:
        return self.runner.run()


def build_facts(
    profile: RomProfile,
    rom_path: str | os.PathLike[str] | None,
    kind: str,
    *,
    opponent_knowledge: str = "guide",
):
    """The battle family's fact provider: Gen-3 mechanics from the ROM (``None`` = BasicFacts)."""
    if kind == "basic" or rom_path is None:
        return None
    from pokegent.games.gen3.adapters.battle_facts import MechanicsFacts

    return MechanicsFacts.from_rom_file(
        rom_path,
        profile_id=profile.profile_id,
        opponent_knowledge=opponent_knowledge,
    )


def build_runner(
    *,
    actor: Any,
    profile: RomProfile,
    rom_path: str | os.PathLike[str] | None = None,
    events: EventSink,
    decider: Decider | None = None,
    config: AppConfig | None = None,
    facts: Any = None,
    catalog: SceneCatalog | None = None,
    sink: RecordingSink | None = None,
    coverage: SceneCoverageReport | None = None,
    clock=time.monotonic,
    objectives: str | os.PathLike[str] | ObjectiveStore | ObjectiveSession | None = None,
    objective_plan: str | os.PathLike[str] | Sequence[Mapping[str, Any]] | None = None,
    objective_directory: MapDirectory | None = None,
    objective_graph: MapGraph | None = None,
    objective_navigation: Any = None,
    sleep=time.sleep,
) -> RunnerApp:
    """Wire reader, composite source, detectors, builders, executor, decider, events, sink.

    ``actor`` is an ``EmulatorActor`` (or anything with its input/read surface); ``decider``
    defaults to the deterministic test stub ``FirstLegalDecider`` (never a policy).

    ``objectives`` (contracts v1.3) is the objective file (path), an :class:`ObjectiveStore` or a
    ready :class:`ObjectiveSession`: the goal is then read every iteration, the field family gets
    the session's objective, and the tracker raises ``progress.*`` and review requests.
    ``objective_plan`` is a scripted queue (``docs/objective-file.md`` section 9) written through
    the same store; without ``objectives`` its file is ``objective.json`` in the working directory,
    so give the file explicitly.
    """
    config = config or AppConfig()
    shop_observation: Callable[[], Any] | None = None
    if actor is not None and profile is not None:
        from pokegent.games.gen3.firered.reader.live import LiveReader
        from pokegent.goals.shop_admission import ShopObservation

        observation_reader = LiveReader(actor, profile, run_id=config.run_id)

        def shop_observation() -> ShopObservation | None:
            """Fresh core-only read from the same actor; never a cached decision snapshot."""
            try:
                snapshot = observation_reader.read().snapshot
            except Exception:  # an optional admission fact read stays UNKNOWN
                return None
            if not snapshot.usable or snapshot.rom_profile != profile.profile_id:
                return None
            return ShopObservation.from_snapshot(snapshot, rom_sha1=profile.rom.sha1)

    session = make_objective_session(
        objectives,
        objective_plan,
        directory=objective_directory,
        dex_fallback=config.dex_fallback,
        story_fallback=config.story_fallback,
        story_path=config.story_path,
        rom_path=rom_path,
        stuck_after=config.objective_stuck_after,
        shop_observation=shop_observation,
    )
    bind_planning = getattr(decider, "bind_planning", None)
    if callable(bind_planning) and session is not None:
        bind_planning(session.store)
    goal_source = session
    objective_source = session.objective_source() if session is not None else None
    catalog = catalog or load_scene_catalog()
    registries = Registries.new(catalog)
    from pokegent.actions.menu.dialog import PromptPolicy
    from pokegent.actions.menu.system_flow.symbols import PRE_GAME_CALLBACKS
    from pokegent.games.gen3.firered.reader.menu_rom import FireRedMenuResolver

    menu_resolver = FireRedMenuResolver(actor, profile)
    tables = None
    if rom_path is not None:
        try:
            from pokegent.games.gen3.firered.rom.rom_tables import GameTables

            tables = GameTables.from_rom_file(rom_path)
        except Exception as exc:
            logger.warning("ROM tables unavailable for %s: %s", rom_path, exc)
    if session is not None and tables is not None:
        session.tracker.tables = tables
    elif session is not None and rom_path is None:
        objective = session.store.current()
        if objective is not None and objective.kind == "heal":
            logger.warning("heal objective max PP unknown: no ROM tables (rom_path is None)")

    def resolve_menu_entries(snapshot, menu_id, count):
        entries = menu_resolver.multichoice(snapshot, menu_id, count)
        resolved_entries = (
            tuple((entry.stable_key, entry.label) for entry in entries) if entries else None
        )
        if objective_navigation is not None and session is not None:
            resolved_entries = objective_navigation.ferry_menu_entries(
                snapshot,
                session.store.resolved(),
                count,
                resolved_entries,
                menu_id=menu_id,
            )
        return resolved_entries

    # The field family cannot work without the map block: with map_mode "off" it is not registered
    # (a field.free claim without a map would only be a bounded wait that starves the executors'
    # post-step budgets of the other families).
    optional = tuple(
        name for name in OPTIONAL_FAMILIES if not (name == "field" and config.map_mode == "off")
    )
    bundle = register_all(
        registries,
        FamilyConfig(
            optional=optional,
            prompt_policy=PromptPolicy(  # type: ignore[arg-type]
                unknown_prompts=config.unknown_prompts,
                menu_entries=resolve_menu_entries,
                tables=tables,
            ),
            facts=facts,
            objectives=objective_source,
            graph=objective_graph,
            navigation=objective_navigation,
            options={
                "system_flow": {
                    "new_game": config.new_game.__dict__,
                    "menus": menu_resolver,
                    "rom_path": str(rom_path) if rom_path is not None else None,
                },
                "field": {
                    "walk_step_limit": (
                        int(config.external.get("walk_step_limit", 8))
                        if bool(getattr(decider, "external_control", False))
                        else None
                    ),
                },
            },
        ),
    )
    field_family = bundle.plugins.get("field")
    allow_unknown_scene = getattr(decider, "escalate_unknown_scene", False) is True
    if allow_unknown_scene:
        from pokegent.actions.unknown import register_unknown_recovery

        register_unknown_recovery(registries)
    world = getattr(getattr(field_family, "builder", None), "world", None)
    source = CompositeSnapshotSource(
        actor,
        profile,
        sidecar=bundle.sidecar,
        run_id=config.run_id,
        map_mode=config.map_mode,
    )
    _external_control = bool(getattr(decider, "external_control", False))
    effective_autoplay = "off"
    runner_config = replace(
        config.runner_config(),
        autoplay=effective_autoplay,
    )
    coverage = coverage if coverage is not None else SceneCoverageReport()
    perceiver = Perceiver(
        source,
        registries.detectors,
        registries.builders,
        run_id=config.run_id,
        goal_id=config.goal_id,
        goal=goal_source,
        catalog=catalog,
        confirm=bundle.confirm_policy(catalog) if runner_config.confirm_scenes else None,
        coverage=coverage,
        transient_retries=runner_config.transient_invalid_frames,
        max_visible_options=config.max_visible_options,
        tables=tables,
        world=world,
        pre_game_callbacks=PRE_GAME_CALLBACKS if config.new_game.enabled else None,
        allow_unknown_scene=allow_unknown_scene,
    )
    executor = Executor(
        actor,
        registries.executors,
        registries.verifiers,
        perceiver,
        clock=clock,
        busy_wait_frames=config.busy_wait_frames,
        busy_wait_wall_s=config.busy_wait_wall_s,
    )
    interrupt_requested = getattr(decider, "interrupt_requested", None)
    clear_interrupt = getattr(decider, "clear_interrupt", None)
    if callable(interrupt_requested):
        executor.bind_interrupt(
            interrupt_requested,
            clear_interrupt if callable(clear_interrupt) else None,
            macro_started=getattr(decider, "macro_started", None),
            macro_finished=getattr(decider, "macro_finished", None),
        )
    if sink is None:
        sink = RecordingSink(review_action=config.on_objective_review)
    runner = Runner(
        decider=decider or FirstLegalDecider(),
        executor=executor,
        events=events,
        perceiver=perceiver,
        catalog=catalog,
        escalation=sink,
        clock=clock,
        config=runner_config,
        coverage=coverage,
        objectives=session,
        sleep=sleep,
        game_clear=getattr(bundle.plugins.get("system_flow"), "game_clear_watch", None),
    )
    return RunnerApp(
        runner, executor, perceiver, source, bundle, events, sink, coverage, config, session
    )


def make_objective_session(
    objectives: str | os.PathLike[str] | ObjectiveStore | ObjectiveSession | None,
    plan: str | os.PathLike[str] | Sequence[Mapping[str, Any]] | None = None,
    *,
    directory: MapDirectory | None = None,
    dex_fallback: bool = False,
    story_fallback: bool = False,
    story_path: str | os.PathLike[str] | None = None,
    rom_path: str | os.PathLike[str] | None = None,
    stuck_after: int = 120,
    shop_observation: Callable[[], Any] | None = None,
) -> ObjectiveSession | None:
    """``None`` (goal-less / fixed ``goal_id``), or the session over the file, store or session."""
    if dex_fallback and story_fallback:
        raise AppError("story fallback cannot combine with dex")
    if objectives is None:
        if dex_fallback or story_fallback:
            raise AppError("fallback needs the objective file it writes through")
        if plan is not None:
            raise AppError("an objective plan needs the objective file it writes through")
        return None
    if isinstance(objectives, ObjectiveSession):
        session = objectives
        store = session.store
    else:
        store = (
            objectives
            if isinstance(objectives, ObjectiveStore)
            else ObjectiveStore(Path(objectives).expanduser(), directory=directory)
        )
        session = None
    if store.directory is None and directory is not None:
        store.directory = directory
    if shop_observation is not None:
        store.bind_observation(shop_observation)
    if not store.path.parent.is_dir():
        raise AppError(f"the objective file's directory does not exist: {store.path.parent}")
    script = None
    if plan is not None:
        try:
            script = ScriptedObjectiveSource(store, plan)
        except PlanError as exc:
            raise AppError(f"objective plan: {exc}") from None
    fallback = None
    if dex_fallback:
        from pokegent.games.gen3.firered.rom.wild import load_wild_table
        from pokegent.goals.dex_fallback import DexFallback

        try:
            fallback = DexFallback(load_wild_table())
        except (OSError, ValueError, RuntimeError, TypeError):
            raise AppError("dex fallback requires valid private ROM encounter data",
                           EXIT_ENVIRONMENT) from None
    from pokegent.goals.story import StoryFallback

    if story_fallback or (not dex_fallback and session is not None
                          and isinstance(session.fallback, StoryFallback)):
        from pokegent.goals.story import load_progression
        from pokegent.goals.story_identity import verify_identity

        try:
            if story_fallback:
                fallback = load_progression(
                    path=Path(story_path).expanduser() if story_path else None,
                    directory=store.directory,
                )
            else:
                fallback = session.fallback
            verify_identity(fallback.graph, rom_path)
        except (ValueError, TypeError):
            raise AppError("story fallback requires valid private progression data",
                           EXIT_ENVIRONMENT) from None
    if session is None:
        return ObjectiveSession(store, script=script, fallback=fallback, stuck_after=stuck_after)
    if fallback is not None:
        session.fallback = fallback
    if script is not None:
        session.script = script
    return session


# -- the System One decider and its safety rule -------------------------------------------


def check_system1_endpoint(url: str | None, *, allow_remote: bool) -> list[str]:
    """Refuse a non-loopback System One endpoint unless ``allow_remote``; returns warnings.

    A loopback port outside the stub range 19700-19799 is allowed but warned about: it may be the
    shared System One endpoint; call only endpoints you are authorized to use.
    """
    if not url:
        raise AppError(
            "--decider system1 needs system_one.provider_url (config or SYSTEM1_BASE_URL)"
        )
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    warnings: list[str] = []
    if host not in LOOPBACK_HOSTS:
        if not allow_remote:
            raise AppError(
                f"refusing the non-loopback System One endpoint {host!r}: pass --allow-remote "
                "to authorize calls to your configured remote System One endpoint"
            )
        warnings.append(f"remote System One endpoint {host!r}: real model calls are made")
    elif parts.port not in STUB_PORTS:
        warnings.append(
            f"loopback port {parts.port} is not in the stub range 19700-19799: this may be the "
            "shared System One endpoint (real model calls)"
        )
    return warnings


def build_decider(
    kind: str,
    *,
    config_raw: Mapping[str, Any],
    events: EventSink,
    out_dir: Path | None,
    allow_remote: bool = False,
    run_id: str | None = None,
    external_port: int = 19760,
    external_timeout: float = 300.0,
    autoplay: str | None = None,
) -> tuple[Decider, list[str], Any]:
    """``(decider, warnings, closer)``: ``first``/``forced`` are local, ``system1`` is System One.

    ``calibration.log_path`` may contain the placeholder ``<run-id>``: with an ``out_dir`` the log
    lands inside the run directory (``out_dir / <file name>``), without one the placeholder expands
    to ``run_id``; neither known is an :class:`AppError`, never a directory literally named
    ``<run-id>`` (``addons.system_one.policy.resolve_log_path``).
    """
    global _external_alias_warned
    from pokegent.env import reject_removed_env

    try:
        reject_removed_env()
    except ValueError as exc:
        raise AppError(str(exc)) from None
    warnings: list[str] = []
    if kind == "external":
        kind = "agent"
        if not _external_alias_warned:
            logger.warning("--decider external is deprecated; use --decider agent instead")
            _external_alias_warned = True
    if kind == "first":
        return FirstLegalDecider(), [], None
    if kind == "forced":
        return ForcedDecider(), [], None
    if kind == "agent":
        from pokegent.server import ExternalDecider

        try:
            decider = ExternalDecider(
                external_port,
                external_timeout,
                autoplay=autoplay or "off",
            )
        except (OSError, ValueError) as exc:
            raise AppError(f"agent decider: {exc}") from None
        return (
            decider,
            [
                f"agent decider listening on 127.0.0.1:{decider.port}; "
                f"connect with `pokegent step --port {decider.port}`",
            ],
            decider.close,
        )
    if kind == "hybrid":
        from addons.system_one.hybrid import HybridDecider

        system_one = config_raw.get("system_one")
        system_one_env = bool(os.environ.get("SYSTEM1_BASE_URL"))
        configured = system_one_env
        if isinstance(system_one, Mapping):
            primary = system_one.get("primary", "http")
            endpoint = system_one.get("provider_url") or system_one.get("base_url")
            configured = (
                primary == "mock"
                or bool(endpoint and not str(endpoint).startswith("$"))
                or system_one_env
            )
        if not configured:
            return build_decider(
                "agent",
                config_raw=config_raw,
                events=events,
                out_dir=out_dir,
                allow_remote=allow_remote,
                run_id=run_id,
                external_port=external_port,
                external_timeout=external_timeout,
                autoplay=autoplay,
            )
        if isinstance(system_one, Mapping) and system_one.get("enabled") is False:
            return build_decider(
                "agent",
                config_raw=config_raw,
                events=events,
                out_dir=out_dir,
                allow_remote=allow_remote,
                run_id=run_id,
                external_port=external_port,
                external_timeout=external_timeout,
                autoplay=autoplay,
            )
        system1, warnings, system1_close = build_decider(
            "system1",
            config_raw=config_raw,
            events=events,
            out_dir=out_dir,
            allow_remote=allow_remote,
            run_id=run_id,
            external_port=external_port,
            external_timeout=external_timeout,
        )
        settings = config_raw.get("hybrid") or {}
        try:
            decider = HybridDecider(
                system1,
                port=external_port,
                external_timeout=external_timeout,
                agent_handoff_timeout_s=float(
                    settings.get("agent_handoff_timeout_s", 300.0)
                ),
                autoplay=autoplay or "off",
                escalate_below_confidence=settings.get("escalate_below_confidence", 0.35),
                override_window_s=float(settings.get("override_window_s", 0.0)),
                escalate_on_no_progress=settings.get("escalate_on_no_progress", True),
                escalate_unknown_scene=settings.get("escalate_unknown_scene", True),
                force_agent_after_repeats=settings.get("force_agent_after_repeats", 2),
                events=events,
            )
        except (OSError, ValueError) as exc:
            if system1_close:
                system1_close()
            raise AppError(f"hybrid decider: {exc}") from None
        warnings = [*warnings, f"hybrid agent bridge listening on 127.0.0.1:{decider.port}"]
        return decider, warnings, decider.close
    if kind != "system1":
        raise AppError(f"unknown decider {kind!r}; choose from {DECIDERS}")
    try:
        from addons.system_one.check import env_config
        from addons.system_one.factory import build_decider as build_system_one
        from addons.system_one.policy import ConfigError, load_config, resolve_log_path
    except ImportError as exc:
        raise AppError(ADDON_MISSING_HINT.format(error=f"{type(exc).__name__}: {exc}")) from None

    try:
        loaded = load_config(raw_system_one(config_raw) or env_config())
    except ConfigError as exc:
        raise AppError(str(exc)) from None
    cfg = loaded.config
    if not cfg.enabled or cfg.primary != "http":
        raise AppError("--decider system1 needs system_one.enabled with primary 'http'")
    warnings = [
        *warnings,
        *loaded.warnings,
        *check_system1_endpoint(cfg.provider_url, allow_remote=allow_remote),
    ]
    calibration = None
    if cfg.calibration.log_path is None:
        if out_dir is not None:
            calibration = out_dir / "calibration.jsonl"
    else:
        try:
            calibration = resolve_log_path(cfg.calibration.log_path, out_dir=out_dir, run_id=run_id)
        except ValueError as exc:
            raise AppError(str(exc)) from None
    decider = build_system_one(cfg, events=events, emit_events=False, calibration_path=calibration)
    return decider, warnings, getattr(decider, "close", None)


def raw_system_one(config_raw: Mapping[str, Any]) -> dict[str, Any] | None:
    section = config_raw.get("system_one")
    if section is None:
        return None
    return {"schema_version": 1, "system_one": section}


# -- one offline run ----------------------------------------------------------------------


@dataclass
class RunReport:
    exit_code: int
    stop_reason: str
    summary: dict[str, Any]
    out_dir: Path | None


def exit_code_for(status: RunStatus) -> int:
    if status in (RunStatus.MAX_STEPS, RunStatus.DEADLINE, RunStatus.INTERRUPTED):
        return EXIT_OK
    if status in (RunStatus.PAUSED, RunStatus.WAITING_OBJECTIVE, RunStatus.ESCALATED):
        return EXIT_PAUSED
    return EXIT_BLOCKED  # SAFE_PAUSE, STOPPED


def summarize(
    app: RunnerApp,
    summary: RunSummary,
    events: MemoryEventLog | EventLog,
    *,
    decider: str,
    state: str | None,
    wall_s: float,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``summary.json`` document: counts, coverage, escalations, stuck episodes, scenes."""
    from pokegent.events import read_events

    if isinstance(events, EventLog):
        envelopes = list(read_events(events.directory, events.basename, skip_corrupt=True))
    else:
        envelopes = list(events.events)
    scenes = Counter(e.scene_id for e in envelopes if e.type == EventType.SCENE_ENTERED.value)
    inputs = sum(1 for e in envelopes if e.type == EventType.ACTION_INPUT.value)
    stuck = [
        {"seq": e.seq, "scene": e.scene_id, "outcome": e.outcome, **dict(e.details)}
        for e in envelopes
        if e.type == EventType.PROGRESS_STUCK.value
    ]
    rejected = [
        {"seq": e.seq, "scene": e.scene_id, "error_code": e.details.get("error_code")}
        for e in envelopes
        if e.type == EventType.OPTION_REJECTED.value and e.details.get("source") != "decider"
    ]
    c = summary.counters
    coverage = summary.coverage
    report = app.bundle.ownership(app.runner.perceiver.catalog).to_dict()
    doc: dict[str, Any] = {
        "run_id": app.config.run_id,
        "state": state,
        "decider": decider,
        "stop_reason": summary.status.value,
        "exit_code": exit_code_for(summary.status),
        "steps": summary.iterations,
        "max_steps": app.config.max_steps,
        "last_scene": summary.last_scene,
        # FLAG_SYS_GAME_CLEAR was seen (event ``progress.game_cleared``, once per run); the
        # supervisor ops/pokegent_live.sh still decides 'clear' from the objective block
        "game_cleared": summary.game_cleared,
        "verdict": summary.verdict,
        "stop_details": summary.stop_details,
        "wall_seconds": round(wall_s, 3),
        "actions": {
            "applied": c["actions_applied"],
            "verified": c["actions_verified"],
            "rejected": c["actions_rejected"],
            "interrupted": c["actions_interrupted"],
            "failed": c["actions_failed"],
            "escalated": c["escalations"],
            "forced": c["forced"],
            "decided": c["decided"],
            "agent_frames": c.get("agent_frames", 0),
            "inputs": inputs,
        },
        "counters": c,
        "coverage": coverage,
        "scenes_entered": dict(sorted(scenes.items())),
        "unknown_scenes": {
            "unknown": c["unknown_scenes"],
            "unsupported": c["unsupported_scenes"],
            "scene_ids": sorted(
                s
                for s, v in coverage["by_scene"].items()
                if v.get("unknown") or v.get("unsupported")
            ),
        },
        "stuck_episodes": stuck,
        "rejected": rejected,
        "escalations": app.sink.as_dicts(),
        "escalations_total": app.sink.total,
        "families": {
            "registered": list(app.bundle.manifests),
            "skipped": list(app.bundle.skipped),
            "p0_gaps": report["totals"]["gaps"],
        },
        "events": {"count": len(envelopes)},
    }
    if app.objectives is not None:
        doc["review_pending"] = summary.review_pending
        doc["objective"] = summary.objective or app.objectives.snapshot()
        doc["objective_events"] = {
            name: sum(1 for e in envelopes if e.type == etype and e.source == "objective")
            for name, etype in (
                ("goal_changed", EventType.PROGRESS_GOAL_CHANGED.value),
                ("milestone_completed", EventType.PROGRESS_MILESTONE_COMPLETED.value),
                ("stuck", EventType.PROGRESS_STUCK.value),
            )
        }
    doc.update(extra or {})
    return doc


class StateRefused(RuntimeError):
    """The reader does not trust the loaded state (ROM header or an invariant failed)."""


def startup_snapshot_allowed(snapshot: Any, scene_id: str | None) -> bool:
    """Return whether a startup snapshot is safe for the selected launch mode.

    A normal saved-state launch still requires a usable core. Only an explicitly identified
    pre-game scene may run with FireRed's not-yet-created avatar during ``--new-game``.
    """
    from pokegent.actions.menu.system_flow.symbols import PRE_GAME_CALLBACKS

    ui = getattr(snapshot, "ui", None)
    callback = getattr(getattr(getattr(ui, "main", None), "callback2", None), "name", None)
    return bool(getattr(snapshot, "usable", False)) or bool(
        scene_id is not None and getattr(ui, "valid", False) and callback in PRE_GAME_CALLBACKS
    )


@contextmanager
def open_emulator(
    rom_path: str | os.PathLike[str], *, fps: float | None = None,
    realtime_idle_while_deciding: bool = False,
) -> Iterator[tuple[Any, RomProfile]]:
    """The real headless emulator behind its single-writer actor, plus the ROM's profile.

    Raises ``UnsupportedRomError`` for a ROM no profile knows. The ROM is read-only, never copied.
    ``fps`` paces every frame to ``1/fps`` wall seconds (``--realtime``) but only while a command
    needs frames (``idle_frames=False``): the game advances exactly as in an unpaced run, only
    slower, so a human can watch it and the run stays replayable. Opt-in decision idle uses
    60 fps when no addon pacing was selected; only provider waits gain extra no-input frames.
    """
    from pokegent.emulator import EmuClock, EmulatorActor, PyGBAEmulator
    from pokegent.games.gen3.firered.rom.rom_data import (
        identify_rom_file,
        load_profiles,
        require_supported,
    )

    profiles = load_profiles()
    profile = require_supported(identify_rom_file(rom_path, profiles), profiles)
    emulator = PyGBAEmulator()
    emulator.load(str(rom_path))
    if realtime_idle_while_deciding and fps is None:
        fps = 60.0
    clock = EmuClock(emulator, fps=fps, idle_frames=False) if fps else None
    with EmulatorActor(
        emulator, clock=clock, realtime_idle_while_deciding=realtime_idle_while_deciding
    ) as actor:
        yield actor, profile


class _StateWriter:
    """Checkpoints during a run and the final state after it (``AppConfig.checkpoint_*``).

    Normal saves run between iterations. Emergency saves hold the actor's exclusive submission
    guard across save, validation load and reader checks. A failed save is reported
    (``runtime.degraded``) and never stops the game.
    """

    def __init__(
        self,
        actor: Any,
        app: RunnerApp,
        config: AppConfig,
        state_dir: Any,
        profile: RomProfile | None = None,
    ) -> None:
        self.actor, self.app, self.config = actor, app, config
        self.profile = profile
        self.dir = Path(state_dir) if state_dir is not None else None
        self.checkpoints: list[str] = []
        self.final: str | None = None
        self.errors = 0
        self._lock = threading.Lock()
        if self.dir is not None and config.checkpoint_every:
            (self.dir / "checkpoints").mkdir(parents=True, exist_ok=True)
            app.runner.after_iteration.append(self._maybe_checkpoint)

    def _save(self, path: Path) -> bool:
        exclusive = getattr(self.actor, "exclusive", None)
        guard = exclusive() if callable(exclusive) else nullcontext()
        with guard:
            return self._save_exclusive(path)

    def _save_exclusive(self, path: Path) -> bool:
        # Keep the currently published state untouched until the bytes have
        # been restored and passed the same checks used at startup.  The actor
        # performs the serialization on its emulator thread, so an in-flight
        # input macro cannot tear the core state; this second boundary protects
        # against a coherent but semantically invalid state.
        pending: Path | None = None
        restore: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, restore_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".restore", dir=str(path.parent)
            )
            os.close(fd)
            restore = Path(restore_name)
            self.actor.save_state(str(restore))
            fd, pending_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".pending", dir=str(path.parent)
            )
            os.close(fd)
            pending = Path(pending_name)
            self.actor.save_state(str(pending))
            if self.profile is not None:
                from pokegent.games.gen3.firered.reader.live import LiveReader

                self.actor.load_state(str(pending))
                check = LiveReader(self.actor, self.profile).after_state_load(path.stem)
                if not check.ok:
                    failures = "; ".join(check.conformance.get("failures", ()))
                    raise StateRefused(
                        f"reader refused newly written {path.name}: {failures or 'ROM header'}"
                    )
            os.replace(pending, path)
            pending = None
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
            return True
        except Exception as exc:
            if pending is not None:
                pending.unlink(missing_ok=True)
            if restore is not None:
                try:
                    self.actor.load_state(str(restore))
                except Exception as restore_exc:
                    exc = RuntimeError(f"{exc}; could not restore pre-save state: {restore_exc}")
            self.errors += 1
            self.app.runner._emit(
                EventType.RUNTIME_DEGRADED,
                details={"component": "checkpoint", "reason": f"{type(exc).__name__}: {exc}"[:200]},
            )
            return False
        finally:
            if restore is not None:
                restore.unlink(missing_ok=True)

    def _maybe_checkpoint(self, iteration: int) -> None:
        if self.dir is None or iteration % self.config.checkpoint_every:
            return
        with self._lock:
            path = self.dir / "checkpoints" / f"ckpt-{iteration:07d}.state"
            if self._save(path):
                self.checkpoints.append(path.name)
                while len(self.checkpoints) > self.config.checkpoint_keep:
                    old = self.dir / "checkpoints" / self.checkpoints.pop(0)
                    old.unlink(missing_ok=True)

    def finish(self) -> bool:
        """Publish the final state once; concurrent watchdog/main calls serialize here."""
        if self.dir is None or not self.config.save_final_state:
            return False
        with self._lock:
            if self.final is not None:
                return True
            self.dir.mkdir(parents=True, exist_ok=True)
            if self._save(self.dir / "final.state"):
                self.final = "final.state"
                return True
            return False

    def report(self) -> dict[str, Any]:
        if self.dir is None:
            return {}
        return {
            "states": {
                "final": self.final,
                "checkpoints": list(self.checkpoints),
                "errors": self.errors,
            }
        }


class StopSignals:
    """Minimal process signal bridge: handlers only assign flags.

    Cancellation, logging, saving and exiting belong to :class:`StopCoordinator`, never to a
    Python signal handler where the interrupted main thread may already hold their locks.
    """

    def __init__(self) -> None:
        self.requested = False
        self.forced = False
        self._old: dict[int, Any] = {}

    @property
    def installed(self) -> bool:
        return bool(self._old)

    def __enter__(self) -> StopSignals:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self._old[sig] = signal.signal(sig, signal.SIG_DFL)
            except ValueError:  # not the main thread (tests, embedding): no handlers
                self._old.clear()
                break
        return self

    def __exit__(self, *exc: object) -> None:
        for sig, old in self._old.items():
            signal.signal(sig, old)
        self._old.clear()

    def enable_graceful(self) -> None:
        """Use flag-only handlers while a coordinator is able to consume them."""
        for sig in self._old:
            signal.signal(sig, self._handle)

    def enable_hard_exit(self) -> None:
        """Use OS-default termination when no coordinator can consume flags."""
        for sig in self._old:
            signal.signal(sig, signal.SIG_DFL)

    def _handle(self, _signum: int, _frame: Any) -> None:
        if self.requested:
            self.forced = True
        else:
            self.requested = True


class StopCoordinator:
    """Cancel work outside the handler and enforce bounded, serialized final-state saves."""

    SAVE_EXIT_BUDGET_S = 1.0
    EVENT_CLOSE_BUDGET_S = 0.5
    POLL_S = 0.01

    def __init__(
        self,
        signals: StopSignals,
        runner: Any,
        writer: _StateWriter,
        *,
        timeout_s: float,
        restart_file: str | None,
        event_sink: EventSink | None = None,
        log: Callable[[str], None] = print,
        exit_process: Callable[[int], Any] = os._exit,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.signals = signals
        self.runner = runner
        self.writer = writer
        self.timeout_s = float(timeout_s)
        self.restart_file = Path(restart_file) if restart_file else None
        self.event_sink = event_sink
        self.log = log
        self._exit_process = exit_process
        self._clock = clock
        self._shutdown = threading.Event()
        self._complete = threading.Event()
        self._restart_requested = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.signals.enable_graceful()
        if self.restart_file is not None:
            try:
                self.restart_file.unlink(missing_ok=True)
            except OSError as exc:
                self.log(f"WARNING could not remove stale restart marker: {exc}")
        self._thread = threading.Thread(target=self._run, name="stop-coordinator", daemon=True)
        self._thread.start()

    def complete(self) -> None:
        self._complete.set()

    def close(self) -> None:
        self.signals.enable_hard_exit()
        self._shutdown.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(2.0)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        requested_at: float | None = None
        while not self._shutdown.is_set():
            self._check_restart_file()
            requested = self.signals.requested or self._restart_requested
            if requested_at is None and requested:
                requested_at = self._clock()
                # A third-party decider's cancellation hook may itself block. Keep it off the
                # watchdog thread so the deadline remains enforceable even in that failure mode.
                threading.Thread(
                    target=self.runner.request_stop,
                    name="stop-cancel",
                    daemon=True,
                ).start()
                source = "restart marker" if self._restart_requested else "signal"
                self.log(f"stop requested ({source}): cancelling active work and saving")
            if requested_at is not None and self.signals.forced:
                self.log("second stop signal: saving for at most 1s, then exiting 130")
                self._save_and_exit(EXIT_FORCED_INTERRUPT)
                return
            if self._complete.is_set():
                return
            if requested_at is not None and self._clock() - requested_at >= self.timeout_s:
                self.log(
                    f"graceful-stop deadline ({self.timeout_s:g}s) expired: "
                    f"saving for at most 1s, then exiting {EXIT_GRACEFUL_TIMEOUT}"
                )
                self._save_and_exit(EXIT_GRACEFUL_TIMEOUT)
                return
            self._shutdown.wait(self.POLL_S)

    def _check_restart_file(self) -> None:
        marker = self.restart_file
        if marker is None or self._restart_requested or not marker.exists():
            return
        self._restart_requested = True
        try:
            marker.unlink(missing_ok=True)
        except OSError as exc:
            self.log(f"WARNING could not remove restart marker: {exc}")

    def _save_and_exit(self, code: int) -> None:
        saved = threading.Event()

        def save() -> None:
            try:
                self.writer.finish()
            finally:
                saved.set()

        threading.Thread(target=save, name="stop-final-save", daemon=True).start()
        saved.wait(self.SAVE_EXIT_BUDGET_S)
        if code == EXIT_GRACEFUL_TIMEOUT and self.event_sink is not None:
            closed = threading.Event()

            def close_events() -> None:
                try:
                    self.event_sink.close()
                finally:
                    closed.set()

            threading.Thread(target=close_events, name="stop-event-close", daemon=True).start()
            closed.wait(self.EVENT_CLOSE_BUDGET_S)
        self._exit_process(code)


def play_state(
    actor: Any,
    profile: RomProfile,
    state_path: str | os.PathLike[str] | None,
    *,
    rom_path: str | os.PathLike[str] | None = None,
    config: AppConfig,
    decider: Decider,
    decider_name: str,
    facts: Any,
    events: EventLog | MemoryEventLog,
    extra: Mapping[str, Any] | None = None,
    clock=time.monotonic,
    objectives: str | os.PathLike[str] | ObjectiveStore | ObjectiveSession | None = None,
    objective_plan: str | os.PathLike[str] | Sequence[Mapping[str, Any]] | None = None,
    objective_directory: MapDirectory | None = None,
    objective_graph: MapGraph | None = None,
    objective_navigation: Any = None,
    on_app: Callable[[RunnerApp], None] | None = None,
    state_dir: str | os.PathLike[str] | None = None,
    state_log: Callable[[str], None] = print,
    stop_signals: StopSignals | None = None,
    state_fallback: bool = True,
) -> tuple[RunnerApp, RunSummary, dict[str, Any]]:
    """Load one savestate into ``actor`` and run the router loop on it: a fresh app per state.

    ``on_app`` is called with the assembled app right before it runs (the viewer taps in there).
    With ``state_dir`` the run writes checkpoints and ``final.state`` there (``checkpoint_*``,
    ``save_final_state``): the savestates a later run or a service restart continues from."""
    from pokegent.games.gen3.firered.reader.live import LiveReader

    started = time.monotonic()
    requested = Path(state_path) if state_path is not None else None
    candidates = [requested] if requested is not None else []
    checkpoint_dir = requested.parent / "checkpoints" if requested is not None else None
    if checkpoint_dir is not None and state_fallback:
        candidates.extend(sorted(checkpoint_dir.glob("ckpt-*.state"), reverse=True))
    path: Path | None = None
    failures: list[str] = []
    for candidate in candidates:
        try:
            actor.load_state(str(candidate))
            check = LiveReader(actor, profile).after_state_load(candidate.stem)
            if check.ok:
                path = candidate
                if candidate != requested:
                    state_log(
                        f"WARNING state {requested.name} failed reader checks; "
                        f"falling back to valid checkpoint {candidate.name}"
                    )
                break
            detail = "; ".join(check.conformance.get("failures", ())) or "ROM header"
            failures.append(f"{candidate.name}: {detail}")
        except Exception as exc:
            failures.append(f"{candidate.name}: {type(exc).__name__}: {exc}")
    if requested is not None and path is None:
        detail = " | ".join(failures) or "no valid state candidates"
        state_log(f"ERROR no valid startup state: {detail}")
        name = requested.name if requested else "none"
        raise StateRefused(f"no valid state (requested {name}; {detail})")
    if requested is None:
        # A cold boot is intentionally not passed through the save-state validator: its core is
        # expected to lack an avatar until NEW GAME creates one. Scene detectors still gate all
        # input, and the normal loop cannot accept a pre-game snapshot as a field state.
        # Let the ROM finish copyright/intro setup before the first perception; frame zero has no
        # UI callback yet and is correctly rejected by the ordinary invalid-core gate.
        actor.advance_frames(1500)
        from pokegent.actions.menu.system_flow import evidence as system_evidence
        from pokegent.games.gen3.firered.reader.live import AtomicSnapshotPort, LiveReader
        from pokegent.state.evidence import UiEvidenceReader

        atomic = actor.read_atomic_snapshot()
        boot = LiveReader(actor, profile).read(atomic).snapshot
        boot = boot.with_ui(UiEvidenceReader(AtomicSnapshotPort(atomic), profile).read())
        verdict = system_evidence.classify(boot)
        if not startup_snapshot_allowed(boot, verdict.scene_id if verdict else None):
            raise StateRefused("cold boot did not reach a recognized pre-game scene")
        path = None
    app = build_runner(
        actor=actor,
        profile=profile,
        rom_path=rom_path,
        events=events,
        decider=decider,
        config=config,
        facts=facts,
        clock=clock,
        objectives=objectives,
        objective_plan=objective_plan,
        objective_directory=objective_directory,
        objective_graph=objective_graph,
        objective_navigation=objective_navigation,
    )
    if on_app is not None:
        on_app(app)
    writer = _StateWriter(actor, app, config, state_dir, profile)
    coordinator = (
        StopCoordinator(
            stop_signals or StopSignals(),
            app.runner,
            writer,
            timeout_s=config.graceful_stop_timeout_s,
            restart_file=config.restart_file,
            event_sink=events,
            log=state_log,
        )
        if stop_signals is not None or config.restart_file is not None
        else None
    )
    if coordinator is not None:
        coordinator.start()
    try:
        summary = app.run()
        writer.finish()
        if coordinator is not None:
            coordinator.complete()
    finally:
        if coordinator is not None:
            coordinator.close()
    doc = summarize(
        app,
        summary,
        events,
        decider=decider_name,
        state=path.name if path is not None else "cold-boot",
        wall_s=time.monotonic() - started,
        extra={
            "emulator_frames": app.executor.frames_advanced,
            **writer.report(),
            **(extra or {}),
        },
    )
    return app, summary, doc


def _restore_rom_env(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        previous = os.environ.get("FIRERED_ROM_PATH")
        try:
            return fn(*args, **kwargs)
        finally:
            if previous is None:
                os.environ.pop("FIRERED_ROM_PATH", None)
            else:
                os.environ["FIRERED_ROM_PATH"] = previous

    return guarded


@_restore_rom_env
def run_offline(
    *,
    state: str | os.PathLike[str] | None,
    out_dir: str | os.PathLike[str],
    decider: str = "hybrid",
    max_steps: int = 200,
    config_path: str | os.PathLike[str] | None = None,
    rom_path: str | os.PathLike[str] | None = None,
    allow_remote: bool = False,
    run_id: str | None = None,
    deadline_s: float | None = None,
    objective: str | os.PathLike[str] | None = None,
    objective_plan: str | os.PathLike[str] | None = None,
    on_objective_done: str | None = None,
    ignore_objective: bool = False,
    decision_history: int | None = None,
    viewer: Mapping[str, Any] | None = None,
    stream: Mapping[str, Any] | None = None,
    dashboard: Mapping[str, Any] | None = None,
    external_port: int = 19760,
    external_timeout: float = 300.0,
    hybrid_escalate_below_confidence: float | None = None,
    hybrid_override_window_s: float | None = None,
    hybrid_agent_handoff_timeout_s: float | None = None,
    system1_outage: str | None = None,
    system1_max_outage_s: float | None = None,
    autoplay: str | None = None,
    external_walk_step_limit: int | None = None,
    new_game: bool = False,
    state_fallback: bool = True,
    log=print,
) -> RunReport:
    """Load ``state`` into a headless emulator and run the router loop for ``max_steps``.

    Writes ``events.jsonl`` and ``summary.json`` to ``out_dir`` (also on failure).
    Point ``out_dir`` at a local run-output directory such as ``runs/<run-id>/``.

    ``viewer`` overrides the deprecated combined ``viewer:`` section of the config (port,
    realtime, static, ...); a port starts both local addons for the length of the run
    (``docs/addons.md``).
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    run_id = run_id or safe_run_id(out.resolve().name)
    state_path = Path(state) if state is not None else None
    raw = load_config_file(config_path)
    if new_game:
        raw["new_game"] = {**dict(raw.get("new_game") or {}), "enabled": True}
    if autoplay is not None:
        raw["autoplay"] = autoplay
    if external_walk_step_limit is not None:
        raw["external"] = {
            **dict(raw.get("external") or {}),
            "walk_step_limit": external_walk_step_limit,
        }
    if any(
        v is not None
        for v in (
            hybrid_escalate_below_confidence,
            hybrid_override_window_s,
            hybrid_agent_handoff_timeout_s,
        )
    ):
        hybrid = dict(raw.get("hybrid") or {})
        if hybrid_escalate_below_confidence is not None:
            hybrid["escalate_below_confidence"] = hybrid_escalate_below_confidence
        if hybrid_override_window_s is not None:
            hybrid["override_window_s"] = hybrid_override_window_s
        if hybrid_agent_handoff_timeout_s is not None:
            hybrid["agent_handoff_timeout_s"] = hybrid_agent_handoff_timeout_s
        raw["hybrid"] = hybrid
    if system1_outage is not None or system1_max_outage_s is not None:
        runner = dict(raw.get("runner") or {})
        if system1_outage is not None:
            runner["system1_outage"] = system1_outage
        if system1_max_outage_s is not None:
            runner["system1_max_outage_s"] = system1_max_outage_s
        raw["runner"] = runner
    cfg = config_from_mapping({k: v for k, v in raw.items() if k != "hybrid"}, run_id=run_id)
    overrides: dict[str, Any] = {"max_steps": max_steps}
    if deadline_s is not None:
        overrides["deadline_s"] = deadline_s
    if on_objective_done is not None:
        overrides["on_objective_done"] = on_objective_done
    if ignore_objective:
        overrides["ignore_objective"] = True
    if decision_history is not None:
        overrides["decision_history"] = decision_history
    if viewer:
        overrides["viewer"] = replace(
            cfg.viewer, **{k: v for k, v in viewer.items() if v is not None}
        )
    for name, values in (("stream", stream), ("dashboard", dashboard)):
        if values:
            overrides[name] = replace(
                getattr(cfg, name), **{k: v for k, v in values.items() if v is not None}
            )
    cfg = AppConfig(**{**cfg.__dict__, **overrides})
    # Every run has a live store so an agent decider can set its first objective after startup.
    # Explicit files remain authoritative; the default is private to this run.  Do not
    # create the default objective.json here: ObjectiveStore creates it lazily on the first
    # accepted POST, so an idle run leaves no misleading empty objective file behind.
    if objective is None:
        objective = out / "objective.json"

    def fail(reason: str, code: int, extra: Mapping[str, Any] | None = None) -> RunReport:
        doc = {
            "run_id": run_id,
            "state": state_path.name if state_path is not None else "cold-boot",
            "decider": decider,
            "stop_reason": reason,
            "exit_code": code,
            "steps": 0,
            **(extra or {}),
        }
        (out / "summary.json").write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
        log(f"run failed before playing: {reason}")
        return RunReport(code, reason, doc, out)

    if decider not in (*DECIDERS, *DECIDER_ALIASES):
        return fail(f"unknown decider {decider!r}", EXIT_USAGE)
    # ``viewer`` is the one-release combined alias.  The split addon configs
    # start independent loopback services when either new port is supplied.
    vcfg = cfg.viewer
    if not vcfg.enabled and (cfg.stream.enabled or cfg.dashboard.enabled):
        vcfg = cfg.dashboard if cfg.dashboard.enabled else cfg.stream
    try:
        vcfg, _ = vcfg.validated()
    except AddonConfigError as exc:
        return fail(f"viewer: {exc}", EXIT_USAGE)
    if state_path is not None and not state_path.is_file():
        return fail(f"savestate not found: {state_path}", EXIT_ENVIRONMENT)
    rom = rom_path or os.environ.get("FIRERED_ROM_PATH")
    if not rom or not Path(rom).is_file():
        return fail("ROM not found: pass --rom or set FIRERED_ROM_PATH", EXIT_ENVIRONMENT)
    # The resolved CLI/config ROM is the source of truth for optional family facts too.  Keep
    # legacy ROM-backed consumers on the same path when callers intentionally did not export the
    # environment variable (the normal ``--rom``/config launch mode).
    if (out / "events.jsonl").exists() and (out / "events.jsonl").stat().st_size > 0:
        return fail(f"{out} already holds a run (events.jsonl); use a fresh --out", EXIT_USAGE)
    if objective_plan is not None:
        from pokegent.goals import load_plan, validate_plan_state

        try:
            plan = load_plan(objective_plan)  # validate before anything is played
            if objective is not None:
                validate_plan_state(
                    plan,
                    Path(objective)
                    .expanduser()
                    .with_name(Path(objective).name + ".plan-state.json"),
                )
        except PlanError as exc:
            return fail(f"objective plan: {exc}", EXIT_USAGE)
    if objective is not None and not Path(objective).expanduser().parent.is_dir():
        return fail(f"the objective file's directory does not exist: {objective}", EXIT_USAGE)

    os.environ["FIRERED_ROM_PATH"] = str(Path(rom).expanduser().resolve())

    # Load static tables before the bridge accepts the first objective request.
    early_objective_directory = None
    try:
        from pokegent.games.gen3.firered.rom.rom_data import (
            identify_rom_file,
            load_profiles,
            require_supported,
        )
        from pokegent.goals.directory import RomMapDirectory

        profiles = load_profiles()
        early_profile = require_supported(identify_rom_file(rom, profiles), profiles)
        early_objective_directory = RomMapDirectory(Path(rom).read_bytes(), early_profile)
    except Exception as exc:
        log(f"WARNING objective ROM tables unavailable before bridge start: {exc}")

    events = EventLog(out, run_id)
    try:
        decider_obj, warnings, closer = build_decider(
            decider,
            config_raw=raw,
            events=events,
            out_dir=out,
            allow_remote=allow_remote,
            run_id=run_id,
            external_port=external_port,
            external_timeout=external_timeout,
            autoplay=cfg.autoplay,
        )
    except AppError as exc:
        events.close()
        return fail(str(exc), exc.exit_code)
    if decider == "external":
        decider = "agent"
    # The external bridge is reachable as soon as build_decider starts its HTTP server. Bind a
    # per-run store before ROM/emulator setup so agent can submit the first objective immediately
    # after observing /status. build_runner later replaces this with the ROM-aware store, which
    # polls the same file and performs the deeper target checks.
    if objective is not None:
        bind_planning = getattr(decider_obj, "bind_planning", None)
        if callable(bind_planning):
            bind_planning(
                ObjectiveStore(Path(objective).expanduser(), directory=early_objective_directory)
            )
    for warning in warnings:
        log(f"WARNING {warning}")

    live = None  # the optional stream/dashboard addons
    if cfg.viewer.enabled or cfg.stream.enabled or cfg.dashboard.enabled:
        try:
            from pokegent.addons import start_addons

            live = start_addons(
                {"viewer": cfg.viewer, "stream": cfg.stream, "dashboard": cfg.dashboard},
                AddonTaps(
                    run_id=run_id,
                    decider=decider,
                    max_steps=cfg.max_steps,
                    log=log,
                ),
            )
        except (AddonConfigError, AddonStartError) as exc:
            events.close()
            if closer is not None:
                closer()
            return fail(f"viewer: {exc}", EXIT_ENVIRONMENT)
        listener = live.events_listener()
        if listener is not None:
            events.add_listener(listener)
        decider_obj = live.wrap_decider(decider_obj)

    stops = StopSignals()
    stops.__enter__()  # SIGTERM/SIGINT end the run between iterations (exit in the finally below)
    try:
        from pokegent.games.gen3.firered.rom.rom_data import UnsupportedRomError

        try:
            with open_emulator(
                rom, fps=vcfg.fps if vcfg.realtime_on else None,
                realtime_idle_while_deciding=cfg.realtime_idle_while_deciding,
            ) as (actor, profile):
                facts = build_facts(
                    profile,
                    rom,
                    cfg.facts,
                    opponent_knowledge=cfg.opponent_knowledge,
                )
                rom_bytes = Path(rom).read_bytes()
                if live is not None:
                    live.attach_actor(actor)
                    live.attach_names(facts, rom, profile)
                attach_screenshot = getattr(decider_obj, "attach_screen_source", None)
                if attach_screenshot is not None:
                    attach_screenshot(actor.reader())
                directory = graph = navigation = None
                # Both explicit and generated objectives need long-distance route hints. Deep
                # objective-file target checks remain conditional on an actual file.
                graph = MapGraph.from_rom(rom_bytes, profile)
                from pokegent.navigation import NavigationAdapter, NavigationGraph

                navigation = NavigationAdapter(
                    NavigationGraph.from_rom(rom_bytes, profile), events=events
                )
                directory = None
                if objective is not None:
                    from pokegent.goals.directory import RomMapDirectory

                    directory = RomMapDirectory(rom_bytes, profile)
                log(
                    f"run {run_id}: state={state_path.name if state_path else 'cold-boot'} "
                    f"decider={decider} "
                    f"max_steps={cfg.max_steps}"
                )

                def on_app(app: RunnerApp) -> None:
                    if live is not None:
                        live.attach_app(app)

                app, summary, doc = play_state(
                    actor,
                    profile,
                    state_path,
                    rom_path=rom,
                    config=cfg,
                    decider=decider_obj,
                    decider_name=decider,
                    facts=facts,
                    events=events,
                    extra={"warnings": warnings},
                    objectives=objective,
                    objective_plan=objective_plan,
                    objective_directory=directory,
                    objective_graph=graph,
                    objective_navigation=navigation,
                    on_app=on_app,
                    state_dir=out,
                    state_log=log,
                    stop_signals=stops,
                    state_fallback=state_fallback,
                )
                if stops.forced:
                    doc["stop_reason"] = "forced_interrupt"
                    doc["exit_code"] = EXIT_FORCED_INTERRUPT
                log(f"families={list(app.bundle.manifests)} skipped={list(app.bundle.skipped)}")
                manifest = getattr(decider_obj, "manifest", None)
                if callable(manifest):  # System One: provider ids, versions, option_order
                    doc["decider_manifest"] = manifest()
                doc["realtime"] = vcfg.realtime_on
                if live is not None:
                    live.finish(stop_reason=doc["stop_reason"], exit_code=doc["exit_code"])
                    doc["viewer"] = live.stats()
        except UnsupportedRomError as exc:
            events.close()
            return fail(f"unsupported ROM: {exc}", EXIT_ENVIRONMENT)
    except StateRefused as exc:
        events.close()
        return fail(str(exc), EXIT_ENVIRONMENT, {"load_check": {"ok": False}})
    except AppError as exc:
        events.close()
        return fail(str(exc), exc.exit_code)
    except Exception as exc:  # emulator or reader failure: nothing trustworthy was played
        events.close()
        return fail(f"{type(exc).__name__}: {exc}", EXIT_ENVIRONMENT)
    finally:
        stops.__exit__(None, None, None)
        if closer is not None:
            try:
                closer()
            except Exception:  # best effort
                pass
        if live is not None:
            live.finish(stop_reason="aborted", exit_code=None)  # no-op after a normal finish
            live.linger()
            live.close()
    events.close()
    (out / "summary.json").write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    stop_line = stop_message(summary.stop_reason, summary.stop_details)
    action_doc = doc.get("actions", {})
    agent_frames = action_doc.get("agent_frames", 0) if isinstance(action_doc, Mapping) else 0
    log(
        f"{stop_line}; agent_frames {agent_frames} after {summary.iterations} steps; "
        f"actions {doc['actions']}; "
        f"exit {doc['exit_code']}"
    )
    log(f"saved: {out / 'summary.json'}")
    return RunReport(doc["exit_code"], doc["stop_reason"], doc, out)
