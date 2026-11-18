"""Single-pass heartbeat brackets; invented RAM/pixels and in-process handlers only."""

import asyncio
import copy
import io
import json
import runpy
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from starlette.requests import Request
from test_public_trainer_card import ram as ram  # reuse invented RAM, not extracted game data

from pokegent.addons import AddonConfig
from pokegent.dashboard.server import create_app
from pokegent.dashboard.state import ViewerState
from pokegent.dashboard.stream.audio import AudioHub
from pokegent.dashboard.stream.frames import FrameHub
from pokegent.dashboard.stream.server import create_app as stream_app
from pokegent.games.gen3.firered.reader.reader import FireRedReader

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://127.0.0.1:19770"


@pytest.fixture
def sample(monkeypatch):
    module = runpy.run_path(str(ROOT / "ops/hermes/owner_heartbeat.py"))
    clock = SimpleNamespace(wall=1000., mono=0.)
    monkeypatch.setattr(module["time"], "time", lambda: clock.wall)
    monkeypatch.setattr(module["time"], "monotonic", lambda: clock.mono)

    def advance(seconds):
        clock.wall += seconds
        clock.mono += seconds

    monkeypatch.setattr(module["time"], "sleep", advance)
    d0 = {"schema": "pokegent-viewer/1", "server": {"emulator_ready": True},
          "state": {"valid": True}, "generated_at": 1000.,
          "freshness": {"state": {"updated_at": 1000., "ttl_s": 2}},
          "run": {"run_id": "invented", "status": "running", "emulator_frame": 7122},
          "game": {"run_id": "invented"}}
    h0 = {"ok": True, "emulator_ready": True, "status": "running",
          "frames": {"attached": True, "errors": 0, "last_error": None,
                     "seq": 3640, "captures": 3640, "emulator_frame": 7122}}
    d1, h1 = copy.deepcopy(d0), copy.deepcopy(h0)
    # A fresh observer is allowed to lag the PNG.
    d1["run"]["emulator_frame"] = 7123
    h1["frames"].update(seq=3642, captures=3642, emulator_frame=7128)
    png = io.BytesIO()
    Image.new("RGB", (240, 160)).save(png, format="PNG")
    parts = [d0, h0, (3641, 7126, png.getvalue()), h1, d1]
    calls = []

    def fetch(url):
        index = len(calls)
        calls.append(url)
        headers = Message()
        value = parts[index]
        if isinstance(value, tuple):
            headers["Content-Type"] = "image/png"
            headers["X-Frame-Seq"], headers["X-Emulator-Frame"] = map(str, value[:2])
            return headers, value[2]
        headers["Content-Type"] = "application/json"
        return headers, json.dumps(value).encode()

    monkeypatch.setitem(module["media_evidence"].__globals__, "fetch", fetch)
    return SimpleNamespace(module=module, clock=clock, advance=advance, parts=parts, calls=calls,
                           fetch=fetch, run=lambda: module["media_evidence"](ORIGIN, ORIGIN))


def test_exact_advancing_shape_and_minimum_expiry(sample):
    sample.parts[0]["freshness"]["state"].update(updated_at=999.9, ttl_s=.3)
    sample.parts[4]["freshness"]["state"].update(updated_at=999.95, ttl_s=.4)
    assert sample.run() == pytest.approx(1000.2)
    assert sample.calls == [ORIGIN + p for p in (
        "/ops/telemetry", "/ops/health", "/frame.png", "/ops/health", "/ops/telemetry")]
    assert sample.clock.mono == .05


def test_sample_expiry_caps_original_ttl(sample):
    assert sample.run() == pytest.approx(1000.5)


def test_still_image(sample):
    sample.parts[4]["run"]["emulator_frame"] = 7122
    sample.parts[3] = copy.deepcopy(sample.parts[1])
    sample.parts[2] = (3640, 7122, sample.parts[2][2])
    assert sample.run()


# Each telemetry/health response independently has to prove its current facts.
@pytest.mark.parametrize("index", [0, 4])
@pytest.mark.parametrize("path,value", [
    (("schema",), "unknown"), (("server", "emulator_ready"), False),
    (("state", "valid"), False), (("generated_at",), 980), (("generated_at",), 1001),
    (("freshness", "state", "updated_at"), 980),
    (("freshness", "state", "updated_at"), 1001),
    (("freshness", "state", "updated_at"), True),
    (("freshness", "state", "ttl_s"), True), (("freshness", "state", "ttl_s"), 0),
    (("freshness", "state", "ttl_s"), 6), (("generated_at",), float("nan")),
    (("run", "status"), "stopped"), (("run", "run_id"), ""),
    (("game", "run_id"), "other"), (("run", "emulator_frame"), True),
    (("run", "emulator_frame"), -1), (("freshness",), {}),
])
def test_invalid_document(sample, index, path, value):
    item = sample.parts[index]
    for key in path[:-1]:
        item = item[key]
    item[path[-1]] = value
    with_suppression(sample)


@pytest.mark.parametrize("index", [1, 3])
@pytest.mark.parametrize("path,value", [
    (("ok",), False), (("emulator_ready",), False), (("status",), "stopped"),
    (("frames", "attached"), False), (("frames", "errors"), True),
    (("frames", "errors"), 1), (("frames", "last_error"), "CaptureError"),
    (("frames", "seq"), True), (("frames", "seq"), 0),
    (("frames", "captures"), True), (("frames", "captures"), 0),
    (("frames", "emulator_frame"), True), (("frames", "emulator_frame"), -1),
])
def test_invalid_health(sample, index, path, value):
    item = sample.parts[index]
    for key in path[:-1]:
        item = item[key]
    item[path[-1]] = value
    with_suppression(sample)


def with_suppression(sample):
    try:
        assert not sample.run()
    except (KeyError, TypeError, ValueError, OSError):
        pass  # The existing CLI suppresses malformed facts without any send or receipt.


@pytest.mark.parametrize("broken", [
    "new_run", "document_rollback", "ram_rollback", "observer_rollback", "seq_rollback",
    "frame_rollback", "capture_rollback", "capture_contradiction", "equal_seq_new_frame",
    "new_seq_equal_frame", "cached_before_cursor", "old_png_then_new_h1", "frozen_hub",
    "bad_png", "wrong_size", "over_budget", "expired_ram", "expired_document",
])
def test_bracket_refuses_ambiguous_evidence(sample, broken):
    d0, h0, png, h1, d1 = sample.parts
    if broken == "new_run":
        d1["run"]["run_id"] = d1["game"]["run_id"] = "new"
    elif broken == "document_rollback":
        d1["generated_at"] -= .1
    elif broken == "ram_rollback":
        d1["freshness"]["state"]["updated_at"] -= .1
    elif broken == "observer_rollback":
        d1["run"]["emulator_frame"] = 7121
    elif broken == "seq_rollback":
        h1["frames"]["seq"] = 3639
    elif broken == "frame_rollback":
        h1["frames"]["emulator_frame"] = 7125
    elif broken == "capture_rollback":
        h1["frames"]["captures"] = 3639
    elif broken == "capture_contradiction":
        h1["frames"]["captures"] = 3640
    elif broken == "equal_seq_new_frame":
        sample.parts[2] = (3640, png[1], png[2])
    elif broken == "new_seq_equal_frame":
        sample.parts[2] = (3641, 7122, png[2])
    elif broken == "cached_before_cursor":
        h0["frames"]["emulator_frame"] = 7120
        sample.parts[2] = (3641, 7121, png[2])
    elif broken == "old_png_then_new_h1":
        sample.parts[2] = (3640, 7122, png[2])
    elif broken == "frozen_hub":
        sample.parts[2] = (3640, 7122, png[2])
        h1["frames"].update(h0["frames"])
    elif broken == "bad_png":
        sample.parts[2] = (*png[:2], b"not png")
    elif broken == "wrong_size":
        body = io.BytesIO()
        Image.new("RGB", (1, 1)).save(body, format="PNG")
        sample.parts[2] = (*png[:2], body.getvalue())
    elif broken == "over_budget":
        sample.module["time"].sleep = lambda _: sample.advance(.501)
    elif broken == "expired_ram":
        d0["freshness"]["state"].update(updated_at=999.99, ttl_s=.05)
    elif broken == "expired_document":
        d0["generated_at"] = 995.01
    with_suppression(sample)


def test_distinct_origin_never_uses_relaxed_bracket(sample, monkeypatch):
    # The old D/P/H sequence still accepts an actual match and rejects moving evidence.
    sample.parts[:] = [sample.parts[0], sample.parts[2], sample.parts[3]]

    def run():
        return sample.module["media_evidence"](ORIGIN, "http://127.0.0.1:19771")
    assert not run()
    assert len(sample.calls) == 3 and sample.clock.mono == 0
    sample.calls.clear()
    sample.parts[1] = (3642, 7122, sample.parts[1][2])
    sample.parts[2]["frames"]["emulator_frame"] = 7122
    assert run()


@pytest.fixture
def runtime(sample, ram):
    profile, port, _ = ram
    reader = FireRedReader(port, profile, run_id="invented")
    viewer = ViewerState(run_id="invented", decider="agent", max_steps=1, realtime=False,
                         clock=lambda: sample.clock.wall)
    viewer.on_event(SimpleNamespace(type="runtime.started", emulator_frame=7122, seq=1,
                                   scene_id=None, request_id=None, outcome=None, source="test",
                                   provider=None, details={}))
    image = Image.new("RGB", (240, 160), (23, 31, 47))
    source = SimpleNamespace(frames_run=7122, failed=False)

    def screen():
        if source.failed:
            raise RuntimeError("invented capture failure")
        return SimpleNamespace(frame=source.frames_run, image=image)

    source.read_screen = screen
    hub = FrameHub(source, clock=lambda: sample.clock.mono)

    def observe(frame):
        snapshot = reader.read_snapshot(emulator_frame=frame)
        assert snapshot.usable, snapshot.health.read_errors
        viewer.on_perception(SimpleNamespace(snapshot=snapshot, frame=None))

    observe(7122)
    config = AddonConfig()
    operator = create_app(viewer, config, frame_hub=hub)
    stream = stream_app(hub, AudioHub(), config)
    handlers = {r.path: r.endpoint for app in (operator, stream) for r in app.routes
                if hasattr(r, "endpoint")}
    request = Request({"type": "http", "headers": []})

    def fetch(url):
        path = url.removeprefix(ORIGIN)
        handler = handlers[path]
        response = asyncio.run(handler(request, scale=1) if path == "/frame.png"
                               else handler(request) if path == "/ops/telemetry"
                               else handler())
        assert response.status_code == 200
        headers = Message()
        for key, value in response.headers.items():
            headers[key] = value
        return headers, response.body

    return SimpleNamespace(viewer=viewer, hub=hub, source=source, observe=observe, fetch=fetch)


@pytest.mark.parametrize("hz", [1, 30, 60])
@pytest.mark.parametrize("scheduled", [False, True])
def test_real_handlers_cadence_phase_and_jitter(sample, runtime, monkeypatch, hz, scheduled):
    """No socket/server/thread: invoke actual route handlers on the same viewer/hub."""
    hub, source = runtime.hub, runtime.source
    hub._period = 1 / hz
    period = 1 / hz
    rates = []
    for jitter in (0, .002, .007):
        accepted = 0
        for phase in range(100):
            # An already active viewer may have captured at any phase of the hub period.
            sample.clock.mono = 10 + phase * .1
            sample.clock.wall = 1000 + sample.clock.mono
            source.frames_run += 20
            hub._capture()
            sample.advance(period * phase / 100)
            runtime.observe(source.frames_run)
            due = [hub.latest().taken_at + period]
            calls = []
            bodies = []

            def fetch(url):
                path = url.removeprefix(ORIGIN)
                calls.append(path)
                sample.advance(.001 + jitter)
                source.frames_run += 1
                # Scheduled production uses the same capture step as FrameHub._run.
                if scheduled:
                    while due[0] <= sample.clock.mono:
                        hub._capture()
                        due[0] += period
                if path == "/ops/telemetry":
                    # Keep RAM fresh but deliberately behind newer screenshots.
                    runtime.observe(source.frames_run - 1)
                headers, body = runtime.fetch(url)
                if path == "/frame.png":
                    bodies.append(body)
                return headers, body

            monkeypatch.setitem(sample.module["media_evidence"].__globals__, "fetch", fetch)
            start = sample.clock.mono
            accepted += bool(sample.run())
            assert sample.clock.mono - start <= .1
            assert calls == ["/ops/telemetry", "/ops/health", "/frame.png",
                             "/ops/health", "/ops/telemetry"]
            assert len(bodies) == 1
        rates.append(accepted)
    # 50 ms covers a default/max period; at 1 Hz only near-due phases qualify.
    assert rates == [100, 100, 100] if hz > 1 else all(0 < n < 20 for n in rates)
    print(f"cadence hz={hz} scheduled={scheduled} accepted/100 phases per jitter={rates}")


@pytest.mark.parametrize("fault", ["still", "frozen", "old_png_then_capture", "detach", "error"])
def test_real_handlers_retained_cache(sample, runtime, monkeypatch, fault):
    hub, source = runtime.hub, runtime.source
    cached = hub.grab()
    calls = []

    def fetch(url):
        path = url.removeprefix(ORIGIN)
        calls.append(path)
        sample.advance(.001)
        if path == "/frame.png" and fault != "still":
            source.frames_run += 4
            if fault == "frozen":
                monkeypatch.setattr(hub, "_capture", lambda: None)
            elif fault == "old_png_then_capture":
                monkeypatch.setattr(hub, "grab", lambda: cached)
            elif fault == "detach":
                hub.detach()
            elif fault == "error":
                source.failed = True
        if path == "/ops/health" and len(calls) == 4 and fault == "old_png_then_capture":
            hub._capture()
        if path == "/ops/telemetry":
            runtime.observe(7122 if fault == "still" else source.frames_run)
        return runtime.fetch(url)

    monkeypatch.setitem(sample.module["media_evidence"].__globals__, "fetch", fetch)
    assert bool(sample.run()) == (fault == "still")
    assert hub.latest().png() == cached.png()  # Same invented pixels, including retained failures.
    if fault == "error":
        assert hub.errors == 1 and hub.last_error == "RuntimeError"
    if fault == "detach":
        assert not hub.stats()["attached"]


@pytest.mark.parametrize("stage", [0, 1, 2, 3, 4, "verify"])
def test_budget_includes_every_read_and_png_verification(sample, monkeypatch, stage):
    original = sample.fetch

    def fetch(url):
        if len(sample.calls) == stage:
            sample.advance(.51)
        return original(url)

    monkeypatch.setitem(sample.module["media_evidence"].__globals__, "fetch", fetch)
    if stage == "verify":
        from PIL import PngImagePlugin
        verify = PngImagePlugin.PngImageFile.verify

        def slow_verify(picture):
            sample.advance(.51)
            return verify(picture)

        monkeypatch.setattr(PngImagePlugin.PngImageFile, "verify", slow_verify)
    assert not sample.run()


def test_original_document_expiry_is_retained(sample):
    sample.parts[0]["generated_at"] = 995.2
    assert sample.run() == pytest.approx(1000.2)


def test_capped_sample_expiry_during_core_recheck(sample, tmp_path):
    helper = sample.module
    terminal = {"state": "live", "status": {"state": "idle", "app": "hermes"},
                "status_ms": 600000, "created_ms": 500000, "pid": 2,
                "foreground": {"pid": 3}, "address": "t1"}
    calls = []

    def query(hc, *args):
        calls.append(args)
        if len(calls) == 2:
            sample.advance(.5)
        return terminal

    state = tmp_path / "private"
    assert not helper["heartbeat"](state, "t1", ORIGIN, ORIGIN, query=query,
                                   media=helper["media_evidence"])
    assert len(calls) == 2 and all(c[0] == "get" for c in calls)
    assert not (state / "receipt.json").exists()
