"""System One configuration: spec chapter 14, section ``system_one`` (+ a few knobs it implies).

* Strict: unknown keys are errors, ranges are checked, every problem is listed at once with its
  path (``ConfigError``). Values that failed validation are never echoed (they may be secrets).
* Env expansion for ``${NAME}`` and ``${NAME:-default}`` in every string of the section.
  A missing variable is an error for ``provider_url`` / ``model`` when System One is enabled and
  a warning for ``api_key`` (System One runs without auth unless ``SYSTEM1_API_KEY`` is set on
  the server).
* Secrets: ``api_key`` is a ``SecretStr`` (``repr``/``str``/``model_dump`` mask it); use
  ``config.api_key_value()`` at the single place that builds the Authorization header.
* Calibration is observe-first: ``confidence_gate: disabled_until_firered_validated`` is the only
  accepted value. Confidence is logged and never used to gate (spec 8.4) until real FireRed
  calibration exists; a different value is a validation error, not a silent no-op.
"""

from __future__ import annotations

import logging
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = 1
#: Literal in ``calibration.log_path`` that stands for "this run" (``resolve_log_path``). The config
#: loader keeps it as written; whoever knows the run directory or the run id resolves it.
RUN_ID_PLACEHOLDER = "<run-id>"
_SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
#: Fields where an unset environment variable is tolerated (with a warning).
OPTIONAL_ENV_FIELDS = frozenset({"api_key"})
_legacy_provider_warned = False
_log = logging.getLogger("pokegent.system_one.policy")


class ConfigError(ValueError):
    """Invalid System One configuration. ``errors`` lists every problem with its path."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("invalid system_one config:\n  - " + "\n  - ".join(self.errors))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RetryPolicy(_Strict):
    """Transient errors only (timeout, 408, 429, 5xx, connection); never 4xx validation errors."""

    max_attempts: int = Field(default=3, ge=1, le=10)  # total attempts including the first
    backoff_base_seconds: float = Field(default=0.25, gt=0, le=60)
    backoff_max_seconds: float = Field(default=4.0, gt=0, le=300)
    jitter: float = Field(default=0.5, ge=0, le=1)  # fraction of each delay that is randomized

    @model_validator(mode="after")
    def _ordered(self) -> RetryPolicy:
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError("backoff_max_seconds must be >= backoff_base_seconds")
        return self


class BreakerPolicy(_Strict):
    failure_threshold: int = Field(default=5, ge=1, le=1000)  # consecutive transient failures
    open_seconds: float = Field(default=30.0, gt=0, le=3600)
    half_open_max_calls: int = Field(default=1, ge=1, le=10)


class RatePolicy(_Strict):
    """Token bucket over HTTP attempts (retries count). Protects the shared lanes."""

    max_requests: int = Field(default=120, ge=1, le=100_000)
    per_seconds: float = Field(default=60.0, gt=0, le=86_400)
    burst: int | None = Field(default=None, ge=1, le=100_000)  # bucket size; default max_requests


class CalibrationPolicy(_Strict):
    mode: Literal["observe_first"] = "observe_first"
    confidence_gate: Literal["disabled_until_firered_validated"] = (
        "disabled_until_firered_validated"
    )
    log_path: str | None = None  # append-only JSONL; None = no calibration file

    @field_validator("confidence_gate", mode="before")
    @classmethod
    def _gate_not_available(cls, v: Any) -> Any:
        if v != "disabled_until_firered_validated":
            raise ValueError(
                "only 'disabled_until_firered_validated' is supported: confidence must not gate "
                "decisions before real FireRed calibration (spec 8.4)"
            )
        return v


class SystemOneConfig(_Strict):
    """``option_order`` is an EXPERIMENT switch (``frame`` | ``shuffle``): the order the criteria
    are sent in, for positional-bias analysis. It changes presentation only; never use ``shuffle``
    in production play (see ``docs/provider-integration.md``, "Option order experiment")."""

    enabled: bool = True
    primary: Literal["http", "mock"] = "http"
    provider_url: str | None = None
    protocol: Literal["systemone", "openai-chat"] = "systemone"
    endpoint_path: str | None = None
    auth_header: str = "Authorization"
    #: The model id this endpoint is EXPECTED to report (log, manifest, ``strict_model_pin``). It is
    #: never sent; request-side model selection is configured separately below.
    model: str | None = None
    #: Explicit selector sent as JSON ``model``; omitted when unset. Endpoint support varies.
    request_model: str | None = None
    api_key: SecretStr | None = None
    timeout_seconds: float = Field(default=8.0, gt=0, le=120)  # hard budget of one decision
    connect_timeout_seconds: float = Field(default=2.0, gt=0, le=60)
    #: Per-attempt read timeout; default half of ``timeout_seconds`` so a timed-out first attempt
    #: still leaves budget for a retry.
    read_timeout_seconds: float | None = Field(default=None, gt=0, le=120)
    fallback_provider: str | None = None
    max_state_tokens: int = Field(default=1200, ge=64, le=65_536)
    max_options_per_question: int = Field(default=32, ge=2, le=256)
    option_order: Literal["frame", "shuffle"] = "frame"
    require_fresh_snapshot: bool = True  # enforced by the executor's revalidation, recorded here
    strict_model_pin: bool = False  # True: a response from another model id is an error
    retry: RetryPolicy = RetryPolicy()
    breaker: BreakerPolicy = BreakerPolicy()
    rate: RatePolicy = RatePolicy()
    calibration: CalibrationPolicy = CalibrationPolicy()

    @field_validator("fallback_provider")
    @classmethod
    def _no_fallback_yet(cls, v: str | None) -> str | None:
        if v is not None:
            raise ValueError("fallback providers are not implemented yet; use null")
        return v

    @field_validator("provider_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        try:
            parts = urlsplit(v)
            _ = parts.port
        except ValueError:
            raise ValueError("must be an http(s) URL with a host and valid port") from None
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("must be an http(s) URL with a host")
        if any(ord(c) <= 32 or ord(c) == 127 for c in v):
            raise ValueError("must not contain whitespace or controls")
        if parts.username is not None or parts.password is not None:
            raise ValueError("must not contain credentials; use api_key")
        if parts.query or parts.fragment:
            raise ValueError("must not contain a query string or fragment")
        return v.rstrip("/")

    @field_validator("model")
    @classmethod
    def _model(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if len(v) > 200 or any(c.isspace() for c in v):
            raise ValueError("must be a model id without whitespace (max 200 chars)")
        return v

    @field_validator("request_model")
    @classmethod
    def _request_model(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if len(v) > 200 or any(c.isspace() for c in v):
            raise ValueError("must be a model id without whitespace (max 200 chars)")
        return v

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def _timeout(cls, v: Any) -> Any:
        try:
            finite = math.isfinite(float(v))
        except (TypeError, ValueError, OverflowError):
            finite = False
        if isinstance(v, bool) or not finite:
            raise ValueError("must be a finite numeric decision budget, not a boolean")
        return v

    @field_validator("auth_header")
    @classmethod
    def _auth_header(cls, v: str) -> str:
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", v):
            raise ValueError("must be a nonempty HTTP header token")
        if v.lower() in {
            "host", "content-type", "content-length", "content-encoding", "connection",
            "transfer-encoding", "trailer", "te", "upgrade", "expect", "accept",
            "user-agent", "x-request-id", "proxy-authorization", "proxy-connection",
        }:
            raise ValueError("reserved transport header cannot carry authentication")
        return "Authorization" if v.lower() == "authorization" else v

    @field_validator("endpoint_path")
    @classmethod
    def _endpoint_path(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if any(ord(c) <= 32 or ord(c) >= 127 for c in v):
            raise ValueError("must be an ASCII URL path without whitespace or controls")
        if not v.startswith("/") or "?" in v or "#" in v or "//" in v:
            raise ValueError("must be an absolute URL path without query or fragment")
        return v.rstrip("/") or "/"

    @field_validator("api_key", mode="before")
    @classmethod
    def _empty_key(cls, v: Any) -> Any:
        if v in ("", None):
            return None
        key = v.get_secret_value() if isinstance(v, SecretStr) else v
        if not isinstance(key, str) or any(ord(c) < 32 or ord(c) > 126 for c in key):
            raise ValueError("must be an ASCII header value without controls")
        return v

    @model_validator(mode="after")
    def _complete(self) -> SystemOneConfig:
        if self.enabled and self.primary == "http":
            if not self.provider_url:
                raise ValueError("required when enabled with primary 'http': provider_url")
        if self.protocol == "openai-chat" and not self.request_model:
            raise ValueError("openai-chat requires an explicit request_model")
        if self.strict_model_pin and not self.model:
            raise ValueError("strict_model_pin requires an expected response model")
        if self.endpoint_path is None:
            object.__setattr__(self, "endpoint_path",
                               "/v1/chat/completions" if self.protocol == "openai-chat"
                               else "/v1/systemone")
        return self

    def effective_read_timeout(self) -> float:
        return self.read_timeout_seconds or self.timeout_seconds / 2.0

    def api_key_value(self) -> str | None:
        return self.api_key.get_secret_value() if self.api_key else None

    def safe_summary(self) -> dict[str, Any]:
        """Config as a dict for logs and ``provider check``: no secret, ever."""
        data = self.model_dump(mode="json")
        data["api_key"] = "set" if self.api_key else "unset"
        return data


def resolve_log_path(
    log_path: str | os.PathLike[str] | None,
    *,
    out_dir: str | os.PathLike[str] | None = None,
    run_id: str | None = None,
) -> Path | None:
    """The calibration file for ``calibration.log_path`` with ``<run-id>`` resolved.

    * No placeholder: the path as written (``None`` stays ``None``: no file).
    * Placeholder and a known ``out_dir`` (the run directory): the log lands INSIDE it, as
      ``out_dir / <file name of log_path>`` (a ``<run-id>`` in the file name itself expands to
      ``run_id``). The directory part of the configured path is the run's own directory by
      definition, so ``runs/<run-id>/system_one_calibration.jsonl`` and ``--out`` agree.
    * Placeholder, no ``out_dir``: the placeholder expands to ``run_id``.

    Never returns a path that still contains the placeholder: that would create a directory
    literally named ``<run-id>``. Raises ``ValueError`` (no run id to expand, an unsafe run id, or
    a path that names no file).
    """
    if log_path is None:
        return None
    text = os.fspath(log_path)
    if RUN_ID_PLACEHOLDER not in text:
        return Path(text)

    def expand(value: str) -> str:
        if RUN_ID_PLACEHOLDER not in value:
            return value
        if run_id is None:
            raise ValueError(
                f"calibration.log_path contains {RUN_ID_PLACEHOLDER!r} but no run id is known "
                "to expand it; give an explicit path or run through `pokegent run`"
            )
        if not _SAFE_RUN_ID.fullmatch(run_id) or run_id.endswith("."):
            raise ValueError(
                f"the run id cannot expand {RUN_ID_PLACEHOLDER!r} in a file path "
                "(letters, digits, '.', '_' and '-' only)"
            )
        return value.replace(RUN_ID_PLACEHOLDER, run_id)

    name = Path(text).name
    if out_dir is not None:
        if not name or name == RUN_ID_PLACEHOLDER:
            raise ValueError(
                "calibration.log_path must end in a file name "
                f"(e.g. runs/{RUN_ID_PLACEHOLDER}/system_one_calibration.jsonl)"
            )
        return Path(out_dir) / expand(name)
    return Path(expand(text))


@dataclass(frozen=True)
class LoadedConfig:
    config: SystemOneConfig
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _expand(value: Any, env: Mapping[str, str], path: str, missing: dict[str, list[str]]) -> Any:
    if isinstance(value, str):

        def sub(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            got = env.get(name)
            if got:
                return got
            if default is not None:
                return default
            missing.setdefault(path, []).append(name)
            return ""

        return _ENV_REF.sub(sub, value)
    if isinstance(value, Mapping):
        return {
            k: _expand(v, env, f"{path}.{k}" if path else str(k), missing) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_expand(v, env, f"{path}[{i}]", missing) for i, v in enumerate(value)]
    return value


def _format_errors(exc: ValidationError, *, skip_required: bool = False) -> list[str]:
    out = []
    for err in exc.errors(include_input=False, include_url=False):
        msg = err["msg"].removeprefix("Value error, ")
        if skip_required and msg.startswith("required when enabled"):
            continue  # already reported per field as an unset environment variable
        loc = ".".join(str(p) for p in err["loc"])
        out.append(f"system_one.{loc}: {msg}" if loc else f"system_one: {msg}")
    return out


def load_config(
    source: str | os.PathLike[str] | Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
) -> LoadedConfig:
    """Load and validate the ``system_one`` section from a YAML path or an already parsed mapping.

    ``source`` may be the full config (with a ``system_one`` key and optional ``schema_version``)
    or the section itself. Pass ``env`` explicitly in tests; the default is ``os.environ``.
    """
    env = os.environ if env is None else env
    if isinstance(source, Mapping):
        raw: Any = dict(source)
    else:
        path = Path(source).expanduser()
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigError([f"cannot read {path}: {exc.strerror or exc}"]) from None
        except yaml.YAMLError as exc:
            raise ConfigError([f"{path}: not valid YAML ({type(exc).__name__})"]) from None
    if not isinstance(raw, Mapping):
        raise ConfigError(["config must be a mapping"])
    errors: list[str] = []
    version = raw.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        errors.append(f"schema_version: expected {SCHEMA_VERSION}, got {version!r}")
    section = raw["system_one"] if "system_one" in raw else raw
    if not isinstance(section, Mapping):
        raise ConfigError([*errors, "system_one must be a mapping"])
    if "system_one" not in raw:
        section = {k: v for k, v in section.items() if k != "schema_version"}

    removed = sorted(name for name in env if name.startswith(("VON_", "POKEVON_")))
    if removed:
        raise ConfigError(
            [
                f"{name} was removed; use SYSTEM1_* environment names and provider 'http'"
                for name in removed
            ]
        )
    expanded_env = dict(env)
    missing: dict[str, list[str]] = {}
    expanded = _expand(dict(section), expanded_env, "", missing)
    if isinstance(expanded, dict) and expanded.get("primary") == "von":
        raise ConfigError(["provider 'von' was removed; use provider 'http'"])
    enabled = bool(expanded.get("enabled", True)) if isinstance(expanded, dict) else True
    warnings: list[str] = []
    for path, names in sorted(missing.items()):
        field_name = path.split(".")[-1]
        msg = f"system_one.{path}: environment variable {', '.join(sorted(set(names)))} is not set"
        if field_name in OPTIONAL_ENV_FIELDS:
            warnings.append(msg + " (requests will be sent without Authorization)")
        elif enabled:
            errors.append(msg)
        else:
            warnings.append(msg)
    try:
        config = SystemOneConfig.model_validate(expanded)
    except ValidationError as exc:
        errors.extend(_format_errors(exc, skip_required=bool(missing)))
        raise ConfigError(errors) from None
    if errors:
        raise ConfigError(errors)

    if config.api_key and config.provider_url:
        host = urlsplit(config.provider_url)
        if host.scheme == "http" and host.hostname not in _LOOPBACK:
            warnings.append("api_key is sent in clear text over http to a non-loopback host")
    if config.option_order != "frame":
        warnings.append(
            f"system_one.option_order is {config.option_order!r}: an experiment switch for "
            "positional-bias analysis, not for production play"
        )
    if config.timeout_seconds < config.connect_timeout_seconds:
        warnings.append("connect_timeout_seconds exceeds timeout_seconds; the hard budget wins")
    return LoadedConfig(config=config, warnings=tuple(warnings))
