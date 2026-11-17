"""Read-only live observer and durable, optional owner review delivery (POSIX)."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from pokegent.live import payload, read_json

VERDICTS = {"none", "other", "clear", "whiteout", "objective_done", "interrupted", "owner_review"}
MAX_INPUT = 16 * 1024
MAX_LINE = 64 * 1024


def sanitize(value: dict, *, home: Path | None = None) -> dict:
    """Whitelist typed facts only; never forward event details or arbitrary prose."""
    if not isinstance(value, dict) or not isinstance(value.get("run_id"), str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value["run_id"]
    ):
        raise ValueError("invalid review run_id")
    if not isinstance(value.get("verdict"), str) or value["verdict"] not in VERDICTS:
        raise ValueError("invalid review verdict")
    rc = value.get("rc")
    revision = value.get("active_objective_revision")
    state = value.get("state_path")
    reference = None
    if home is not None and isinstance(state, str):
        root = home.resolve()
        run = Path("runs") / value["run_id"]
        for prefix in (str(root / run) + "/", run.as_posix() + "/"):
            if state.startswith(prefix):
                artifact = state[len(prefix):]
                if re.fullmatch(r"final\.state|checkpoints/ckpt-[0-9]{7,}\.state", artifact):
                    candidate = root / run / artifact
                    if candidate.resolve() == candidate:
                        reference = (run / artifact).as_posix()
                break
    return {
        "run_id": value["run_id"], "verdict": value["verdict"],
        "rc": rc if type(rc) is int and -255 <= rc <= 255 else None,
        "state_path": reference,
        "active_objective_revision": (
            revision if type(revision) is int and 1 <= revision < 2**63 else None
        ),
    }


def review_id(value: dict, kind: str) -> str:
    identity = [value["run_id"], kind, value["active_objective_revision"]]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Settings:
    home: Path
    command: tuple[str, ...] = ()
    target: str = ""
    min_interval: float = 180
    timeout: float = 10
    poll: float = 2
    queue_limit: int = 32
    batch_limit: int = 4

    @classmethod
    def from_env(cls) -> Settings:
        if not os.environ.get("LIVE_HOME"):
            raise ValueError("LIVE_HOME is required")

        def number(name, default, cast=float, minimum=0):
            value = cast(os.environ.get("POKEGENT_OWNER_" + name, default))
            if not math.isfinite(value) or value < minimum:
                raise ValueError(f"invalid owner setting: {name}")
            return value

        target = os.environ.get("POKEGENT_OWNER_TARGET", "")
        raw = os.environ.get("POKEGENT_OWNER_COMMAND")
        command = json.loads(raw) if raw else (
            ["hc", "send", "--if", "idle", "{target}", "{message}"] if target else []
        )
        if not isinstance(command, list) or any(
            not isinstance(arg, str) or not arg or "\x00" in arg for arg in command
        ):
            raise ValueError("POKEGENT_OWNER_COMMAND must be a JSON argv array")
        return cls(
            Path(os.environ["LIVE_HOME"]).expanduser().resolve(), tuple(command), target,
            number("MIN_INTERVAL_S", 180), number("TIMEOUT_S", 10, minimum=0.01),
            number("POLL_S", 2, minimum=0.01), number("QUEUE_LIMIT", 32, int, 1),
            number("BATCH_LIMIT", 4, int, 1),
        )


class Queue:
    """The queue, delivery receipts and log cursors share one private SQLite journal."""

    def __init__(self, settings: Settings):
        self.settings = settings
        directory = settings.home / "owner-review"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory = directory
        self.db = sqlite3.connect(directory / "queue.sqlite3", timeout=1)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS reviews (
                id TEXT PRIMARY KEY, body TEXT, delivered INTEGER NOT NULL DEFAULT 0,
                kind TEXT NOT NULL DEFAULT 'run_end'
            );
            CREATE TABLE IF NOT EXISTS event_cursors (
                generation TEXT PRIMARY KEY, identity TEXT NOT NULL,
                offset INTEGER NOT NULL, skipping INTEGER NOT NULL, boundary TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS finished (run TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS clock (id INTEGER PRIMARY KEY, attempted REAL NOT NULL);
        """)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            columns = {column[1] for column in self.db.execute("PRAGMA table_info(reviews)")}
            if "kind" not in columns:
                self.db.execute("ALTER TABLE reviews ADD COLUMN kind TEXT NOT NULL "
                                "DEFAULT 'run_end'")

    def close(self):
        self.db.close()

    def enqueue(self, value: dict, *, kind: str = "run_end") -> bool:
        facts = sanitize(value, home=self.settings.home)
        body = json.dumps(facts, sort_keys=True, separators=(",", ":"))
        key = review_id(facts, kind)
        legacy_key = hashlib.sha256(body.encode()).hexdigest()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute("SELECT 1 FROM reviews WHERE id IN (?, ?)",
                               (key, legacy_key)).fetchone():
                return True
            if self.db.execute("SELECT COUNT(*) FROM reviews WHERE delivered=0").fetchone()[0] >= (
                self.settings.queue_limit
            ):
                return False
            self.db.execute("INSERT INTO reviews(id, body, kind) VALUES (?, ?, ?)",
                            (key, body, kind))
        return True

    def drain(self, *, now: float | None = None) -> bool:
        """Success means an acknowledged delivery or no pending work, never busy/timeout."""
        with (self.directory / "delivery.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            rows = self.db.execute(
                "SELECT id, body, kind FROM reviews WHERE delivered=0 ORDER BY rowid LIMIT ?",
                (self.settings.batch_limit,),
            ).fetchall()
            if not rows:
                return True
            if not self.settings.command:
                return False
            now = time.time() if now is None else now
            last = self.db.execute("SELECT attempted FROM clock WHERE id=1").fetchone()
            if last and now - last[0] < self.settings.min_interval:
                return False
            # Limit attempts too: a busy or unavailable owner must not be hammered.
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO clock VALUES (1, ?)", (now,))
            # Revalidate pending records too: older journals may contain unchecked path text.
            items = []
            for _, raw, kind in rows:
                facts = sanitize(json.loads(raw), home=self.settings.home)
                items.append({**facts, "review_id": review_id(facts, kind)})
            body = json.dumps(items[0] if len(items) == 1 else {"reviews": items}, sort_keys=True)
            message = (
                "Pokegent owner review: inspect fresh status and the current objective before "
                "deciding whether to write one newer, checkable objective. Gameplay continues "
                "independently. Do not send game input or control services. The same review id "
                "may arrive twice; treat it as one. Review facts: " + body
            )
            command = [arg.replace("{target}", self.settings.target).replace("{message}", message)
                       for arg in self.settings.command]
            # This batch token is a convenience; individual review_ids survive rebatching.
            receipt = hashlib.sha256(
                "\n".join(item["review_id"] for item in items).encode()
            ).hexdigest()
            try:
                with subprocess.Popen(
                    command, stdin=subprocess.PIPE, text=True, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True,
                    env={**os.environ, "POKEGENT_REVIEW_ID": receipt},
                ) as child:
                    try:
                        child.communicate(body + "\n", timeout=self.settings.timeout)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
                        return False
                    rc = child.returncode
            except OSError:
                return False
            if rc != 0:
                return False
            with self.db:
                self.db.executemany(
                    "UPDATE reviews SET delivered=1, body=NULL WHERE id=?", [(r[0],) for r in rows]
                )
            return True

    def events(self, out: Path, run_id: str) -> bool:
        """Follow logical generations; an inode alone cannot establish log continuity."""
        paths = sorted(out.glob("events-??????.jsonl")) + [out / "events.jsonl"]
        budget = 256
        for path in paths:
            try:
                with path.open("rb") as source:
                    stat = os.fstat(source.fileno())
                    identity = f"{stat.st_dev}:{stat.st_ino}"
                    first = source.readline(MAX_LINE + 1)
                    try:
                        sequence = json.loads(first).get("seq")
                    except (ValueError, AttributeError):
                        sequence = None
                    sequence = sequence if type(sequence) is int else None
                    key = f"{run_id}:{sequence}:{hashlib.sha256(first).hexdigest()}"
                    cursor = self.db.execute(
                        "SELECT identity, offset, skipping, boundary FROM event_cursors "
                        "WHERE generation=?", (key,)
                    ).fetchone()
                    offset, skipping = 0, 0
                    if cursor and cursor[0] == identity and cursor[1] <= stat.st_size:
                        if log_boundary(source, cursor[1]) == cursor[3]:
                            offset, skipping = cursor[1:3]
                    source.seek(offset)
                    while budget:
                        line = source.readline(MAX_LINE + 1)
                        if not line:
                            break
                        budget -= 1
                        if not line.endswith(b"\n") and len(line) <= MAX_LINE:
                            break  # Partial publication: retry it next poll.
                        if len(line) > MAX_LINE or skipping:
                            skipping = int(not line.endswith(b"\n"))
                        else:
                            try:
                                event = json.loads(line)
                                details = event.get("details", {})
                                if event.get("type") == "progress.milestone_completed" and (
                                    event.get("run_id") == run_id
                                    and details.get("completed") is True
                                    and type(details.get("revision")) is int
                                    and 1 <= details["revision"] < 2**63
                                ):
                                    if not self.enqueue({
                                        "run_id": run_id, "verdict": "objective_done",
                                        "active_objective_revision": details.get("revision"),
                                    }, kind="objective_completion"):
                                        return False
                            except (ValueError, AttributeError, TypeError):
                                pass  # A malformed record never becomes an owner prompt.
                        offset = source.tell()
                        with self.db:
                            self.db.execute(
                                "INSERT OR REPLACE INTO event_cursors VALUES (?, ?, ?, ?, ?)",
                                (key, identity, offset, skipping, log_boundary(source, offset)),
                            )
                    if not budget:
                        return False
            except FileNotFoundError:
                if path.name != "events.jsonl":
                    return False  # Rename between listing and open: retry next poll.
        return True

    def observe(self):
        """Scan segment records too, so fast segment transitions cannot hide run ends."""
        metadata = read_json(self.settings.home / "live-run.json") or {}
        for out in sorted((self.settings.home / "runs").glob("live-*")):
            if not out.is_dir() or out.is_symlink():
                continue
            run_id = out.name
            if self.db.execute("SELECT 1 FROM finished WHERE run=?", (run_id,)).fetchone():
                continue
            record = read_json(out / "exit.json")
            # A dead child's supervisor owns final publication. Only synthesize a review if
            # both are dead; never race the supervisor's final owner_review classification.
            orphan = metadata.get("run_id") == run_id and metadata.get("status") == "running" and (
                not alive(metadata.get("pid")) and not alive(metadata.get("supervisor_pid"))
            )
            # Final publication must precede the scan that authorizes a finished receipt.
            if not self.events(out, run_id):
                continue
            if record is not None or orphan:
                name = record.get("verdict") if record else None
                name = name if isinstance(name, str) and name in VERDICTS else None
                review = payload({"run_id": run_id}, out, name)
                if record and "active_objective_revision" in record:
                    review["active_objective_revision"] = record["active_objective_revision"]
                if self.enqueue(review):
                    with self.db:
                        self.db.execute("INSERT OR IGNORE INTO finished VALUES (?)", (run_id,))


def log_boundary(source, offset: int) -> str:
    """Check the bytes preceding an offset before trusting it in a later scan."""
    source.seek(max(0, offset - 256))
    return hashlib.sha256(source.read(offset - source.tell())).hexdigest()


def alive(pid) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        status = Path(f"/proc/{pid}/stat")
        return not status.exists() or status.read_text().rsplit(")", 1)[-1].split()[0] != "Z"
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True  # Unknown ownership is never evidence of death.


def deliver(settings: Settings) -> bool:
    queue = Queue(settings)
    try:
        return queue.drain()
    finally:
        queue.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("monitor", "notify"))
    parser.add_argument("--drain", action="store_true", help="attempt one delivery batch")
    parser.add_argument("--once", action="store_true", help="observe once without delivery")
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
        queue = Queue(settings)
        try:
            if args.mode == "notify":
                if args.drain:
                    return 0 if queue.drain() else 75
                raw = sys.stdin.read(MAX_INPUT + 1)
                if len(raw) > MAX_INPUT:
                    raise ValueError("review input too large")
                return 0 if queue.enqueue(json.loads(raw)) else 75
            if args.once:
                queue.observe()
                return 0
            with ThreadPoolExecutor(max_workers=1) as worker:
                pending = None
                while True:
                    queue.observe()
                    if pending is None or pending.done():
                        if pending is not None:
                            pending.result()
                        pending = worker.submit(deliver, settings)
                    time.sleep(settings.poll)
        finally:
            queue.close()
    except (ValueError, OSError, sqlite3.Error):
        print("owner review failed; check private configuration/journal", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
