"""
Pokemon Agent — CLI entry point.

Implementation of the small agent-facing objective and status commands.

Usage:
    pokegent info  --rom path/to/rom.gba
    pokegent provider check [--config cfg.yaml] [--live]
      (System One, docs/provider-integration.md)
    pokegent run --state s.state --decider agent|system1|hybrid|first|forced
                      --max-steps N --out runs/<id>/
                      [--objective FILE] [--objective-plan PLAN.json]
                      [--on-objective-done pause|hold|continue]
                      [--viewer-port 19700-19799 [--realtime|--no-realtime]
                       [--viewer-static DIR] [--viewer-public-only]]    (docs/addons.md)
    pokegent scenes [--all] [--markdown|--json]            (scene ownership / coverage table)
    pokegent objective show|validate|write|clear [--file objective.json]
    pokegent status [--live-home DIR]                     (agent bridge status)
    pokegent dex status [--state FILE] [--live-home DIR] [--json]
    pokegent --version
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any

from pokegent.goals import ObjectiveStore, validate

__version__ = "0.1.0"


def _removed_hybrid_timeout(_value: str) -> float:
    raise argparse.ArgumentTypeError(
        "--hybrid-escalation-timeout-s was removed: escalations wait for the agent"
    )


def _removed_external_timeout(_value: str) -> float:
    raise argparse.ArgumentTypeError(
        "--external-timeout was removed: agent choices wait for the agent"
    )


def _frame_wait(value: str) -> float:
    try:
        wait = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--wait must be a number between 0 and 30") from None
    if not 0 <= wait <= 30:
        raise argparse.ArgumentTypeError("--wait must be a number between 0 and 30")
    return wait


@lru_cache(maxsize=1)
def _registered_internal_refs() -> frozenset[str]:
    """Refs registered by the same family registry used by the runnable app."""
    from pokegent.actions import Registries, register_all

    registries = Registries.new()
    register_all(registries)
    return registries.executors.refs() | registries.verifiers.refs()


def _objective_text_warnings(objective: Any) -> list[dict[str, str]]:
    if not isinstance(objective, dict):
        return []
    refs = _registered_internal_refs()
    warnings = []
    for field in ("rationale",):
        text = json.dumps(objective.get(field, ""), ensure_ascii=False)
        matches = sorted(
            (ref for ref in refs if ref in text), key=lambda item: (text.index(item), item)
        )
        for ref in matches:
            warnings.append(
                {
                    "code": "internal_reference_in_text",
                    "field": field,
                    "ref": ref,
                    "detail": f"{field} contains registered internal reference {ref!r}",
                }
            )
    return warnings


def live_home(value: str | None = None) -> Path:
    selected = value or os.environ.get("LIVE_HOME")
    if not selected:
        raise ValueError("live home is required: pass --live-home or set LIVE_HOME")
    return Path(selected).expanduser()


def objective_path(value: str | None, home: str | None = None) -> Path:
    return Path(value).expanduser() if value else live_home(home) / "objective.json"


def _directory(rom: str | None):
    path = rom or os.environ.get("FIRERED_ROM_PATH")
    if not path or not Path(path).is_file():
        return None, None
    from pokegent.games.gen3.firered.rom.rom_data import load_profiles
    from pokegent.goals.directory import RomMapDirectory

    profile = load_profiles().get("firered-us-rev0")
    if profile is None:
        return None, "firered-us-rev0 profile is unavailable"
    return RomMapDirectory(Path(path).read_bytes(), profile), None


def _json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True, default=str))


def _http_error(exc: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(exc.read().decode("utf-8"))
        reasons = body.get("reasons") if isinstance(body, dict) else None
        if not reasons and isinstance(body, dict):
            proposal = body.get("proposal")
            reasons = proposal.get("reasons") if isinstance(proposal, dict) else None
        if reasons:
            return f"HTTP {exc.code}: " + "; ".join(
                ": ".join(
                    str(item.get(key, "")) for key in ("code", "field", "detail") if item.get(key)
                )
                for item in reasons
                if isinstance(item, dict)
            )
        return json.dumps(body, sort_keys=True)
    except (OSError, ValueError, UnicodeDecodeError):
        return str(exc)


def _result(path: Path, store: ObjectiveStore, verdict) -> dict[str, Any]:
    objective = verdict.objective.model_dump(mode="json") if verdict.objective else None
    return {
        "ok": verdict.ok,
        "file": str(path),
        "revision": store.revision(),
        "reasons": [reason.as_dict() for reason in verdict.reasons],
        "objective": objective,
        "warnings": _objective_text_warnings(objective),
        "deep_checks": store.directory is not None,
    }


def objective_show(path: Path, rom: str | None = None) -> dict[str, Any]:
    directory, directory_error = _directory(rom)
    store = ObjectiveStore(path, directory=directory)
    active = store.current()
    return {
        "ok": directory_error is None,
        "file": str(path),
        "active": active.model_dump(mode="json") if active else None,
        "revision": store.revision(),
        "deep_checks": directory is not None,
        "deep_check_error": directory_error,
        "warnings": [warning.as_dict() for warning in store.warnings],
    }


def objective_validate(path: Path, rom: str | None = None) -> dict[str, Any]:
    directory, directory_error = _directory(rom)
    store = ObjectiveStore(path, directory=directory)
    if directory_error:
        return {
            "ok": False,
            "file": str(path),
            "reasons": [{"code": "rom", "detail": directory_error}],
        }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {
            "ok": False,
            "file": str(path),
            "reasons": [{"code": "unreadable_file", "detail": f"{type(exc).__name__}: {exc}"}],
        }
    verdict = validate(raw, directory=directory)
    return _result(path, store, verdict)


def _split(values: list[str] | None) -> list[str]:
    return [item for value in values or [] for item in value.split(",") if item]


def _write_payload(args) -> dict[str, Any]:
    if args.json:
        source = (
            sys.stdin.read() if args.json == "-" else Path(args.json).read_text(encoding="utf-8")
        )
        payload = json.loads(source)
        if not isinstance(payload, dict):
            raise ValueError("--json must contain one objective object")
    else:
        payload = {}
    target = dict(payload.get("target") or {})
    if args.json and args.set_by is not None and "set_by" in payload:
        if payload["set_by"] != args.set_by:
            raise ValueError(
                "--set-by conflicts with JSON payload set_by: "
                f"{args.set_by!r} != {payload['set_by']!r}"
            )
    fields = (
        ("map", args.target_map),
        ("warp", args.warp),
        ("x", args.x),
        ("y", args.y),
        ("npc", args.npc),
    )
    for name, value in fields:
        if value is not None:
            target[name] = value
    if target or args.kind == "hold":
        payload["target"] = target
    fields = (
        ("objective_id", args.objective_id),
        ("kind", args.kind),
        ("rationale", args.rationale),
        ("set_by", args.set_by),
    )
    for name, value in fields:
        if value is not None:
            payload[name] = value
    if args.abort_if is not None:
        payload["abort_if"] = _split(args.abort_if)
    if args.next_review_on is not None:
        payload["next_review_on"] = _split(args.next_review_on)
    if args.success_probe is not None:
        payload["success_probes"] = [json.loads(item) for item in args.success_probe]
    return payload


def objective_write(args) -> int:
    path = objective_path(args.file, args.live_home)
    directory, directory_error = _directory(args.rom)
    if directory_error:
        _json(
            {
                "ok": False,
                "file": str(path),
                "reasons": [{"code": "rom", "detail": directory_error}],
            }
        )
        return 2
    try:
        payload = _write_payload(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _json({"ok": False, "file": str(path), "reasons": [{"code": "input", "detail": str(exc)}]})
        return 2
    store = ObjectiveStore(path, directory=directory)
    payload.setdefault("revision", store.revision() + 1)
    payload.setdefault("set_by", "agent")
    result = store.propose(payload, expected_revision=args.expected_revision)
    warnings = _objective_text_warnings(payload)
    _json(
        {
            "ok": result.accepted,
            "file": str(path),
            "proposal": result.as_dict(),
            "objective": result.objective.model_dump(mode="json") if result.objective else None,
            "deep_checks": directory is not None,
            "warnings": warnings,
        }
    )
    return 0 if result.accepted else 2


def objective_clear(args) -> int:
    path = objective_path(args.file, args.live_home)
    store = ObjectiveStore(path)
    result = store.clear(expected_revision=args.expected_revision, set_by=args.set_by)
    _json({"ok": bool(result.get("cleared")), "file": str(path), **result})
    return 0 if result.get("cleared") else 2


def objective_command(args) -> int:
    path = objective_path(args.file, args.live_home)
    if args.action == "show":
        _json(objective_show(path, args.rom))
        return 0
    if args.action == "validate":
        result = objective_validate(path, args.rom)
        _json(result)
        return 0 if result["ok"] else 2
    if args.action == "write":
        return objective_write(args)
    return objective_clear(args)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _latest_run(home: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    runs = [p for p in (home / "runs").glob("live-*") if p.is_dir()]
    runs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    summary = _read_json(runs[0] / "summary.json") if runs else None
    events: list[dict[str, Any]] = []
    if runs:
        try:
            lines = (runs[0] / "events.jsonl").read_text(encoding="utf-8").splitlines()
            for line in lines[-20:]:
                value = json.loads(line)
                if isinstance(value, dict):
                    events.append(value)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return summary or {}, events


def _viewer(port: int, event_limit: int) -> dict[str, Any] | None:
    try:
        url = f"http://127.0.0.1:{port}/ops/telemetry"
        with urllib.request.urlopen(url, timeout=0.35) as response:
            value = json.loads(response.read())
        if not isinstance(value, dict):
            return None
        events_url = f"http://127.0.0.1:{port}/ops/events?limit={event_limit}"
        with urllib.request.urlopen(events_url, timeout=0.35) as response:
            events = json.loads(response.read())
        value["events"] = events.get("events", []) if isinstance(events, dict) else []
        return value
    except (OSError, ValueError, urllib.error.URLError):
        return None


def status_command(args) -> int:
    home = live_home(args.live_home)
    telemetry = _viewer(args.viewer_port, args.events)
    summary, file_events = _latest_run(home)
    objective = objective_show(objective_path(args.objective, args.live_home))
    if telemetry is not None:
        state = telemetry.get("state") or {}
        run = telemetry.get("run") or {}
        result = {
            "source": "viewer",
            "where": {
                "map": (state.get("map") or {}).get("name"),
                "position": state.get("position"),
            },
            "party": state.get("party", []),
            "objective": {**objective, "telemetry": telemetry.get("objectives")},
            "progress": {"run": run, "objective": telemetry.get("objectives")},
            "last_decisions": (telemetry.get("pokegent") or {}).get("decisions", [])[
                -args.events :
            ],
            "events": telemetry.get("events", [])[-args.events :],
            "whiteouts": (telemetry.get("run_stats") or {}).get("blackouts", 0),
            "attempts": (run.get("counters") or {}).get("iterations", 0),
        }
    else:
        journal = []
        try:
            journal = (home / "journal.log").read_text(encoding="utf-8").splitlines()[-200:]
        except OSError:
            pass
        result = {
            "source": "files",
            "where": {
                "state": (
                    (home / "current.state").read_text(encoding="utf-8").strip()
                    if (home / "current.state").is_file()
                    else None
                )
            },
            "party": [],
            "objective": objective,
            "progress": {"summary": summary},
            "last_decisions": [],
            "events": file_events[-args.events :],
            "whiteouts": sum("whiteout" in line.lower() for line in journal),
            "attempts": sum(" start live-" in line for line in journal),
        }
    _json(result)
    return 0


__all__ = ["objective_command", "status_command"]

BANNER = r"""
  ____       _                              _                    _
 |  _ \ ___ | | _____ _ __ ___   ___  _ __ / \   __ _  ___ _ __ | |_
 | |_) / _ \| |/ / _ \ '_ ` _ \ / _ \| '_ / _ \ / _` |/ _ \ '_ \| __|
 |  __/ (_) |   <  __/ | | | | | (_) | | / ___ \ (_| |  __/ | | | |_
 |_|   \___/|_|\_\___|_| |_| |_|\___/|_|/_/   \_\__, |\___|_| |_|\__|
                                                  |___/  v{version}
"""

_legacy_notice_printed = False


def _detect_game_type(rom_path: str) -> str:
    ext = Path(rom_path).suffix.lower()
    if ext in (".gb", ".gbc"):
        return "red"
    elif ext == ".gba":
        return "firered"
    return "unknown"


def cmd_info(args):
    """Display ROM information."""
    rom = Path(args.rom).expanduser().resolve()
    if not rom.exists():
        print(f"ERROR: ROM file not found: {rom}", file=sys.stderr)
        sys.exit(1)

    game_type = _detect_game_type(str(rom))
    size = rom.stat().st_size

    # Compute SHA-256
    sha = hashlib.sha256()
    with open(rom, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)

    print(f"ROM path:     {rom}")
    print(f"File size:    {size:,} bytes ({size / 1024 / 1024:.2f} MB)")
    print(f"SHA-256:      {sha.hexdigest()}")
    print(f"Extension:    {rom.suffix}")
    print(f"Detected as:  {game_type}")


def cmd_provider(args):
    """System One provider tools (addons/system_one)."""
    if getattr(args, "provider_unavailable", None):
        from pokegent.app import ADDON_MISSING_HINT

        print(ADDON_MISSING_HINT.format(error=args.provider_unavailable), file=sys.stderr)
        sys.exit(2)
    if getattr(args, "provider_command", None) != "check":
        print("usage: pokegent provider check [--config PATH] [--live]", file=sys.stderr)
        sys.exit(2)
    config_path = args.config or ("pokegent.yaml" if Path("pokegent.yaml").is_file() else None)
    if config_path:
        import yaml

        try:
            raw_config = (
                yaml.safe_load(Path(config_path).expanduser().read_text(encoding="utf-8")) or {}
            )
        except (OSError, yaml.YAMLError, ValueError) as exc:
            print(f"ERROR: {config_path}: {exc}", file=sys.stderr)
            sys.exit(2)
        if "system_one" not in raw_config:
            from pokegent.user_config import load_user_config, to_legacy_config

            try:
                args.config = to_legacy_config(load_user_config(config_path))
            except ValueError as exc:
                print(f"ERROR: {config_path}: {exc}", file=sys.stderr)
                sys.exit(2)
    from addons.system_one.check import run_check

    sys.exit(run_check(args))


def cmd_run(args):
    """Run the router loop headless on a savestate (offline; no server, no agent)."""
    from pokegent.app import EXIT_USAGE, AppError, run_offline, safe_run_id
    from pokegent.user_config import load_user_config

    try:
        user = load_user_config(
            args.config,
            cli={
                "decider": args.decider,
                "rom_path": args.rom,
                "autoplay": args.autoplay,
                "external": {"port": args.external_port},
                "hybrid": {
                    "escalate_below_confidence": args.hybrid_escalate_below_confidence,
                    "override_window_s": args.hybrid_override_window_s,
                },
                "system1": {
                    "outage": args.system1_outage,
                    "max_outage_s": args.system1_max_outage_s,
                },
                "output": {"run_dir": args.out},
            },
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
    args.decider = user["decider"]
    args.rom = args.rom or user.get("rom_path")
    args.external_port = (
        user["external"]["port"] if args.external_port is None else args.external_port
    )
    args.external_timeout = 300.0
    args.hybrid_escalate_below_confidence = (
        user["hybrid"]["escalate_below_confidence"]
        if args.hybrid_escalate_below_confidence is None
        else args.hybrid_escalate_below_confidence
    )
    args.hybrid_override_window_s = (
        user["hybrid"]["override_window_s"]
        if args.hybrid_override_window_s is None
        else args.hybrid_override_window_s
    )
    args.system1_outage = (
        user.get("system1", {}).get("outage")
        if args.system1_outage is None
        else args.system1_outage
    )
    args.system1_max_outage_s = (
        user.get("system1", {}).get("max_outage_s")
        if args.system1_max_outage_s is None
        else args.system1_max_outage_s
    )
    args.out = args.out or user.get("output", {}).get("run_dir")
    # Non-teaching field-menu actions remain opt-in. Export only an enabled config value so a
    # run cannot mutate the process environment to a false value and contaminate later runs.
    if "POKEGENT_FIELD_MENU" not in os.environ:
        os.environ["POKEGENT_FIELD_MENU"] = "1" if user.get("field", {}).get("menus", True) else "0"
    state = Path(args.state).expanduser() if args.state else None
    run_id = args.run_id or safe_run_id(
        state.stem if state else "new-game", time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    )
    out = args.out or str(Path("runs") / run_id)
    try:
        report = run_offline(
            state=state,
            out_dir=out,
            decider=args.decider,
            max_steps=args.max_steps,
            config_path=args.config,
            rom_path=args.rom,
            allow_remote=args.allow_remote,
            run_id=run_id,
            deadline_s=args.deadline_s,
            objective=args.objective,
            objective_plan=args.objective_plan,
            on_objective_done=args.on_objective_done,
            ignore_objective=args.ignore_objective,
            decision_history=args.decision_history,
            viewer=_viewer_overrides(args),
            stream={"port": args.stream_port} if args.stream_port is not None else None,
            dashboard={"port": args.dashboard_port} if args.dashboard_port is not None else None,
            external_port=args.external_port,
            external_timeout=args.external_timeout,
            autoplay=args.autoplay,
            external_walk_step_limit=args.external_walk_step_limit,
            new_game=args.new_game,
            state_fallback=not args.no_state_fallback,
            hybrid_escalate_below_confidence=args.hybrid_escalate_below_confidence,
            hybrid_override_window_s=args.hybrid_override_window_s,
            system1_outage=args.system1_outage,
            system1_max_outage_s=args.system1_max_outage_s,
        )
    except AppError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(getattr(exc, "exit_code", EXIT_USAGE))
    sys.exit(report.exit_code)


def _viewer_overrides(args):
    """The ``--viewer-*`` / ``--realtime`` flags the user gave (the rest comes from ``viewer:``)."""
    given = {
        "port": args.viewer_port,
        "public_only": True if args.viewer_public_only else None,
        "static": args.viewer_static,
        "realtime": args.realtime,
        "linger_s": args.viewer_linger,
    }
    return {k: v for k, v in given.items() if v is not None}


def cmd_scenes(args):
    """Print who detects/builds each catalog scene and the P0 gaps (no emulator needed)."""
    import json

    from pokegent.actions import Registries, register_all
    from pokegent.state.catalog import load_scene_catalog

    catalog = load_scene_catalog()
    report = register_all(Registries.new(catalog)).ownership(catalog)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    elif args.markdown:
        print(report.format_markdown())
    else:
        print(report.format_text(all_rows=args.all))
    sys.exit(1 if report.conflicts else 0)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="pokegent",
        description="Pokemon Agent — AI-powered Pokemon game controller",
    )
    try:
        from pokegent.env import apply_legacy_env_aliases, reject_removed_env

        apply_legacy_env_aliases()
        reject_removed_env()
    except ValueError as exc:
        parser.error(str(exc))
    parser.add_argument("--version", action="version", version=f"pokegent {__version__}")

    sub = parser.add_subparsers(dest="command")

    # --- info ---
    info_p = sub.add_parser("info", help="Show ROM information")
    info_p.add_argument("--rom", required=True, help="Path to Pokemon ROM file")

    # --- run (offline router loop, M2.5) ---
    run_p = sub.add_parser(
        "run",
        help="Run the router loop headless on a savestate",
        description=(
            "Load a savestate into the emulator and run the router loop for a bounded number of "
            "steps. Writes events.jsonl and summary.json to --out. Exit codes: 0 step budget "
            "reached, 1 SAFE_PAUSE/blocked/stopped, 2 usage, 3 escalation paused the run or the "
            "objective is done and nothing newer is in the file (WAITING_OBJECTIVE), "
            "4 ROM/state/emulator problem, 124 graceful-stop deadline, 130 forced interrupt."
        ),
    )
    source = run_p.add_mutually_exclusive_group(required=True)
    source.add_argument("--state", help="savestate (*.state), read-only")
    source.add_argument("--new-game", action="store_true", help="cold boot and start a fresh game")
    run_p.add_argument(
        "--no-state-fallback", action="store_true",
        help="refuse an invalid requested savestate without trying older checkpoints",
    )
    run_p.add_argument(
        "--decider",
        choices=("agent", "system1", "hybrid", "first", "forced", "external"),
        default=None,
        help="agent: loopback HTTP agent bridge; system1: System One; "
        "hybrid: System One with agent override (falls back to agent); "
        "first: first legal option (test stub); "
        "forced: only scenes with one option",
    )
    run_p.add_argument(
        "--autoplay",
        choices=("off",),
        default=None,
        help="auto-run policy (off; action selection belongs to the decider)",
    )
    run_p.add_argument(
        "--external-port", type=int, default=None, help="external decider port (19700-19799)"
    )
    run_p.add_argument(
        "--external-timeout", type=_removed_external_timeout, default=None, help=argparse.SUPPRESS
    )
    run_p.add_argument(
        "--external-walk-step-limit",
        type=int,
        default=None,
        help="maximum grass-walk steps per external action (default 8)",
    )
    run_p.add_argument("--hybrid-escalate-below-confidence", type=float, default=None)
    run_p.add_argument(
        "--hybrid-escalation-timeout-s",
        type=_removed_hybrid_timeout,
        default=None,
        help=argparse.SUPPRESS,
    )
    run_p.add_argument("--hybrid-override-window-s", type=float, default=None)
    run_p.add_argument("--system1-outage", choices=("wait", "pause"), default=None)
    run_p.add_argument("--system1-max-outage-s", type=float, default=None)
    run_p.add_argument("--max-steps", type=int, default=200, help="router iterations (default 200)")
    run_p.add_argument(
        "--out", default=None, help="output directory (default from config or runs/<run-id>/)"
    )
    run_p.add_argument("--run-id", default=None, help="run id (default <state>-<utc time>)")
    run_p.add_argument("--config", default=None, help="user YAML (default ./pokegent.yaml)")
    run_p.add_argument("--rom", default=None, help="ROM path (default $FIRERED_ROM_PATH)")
    run_p.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow a non-loopback System One endpoint (real calls to the shared provider)",
    )
    run_p.add_argument("--deadline-s", type=float, default=None, help="wall-clock budget")
    run_p.add_argument(
        "--objective",
        default=None,
        metavar="FILE",
        help="the objective file agent writes (docs/objective-file.md); read every iteration, "
        "a corrupt or stale file keeps the last valid objective. Default: none (goal-less)",
    )
    run_p.add_argument(
        "--objective-plan",
        default=None,
        metavar="PLAN.json",
        help="a scripted objective queue (no agent): each objective is written through the "
        "objective file when the previous one's probes succeed. Without --objective the file is "
        "<out>/objective.json",
    )
    run_p.add_argument(
        "--on-objective-done",
        choices=("pause", "hold", "continue"),
        default=None,
        help="a finished objective at the next free-roaming decision: hold = stay alive without "
        "input and poll the file (default), pause = stop with WAITING_OBJECTIVE (exit 3), "
        "continue = clear the completed revision and keep playing in the same run",
    )
    run_p.add_argument(
        "--ignore-objective",
        action="store_true",
        help="headless/maintenance opt-out: a satisfied objective success probe does not stop "
        "the run (an objective ended early by abort_if still finishes it); default off",
    )
    run_p.add_argument(
        "--decision-history",
        type=int,
        default=None,
        metavar="N",
        help="removed: decision history is owned by the agent",
    )

    viewer = run_p.add_argument_group(
        "viewer",
        "A read-only HTTP viewer inside the run process (telemetry JSON, game frames, a fallback "
        "page): docs/addons.md. GET only, loopback-only, ports 19700-19799 only.",
    )
    viewer.add_argument(
        "--viewer-port",
        type=int,
        default=None,
        metavar="PORT",
        help=(
            "[deprecated] start the combined viewer; use --stream-port and --dashboard-port "
            "instead (port must be within 19700-19799). Also pace the emulator at 60 fps "
            "unless --no-realtime"
        ),
    )
    viewer.add_argument(
        "--viewer-static",
        default=None,
        metavar="DIR",
        help="serve this directory read-only at /dashboard/. Default: the local dashboard addon; "
        "'' = serve none",
    )
    viewer.add_argument(
        "--viewer-public-only",
        action="store_true",
        help="serve ONLY the public surface; default: both surfaces",
    )
    viewer.add_argument(
        "--realtime",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="pace the emulator at ~60 frames per wall second while a command needs frames, so a "
        "human can follow (same game, only slower). Default: on with --viewer-port, else off",
    )
    viewer.add_argument(
        "--stream-port",
        type=int,
        default=None,
        metavar="PORT",
        help="enable the stream addon on loopback (frame.png, frames/audio WebSockets)",
    )
    viewer.add_argument(
        "--dashboard-port",
        type=int,
        default=None,
        metavar="PORT",
        help="enable the dashboard addon on loopback (operator UI, telemetry, decisions)",
    )
    viewer.add_argument(
        "--viewer-linger",
        type=float,
        default=None,
        metavar="SECONDS",
        help="keep serving the final state this long after the run ended (default 3)",
    )

    # --- external decider helper ---------------------------------------------------------------
    step_p = sub.add_parser("step", help="Read or answer an external decider frame")
    step_p.add_argument("--port", type=int, default=19760)
    step_p.add_argument(
        "--wait", type=_frame_wait, default=None, help="frame long-poll seconds (default 30)"
    )
    step_sub = step_p.add_subparsers(dest="step_action")
    choose_p = step_sub.add_parser("choose", help="choose an option")
    choose_p.add_argument("option_id")
    step_sub.add_parser("interrupt", help="stop a running macro at its next safe boundary")
    screenshot_p = step_sub.add_parser("screenshot", help="save the latest external game frame")
    screenshot_p.add_argument("--out", default="screenshot.png", metavar="FILE")
    step_sub.add_parser("briefing", help="read the compact planning briefing")
    objective_step = step_sub.add_parser("objective", help="read or change the live objective")
    objective_step.add_argument("objective_action", choices=("get", "set", "clear"))
    objective_step.add_argument("source", nargs="?", help="objective JSON file or inline JSON")
    objective_step.add_argument(
        "--expected-revision",
        type=int,
        default=None,
        help="compare-and-set revision; defaults to current + 1",
    )
    objective_step.add_argument("--set-by", default=None, help="writer identity (default: agent)")

    # --- scenes (ownership / coverage table) ---
    # --- agent bridge -------------------------------------------------------------------------
    objective_p = sub.add_parser("objective", help="Read, validate, write or clear an objective")
    objective_sub = objective_p.add_subparsers(dest="action", required=True)
    for action in ("show", "validate", "write", "clear"):
        action_parser = objective_sub.add_parser(action)
        action_parser.add_argument(
            "--file", default=None, help="objective.json (default LIVE_HOME/objective.json)"
        )
        action_parser.add_argument("--live-home", default=None, help="LIVE_HOME override")
        action_parser.add_argument(
            "--rom", default=None, help="ROM for deep map checks (default $FIRERED_ROM_PATH)"
        )
        action_parser.add_argument("--expected-revision", type=int, default=None)
        action_parser.add_argument("--set-by", default=None)
        if action == "write":
            action_parser.add_argument(
                "--json", default=None, help="objective JSON file, or - for stdin"
            )
            action_parser.add_argument("--objective-id", default=None)
            action_parser.add_argument(
                "--kind",
                choices=("reach", "talk", "fight", "heal", "train", "hold"),
                default=None,
            )
            action_parser.add_argument("--map", dest="target_map", default=None)
            action_parser.add_argument("--warp", type=int, default=None)
            action_parser.add_argument("--x", type=int, default=None)
            action_parser.add_argument("--y", type=int, default=None)
            action_parser.add_argument("--npc", type=int, default=None)
            action_parser.add_argument("--rationale", default=None)
            action_parser.add_argument("--abort-if", action="append", default=None)
            action_parser.add_argument("--next-review-on", action="append", default=None)
            action_parser.add_argument(
                "--success-probe", action="append", default=None, metavar="JSON"
            )

    status_p = sub.add_parser("status", help="Print concise JSON for agent")
    status_p.add_argument("--live-home", default=None)
    status_p.add_argument("--objective", default=None, help="objective file override")
    status_p.add_argument("--viewer-port", type=int, default=None)
    status_p.add_argument("--events", type=int, default=10)

    rom_data_p = sub.add_parser("rom-data", help="Build private ROM-derived data")
    rom_data_sub = rom_data_p.add_subparsers(dest="rom_data_action", required=True)
    rom_data_build = rom_data_sub.add_parser("build", help="Generate a hash-keyed ROM data cache")
    rom_data_build.add_argument("--rom", default=None, help="ROM path (default $FIRERED_ROM_PATH)")
    rom_data_build.add_argument('--if-missing', action='store_true',
                                help='reuse a valid cache; repair missing or invalid data')
    local_setup = rom_data_sub.add_parser('setup', help='Generate all seven private local files')
    local_setup.add_argument('--rom', type=Path, required=True)
    local_setup.add_argument('--out-dir', type=Path, required=True,
                             help='absolute external path; existing manual data is preserved')
    local_setup.add_argument('--decomp', type=Path,
                             help='offline clean pinned checkout or gzip tarball')
    map_index = rom_data_sub.add_parser("map-index", help="Generate a numeric structural index")
    map_index.add_argument("--numeric-labels", action="store_true", required=True,
                           help="explicitly accept numeric labels without named compatibility")
    map_index.add_argument("--rom", type=Path, required=True)
    map_index.add_argument("--out-dir", type=Path, required=True,
                           help="new empty directory outside the checkout; never overwritten")
    # --- scenes (ownership / coverage table) ---
    scenes_p = sub.add_parser("scenes", help="Scene ownership and coverage table")
    scenes_p.add_argument("--all", action="store_true", help="include unclaimed P1/P2 rows")
    scenes_p.add_argument("--markdown", action="store_true")
    scenes_p.add_argument("--json", action="store_true")

    try:  # the System One addon is optional (spec 3: fail-open addons), but never silently absent
        from addons.system_one.check import add_parser as add_provider_parser

        add_provider_parser(sub)
    except ImportError as exc:
        unavailable = sub.add_parser(
            "provider", help="System One tools (UNAVAILABLE: addon not importable)"
        )
        unavailable.add_argument("rest", nargs="*")
        unavailable.set_defaults(provider_unavailable=f"{type(exc).__name__}: {exc}")

    args = parser.parse_args(argv)

    if args.command == "provider":
        cmd_provider(args)
    elif args.command == "info":
        cmd_info(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "step":
        from pokegent.server import request

        try:
            if args.step_action == "choose":
                wait = "" if args.wait is None else f"&wait={args.wait}"
                frame = request(args.port, f"/frame?all=1{wait}")
                request(
                    args.port,
                    "/choose",
                    body={
                        "frame_id": frame["frame_id"],
                        "option_id": args.option_id,
                    },
                )
                path = f"/frame?wait={args.wait}" if args.wait is not None else "/frame"
                print(json.dumps(request(args.port, path), indent=2))
            elif args.step_action == "screenshot":
                from pokegent.server import request_bytes

                image = request_bytes(args.port, "/screenshot")
                with open(args.out, "wb") as output:
                    output.write(image)
            elif args.step_action == "interrupt":
                print(json.dumps(request(args.port, "/interrupt", body={}), indent=2))
            elif args.step_action == "briefing":
                print(json.dumps(request(args.port, "/briefing"), indent=2))
            elif args.step_action == "objective":
                from urllib.request import Request, urlopen

                path = "/objective"
                if args.objective_action == "get":
                    print(json.dumps(request(args.port, path), indent=2))
                elif args.objective_action == "clear":
                    query = []
                    if args.expected_revision is not None:
                        query.append(f"expected_revision={args.expected_revision}")
                    if args.set_by is not None:
                        query.append(f"set_by={args.set_by}")
                    suffix = "?" + "&".join(query) if query else ""
                    with urlopen(
                        Request(f"http://127.0.0.1:{args.port}{path}{suffix}", method="DELETE")
                    ) as response:
                        print(response.read().decode())
                else:
                    if not args.source:
                        raise ValueError("objective set requires a JSON file or inline JSON")
                    if args.source.startswith("{"):
                        payload = json.loads(args.source)
                    else:
                        payload = json.loads(Path(args.source).read_text(encoding="utf-8"))
                    if args.expected_revision is None:
                        current = request(args.port, path)
                        payload.setdefault("revision", int(current.get("revision", 0)) + 1)
                    else:
                        payload.setdefault("revision", args.expected_revision + 1)
                    payload.setdefault("set_by", args.set_by or "agent")
                    print(json.dumps(request(args.port, path, body=payload), indent=2))
            else:
                path = f"/frame?wait={args.wait}" if args.wait is not None else "/frame"
                print(json.dumps(request(args.port, path), indent=2))
        except urllib.error.HTTPError as exc:
            print(f"ERROR: {_http_error(exc)}", file=sys.stderr)
            sys.exit(1)
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "scenes":
        cmd_scenes(args)
    elif args.command == "objective":
        sys.exit(objective_command(args))
    elif args.command == "status":
        args.viewer_port = args.viewer_port or int(os.environ.get("PORT", "19750"))
        try:
            sys.exit(status_command(args))
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(2)
    elif args.command == "rom-data":
        if args.rom_data_action == 'setup':
            from pokegent.games.gen3.firered.rom.local_setup import setup
            from pokegent.games.gen3.firered.rom.rom_data import ProfileError, UnsupportedRomError
            from pokegent.games.gen3.firered.rom.setup_source import SetupError
            from pokegent.local_data import LocalDataError

            try:
                setup(args.rom, args.out_dir, decomp=args.decomp)
            except SetupError as exc:
                print(f'error: {exc}', file=sys.stderr)
                sys.exit(2)
            except (OSError, ProfileError, UnsupportedRomError, LocalDataError,
                    ValueError, TypeError, KeyError):
                print('error: local setup failed; check supported ROM, pinned source, and '
                      'external output. Existing files are preserved; see docs/local-data.md.',
                      file=sys.stderr)
                sys.exit(2)
            print('Private local data and ROM cache are ready. Restart runtime processes.')
            return
        if args.rom_data_action == "map-index":
            from pokegent.games.gen3.firered.rom.map_index import MapIndexError, build_map_index
            from pokegent.games.gen3.firered.rom.rom_data import ProfileError

            try:
                build_map_index(args.rom, args.out_dir)
            except (MapIndexError, OSError, ProfileError):
                print("error: numeric map index failed; check ROM and empty external output.",
                      file=sys.stderr)
                sys.exit(2)
            print("Numeric map index written.")
            return
        from pokegent.games.gen3.firered.rom.rom_cache import RomDataError, build, ensure

        try:
            operation = ensure if args.if_missing else build
            target = operation(args.rom or os.environ.get("FIRERED_ROM_PATH"))
        except (RomDataError, TypeError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(2)
        print(f"ROM data cache: {target}")
    else:
        parser.print_help()
        sys.exit(1)


def legacy_main(argv=None):
    print(
        "deprecated: pokejev-agent is an alias for pokegent and will be removed after one release",
        file=sys.stderr,
    )
    return main(argv)


if __name__ == "__main__":
    main()
