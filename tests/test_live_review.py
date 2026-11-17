"""ROM-free review delivery drills. All transports are synthetic local commands."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from pokegent.review import MAX_INPUT, MAX_LINE, Queue, Settings, main, sanitize

ROOT = Path(__file__).resolve().parents[1]
FAKE = '''
import json, os, sys, time
from pathlib import Path
home = Path(os.environ["LIVE_HOME"])
data = sys.stdin.read()
with (home / "attempts").open("a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "input": data}) + "\\n")
if (home / "busy").exists():
    raise SystemExit(10)
if (home / "slow").exists():
    (home / "entered").touch()
    time.sleep(10)
with (home / "delivered").open("a") as log:
    log.write(data)
'''


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_HOME", str(tmp_path))
    script = tmp_path / "transport.py"
    script.write_text(FAKE)
    return Settings(tmp_path, (sys.executable, str(script), "{target}", "{message}"),
                    target="synthetic-owner", min_interval=0, timeout=1, poll=.01)


def review(revision=1, **kwargs):
    return {"run_id": "live-0001", "verdict": "objective_done",
            "active_objective_revision": revision, **kwargs}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def event(revision=1, **kwargs):
    return {"run_id": "live-0001", "type": "progress.milestone_completed",
            "details": {"completed": True, "revision": revision}, **kwargs}


def append_event(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as log:
        log.write(json.dumps(value) + "\n")


def pending(queue):
    return queue.db.execute("SELECT COUNT(*) FROM reviews WHERE delivered=0").fetchone()[0]


def test_whitelist_and_bounded_input(settings, monkeypatch):
    cleaned = sanitize(review(token="never-copy", details={"password": "hidden"},
                              rc=True, state_path="secret=bad.state"))
    assert set(cleaned) == {"run_id", "verdict", "rc", "state_path", "active_objective_revision"}
    assert cleaned["rc"] is None and cleaned["state_path"] is None
    from io import StringIO
    monkeypatch.setattr(sys, "stdin", StringIO(" " * (MAX_INPUT + 1)))
    assert main(["notify"]) == 2
    assert not (settings.home / "attempts").exists()


@pytest.mark.parametrize("value", [
    [], {}, review(run_id="bad\nname"), review(verdict="secret"),
    review(verdict=[]), review(run_id=1),
])
def test_rejects_untrusted_identity(value):
    with pytest.raises(ValueError):
        sanitize(value)


def test_busy_failure_retry_restart_and_delivery_once(settings):
    (settings.home / "busy").touch()
    queue = Queue(settings)
    assert queue.enqueue(review(token="never-copy"))
    assert not queue.drain() and pending(queue) == 1
    queue.close()
    (settings.home / "busy").unlink()
    queue = Queue(settings)
    assert queue.enqueue(review()) and queue.drain()
    assert pending(queue) == 0
    queue.close()
    queue = Queue(settings)
    assert queue.enqueue(review()) and queue.drain()
    assert len((settings.home / "delivered").read_text().splitlines()) == 1
    assert "never-copy" not in (settings.home / "attempts").read_text()
    queue.close()


def test_rate_limit_coalesces_and_queue_full_retries(settings):
    queue = Queue(replace(settings, min_interval=20, queue_limit=2))
    queue.enqueue(review(1))
    assert queue.drain(now=100)
    queue.enqueue(review(2))
    queue.enqueue(review(3))
    assert not queue.enqueue(review(4))
    assert queue.enqueue(review(1))  # A known receipt is success even at capacity.
    assert not queue.drain(now=119)
    assert queue.drain(now=120)
    deliveries = [json.loads(row) for row in (settings.home / "delivered").read_text().splitlines()]
    assert [row["active_objective_revision"] for row in deliveries[1]["reviews"]] == [2, 3]
    assert queue.enqueue(review(4))
    queue.close()


def test_timeout_preserves_pending_and_disabled_delivery(settings):
    queue = Queue(replace(settings, timeout=.1))
    queue.enqueue(review())
    (settings.home / "slow").touch()
    before = time.monotonic()
    assert not queue.drain() and pending(queue) == 1
    assert time.monotonic() - before < 2
    (settings.home / "slow").unlink()
    assert queue.drain()
    queue.close()
    queue = Queue(replace(settings, command=()))
    queue.enqueue(review(2))
    assert not queue.drain() and pending(queue) == 1
    queue.close()


def test_in_run_completion_rotation_partial_and_restart(settings):
    out = settings.home / "runs/live-0001"
    log = out / "events.jsonl"
    append_event(log, event())
    write(settings.home / "live-run.json", {"run_id": out.name, "pid": os.getpid(),
                                           "supervisor_pid": os.getpid(), "status": "running"})
    queue = Queue(settings)
    queue.observe()
    assert pending(queue) == 1
    assert queue.drain()
    log.rename(out / "events-000001.jsonl")
    log.write_text(json.dumps(event(2))[:-1])
    queue.observe()
    assert pending(queue) == 0
    with log.open("a") as target:
        target.write("}\n")
    queue.close()
    queue = Queue(settings)
    queue.observe()
    queue.observe()
    assert pending(queue) == 1 and queue.drain()
    assert [json.loads(s)["active_objective_revision"] for s in
            (settings.home / "delivered").read_text().splitlines()] == [1, 2]
    assert (out / "events-000001.jsonl").exists()
    queue.close()


def test_run_end_owner_review_and_fast_segments(settings):
    queue = Queue(settings)
    for n in (1, 2):
        out = settings.home / f"runs/live-{n:04d}"
        write(out / "exit.json", {"verdict": "owner_review", "rc": 3,
                                 "state_path": str(out / "final.state"), "token": "hidden"})
        write(out / "summary.json", {"objective": {"active": {"revision": n}},
                                    "scene": "private facts"})
    # Current metadata can have moved on while the monitor was offline.
    write(settings.home / "live-run.json", {"run_id": "live-0003", "pid": os.getpid()})
    queue.observe()
    assert pending(queue) == 2 and queue.drain()
    queue.close()
    queue = Queue(settings)
    queue.observe()
    assert pending(queue) == 0
    data = json.loads((settings.home / "delivered").read_text())["reviews"]
    assert len(data) == 2 and all(row["verdict"] == "owner_review" for row in data)
    assert all(row["rc"] == 3 and row["state_path"].endswith("final.state") for row in data)
    assert "private facts" not in (settings.home / "attempts").read_text()
    queue.close()


def test_exit_record_preserves_revision_without_summary(settings):
    out = settings.home / "runs/live-0001"
    write(out / "exit.json", {"verdict": "whiteout", "rc": 1,
                             "active_objective_revision": 3})
    queue = Queue(settings)
    queue.observe()
    assert queue.drain()
    data = json.loads((settings.home / "delivered").read_text())
    assert data["verdict"] == "whiteout" and data["active_objective_revision"] == 3
    queue.close()


def test_dead_child_waits_for_live_supervisor_then_reports_orphan(settings):
    out = settings.home / "runs/live-0001"
    out.mkdir(parents=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=2)
    metadata = {"run_id": out.name, "status": "running", "pid": dead.pid,
                "supervisor_pid": os.getpid()}
    write(settings.home / "live-run.json", metadata)
    queue = Queue(settings)
    queue.observe()
    assert pending(queue) == 0
    metadata["supervisor_pid"] = dead.pid
    write(settings.home / "live-run.json", metadata)
    queue.observe()
    assert pending(queue) == 1 and queue.drain()
    assert json.loads((settings.home / "delivered").read_text())["verdict"] == "none"
    queue.close()


def test_full_queue_keeps_completion_cursor_for_later(settings):
    config = replace(settings, queue_limit=1)
    queue = Queue(config)
    log = settings.home / "runs/live-0001/events.jsonl"
    append_event(log, event(1))
    append_event(log, event(2))
    queue.observe()
    assert pending(queue) == 1 and queue.drain()
    queue.close()
    queue = Queue(config)
    queue.observe()
    assert pending(queue) == 1 and queue.drain()
    assert len((settings.home / "delivered").read_text().splitlines()) == 2
    queue.close()


def test_observation_does_not_wait_for_delivery(settings):
    (settings.home / "slow").touch()
    queue = Queue(replace(settings, timeout=.5))
    queue.enqueue(review())

    def slow_delivery():
        other = Queue(replace(settings, timeout=.5))
        try:
            return other.drain()
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(slow_delivery)
        deadline = time.monotonic() + 2
        while not (settings.home / "entered").exists():
            assert time.monotonic() < deadline
            time.sleep(.005)
        append_event(settings.home / "runs/live-0001/events.jsonl", event(2))
        queue.observe()
        assert pending(queue) == 2 and not future.done()
        assert not future.result(timeout=2)
    queue.close()


def test_default_hc_has_atomic_idle_guard(settings, monkeypatch):
    # Shadow hc with a local fake; never contact a real Core or owner.
    fake = settings.home / "hc"
    fake.write_text(f"#!{sys.executable}\n" + FAKE)
    fake.chmod(0o700)
    monkeypatch.setenv("PATH", str(settings.home))
    monkeypatch.delenv("POKEGENT_OWNER_COMMAND", raising=False)
    monkeypatch.setenv("POKEGENT_OWNER_TARGET", "synthetic-owner")
    queue = Queue(Settings.from_env())
    queue.enqueue(review())
    assert queue.drain()
    args = json.loads((settings.home / "attempts").read_text())["argv"]
    assert args[:4] == ["send", "--if", "idle", "synthetic-owner"]
    queue.close()


def test_wrappers_queue_and_drain(settings):
    env = {**os.environ, "PY": sys.executable, "LIVE_HOME": str(settings.home),
           "POKEGENT_OWNER_COMMAND": json.dumps(list(settings.command)),
           "POKEGENT_OWNER_MIN_INTERVAL_S": "0"}
    notifier = ROOT / "ops/hermes/pokegent-owner-notify"
    subprocess.run([str(notifier)], env=env, input=json.dumps(review()), text=True, check=True)
    assert not (settings.home / "delivered").exists()
    subprocess.run([str(notifier), "--drain"], env=env, check=True)
    subprocess.run([str(ROOT / "ops/pokegent_live.sh"), "--once"], env=env, check=True)


def test_malformed_oversized_and_foreign_events_do_not_leak(settings):
    out = settings.home / "runs/live-0001"
    out.mkdir(parents=True)
    (out / "events.jsonl").write_text("{" + "x" * (MAX_LINE + 10) + "\n{bad}\n")
    append_event(out / "events.jsonl", event(run_id="foreign"))
    append_event(out / "events.jsonl", event(details={"completed": False, "secret": "hidden"}))
    append_event(out / "events.jsonl", event(details={"completed": True, "revision": []}))
    append_event(out / "events.jsonl", event())
    queue = Queue(settings)
    queue.observe()
    assert pending(queue) == 1 and queue.drain()
    assert "hidden" not in (settings.home / "attempts").read_text()
    queue.close()


@pytest.mark.parametrize(("key", "value"), [
    ("TIMEOUT_S", "nan"), ("POLL_S", "0"), ("QUEUE_LIMIT", "-1"),
    ("MIN_INTERVAL_S", "inf"), ("COMMAND", "{}"), ("COMMAND", '[1]'),
])
def test_invalid_settings_fail(settings, monkeypatch, key, value):
    monkeypatch.setenv("POKEGENT_OWNER_" + key, value)
    with pytest.raises(ValueError):
        Settings.from_env()


def test_continuous_monitor_busy_owner_restart(settings):
    env = {**os.environ, "PY": sys.executable, "LIVE_HOME": str(settings.home),
           "POKEGENT_OWNER_COMMAND": json.dumps(list(settings.command)),
           "POKEGENT_OWNER_MIN_INTERVAL_S": "0", "POKEGENT_OWNER_POLL_S": ".01"}
    (settings.home / "busy").touch()
    observer = subprocess.Popen([str(ROOT / "ops/pokegent_live.sh")], env=env)
    queue = Queue(settings)
    try:
        log = settings.home / "runs/live-0001/events.jsonl"
        append_event(log, event(1))
        append_event(log, event(2))
        deadline = time.monotonic() + 5
        while pending(queue) < 2 or not (settings.home / "attempts").exists():
            assert observer.poll() is None and time.monotonic() < deadline
            time.sleep(.01)
        assert not (settings.home / "delivered").exists()
    finally:
        observer.terminate()
        observer.wait(timeout=2)
        queue.close()
    (settings.home / "busy").unlink()
    subprocess.run([str(ROOT / "ops/pokegent_live.sh"), "--once"], env=env, check=True)
    subprocess.run([str(ROOT / "ops/hermes/pokegent-owner-notify"), "--drain"],
                   env=env, check=True)
    rows = (settings.home / "delivered").read_text().splitlines()
    assert len(rows) == 1
    assert [r["active_objective_revision"] for r in json.loads(rows[0])["reviews"]] == [1, 2]


def test_documented_owner_commands(settings, monkeypatch, capsys):
    from pokegent import cli

    monkeypatch.delenv("FIRERED_ROM_PATH", raising=False)
    common = ["--file", str(settings.home / "objective.json")]
    commands = [
        ["status", "--live-home", str(settings.home)],
        ["objective", "show", *common],
        ["objective", "write", *common, "--expected-revision", "0", "--objective-id",
         "restore_health", "--kind", "heal", "--rationale", "Restore party health."],
        ["objective", "validate", *common],
        ["objective", "show", *common],
    ]
    for command in commands:
        with pytest.raises(SystemExit) as ended:
            cli.main(command)
        assert ended.value.code == 0
        capsys.readouterr()
    assert json.loads((settings.home / "objective.json").read_text())["revision"] == 1
