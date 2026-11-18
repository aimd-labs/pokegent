"""Continuous foreground supervisor for installed ``pokegent run`` segments (POSIX).

Configuration is declarative environment data; see docs/live-supervisor.md.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pokegent.events import EventType, read_events
from pokegent.live import payload, read_json, successor_state, verdict


def atomic_write(path: Path, content: str) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path: Path, value: dict) -> None:
    atomic_write(path, json.dumps(value, sort_keys=True) + "\n")


@dataclass(frozen=True)
class Settings:
    home: Path
    config: Path
    objective: Path
    runner: str
    decider: str = "system1"
    on_objective_done: str = "continue"
    max_steps: int = 3000
    port: int | None = None
    plan: Path | None = None
    allow_remote: bool = False
    allow_checkpoint: bool = False
    stop_on_clear: bool = True
    max_whiteouts: int = 10
    max_stalled_segments: int = 3
    max_crashes: int = 5
    crash_window: float = 600
    crash_backoff: float = 10
    crash_backoff_max: float = 60
    segment_delay: float = 1
    stop_timeout: float = 15
    poll_interval: float = 0.2

    @classmethod
    def from_env(cls) -> Settings:
        def number(name, default, cast=float, minimum=0):
            value = cast(os.environ.get(name, default))
            if not math.isfinite(value) or value < minimum:
                raise ValueError(f"{name} must be finite and >= {minimum}")
            return value

        def boolean(name, default=False):
            value = os.environ.get(name, str(default)).lower()
            if value not in {"true", "false", "1", "0"}:
                raise ValueError(f"{name} must be true or false")
            return value in {"true", "1"}

        if not os.environ.get("LIVE_HOME"):
            raise ValueError("LIVE_HOME is required")
        home = Path(os.environ["LIVE_HOME"]).expanduser().resolve()
        port = number("PORT", 0, int)
        if port and not 19700 <= port <= 19799:
            raise ValueError("PORT must be 0 (disabled) or 19700-19799")
        decider = os.environ.get("DECIDER", "system1")
        if decider not in {"agent", "system1", "hybrid", "first", "forced", "external"}:
            raise ValueError("invalid DECIDER")
        on_objective_done = os.environ.get("ON_OBJECTIVE_DONE", "continue")
        if on_objective_done not in {"continue", "hold", "pause"}:
            raise ValueError("ON_OBJECTIVE_DONE must be continue, hold or pause")
        default_runner = Path(sys.executable).with_name("pokegent")
        return cls(
            home=home,
            config=Path(os.environ.get("RUN_CONFIG", home / "run.yaml")).resolve(),
            objective=Path(os.environ.get("OBJECTIVE_FILE", home / "objective.json")).resolve(),
            runner=os.environ.get("RUNNER_BIN", str(default_runner)),
            decider=decider,
            on_objective_done=on_objective_done,
            max_steps=number("MAX_STEPS", 3000, int, 1),
            port=port or None,
            plan=Path(os.environ["OBJECTIVE_PLAN"]).resolve()
            if os.environ.get("OBJECTIVE_PLAN") else None,
            allow_remote=boolean("ALLOW_REMOTE"),
            allow_checkpoint=boolean("ALLOW_CHECKPOINT_RECOVERY"),
            stop_on_clear=boolean("STOP_ON_CLEAR", True),
            max_whiteouts=number("MAX_WHITEOUTS", 10, int),
            max_stalled_segments=number("MAX_STALLED_SEGMENTS", 3, int, 1),
            max_crashes=number("MAX_CRASHES", 5, int, 1),
            crash_window=number("CRASH_WINDOW_S", 600, minimum=0.01),
            crash_backoff=number("CRASH_BACKOFF_S", 10),
            crash_backoff_max=number("CRASH_BACKOFF_MAX_S", 60),
            segment_delay=number("SEGMENT_DELAY_S", 1),
            stop_timeout=number("STOP_TIMEOUT_S", 15, minimum=0.01),
            poll_interval=number("POLL_INTERVAL_S", 0.2, minimum=0.01),
        )


class Supervisor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.home = settings.home
        self.stopping = False
        self.crashes: deque[float] = deque()
        self.whiteouts = 0
        self.stalled_segments = 0

    def journal(self, message: str) -> None:
        line = f"{datetime.now(timezone.utc).isoformat()} {message}\n"
        with (self.home / "journal.log").open("a", encoding="utf-8") as target:
            target.write(line)
        print(line, end="", flush=True)

    def stop_requested(self) -> bool:
        return self.stopping or (self.home / "stop").exists()

    def delay(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self.stop_requested() and not (self.home / "restart").exists():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(self.settings.poll_interval, remaining))

    def command(self, state: Path, run_id: str, out: Path) -> list[str]:
        s = self.settings
        args = [
            s.runner, "run", "--state", str(state), "--no-state-fallback",
            "--config", str(s.config),
            "--objective", str(s.objective), "--on-objective-done", s.on_objective_done,
            "--decider", s.decider, "--max-steps", str(s.max_steps),
            "--run-id", run_id, "--out", str(out),
        ]
        if s.port:
            args += ["--viewer-port", str(s.port)]
        if s.allow_remote:
            args.append("--allow-remote")
        if s.plan:
            args += ["--objective-plan", str(s.plan)]
        return args

    def promote(self, out: Path) -> Path:
        state = successor_state(out, allow_checkpoint=self.settings.allow_checkpoint)
        atomic_write(self.home / "current.state", str(state) + "\n")
        return state

    def objective_revision(self) -> int | None:
        value = (read_json(self.settings.objective) or {}).get("revision")
        return value if type(value) is int else None

    def block(self, result: dict, reason: str) -> None:
        result.update(status="blocked", supervisor_stop_reason=reason)
        write_json(self.home / "live-run.json", result)
        self.journal(reason)

    def track_stalls(self, result: dict) -> None:
        out = Path(result["out"])
        summary = read_json(out / "summary.json") or {}
        stalled = not result.get("requested") and (
            summary.get("stop_reason") in {"safe_pause", "no_progress"}
            or summary.get("verdict") == "no_progress"
        )
        progressed = any(
            event.type in {
                EventType.ACTION_VERIFIED.value, EventType.ACTION_INTERRUPTED.value,
                EventType.ACTION_FAILED.value,
            } and event.details.get("progressed") is True
            for event in read_events(out, skip_corrupt=True)
        )
        self.stalled_segments = self.stalled_segments + 1 if stalled and not progressed else 0
        result["stalled_segments"] = self.stalled_segments
        if (result["status"] != "blocked"
                and self.stalled_segments >= self.settings.max_stalled_segments):
            result.update(
                status="blocked", verdict="owner_review",
                supervisor_stop_reason="stalled segment limit reached: owner review required",
            )

    def block_provider(self, record: dict, out: Path) -> bool:
        """A published terminal disposition wins over recovery and state promotion."""
        if record.get("verdict") != "owner_review" and verdict(out) != "owner_review":
            return False
        record.update(status="blocked", verdict="owner_review",
                      supervisor_stop_reason="provider_configuration: owner review required")
        # Publish both artifacts before returning; either one prevents restart recovery.
        result = payload(record, out, "owner_review")
        result["reason"] = "provider_configuration"
        result["rc"] = record.get("rc", result["rc"])
        details = ((read_json(out / "summary.json") or {}).get("stop_details")
                   or (read_json(out / "exit.json") or {}).get("stop_details")
                   or record.get("stop_details"))
        if isinstance(details, dict):
            result["stop_details"] = details
            record["stop_details"] = details
        write_json(out / "exit.json", result)
        write_json(self.home / "live-run.json", record)
        return True

    def recover(self) -> dict | None:
        record_path = self.home / "live-run.json"
        if not record_path.exists():
            return
        record = read_json(record_path)
        if not record or record.get("status") not in (
            "preparing", "running", "finished", "blocked"
        ):
            raise ValueError("unrecognized live-run metadata; ownership needs review")
        if record["status"] == "blocked":
            raise ValueError("previous run is blocked; review live-run.json before resuming")
        self.stalled_segments = record.get("stalled_segments", 0)
        if type(self.stalled_segments) is not int or self.stalled_segments < 0:
            raise ValueError("invalid stalled segment count; ownership needs review")
        if record["status"] == "finished" and not record.get("out"):
            if record.get("verdict") == "owner_review":
                self.block(record, "provider_configuration: owner review required")
            return record
        out = Path(record.get("out", ""))
        if out.parent != self.home / "runs" or out.name != record.get("run_id"):
            raise ValueError("invalid previous run directory")
        if self.block_provider(record, out):
            return record
        if record["status"] == "finished":
            return record
        pid = record.get("pid")
        if pid is not None:
            if type(pid) is not int or pid <= 0:
                raise ValueError("invalid previous PID; ownership needs review")
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise ValueError("previous PID still exists; ownership needs review")
        state = self.promote(out)
        result = payload(record, out)
        result.update(state_path=str(state), recovered=True)
        record.update(result, status="finished", objective_revision=self.objective_revision())
        self.track_stalls(record)
        result.update(verdict=record["verdict"], stalled_segments=record["stalled_segments"])
        write_json(out / "exit.json", result)
        write_json(record_path, record)
        self.journal(f"recovered {out.name} from atomic state publication")
        return record

    def next_run(self) -> tuple[str, Path]:
        counter = self.home / "counter"
        number = int(counter.read_text().strip()) if counter.exists() else 0
        if number < 0:
            raise ValueError("invalid run counter")
        while True:
            number += 1
            run_id = f"live-{number:04d}"
            out = self.home / "runs" / run_id
            try:
                out.mkdir()
            except FileExistsError:
                continue
            atomic_write(counter, f"{number}\n")
            return run_id, out

    def segment(self, lock_fd: int, state: Path) -> tuple[dict, bool]:
        run_id, out = self.next_run()
        metadata = {
            "run_id": run_id, "out": str(out), "state": str(state), "pid": None,
            "supervisor_pid": os.getpid(), "status": "preparing",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "objective_revision": self.objective_revision(),
            "stalled_segments": self.stalled_segments,
        }
        record_path = self.home / "live-run.json"
        write_json(record_path, metadata)
        self.journal(f"start {run_id}")
        requested = False
        forced = False
        deadline = None
        with (self.home / "runs" / f"{run_id}.log").open("wb") as log:
            # The child retains the lock if its supervisor dies. Never start a second writer.
            child = subprocess.Popen(
                self.command(state, run_id, out), stdout=log, stderr=subprocess.STDOUT,
                pass_fds=(lock_fd,),
            )
            try:
                metadata.update(pid=child.pid, status="running")
                write_json(record_path, metadata)
                while child.poll() is None:
                    if deadline is None and (
                        self.stop_requested() or (self.home / "restart").exists()
                    ):
                        requested = True
                        (self.home / "restart").unlink(missing_ok=True)
                        with suppress(ProcessLookupError):
                            child.terminate()
                        deadline = time.monotonic() + self.settings.stop_timeout
                    if deadline is not None and time.monotonic() >= deadline:
                        with suppress(ProcessLookupError):
                            child.kill()
                        forced = True
                        break
                    time.sleep(self.settings.poll_interval)
                rc = child.wait(timeout=5)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
        metadata.update(rc=rc, requested=requested, forced=forced)
        if self.block_provider(metadata, out):
            return metadata, read_json(out / "summary.json") is not None
        name = "interrupted" if requested else verdict(out)
        state_error = None
        try:
            successor = str(self.promote(out))
        except (ValueError, OSError) as exc:
            successor = None
            state_error = str(exc)
        result = payload(metadata, out, name)
        result.update(rc=rc, state_path=successor, requested=requested, forced=forced)
        metadata.update(
            result, status="blocked" if state_error or forced else "finished",
            finished_at=datetime.now(timezone.utc).isoformat(), state_error=state_error,
            objective_revision=self.objective_revision(),
        )
        self.track_stalls(metadata)
        result.update(
            verdict=metadata["verdict"], stalled_segments=metadata["stalled_segments"],
        )
        write_json(out / "exit.json", result)
        write_json(record_path, metadata)
        self.journal(f"end {run_id} rc={rc} verdict={name} state_ok={successor is not None}")
        return metadata, read_json(out / "summary.json") is not None

    def run(self) -> int:
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "runs").mkdir(exist_ok=True)
        # Do not unlink this file: replacing it would allow two independent flock inodes.
        with (self.home / "live.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("live lifecycle lock is held") from exc
            if self.stop_requested():
                (self.home / "stop").unlink(missing_ok=True)
                return 0
            if not self.settings.config.is_file():
                raise ValueError("RUN_CONFIG must name an existing file")
            if self.settings.plan is not None and not self.settings.plan.is_file():
                raise ValueError("OBJECTIVE_PLAN must name an existing file")
            recovered = self.recover()
            if recovered:
                if recovered["status"] == "blocked":
                    self.journal(recovered["supervisor_stop_reason"])
                    return 3 if recovered["verdict"] == "owner_review" else 4
                if recovered.get("verdict") == "clear" and self.settings.stop_on_clear:
                    self.journal("recovered game clear: stopping")
                    return 0
            while not self.stop_requested():
                (self.home / "restart").unlink(missing_ok=True)
                pointer = (self.home / "current.state").read_text(encoding="utf-8").strip()
                state = Path(pointer)
                if not state.is_absolute() or not state.is_file() or state.stat().st_size == 0:
                    raise ValueError("current.state must point to a nonempty absolute file")
                result, has_summary = self.segment(lock.fileno(), state)
                if result["status"] == "blocked":
                    self.journal(result.get("supervisor_stop_reason")
                                 or "blocked: successor state or shutdown needs review")
                    return 3 if result["verdict"] == "owner_review" else 4
                if self.stop_requested():
                    (self.home / "stop").unlink(missing_ok=True)
                    return 0
                if result["verdict"] == "clear" and self.settings.stop_on_clear:
                    self.journal("game clear: stopping")
                    return 0
                self.whiteouts = self.whiteouts + 1 if result["verdict"] == "whiteout" else 0
                if self.settings.max_whiteouts and self.whiteouts >= self.settings.max_whiteouts:
                    self.block(result, "whiteout limit reached: owner review required")
                    return 3
                if result["rc"] in {2, 4} and not result["requested"]:
                    self.block(result, "usage/environment failure: owner review required")
                    return 4
                crashed = not result["requested"] and result["verdict"] != "interrupted" and (
                    not has_summary or result["rc"] not in {0, 1, 3}
                )
                delay = self.settings.segment_delay
                if crashed:
                    now = time.monotonic()
                    self.crashes.append(now)
                    while self.crashes and now - self.crashes[0] > self.settings.crash_window:
                        self.crashes.popleft()
                    if len(self.crashes) >= self.settings.max_crashes:
                        self.block(result, "crash limit reached: owner review required")
                        return 4
                    delay = min(
                        self.settings.crash_backoff_max,
                        self.settings.crash_backoff * 2 ** (len(self.crashes) - 1),
                    )
                    self.journal(f"crash: backoff {delay:g}s")
                self.delay(delay)
            (self.home / "stop").unlink(missing_ok=True)
            return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        supervisor = Supervisor(Settings.from_env())
        previous = {}
        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous[signum] = signal.signal(
                    signum, lambda *_: setattr(supervisor, "stopping", True)
                )
            return supervisor.run()
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"supervisor: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
