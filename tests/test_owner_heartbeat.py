"""Synthetic Core/media transports: no owner sends, network, profile or live state."""

import copy
import io
import json
import os
import runpy
import signal
import subprocess
import sys
import threading
import time
from email.message import Message
from pathlib import Path

import pytest
from PIL import Image

MODULE = Path(__file__).resolve().parents[1] / "ops/hermes/owner_heartbeat.py"


@pytest.fixture
def heartbeat(tmp_path):
    module = runpy.run_path(str(MODULE))
    state = tmp_path / "private"
    terminal = {"address": "t1", "state": "live", "status": {"state": "idle", "app": "hermes"},
                "created_ms": 1000, "status_ms": int((time.time() - 301) * 1000),
                "pid": 23, "foreground": {"pid": 24}}
    calls = []

    def query(hc, *args):
        calls.append(args)
        return copy.deepcopy(terminal) if args[0] == "get" else {"status": "idle", "turn": 3}

    def run(**kwargs):
        return module["heartbeat"](state, "t1", "http://dashboard", "http://stream",
                                   query=kwargs.pop("query", query),
                                   media=kwargs.pop("media", lambda *a: time.time() + 2), **kwargs)

    return module, state, terminal, calls, query, run


def test_success_private_receipt_dedup_and_new_process_epoch(heartbeat):
    _, state, terminal, calls, _, run = heartbeat
    assert run()
    assert calls[-1][:5] == ("send", "--json", "--if", "idle", "t1")
    assert "No further GO" in calls[-1][-1] and "legality" in calls[-1][-1]
    receipt = json.loads((state / "receipt.json").read_text())
    assert receipt == ["t1", "t1", 1000, 23, 24, terminal["status_ms"]]
    assert state.stat().st_mode & 0o777 == 0o700
    assert (state / "receipt.json").stat().st_mode & 0o777 == 0o600
    assert not run()
    terminal["foreground"]["pid"] = 25
    assert run()
    assert sum(c[0] == "send" for c in calls) == 2


@pytest.mark.parametrize("broken", ["working", "blocked", "unknown", "finished", "future",
                                   "young", "threshold", "malformed", "pid", "foreign_app"])
def test_ineligible_status_never_sends(heartbeat, broken, monkeypatch):
    module, state, terminal, calls, _, run = heartbeat
    if broken in {"working", "blocked", "unknown"}:
        terminal["status"]["state"] = broken
    elif broken == "finished":
        terminal["state"] = "finished"
    elif broken == "foreign_app":
        terminal["status"]["app"] = "other"
    elif broken == "future":
        terminal["status_ms"] = int((time.time() + 1) * 1000)
    elif broken == "young":
        terminal["status_ms"] = int(time.time() * 1000) - 1000
    elif broken == "threshold":
        monkeypatch.setattr(module["time"], "time", lambda: 1000000.0)
        terminal["status_ms"] = 1000000000 - 300000
    elif broken == "pid":
        terminal["pid"] = True
    else:
        terminal["status_ms"] = "bad"
    assert not run()
    assert not (state / "receipt.json").exists()
    assert all(c[0] == "get" for c in calls)


def test_changed_epoch_and_absent_media_suppress(heartbeat):
    _, state, terminal, calls, query, run = heartbeat
    assert not run(media=lambda *a: False)
    assert len(calls) == 1

    def media(*args):
        terminal["status_ms"] -= 1
        return time.time() + 2

    assert not run(media=media)
    assert all(c[0] == "get" for c in calls)
    assert not (state / "receipt.json").exists()


@pytest.mark.parametrize("failure", ["busy", "timeout", "command", "unknown_ack"])
def test_guard_refusal_and_failure_leave_no_receipt(heartbeat, failure):
    _, state, _, _, query, run = heartbeat

    def fails(hc, *args):
        if args[0] == "send":
            if failure == "unknown_ack":
                return {"turn": 4}
            if failure == "timeout":
                raise subprocess.TimeoutExpired("synthetic", 5)
            raise subprocess.CalledProcessError(10 if failure == "busy" else 1, "synthetic")
        return query(hc, *args)

    if failure == "unknown_ack":
        assert not run(query=fails)
    else:
        with pytest.raises(subprocess.SubprocessError):
            run(query=fails)
    assert not (state / "receipt.json").exists()


def test_overlap_locks_entire_transaction(heartbeat):
    _, _, _, calls, query, run = heartbeat
    entered, release = threading.Event(), threading.Event()
    result = []

    def slow(*args):
        entered.set()
        assert release.wait(3)
        return time.time() + 2

    worker = threading.Thread(target=lambda: result.append(run(media=slow)))
    worker.start()
    assert entered.wait(3)
    assert not run()
    assert len(calls) == 1
    release.set()
    worker.join(3)
    assert result == [True] and sum(c[0] == "send" for c in calls) == 1


def test_accepted_send_receipt_failure_is_at_least_once(heartbeat, monkeypatch):
    module, state, _, calls, _, run = heartbeat
    commit = module["commit_receipt"]

    def fails(*args):
        raise OSError("synthetic disk failure after acceptance")

    monkeypatch.setitem(module["heartbeat"].__globals__, "commit_receipt", fails)
    with pytest.raises(OSError):
        run()
    assert not (state / "receipt.json").exists()
    monkeypatch.setitem(module["heartbeat"].__globals__, "commit_receipt", commit)
    assert run()
    assert sum(c[0] == "send" for c in calls) == 2


@pytest.mark.parametrize("broken", ["permissions", "lock_symlink", "receipt_symlink", "corrupt"])
def test_private_state_fails_closed(heartbeat, tmp_path, broken):
    _, state, _, calls, _, run = heartbeat
    state.mkdir(mode=0o700)
    if broken == "permissions":
        state.chmod(0o755)
    elif broken.endswith("symlink"):
        outside = tmp_path / "outside"
        outside.write_text("private unchanged")
        name = "heartbeat.lock" if broken == "lock_symlink" else "receipt.json"
        (state / name).symlink_to(outside)
    else:
        receipt = state / "receipt.json"
        receipt.write_text("malformed")
        receipt.chmod(0o600)
    with pytest.raises((ValueError, OSError)):
        run()
    assert all(c[0] == "get" for c in calls)


@pytest.fixture
def media(heartbeat, monkeypatch):
    module = heartbeat[0]
    now = time.time()
    doc = {"schema": "pokegent-viewer/1", "server": {"emulator_ready": True},
           "state": {"valid": True}, "generated_at": now,
           "freshness": {"state": {"updated_at": now, "ttl_s": 2}},
           "run": {"run_id": "synthetic", "status": "running", "emulator_frame": 30},
           "game": {"run_id": "synthetic"}}
    health = {"ok": True, "emulator_ready": True, "status": "running",
              "frames": {"attached": True, "errors": 0, "last_error": None,
                         "seq": 2, "captures": 2, "emulator_frame": 30}}
    png = io.BytesIO()
    Image.new("RGB", (240, 160)).save(png, format="PNG")
    picture = [png.getvalue()]

    def fetch(url):
        headers = Message()
        if url.endswith("frame.png"):
            headers["Content-Type"] = "image/png"
            headers["X-Frame-Seq"], headers["X-Emulator-Frame"] = "2", "30"
            return headers, picture[0]
        headers["Content-Type"] = "application/json"
        return headers, json.dumps(health if url.endswith("health") else doc).encode()

    monkeypatch.setitem(module["media_evidence"].__globals__, "fetch", fetch)
    return module, doc, health, picture


def test_supported_live_evidence_allows_unchanging_pixels(media):
    module, *_ = media
    assert module["media_evidence"]("http://dashboard", "http://stream")
    assert module["media_evidence"]("http://dashboard", "http://stream")


@pytest.mark.parametrize("broken", ["detached", "capture_failed", "stale", "future", "ended",
                                   "wrong_run", "unknown_freshness", "wrong_frame", "bad_png"])
def test_cached_png_never_substitutes_for_live_freshness(media, broken):
    module, doc, health, picture = media
    if broken == "detached":
        health["frames"]["attached"] = False
    elif broken == "capture_failed":
        health["frames"].update(errors=1, last_error="CaptureError")
    elif broken == "stale":
        doc["freshness"]["state"]["updated_at"] -= 20
    elif broken == "future":
        doc["generated_at"] += 20
    elif broken == "ended":
        doc["run"]["status"] = "stopped"
    elif broken == "wrong_run":
        doc["game"]["run_id"] = "old"
    elif broken == "unknown_freshness":
        doc["freshness"] = {}
    elif broken == "wrong_frame":
        health["frames"]["emulator_frame"] = 100
    else:
        picture[0] = b"not png"
    try:
        assert not module["media_evidence"]("http://dashboard", "http://stream")
    except (ValueError, OSError, KeyError):
        pass  # CLI suppresses malformed/unknown facts; no message can be sent.


def test_state_inside_git_checkout_refused(heartbeat, tmp_path):
    module = heartbeat[0]
    repo = tmp_path / "synthetic-repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git/HEAD").write_text("ref: refs/heads/main\n")
    with pytest.raises(ValueError, match="outside Git"):
        module["private_directory"](repo / "state")


def test_core_transport_is_bounded_and_argv_only(heartbeat, monkeypatch):
    module = heartbeat[0]
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        kwargs["stdout"].write(b'{"status":"idle","turn":2}')

    monkeypatch.setattr(module["subprocess"], "run", run)
    assert module["core"]("/tool/hc", "get", "--json", "t1") == {"status": "idle", "turn": 2}
    command, options = calls[0]
    assert command == ["/tool/hc", "get", "--json", "t1"]
    assert options["timeout"] == 5 and options["check"] is True
    assert options.get("shell", False) is False


def test_media_transport_deadline_size_and_redirect_policy(heartbeat, monkeypatch):
    module = heartbeat[0]
    made = []

    class Response:
        status = 200
        headers = Message()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read1(self, size):
            assert size == 65536
            return b"x" * 65536

    def opener(*handlers):
        made.extend(handlers)
        return type("Opener", (), {"open": lambda self, req, timeout: Response()})()

    monkeypatch.setattr(module["urllib"].request, "build_opener", opener)
    with pytest.raises(ValueError, match="oversized live response"):
        module["fetch"]("http://synthetic/frame.png")
    assert any(isinstance(h, module["NoRedirect"]) for h in made)
    assert module["NoRedirect"]().redirect_request() is None
    ticks = iter([0, 4])
    monkeypatch.setattr(module["time"], "monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError):
        module["fetch"]("http://synthetic/frame.png")


def test_freshness_expiring_during_core_recheck_suppresses(heartbeat, monkeypatch):
    module, state, _, calls, query, run = heartbeat
    now = time.time()
    moment = [now]
    monkeypatch.setattr(module["time"], "time", lambda: moment[0])

    def slow_recheck(hc, *args):
        value = query(hc, *args)
        if len(calls) == 2:
            moment[0] += 3
        return value

    assert not run(query=slow_recheck, media=lambda *args: now + 2)
    assert not (state / "receipt.json").exists()
    assert all(c[0] == "get" for c in calls)


@pytest.fixture
def probe_child(media, tmp_path, monkeypatch):
    module, doc, health, picture = media
    marker = tmp_path / "probe-entered"
    payload = json.dumps([doc, health, picture[0].hex()])
    children = []
    popen = subprocess.Popen
    driver = r'''
import io, json, os, runpy, socket, sys, time, urllib.request
from pathlib import Path
from PIL import Image, PngImagePlugin

stage, marker, payload, script, flag, dashboard, stream = sys.argv[1:]
doc, health, png = json.loads(payload)
reads = 0

def stall(*args, **kwargs):
    Path(marker).write_text(str(os.getpid()))
    time.sleep(30)
    raise AssertionError("parent failed to kill stalled probe")

class Body(io.BytesIO):
    def readline(self, *args):
        if stage == "headers":
            stall()
        return super().readline(*args)

    def read1(self, *args):
        if stage == "body":
            stall()
        return super().read1(*args)

class Connection:
    def setsockopt(self, *args):
        pass

    def sendall(self, data):
        self.path = data.split(b" ")[1].decode()

    def makefile(self, *args):
        global reads
        reads += 1
        if stage == "read" + str(reads):
            stall()
        if stage == "cumulative":
            Path(marker).write_text(str(os.getpid()))
            time.sleep(1.1)
        if self.path == "/frame.png":
            body = bytes.fromhex(png)
            headers = "Content-Type: image/png\r\nX-Frame-Seq: 2\r\nX-Emulator-Frame: 30"
        else:
            body = json.dumps(health if self.path == "/ops/health" else doc).encode()
            headers = "Content-Type: application/json"
        return Body(("HTTP/1.1 200 OK\r\n" + headers + "\r\nContent-Length: "
                     + str(len(body)) + "\r\n\r\n").encode() + body)

    def close(self):
        pass

def forbidden(*args, **kwargs):
    raise AssertionError("synthetic probe must never use network")

socket.socket = forbidden
if stage == "resolver":
    socket.getaddrinfo = stall
else:
    socket.getaddrinfo = forbidden
    socket.create_connection = stall if stage == "open" else lambda *a, **kw: Connection()
if stage == "png":
    Image.open = stall
if stage == "verify":
    PngImagePlugin.PngImageFile.verify = stall
sys.argv = [script, flag, dashboard, stream]
runpy.run_path(script, run_name="__main__")
'''

    def install(stage, paired=False):
        def start(command, **kwargs):
            assert command == [sys.executable, str(MODULE), "--media-probe",
                               "http://dashboard", "http://dashboard" if paired else "http://stream"]
            assert kwargs["close_fds"] is True
            child = popen([sys.executable, "-c", driver, stage, str(marker), payload,
                           *command[1:]], **kwargs)
            children.append(child)
            return child

        monkeypatch.setattr(module["subprocess"], "Popen", start)
        return marker, children

    return install


@pytest.mark.parametrize("stage", ["resolver", "open", "headers", "body", "png", "cumulative"])
def test_complete_media_timeout_kills_reaps_and_releases_lock(heartbeat, probe_child, stage):
    module, state, _, calls, _, run = heartbeat
    marker, children = probe_child(stage)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run(media=module["media_available"])
    elapsed = time.monotonic() - started
    assert 3 <= elapsed < 6
    assert len(children) == 1 and int(marker.read_text()) == children[0].pid
    assert children[0].returncode == -signal.SIGKILL
    with pytest.raises(ChildProcessError):
        os.waitpid(children[0].pid, os.WNOHANG)
    assert all(c[0] == "get" for c in calls)
    assert not (state / "receipt.json").exists()
    assert run()  # Same epoch can acquire the released lock and retry with fresh evidence.


def test_supervised_media_success_preserves_expiry_and_guard(heartbeat, probe_child):
    module, state, _, calls, _, run = heartbeat
    _, children = probe_child("success")
    assert run(media=module["media_available"])
    assert len(children) == 1 and children[0].returncode == 0
    assert calls[-1][:5] == ("send", "--json", "--if", "idle", "t1")
    assert (state / "receipt.json").exists()


@pytest.mark.parametrize("stage", ["read1", "read2", "read3", "read4", "read5", "verify"])
def test_paired_probe_stall_kills_reaps_and_releases_lock(heartbeat, probe_child, stage):
    module, state, _, calls, _, run = heartbeat
    marker, children = probe_child(stage, paired=True)

    def media(*args):
        return module["media_available"]("http://dashboard", "http://dashboard")

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run(media=media)
    assert 3 <= time.monotonic() - started < 6
    assert int(marker.read_text()) == children[0].pid
    assert children[0].returncode == -signal.SIGKILL
    with pytest.raises(ChildProcessError):
        os.waitpid(children[0].pid, os.WNOHANG)
    assert all(c[0] == "get" for c in calls)
    assert not (state / "receipt.json").exists()
    assert run()


def test_supervised_paired_probe_accepts_still_image(heartbeat, probe_child):
    module, _, _, calls, _, run = heartbeat
    _, children = probe_child("success", paired=True)
    assert run(media=lambda *args: module["media_available"]("http://dashboard", "http://dashboard"))
    assert children[0].returncode == 0
    assert calls[-1][:5] == ("send", "--json", "--if", "idle", "t1")
