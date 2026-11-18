"""User-facing configuration with file < environment < CLI precedence."""

from __future__ import annotations

import math
import os
import warnings
from pathlib import Path
from typing import Any, Mapping

import yaml

from pokegent.env import get_system1_env

DEFAULTS: dict[str, Any] = {
    "decider": "hybrid",
    "autoplay": None,
    "external": {"port": 19760, "walk_step_limit": 8},
    "hybrid": {
        "escalate_below_confidence": 0.35,
        "override_window_s": 0.0,
        "escalate_on_no_progress": True,
        "escalate_unknown_scene": True,
        "force_agent_after_repeats": 2,
    },
    "battle": {"opponent_knowledge": "guide"},
    "field": {"menus": True},
    "output": {"run_dir": None},
}


def _merge(base: dict[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in extra.items():
        out[key] = (
            _merge(dict(out[key]), value)
            if isinstance(value, Mapping) and isinstance(out.get(key), Mapping)
            else value
        )
    return out


def _env(environ: Mapping[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    simple = {
        "POKEGENT_ROM_PATH": ("rom_path", str),
        "POKEGENT_DECIDER": ("decider", str),
        "POKEGENT_AUTOPLAY": ("autoplay", str),
        "POKEGENT_OUTPUT_RUN_DIR": ("output.run_dir", str),
        "SYSTEM1_BASE_URL": ("system1.base_url", str),
        "SYSTEM1_API_KEY": ("system1.api_key", str),
        "SYSTEM1_MODEL": ("system1.model", str),
        "SYSTEM1_MODEL_ID": ("system1.model", str),
        "SYSTEM1_REQUEST_MODEL": ("system1.request_model", str),
        "SYSTEM1_TIMEOUT": ("system1.timeouts", float),
        "SYSTEM1_OUTAGE": ("system1.outage", str),
        "SYSTEM1_MAX_OUTAGE_S": ("system1.max_outage_s", float),
        "POKEGENT_EXTERNAL_PORT": ("external.port", int),
        "POKEGENT_EXTERNAL_WALK_STEP_LIMIT": ("external.walk_step_limit", int),
        "POKEGENT_HYBRID_ESCALATE_BELOW_CONFIDENCE": ("hybrid.escalate_below_confidence", float),
        "POKEGENT_HYBRID_OVERRIDE_WINDOW_S": ("hybrid.override_window_s", float),
    }
    for name, (path, cast) in simple.items():
        value = (
            get_system1_env(name, environ=environ)
            if name.startswith("SYSTEM1_")
            else environ.get(name)
        )
        if value is not None and value != "":
            target = result
            parts = path.split(".")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = cast(value)
    for name, key in (("POKEGENT_FIELD_MENU", "menus"),):
        if name in environ:
            result.setdefault("field", {})[key] = environ[name].lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
    if "SYSTEM1_API_KEY_ENV" in environ:
        result.setdefault("system1", {})["api_key_env"] = environ["SYSTEM1_API_KEY_ENV"]
    return result


def load_user_config(
    path: str | os.PathLike[str] | None = None, *, environ=None, cli=None
) -> dict[str, Any]:
    """Load defaults, optional YAML, environment, then non-None CLI values."""
    environ = os.environ if environ is None else environ
    if path is None:
        new_path, old_path = Path("pokegent.yaml"), Path("pokejev.yaml")
        if new_path.is_file() and old_path.is_file():
            raise ValueError("both pokegent.yaml and deprecated pokejev.yaml exist")
        selected = new_path
        if old_path.is_file() and not new_path.is_file():
            warnings.warn(
                "pokejev.yaml is deprecated; use pokegent.yaml", DeprecationWarning, stacklevel=2
            )
            selected = old_path
    else:
        selected = Path(path).expanduser()
    data = yaml.safe_load(selected.read_text(encoding="utf-8")) if selected.is_file() else {}
    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        raise ValueError("user config must be a mapping")
    if (
        "hybrid" in data
        and isinstance(data["hybrid"], Mapping)
        and "escalation_timeout_s" in data["hybrid"]
    ):
        raise ValueError("hybrid.escalation_timeout_s was removed: escalations wait for the agent")
    if "POKEGENT_HYBRID_ESCALATION_TIMEOUT_S" in environ:
        raise ValueError(
            "POKEGENT_HYBRID_ESCALATION_TIMEOUT_S was removed: escalations wait for the agent"
        )
    if (
        "external" in data
        and isinstance(data["external"], Mapping)
        and "timeout" in data["external"]
    ):
        raise ValueError("external.timeout was removed: agent choices wait for the agent")
    if "POKEGENT_EXTERNAL_TIMEOUT" in environ:
        raise ValueError("POKEGENT_EXTERNAL_TIMEOUT was removed: agent choices wait for the agent")
    if isinstance(data.get("field"), Mapping) and "teach_tm" in data["field"]:
        raise ValueError("field.teach_tm was removed: legal TM/HM teaching is always enabled")
    if "recovery" in data:
        raise ValueError(
            "recovery config was removed: the runner now reports no progress and stops for "
            "supervisor review"
        )
    if "objectives" in data:
        raise ValueError(
            "objectives.fallback was removed: an empty objective file remains goal-less"
        )
    if (
        "instructions" in data
        or "run" in data
        and isinstance(data["run"], Mapping)
        and "instructions" in data["run"]
    ):
        raise ValueError(
            "instructions config was removed: tactical run instructions are no longer accepted"
        )
    if data.get("autoplay") == "forced":
        raise ValueError("autoplay was removed: forced autoplay no longer exists")
    cli_values = {
        key: (
            {
                nested_key: nested_value
                for nested_key, nested_value in value.items()
                if nested_value is not None
            }
            if isinstance(value, Mapping)
            else value
        )
        for key, value in (cli or {}).items()
        if value is not None
    }
    result = _merge(_merge(_merge(DEFAULTS, data), _env(environ)), cli_values)
    system1 = result.get("system1", {})
    key_env = system1.get("api_key_env")
    if key_env and key_env in environ:
        result["system1"] = dict(system1, api_key=environ[key_env])
    port = int(result["external"]["port"])
    if not 19700 <= port <= 19799:
        raise ValueError("external.port must be between 19700 and 19799")
    if result["decider"] not in {"agent", "system1", "first", "forced", "hybrid", "external"}:
        raise ValueError("decider must be agent|system1|hybrid|first|forced")
    if result.get("autoplay") == "forced":
        raise ValueError("autoplay was removed: forced autoplay no longer exists")
    if result.get("autoplay") not in (None, "off"):
        raise ValueError("autoplay must be off")
    if int(result["external"].get("walk_step_limit", 8)) < 1:
        raise ValueError("external.walk_step_limit must be >= 1")
    hybrid = result["hybrid"]
    threshold = hybrid["escalate_below_confidence"]
    if isinstance(threshold, Mapping):
        for scene_id, value in threshold.items():
            if not isinstance(scene_id, str) or (scene_id != "default" and not scene_id):
                raise ValueError(
                    "hybrid.escalate_below_confidence mapping keys must be non-empty strings"
                )
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(
                    "hybrid.escalate_below_confidence values must be numbers between 0 and 1"
                )
            if not math.isfinite(float(value)) or not 0 <= value <= 1:
                raise ValueError("hybrid.escalate_below_confidence values must be between 0 and 1")
    elif isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ValueError("hybrid.escalate_below_confidence must be a number or mapping")
    elif not math.isfinite(float(threshold)) or not 0 <= threshold <= 1:
        raise ValueError("hybrid.escalate_below_confidence must be between 0 and 1")
    if not isinstance(hybrid["escalate_on_no_progress"], bool):
        raise ValueError("hybrid.escalate_on_no_progress must be a boolean")
    if not isinstance(hybrid["escalate_unknown_scene"], bool):
        raise ValueError("hybrid.escalate_unknown_scene must be a boolean")
    repeats = hybrid["force_agent_after_repeats"]
    if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats < 1:
        raise ValueError("hybrid.force_agent_after_repeats must be a positive integer")
    if hybrid["override_window_s"] < 0:
        raise ValueError("hybrid.override_window_s must be non-negative")
    if result["battle"]["opponent_knowledge"] not in {"guide", "fair_play"}:
        raise ValueError("battle.opponent_knowledge must be guide|fair_play")
    return result


def to_legacy_config(config: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if config.get("system1"):
        section = dict(config["system1"])
        section.update(enabled=True, primary="http")  # noqa: E702
        outage = {}
        if "outage" in section:
            outage["system1_outage"] = section.pop("outage")
        if "max_outage_s" in section:
            outage["system1_max_outage_s"] = section.pop("max_outage_s")
        section.pop("api_key_env", None)
        if "base_url" in section:
            section["provider_url"] = section.pop("base_url")
        if "timeouts" in section:
            timeouts = section.pop("timeouts")
            section["timeout_seconds"] = (
                timeouts if not isinstance(timeouts, Mapping) else timeouts.get("request", 8)
            )
        out["system_one"] = section
        if outage:
            out["runner"] = outage
    if config.get("hybrid"):
        out["hybrid"] = dict(config["hybrid"])
    if config.get("autoplay") is not None:
        out["autoplay"] = config["autoplay"]
    if config.get("external"):
        out["external"] = {"walk_step_limit": int(config["external"].get("walk_step_limit", 8))}
    return out
