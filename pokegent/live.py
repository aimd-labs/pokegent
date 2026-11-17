"""Shared, ROM-free run-end classification for live operations."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as source:
            raw = source.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def verdict(run_dir: str | Path) -> str:
    out = Path(run_dir)
    if (read_json(out / "exit.json") or {}).get("requested") is True:
        return "interrupted"
    data = read_json(out / "summary.json")
    if data is None:
        return "none"
    if data.get("stop_reason") in ("interrupted", "forced_interrupt", "graceful_stop_timeout"):
        return "interrupted"
    if data.get("game_cleared") is True:
        return "clear"
    scenes = data.get("scenes_entered") or {}
    if isinstance(scenes, dict) and isinstance(scenes.get("system.whiteout"), int):
        if scenes["system.whiteout"] > 0:
            return "whiteout"
    objective = data.get("objective") or {}
    if isinstance(objective, dict) and (
        objective.get("done") or objective.get("completed") or objective.get("aborted")
    ):
        return "objective_done"
    return "other"


def successor_state(run_dir: Path, *, allow_checkpoint: bool = False) -> Path:
    """Trust only this run's atomic publications, never a previous run's state.

    Native/RAM validation belongs to the runner. A summary that refuses a final save must
    not be overridden by the mere presence of a file. Without a summary (hard stop), only
    the runner's atomic final publication or explicitly enabled checkpoint recovery is used.
    """
    summary = read_json(run_dir / "summary.json")
    states = summary.get("states") if summary is not None else None
    final = run_dir / "final.state"
    if final.exists():
        if summary is not None and (
            not isinstance(states, dict) or states.get("final") != final.name
        ):
            raise ValueError("final state was not accepted by the runner")
        candidate = final
    elif isinstance(states, dict) and states.get("final") is not None:
        raise ValueError("runner's accepted final state is missing")
    elif allow_checkpoint:
        if summary is None:
            names = [p.name for p in (run_dir / "checkpoints").glob("ckpt-*.state")]
        else:
            names = states.get("checkpoints", []) if isinstance(states, dict) else []
        if not isinstance(names, list) or not names or any(
            not isinstance(name, str) or not re.fullmatch(r"ckpt-\d{7,}\.state", name)
            for name in names
        ):
            raise ValueError("no accepted checkpoint")
        candidate = run_dir / "checkpoints" / max(names, key=lambda name: int(name[5:-6]))
    else:
        raise ValueError("no final state; checkpoint recovery is disabled")
    if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size == 0:
        raise ValueError("successor state is missing, empty or a symlink")
    if not candidate.resolve().is_relative_to(run_dir.resolve()):
        raise ValueError("successor state is outside the run directory")
    return candidate.resolve()


def payload(
    metadata: str | dict, run_dir: str | Path, verdict_name: str | None = None
) -> dict[str, Any]:
    metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
    out = Path(run_dir)
    summary = read_json(out / "summary.json") or {}
    exit_record = read_json(out / "exit.json") or {}
    objective = summary.get("objective") or {}
    active = (objective.get("active") or {}) if isinstance(objective, dict) else {}
    state = exit_record.get("state_path")
    if not exit_record:
        try:
            state = str(successor_state(out))
        except (ValueError, OSError):
            state = None
    return {
        "run_id": metadata["run_id"],
        "verdict": verdict_name or verdict(out),
        "rc": exit_record.get("rc", summary.get("exit_code")),
        "state_path": state,
        "active_objective_revision": active.get("revision") if isinstance(active, dict) else None,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", nargs=3, metavar=("METADATA_JSON", "RUN_DIR", "VERDICT"))
    parser.add_argument("run_dir", nargs="?")
    args = parser.parse_args(argv)
    if args.payload:
        print(json.dumps(payload(*args.payload), sort_keys=True))
    elif args.run_dir:
        print(verdict(args.run_dir))
    else:
        parser.error("run_dir or --payload is required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
