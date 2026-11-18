"""``pokegent provider check``: config validation, offline self-tests, optional live probe.

* config: load (env expansion), validate, print the summary with the key masked;
* mock self-test: frames through ``SyncDecider`` with a ``MockProvider`` (flat, hierarchical,
  state over budget, a wrong key must be refused). No network;
* stub self-test: the real ``HttpProvider`` against the local stub server (127.0.0.1, port
  19700-19799, started and stopped in-process). The developer stub is optional; this check is
  skipped when it is not installed. The configured API key is never sent to the stub;
* ``--live``: ONE tiny Choice request to the configured endpoint (a real call: only on purpose).

Exit code 0 = everything requested passed, 1 = a check failed, 2 = invalid configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from pokegent.env import get_system1_env
from pokegent.options.models import DecisionFrame, Legality, Option, check_choice

from .mock import MockProvider
from .policy import ConfigError, LoadedConfig, SystemOneConfig, load_config
from .provider import InvalidResponse, SyncDecider
from .questions import QuestionLimits
from .redact import Scrubber


def env_config() -> dict[str, str | None]:
    base_url = get_system1_env("SYSTEM1_BASE_URL")
    api_key = get_system1_env("SYSTEM1_API_KEY")
    return {
        "provider_url": base_url if base_url is not None else "${SYSTEM1_BASE_URL}",
        "model": (get_system1_env("SYSTEM1_MODEL_ID")
                  or get_system1_env("SYSTEM1_MODEL", "")),
        "protocol": get_system1_env("SYSTEM1_PROTOCOL") or "systemone",
        "auth_header": get_system1_env("SYSTEM1_AUTH_HEADER") or "Authorization",
        "timeout_seconds": get_system1_env("SYSTEM1_TIMEOUT") or 8.0,
        "request_model": get_system1_env("SYSTEM1_REQUEST_MODEL"),
        "api_key": api_key if api_key is not None else "${SYSTEM1_API_KEY}",
    }


ENV_CONFIG = env_config()


def add_parser(sub: Any) -> None:
    """Register ``provider check`` on the top-level subparsers."""
    provider = sub.add_parser("provider", help="System One provider tools")
    psub = provider.add_subparsers(dest="provider_command")
    check = psub.add_parser("check", help="Validate System One config and run self-tests")
    check.add_argument(
        "--config",
        default=None,
        help=("YAML with a system_one section (default: SYSTEM1_* environment settings)"),
    )
    check.add_argument(
        "--live",
        action="store_true",
        help="make one tiny logical Choice decision (transient attempts may retry; use only "
        "an endpoint you are authorized to call)",
    )
    check.add_argument(
        "--no-stub", action="store_true", help="skip the local stub-server self-test"
    )


def _frame(n: int, *, context: dict[str, Any] | None = None) -> DecisionFrame:
    options = [
        Option(
            id=f"move:{i}",
            label=f"Move {i}",
            description=f"Attack number {i}",
            legality=Legality.LEGAL,
            executor_ref="battle.use_move",
            executor_args={"move_slot": i % 4 + 1},
            verb="USE_MOVE" if i % 2 else "USE_ITEM",
        )
        for i in range(n)
    ]
    return DecisionFrame.from_options(
        options,
        decision_id="d-selftest",
        run_id="selftest",
        snapshot_seq=1,
        scene_id="battle.action",
        scene_confidence=0.9,
        state_fingerprint="sha256:" + "0" * 64,
        goal_id="self_test_goal",
        context=context or {"summary": "Self-test frame"},
    )


class _Report:
    def __init__(self) -> None:
        self.failed = 0

    def line(self, ok: bool | None, name: str, detail: str = "") -> None:
        mark = {True: "PASS", False: "FAIL", None: "SKIP"}[ok]
        self.failed += ok is False
        print(f"  [{mark}] {name}" + (f": {detail}" if detail else ""))


def _mock_self_test(rep: _Report) -> None:
    print("mock self-test (no network)")
    big = {"history": ["event"] * 400, "summary": "x " * 400}
    cases = [
        ("flat choice", _frame(4), QuestionLimits()),
        ("hierarchical choice", _frame(40), QuestionLimits(max_options_per_question=8)),
        ("state over budget", _frame(4, context=big), QuestionLimits(max_state_tokens=100)),
    ]
    for name, frame, limits in cases:
        decider = SyncDecider(MockProvider("last"), limits=limits, timeout_s=5.0)
        try:
            result = decider.decide(frame)
            ok = (
                check_choice(frame, result) is None
                and result.option_id == frame.chooseable_ids()[-1]
            )
            rep.line(ok, name, f"{result.option_id} truncated={result.truncated}")
        except Exception as exc:  # report, never crash the check
            rep.line(False, name, f"{type(exc).__name__}: {exc}")
        finally:
            decider.close()
    decider = SyncDecider(MockProvider(script=["not_a_criterion"]), timeout_s=5.0)
    try:
        decider.decide(_frame(3))
        rep.line(False, "wrong key is refused", "accepted a key outside the criteria")
    except InvalidResponse as exc:
        rep.line(True, "wrong key is refused", exc.code.value)
    finally:
        decider.close()


def _import_stub():
    try:
        from tests.system_one import stub_server

        return stub_server
    except ImportError:
        root = Path(__file__).resolve().parents[2]
        if (root / "tests" / "system_one" / "stub_server.py").exists():
            sys.path.insert(0, str(root))
            try:
                from tests.system_one import stub_server

                return stub_server
            except ImportError:
                return None
        return None


def _stub_self_test(rep: _Report) -> None:
    print("stub self-test (HTTP provider against the local stub, 127.0.0.1:19700-19799)")
    stub_mod = _import_stub()
    if stub_mod is None:
        rep.line(None, "stub server", "optional developer stub is not installed")
        return
    from .http_provider import HttpProvider

    server = stub_mod.StubServer().start()
    try:
        cfg = SystemOneConfig(
            provider_url=server.url,
            model="selftest",
            timeout_seconds=3.0,
            retry={"max_attempts": 3, "backoff_base_seconds": 0.01, "backoff_max_seconds": 0.05},
        )
        provider = HttpProvider(cfg)
        decider = SyncDecider(provider, timeout_s=3.0)
        frame = _frame(4)
        try:
            res = decider.decide(frame)
            rep.line(check_choice(frame, res) is None, "valid choice", res.option_id)
            server.script(stub_mod.status(503), stub_mod.valid())
            res = decider.decide(frame)
            rep.line(
                len(server.requests) == 3, "5xx then success", f"{len(server.requests)} requests"
            )
            server.script(stub_mod.wrong_key())
            try:
                decider.decide(frame)
                rep.line(False, "wrong key is refused", "accepted")
            except InvalidResponse as exc:
                rep.line(True, "wrong key is refused", exc.code.value)
        finally:
            decider.close()
    except Exception as exc:
        rep.line(False, "stub self-test", f"{type(exc).__name__}: {exc}")
    finally:
        server.stop()


def _live(rep: _Report, loaded: LoadedConfig) -> None:
    print("live probe (one logical decision; transient attempts may retry)")
    cfg = loaded.config
    if not cfg.enabled or cfg.primary != "http":
        rep.line(False, "live", "system_one is disabled or not primary 'http' in this config")
        return
    from .factory import build_provider

    scrubber = Scrubber([cfg.api_key_value() or ""])
    provider = build_provider(cfg, scrubber=scrubber)
    try:
        info = provider.client.fetch_health(timeout_s=min(3.0, cfg.timeout_seconds))
        if info:
            print("  server /health: " + ", ".join(f"{k}={v}" for k, v in sorted(info.items())))
        else:
            print("  server /health: not available (informational)")
        res = asyncio.run(provider.probe())
    except Exception as exc:
        rep.line(False, "live request", scrubber.text(f"{type(exc).__name__}: {exc}"))
        return
    finally:
        provider.close()
    differs = bool(cfg.model) and res.model_id != cfg.model
    note = f" (differs from expected {cfg.model!r})" if differs else ""
    rep.line(
        True,
        "live request",
        scrubber.text(
            f"choice={res.option_id} confidence={res.confidence} latency_ms={res.latency_ms:.0f} "
            f"model={res.model_id!r}{note} "
            f"truncated={res.truncated} attempts={res.meta.get('attempts')}"
        ),
    )


def run_check(args: argparse.Namespace) -> int:
    rep = _Report()
    print("config")
    code = 0
    loaded: LoadedConfig | None = None
    try:
        loaded = load_config(args.config if args.config else env_config())
        for key, value in loaded.config.safe_summary().items():
            print(f"  {key}: {value}")
        for warning in loaded.warnings:
            print(f"  WARNING {warning}")
        rep.line(True, "config valid")
    except ConfigError as exc:
        for err in exc.errors:
            print(f"  ERROR {err}")
        rep.line(False, "config valid")
        code = 2
    _mock_self_test(rep)
    if not args.no_stub:
        _stub_self_test(rep)
    if args.live:
        if loaded is None:
            rep.line(False, "live", "needs a valid config")
        else:
            _live(rep, loaded)
    print("RESULT: " + ("FAIL" if rep.failed else "OK"))
    return code or (1 if rep.failed else 0)
