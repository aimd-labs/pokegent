"""Supervisor integration drills use a synthetic child; no ROM, emulator or services."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_hybrid_stuck_escalation import harness as hybrid_harness  # noqa: F401

from pokegent.live import payload, successor_state, verdict
from pokegent.supervisor import Settings, atomic_write

FAKE_RUNNER = r'''
import argparse
import json
import os
import signal
import time
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("command", choices=["run"])
for key in ("state", "config", "objective", "on-objective-done", "decider", "max-steps",
            "run-id", "out", "viewer-port", "objective-plan"):
    p.add_argument("--" + key)
p.add_argument("--allow-remote", action="store_true")
p.add_argument("--no-state-fallback", action="store_true")
a = p.parse_args()
home = Path(os.environ["LIVE_HOME"])
out = Path(a.out)
n = int(a.run_id.split("-")[-1])
steps = json.loads((home / "scenario.json").read_text())
step = steps[min(n - 1, len(steps) - 1)]
writer = home / "writer"
fd = os.open(writer, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.close(fd)
calls = home / "calls.jsonl"
with calls.open("a") as log:
    log.write(json.dumps({**vars(a), "pid": os.getpid(),
                          "input": Path(a.state).read_text()}) + "\n")
stopped = False
def stop(*_):
    global stopped
    stopped = True
signal.signal(signal.SIGTERM, signal.SIG_IGN if step.get("ignore_term") else stop)
signal.signal(signal.SIGINT, stop)
(out / "ready").touch()
if step.get("wait"):
    while not stopped:
        time.sleep(.01)
if step.get("objective"):
    pending = home / "objective.pending"
    pending.write_text(json.dumps(step["objective"]))
    pending.replace(Path(a.objective))
if step.get("checkpoint"):
    (out / "checkpoints").mkdir()
    (out / "checkpoints" / "ckpt-0000001.state").write_text("checkpoint")
if step.get("final", True):
    pending = out / "pending.state"
    pending.write_text(step.get("bytes", a.run_id))
    pending.replace(out / "final.state")
summary = {"run_id": a.run_id, "exit_code": step.get("rc", 0),
           "stop_reason": "interrupted" if stopped else "max_steps",
           "states": {"final": "final.state" if step.get("final", True) else None,
                      "checkpoints": ["ckpt-0000001.state"] if step.get("checkpoint") else []},
           **step.get("summary", {})}
if not step.get("no_summary"):
    (out / "summary.json").write_text(json.dumps(summary))
(out / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in step.get("events", [])))
writer.unlink()
raise SystemExit(step.get("rc", 0))
'''


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("synthetic child did not reach expected boundary")


@pytest.fixture
def live(tmp_path):
    home = tmp_path / "live data"
    home.mkdir()
    initial = home / "initial.state"
    initial.write_text("initial")
    (home / "current.state").write_text(str(initial) + "\n")
    (home / "run.yaml").write_text("run: {}\n")
    runner = tmp_path / "fake runner"
    runner.write_text(f"#!{sys.executable}\n" + FAKE_RUNNER)
    runner.chmod(0o700)
    env = {
        key: value for key, value in os.environ.items()
        if key not in {
            "PORT", "DECIDER", "RUN_CONFIG", "OBJECTIVE_FILE", "OBJECTIVE_PLAN", "ALLOW_REMOTE",
            "ALLOW_CHECKPOINT_RECOVERY", "STOP_ON_CLEAR", "RUNNER_BIN", "MAX_WHITEOUTS",
            "ON_OBJECTIVE_DONE", "MAX_STALLED_SEGMENTS",
        }
    }
    env.update(
        LIVE_HOME=str(home), RUNNER_BIN=str(runner), SEGMENT_DELAY_S="0.02",
        CRASH_BACKOFF_S="0.03", CRASH_BACKOFF_MAX_S="0.06", POLL_INTERVAL_S="0.01",
        STOP_TIMEOUT_S="0.1", MAX_CRASHES="3", CRASH_WINDOW_S="10",
    )
    children = []

    def start(steps, **overrides):
        (home / "scenario.json").write_text(json.dumps(steps))
        log = (tmp_path / f"supervisor-{len(children)}.log").open("w")
        child = subprocess.Popen(
            [sys.executable, "-m", "pokegent.supervisor"], env={**env, **overrides},
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        log.close()
        children.append(child)
        return child

    yield home, start
    for child in children:
        # Includes synthetic orphans created by the lock inheritance drill.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=5)


def load(path):
    return json.loads(path.read_text())


def calls(home):
    return [json.loads(line) for line in (home / "calls.jsonl").read_text().splitlines()]


def test_two_segments_continuity_and_explicit_config(live):
    home, start = live
    plan = home / "plan.json"
    plan.write_text('{"objectives": []}')
    child = start(
        [{"checkpoint": True}, {"summary": {"game_cleared": True}}],
        PORT="19701", ALLOW_REMOTE="true", DECIDER="first", OBJECTIVE_PLAN=str(plan),
    )
    assert child.wait(timeout=5) == 0
    first, second = calls(home)
    assert first["run_id"] != second["run_id"]
    assert first["out"] != second["out"]
    assert second["state"] == str(Path(first["out"]) / "final.state")
    assert second["input"] == first["run_id"]
    assert first["on_objective_done"] == "continue"
    assert first["no_state_fallback"]
    assert first["viewer_port"] == "19701" and first["allow_remote"]
    assert first["objective_plan"] == str(plan) and first["decider"] == "first"
    metadata = load(home / "live-run.json")
    assert metadata["status"] == "finished" and metadata["verdict"] == "clear"
    assert (home / "current.state").read_text().strip() == metadata["state_path"]
    assert load(Path(first["out"]) / "exit.json")["state_path"].endswith("final.state")


@pytest.mark.parametrize("policy", ["hold", "pause", "continue"])
def test_objective_done_policy_is_configurable(live, policy):
    home, start = live
    child = start([{"summary": {"game_cleared": True}}], ON_OBJECTIVE_DONE=policy)
    assert child.wait(timeout=5) == 0
    assert calls(home)[0]["on_objective_done"] == policy


@pytest.mark.parametrize("objective_end", [{"done": True}, {"aborted": "changed"}])
def test_objective_done_or_changed_never_stops_or_clears_newer_owner(live, objective_end):
    home, start = live
    newer = {"revision": 8, "set_by": "operator", "forbid": ["pc"]}
    child = start([
        {"objective": newer, "rc": 3, "summary": {
            "objective": {"active": {"revision": 7}, **objective_end},
        }},
        {"summary": {"game_cleared": True}},
    ])
    assert child.wait(timeout=5) == 0
    assert len(calls(home)) == 2
    assert load(home / "objective.json") == newer
    first = load(home / "runs/live-0001/exit.json")
    assert first["verdict"] == "objective_done" and first["active_objective_revision"] == 7
    assert load(home / "live-run.json")["objective_revision"] == 8


@pytest.mark.parametrize("trigger", ["restart", "stop", "signal"])
def test_graceful_restart_stop_and_sigterm(live, trigger):
    home, start = live
    child = start([{"wait": True}, {"summary": {"game_cleared": True}}])
    wait_until(lambda: (home / "runs/live-0001/ready").exists())
    if trigger == "signal":
        child.send_signal(signal.SIGTERM)
    else:
        (home / trigger).touch()
    assert child.wait(timeout=5) == 0
    assert len(calls(home)) == (2 if trigger == "restart" else 1)
    first = load(home / "runs/live-0001/exit.json")
    assert first["requested"] and first["verdict"] == "interrupted"
    assert first["state_path"].endswith("final.state")
    assert verdict(home / "runs/live-0001") == "interrupted"


def test_controlled_interrupt_without_successor_recovers_from_last_verified_state(live):
    """A blocked record from a controlled interrupt with a valid start-state pointer must
    resume from that state on the next start instead of staying blocked forever.

    Regression for issue #33: a ``requested`` stop (SIGTERM/restart/stop) whose runner
    never published a final.state left ``live-run.json`` with ``status=blocked``; every
    restart then exited rc=4 ("previous run is blocked") and the viewer port spun empty.
    """
    home, start = live
    initial = (home / "initial.state")
    (home / "runs").mkdir(exist_ok=True)
    out = home / "runs" / "live-0099"
    out.mkdir()
    blocked = {
        "run_id": "live-0099", "out": str(out), "state": str(initial),
        "status": "blocked", "verdict": "interrupted", "requested": True, "forced": False,
        "rc": -15, "state_error": "no final state", "state_path": None,
    }
    (home / "live-run.json").write_text(json.dumps(blocked))

    # Supervisor should recover the start state and run a normal segment, not exit 4.
    child = start([{"summary": {"game_cleared": True}}])
    assert child.wait(timeout=5) == 0
    assert len(calls(home)) == 1
    assert calls(home)[0]["input"] == "initial"
    assert load(out / "exit.json")["recovered"] is True
    metadata = load(home / "live-run.json")
    assert metadata["status"] == "finished"


def test_controlled_interrupt_without_valid_state_stays_fail_closed(live):
    """A controlled-interrupt blocked record with a missing or empty start-state pointer
    must NOT resume: the fail-closed check is preserved when the last verified state is
    unavailable (genuine corruption / rejected successor).
    """
    home, start = live
    (home / "runs").mkdir(exist_ok=True)
    for state_value in (str(home / "does-not-exist.state"), ""):
        out = home / "runs" / ("live-0098" if state_value else "live-0097")
        out.mkdir()
        (home / "live-run.json").write_text(json.dumps({
            "run_id": out.name, "out": str(out), "state": state_value,
            "status": "blocked", "verdict": "interrupted", "requested": True, "forced": False,
            "rc": -15, "state_error": "no final state", "state_path": None,
        }))
        child = start([{"summary": {"game_cleared": True}}])
        assert child.wait(timeout=5) == 4
        # No segment may have been launched: fail-closed blocked must not fake progress.
        assert not (home / "calls.jsonl").exists()
        # Make sure the next fixture reuse leaves a clean journal/counter for the next case.
        (home / "live-run.json").unlink()


def test_unrequested_missing_successor_stays_fail_closed(live):
    """A blocked record from an unrequested (crash/corruption) run with no successor stays
    blocked: the recovery path only applies to the controlled-interrupt case (#33).
    """
    home, start = live
    initial = (home / "initial.state")
    (home / "runs").mkdir(exist_ok=True)
    out = home / "runs" / "live-0096"
    out.mkdir()
    (home / "live-run.json").write_text(json.dumps({
        "run_id": "live-0096", "out": str(out), "state": str(initial),
        "status": "blocked", "verdict": "other", "requested": False, "forced": False,
        "rc": 7, "state_error": "no final state", "state_path": None,
    }))
    child = start([{"summary": {"game_cleared": True}}])
    assert child.wait(timeout=5) == 4
    # No segment may have been launched: genuine unrequested failure stays blocked.
    assert not (home / "calls.jsonl").exists()
    assert load(home / "live-run.json")["status"] == "blocked"


def test_interrupt_recovery_rejects_invalid_out_dir(live, tmp_path):
    """Recovery must validate the record's ``out`` directory (home/runs/<run_id>) before
    writing exit.json: a missing/mismatched ``out`` must stay fail-closed and must not write
    exit.json into the current working directory (review).
    """
    home, start = live
    initial = (home / "initial.state")
    (home / "runs").mkdir(exist_ok=True)
    # out points outside home/runs (and no run_id) — must NOT recover.
    (home / "live-run.json").write_text(json.dumps({
        "run_id": "live-0095", "out": str(tmp_path / "elsewhere"),
        "state": str(initial), "status": "blocked", "verdict": "interrupted",
        "requested": True, "forced": False, "rc": -15, "state_path": None,
    }))
    child = start([{"summary": {"game_cleared": True}}])
    assert child.wait(timeout=5) == 4
    assert not (home / "calls.jsonl").exists()
    # No stray exit.json may have been written into the cwd.
    assert not (tmp_path / "elsewhere").exists() or not (
        (tmp_path / "elsewhere" / "exit.json").exists()
        or Path("exit.json").exists()
    )
    assert load(home / "live-run.json")["status"] == "blocked"


def test_interrupt_recovery_requires_real_boolean_flags(live):
    """The requested/forced flags must be actual booleans: a JSON string \"false\" must NOT
    resume the run (review). Fail-closed otherwise."""
    home, start = live
    initial = (home / "initial.state")
    (home / "runs").mkdir(exist_ok=True)
    out = home / "runs" / "live-0094"
    out.mkdir()
    for requested, forced in (("false", False), (False, "false"), ("true", False)):
        (home / "live-run.json").write_text(json.dumps({
            "run_id": "live-0094", "out": str(out), "state": str(initial),
            "status": "blocked", "verdict": "interrupted",
            "requested": requested, "forced": forced,
            "rc": -15, "state_error": "no final state", "state_path": None,
        }))
        child = start([{"summary": {"game_cleared": True}}])
        assert child.wait(timeout=5) == 4
        assert not (home / "calls.jsonl").exists()
    assert load(home / "live-run.json")["status"] == "blocked"


def test_controlled_stop_without_final_restarts_from_last_verified_state(live):
    """End-to-end drill for issue #33: run a real segment, interrupt it (requested stop) so it
    leaves a NO-final-state blocked record, then restart and verify the supervisor resumes from
    the last verified state instead of exiting rc=4 forever.
    """
    home, start = live
    # Segment stays alive (wait) and publishes NO final.state when it exits (final: False),
    # simulating a hard SIGTERM before a save landed. That produces a blocked record; the
    # supervisor must exit 4 (blocked) in THIS process and resume on the NEXT start.
    child = start([{"wait": True, "final": False}, {"summary": {"game_cleared": True}}])
    wait_until(lambda: (home / "runs/live-0001/ready").exists())
    child.send_signal(signal.SIGTERM)
    assert child.wait(timeout=5) == 4  # blocked this process (no successor, owner-review shape)
    record = load(home / "live-run.json")
    assert record["status"] == "blocked" and record["requested"] is True
    assert record["state_path"] is None  # no successor published

    # Restart: systemd Restart=on-failure relaunches; recover() must resume from the last
    # verified state (initial.state), not block forever.
    child = start([{"summary": {"game_cleared": True}}])
    assert child.wait(timeout=5) == 0
    assert len(calls(home)) == 2
    assert calls(home)[1]["input"] == "initial"
    metadata = load(home / "live-run.json")
    assert metadata["status"] == "finished"
    assert load(home / "runs/live-0001/exit.json")["recovered"] is True


def test_stop_marker_at_start_does_not_spawn(live):
    home, start = live
    (home / "stop").touch()
    assert start([{}]).wait(timeout=5) == 0
    assert not (home / "calls.jsonl").exists()


def test_restart_marker_at_start_is_stale(live):
    home, start = live
    (home / "restart").touch()
    assert start([{"summary": {"game_cleared": True}}]).wait(timeout=5) == 0
    assert not load(home / "runs/live-0001/exit.json")["requested"]


def test_lock_prevents_second_writer_even_after_supervisor_killed(live):
    home, start = live
    first = start([{"wait": True}])
    wait_until(lambda: (home / "runs/live-0001/ready").exists())
    assert start([{"wait": True}]).wait(timeout=5) == 4
    first.kill()
    first.wait(timeout=5)
    assert start([{"wait": True}]).wait(timeout=5) == 4
    assert len(calls(home)) == 1


def test_live_pid_record_is_never_signalled_or_retried(live):
    home, start = live
    (home / "live-run.json").write_text(json.dumps({"status": "running", "pid": os.getpid()}))
    assert start([{}]).wait(timeout=5) == 4
    assert not (home / "calls.jsonl").exists()


def test_stale_dead_pid_recovers_only_its_final_state_and_unique_counter(live):
    home, start = live
    old = home / "runs/live-0001"
    old.mkdir(parents=True)
    (old / "final.state").write_text("recovered")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=5)
    (home / "live-run.json").write_text(json.dumps({
        "run_id": old.name, "out": str(old), "status": "running", "pid": dead.pid,
    }))
    child = start([{}, {"summary": {"game_cleared": True}}])
    assert child.wait(timeout=5) == 0
    assert calls(home)[0]["run_id"] == "live-0002"
    assert calls(home)[0]["input"] == "recovered"
    assert load(old / "exit.json")["recovered"]


@pytest.mark.parametrize("stop_on_clear", [True, False])
def test_recovered_clear_applies_policy_before_launching(live, stop_on_clear):
    home, start = live
    old = home / "runs/live-0001"
    old.mkdir(parents=True)
    (old / "final.state").write_text("recovered clear")
    (old / "summary.json").write_text(json.dumps({
        "game_cleared": True, "exit_code": 0,
        "states": {"final": "final.state", "checkpoints": []},
    }))
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=5)
    (home / "live-run.json").write_text(json.dumps({
        "run_id": old.name, "out": str(old), "status": "running", "pid": dead.pid,
    }))
    child = start([{}, {"wait": True}], STOP_ON_CLEAR=str(stop_on_clear).lower())
    if not stop_on_clear:
        wait_until(lambda: (home / "runs/live-0002/ready").exists())
        (home / "stop").touch()
    assert child.wait(timeout=5) == 0
    recovered = load(old / "exit.json")
    assert recovered["recovered"] and recovered["verdict"] == "clear"
    if stop_on_clear:
        assert not (home / "calls.jsonl").exists()
        assert not (home / "counter").exists()
        assert (home / "current.state").read_text().strip() == str(old / "final.state")
        # A subsequent supervisor restart must keep honoring the finished clear.
        assert start([{}]).wait(timeout=5) == 0
        assert not (home / "calls.jsonl").exists()
    else:
        assert len(calls(home)) == 1 and calls(home)[0]["input"] == "recovered clear"


def test_recovered_stall_limit_applies_before_launching(live, hybrid_segment):
    home, start = live
    old = home / "runs/live-0001"
    old.mkdir(parents=True)
    paused = hybrid_segment()
    (old / "final.state").write_text("recovered pause")
    (old / "summary.json").write_text(json.dumps({
        **paused["summary"], "states": {"final": "final.state", "checkpoints": []},
    }))
    (home / "live-run.json").write_text(json.dumps({
        "run_id": old.name, "out": str(old), "status": "running", "stalled_segments": 2,
    }))
    assert start([{}]).wait(timeout=5) == 3
    assert not (home / "calls.jsonl").exists()
    assert load(home / "live-run.json")["verdict"] == "owner_review"


@pytest.mark.parametrize("step", [
    {"final": False},
    {"final": False, "no_summary": True},
    {"summary": {"states": {"final": None}}},
    {"bytes": ""},
])
def test_missing_or_rejected_successor_blocks_without_old_save_retry(live, step):
    home, start = live
    before = (home / "current.state").read_text()
    assert start([step]).wait(timeout=5) == 4
    assert len(calls(home)) == 1
    assert (home / "current.state").read_text() == before
    assert load(home / "live-run.json")["status"] == "blocked"
    assert start([{}]).wait(timeout=5) == 4
    assert len(calls(home)) == 1


@pytest.mark.parametrize("allow", [False, True])
def test_checkpoint_recovery_requires_explicit_policy(live, allow):
    home, start = live
    child = start([
        {"final": False, "checkpoint": True}, {"summary": {"game_cleared": True}},
    ], ALLOW_CHECKPOINT_RECOVERY=str(allow).lower())
    assert child.wait(timeout=5) == (0 if allow else 4)
    assert len(calls(home)) == (2 if allow else 1)
    if allow:
        assert calls(home)[1]["input"] == "checkpoint"


@pytest.mark.parametrize("step", [{"rc": 7}, {"no_summary": True}, {"no_summary": True, "rc": 7}])
def test_crashes_and_missing_summary_are_bounded_with_backoff(live, step):
    home, start = live
    assert start([step]).wait(timeout=5) == 4
    assert len(calls(home)) == 3
    assert calls(home)[1]["input"] == "live-0001"
    log = (home / "journal.log").read_text()
    assert "backoff 0.03s" in log and "backoff 0.06s" in log
    assert "crash limit reached" in log
    assert start([{}]).wait(timeout=5) == 4
    assert len(calls(home)) == 3


def test_expired_crashes_leave_the_window(live):
    home, start = live
    assert start([
        {"rc": 7}, {"rc": 7}, {"summary": {"game_cleared": True}},
    ], CRASH_WINDOW_S="0.01", MAX_CRASHES="2").wait(timeout=5) == 0
    assert len(calls(home)) == 3


def test_stop_interrupts_crash_backoff(live):
    home, start = live
    child = start([{"rc": 7}], CRASH_BACKOFF_S="30", CRASH_BACKOFF_MAX_S="30")
    wait_until(lambda: "backoff 30s" in (home / "journal.log").read_text()
               if (home / "journal.log").exists() else False)
    (home / "stop").touch()
    assert child.wait(timeout=5) == 0
    assert len(calls(home)) == 1


@pytest.mark.parametrize("rc", [2, 4])
def test_usage_or_environment_failure_is_not_retried(live, rc):
    home, start = live
    assert start([{"rc": rc}]).wait(timeout=5) == 4
    assert len(calls(home)) == 1


def test_requested_timeout_is_bounded_and_blocks(live):
    home, start = live
    child = start([{"wait": True, "ignore_term": True}])
    wait_until(lambda: (home / "runs/live-0001/ready").exists())
    (home / "restart").touch()
    assert child.wait(timeout=5) == 4
    result = load(home / "runs/live-0001/exit.json")
    assert result["forced"] and result["verdict"] == "interrupted"
    assert len(calls(home)) == 1


def test_whiteout_limit_uses_current_scene_schema(live):
    home, start = live
    assert start([
        {"summary": {"scenes_entered": {"system.whiteout": 1}}},
    ], MAX_WHITEOUTS="2").wait(timeout=5) == 3
    assert len(calls(home)) == 2


@pytest.fixture
def hybrid_segment(hybrid_harness):  # noqa: F811
    def build(**kwargs):
        runner, _, actions, _, events = hybrid_harness(**kwargs)
        summary = runner.run()
        assert summary.status.value == "safe_pause" and summary.verdict == "no_progress"
        assert len(actions) == (7 if kwargs.get("progress_at") else 4)
        return {"rc": 1, "summary": {
            "stop_reason": summary.status.value, "verdict": summary.verdict,
        }, "events": [e.model_dump(mode="json") for e in events.events]}
    return build


@pytest.mark.parametrize("limit", [None, 2])
def test_hybrid_stalled_segments_stop_for_owner_review(live, hybrid_segment, limit):
    home, start = live
    paused = hybrid_segment(semantic_at=range(3, 31))
    overrides = {} if limit is None else {"MAX_STALLED_SEGMENTS": str(limit)}
    child = start([paused], MAX_CRASHES="2", **overrides)
    assert child.wait(timeout=5) == 3
    assert len(calls(home)) == (limit or 3)
    result = load(home / "live-run.json")
    assert result["status"] == "blocked" and result["verdict"] == "owner_review"
    assert result["stalled_segments"] == (limit or 3)
    assert result["state_path"].endswith("final.state")
    assert load(Path(result["out"]) / "exit.json")["verdict"] == "owner_review"
    assert verdict(result["out"]) == "owner_review"
    assert start([{}]).wait(timeout=5) == 4
    assert len(calls(home)) == (limit or 3)


def test_durable_progress_resets_streak_even_if_segment_later_pauses(live, hybrid_segment):
    home, start = live
    paused = hybrid_segment()
    progressed = hybrid_segment(progress_at=(3,))
    child = start([paused, paused, progressed, paused])
    assert child.wait(timeout=5) == 3
    assert len(calls(home)) == 6
    streaks = [load(home / f"runs/live-{n:04d}/exit.json")["stalled_segments"]
               for n in range(1, 7)]
    assert streaks == [1, 2, 0, 1, 2, 3]


@pytest.mark.parametrize("stop_reason", ["safe_pause", "no_progress"])
def test_stall_limit_accepts_stop_reason_without_runner_verdict(live, stop_reason):
    home, start = live
    assert start([{"rc": 1, "summary": {"stop_reason": stop_reason}}],
                 MAX_STALLED_SEGMENTS="2").wait(timeout=5) == 3
    assert len(calls(home)) == 2


def test_stall_streak_survives_supervisor_restart(live, hybrid_segment):
    home, start = live
    (home / "live-run.json").write_text(json.dumps({
        "status": "finished", "stalled_segments": 2,
    }))
    assert start([hybrid_segment()]).wait(timeout=5) == 3
    assert len(calls(home)) == 1


def test_game_clear_policy_can_continue(live):
    home, start = live
    child = start([{"summary": {"game_cleared": True}}, {"wait": True}], STOP_ON_CLEAR="false")
    wait_until(lambda: (home / "runs/live-0002/ready").exists())
    (home / "stop").touch()
    assert child.wait(timeout=5) == 0
    assert len(calls(home)) == 2


@pytest.mark.parametrize(("summary", "expected"), [
    ({"game_cleared": True}, "clear"),
    ({"game_cleared": "false"}, "other"),
    ({"game_cleared": True, "stop_reason": "interrupted"}, "interrupted"),
    ({"scenes_entered": {"system.whiteout": 1}}, "whiteout"),
    ({"objective": {"done": True}}, "objective_done"),
    ({"objective": {"active": {"objective_id": "some_campaign_step"}}}, "other"),
])
def test_shared_verdict_schema(tmp_path, summary, expected):
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    assert verdict(tmp_path) == expected


def test_missing_corrupt_summary_and_small_payload(tmp_path):
    assert verdict(tmp_path) == "none"
    (tmp_path / "summary.json").write_text("{")
    assert verdict(tmp_path) == "none"
    (tmp_path / "exit.json").write_text(json.dumps({
        "rc": 0, "requested": True, "state_path": None,
    }))
    assert verdict(tmp_path) == "interrupted"
    assert payload({"run_id": "synthetic"}, tmp_path) == {
        "run_id": "synthetic", "verdict": "interrupted", "rc": 0,
        "state_path": None, "active_objective_revision": None,
    }


@pytest.mark.parametrize("content", ["{", '{"status":"unrecognized"}'])
def test_bad_ownership_metadata_blocks(live, content):
    home, start = live
    (home / "live-run.json").write_text(content)
    assert start([{}]).wait(timeout=5) == 4
    assert not (home / "calls.jsonl").exists()


def test_invalid_initial_pointer_blocks_before_spawn(live):
    home, start = live
    (home / "current.state").write_text("missing.state\n")
    assert start([{}]).wait(timeout=5) == 4
    assert not (home / "calls.jsonl").exists()


def test_successor_refuses_missing_accepted_final_or_untrusted_checkpoint(tmp_path):
    checkpoints = tmp_path / "checkpoints"
    checkpoints.mkdir()
    (checkpoints / "ckpt-0000001.state").write_text("older")
    (tmp_path / "summary.json").write_text(json.dumps({
        "states": {"final": "final.state", "checkpoints": ["ckpt-0000001.state"]},
    }))
    with pytest.raises(ValueError, match="final state is missing"):
        successor_state(tmp_path, allow_checkpoint=True)
    (tmp_path / "summary.json").write_text(json.dumps({
        "states": {"final": None, "checkpoints": ["../outside.state"]},
    }))
    with pytest.raises(ValueError, match="accepted checkpoint"):
        successor_state(tmp_path, allow_checkpoint=True)


def test_newest_checkpoint_is_never_skipped_and_symlink_final_is_refused(tmp_path):
    checkpoints = tmp_path / "checkpoints"
    checkpoints.mkdir()
    old = checkpoints / "ckpt-0000001.state"
    old.write_text("older")
    (checkpoints / "ckpt-0000002.state").write_text("")
    with pytest.raises(ValueError, match="empty"):
        successor_state(tmp_path, allow_checkpoint=True)
    (tmp_path / "final.state").symlink_to(old)
    with pytest.raises(ValueError, match="symlink"):
        successor_state(tmp_path, allow_checkpoint=True)


def test_atomic_pointer_replaces_only_after_complete_write(tmp_path, monkeypatch):
    pointer = tmp_path / "current.state"
    pointer.write_text("previous\n")
    replace = os.replace

    def inspect(source, destination):
        assert pointer.read_text() == "previous\n"
        assert Path(source).read_text() == "successor\n"
        replace(source, destination)

    monkeypatch.setattr(os, "replace", inspect)
    atomic_write(pointer, "successor\n")
    assert pointer.read_text() == "successor\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["current.state"]


@pytest.mark.parametrize(("name", "value"), [
    ("CRASH_WINDOW_S", "nan"), ("STOP_TIMEOUT_S", "0"), ("MAX_STEPS", "-1"),
    ("ALLOW_REMOTE", "yes"), ("PORT", "80"), ("DECIDER", "unexpected"),
    ("ON_OBJECTIVE_DONE", "unexpected"),
    ("MAX_STALLED_SEGMENTS", "0"), ("MAX_STALLED_SEGMENTS", "-1"),
])
def test_invalid_environment_fails_before_launch(tmp_path, monkeypatch, name, value):
    monkeypatch.setenv("LIVE_HOME", str(tmp_path))
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Settings.from_env()


@pytest.mark.parametrize("fallback", [False, True])
def test_native_strict_startup_does_not_load_older_checkpoint(tmp_path, monkeypatch, fallback):
    from pokegent import app
    from pokegent.games.gen3.firered.reader import live as reader

    requested = tmp_path / "final.state"
    requested.write_text("rejected synthetic state")
    checkpoints = tmp_path / "checkpoints"
    checkpoints.mkdir()
    (checkpoints / "ckpt-0000001.state").write_text("valid synthetic checkpoint")
    loaded = []
    actor = SimpleNamespace(load_state=lambda path: loaded.append(Path(path).name))
    monkeypatch.setattr(reader, "LiveReader", lambda *_: SimpleNamespace(
        after_state_load=lambda stem: SimpleNamespace(ok=stem != "final", conformance={}),
    ))

    def reached_runner(**kwargs):
        raise RuntimeError("runner reached after checkpoint")

    monkeypatch.setattr(app, "build_runner", reached_runner)
    error = RuntimeError if fallback else app.StateRefused
    message = "runner reached" if fallback else "no valid state"
    with pytest.raises(error, match=message):
        app.play_state(
            actor, None, requested, config=app.AppConfig(), decider=None, decider_name="first",
            facts=None, events=None, **({} if fallback else {"state_fallback": False}),
        )
    assert loaded == (["final.state", "ckpt-0000001.state"] if fallback else ["final.state"])


@pytest.mark.parametrize("strict", [False, True])
def test_run_cli_forwards_state_fallback_flag(tmp_path, monkeypatch, strict):
    from pokegent import app, cli

    forwarded = {}

    def run(**kwargs):
        forwarded.update(kwargs)
        return SimpleNamespace(exit_code=0)

    monkeypatch.setattr(app, "run_offline", run)
    config = tmp_path / "run.yaml"
    config.write_text("{}")
    command = ["run", "--state", str(tmp_path / "synthetic.state"), "--config", str(config)]
    with pytest.raises(SystemExit) as ended:
        cli.main(command + (["--no-state-fallback"] if strict else []))
    assert ended.value.code == 0
    assert forwarded["state_fallback"] is not strict
