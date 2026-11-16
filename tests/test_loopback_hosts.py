"""Exercise the real ASGI apps without sockets or a ROM."""
import asyncio

import pytest

from pokegent.addons import AddonConfig
from pokegent.dashboard.server import create_app as dashboard_app
from pokegent.dashboard.state import ViewerState
from pokegent.dashboard.stream.audio import AudioHub
from pokegent.dashboard.stream.frames import FrameHub
from pokegent.dashboard.stream.server import create_app as stream_app


@pytest.fixture(params=["dashboard", "stream", "combined"])
def app(request):
    if request.param == "stream":
        return stream_app(FrameHub(None), AudioHub(), AddonConfig())
    state = ViewerState(run_id="synthetic", decider="agent", max_steps=1, realtime=False)
    application = dashboard_app(state, AddonConfig())
    if request.param == "combined":
        application.mount("", stream_app(
            FrameHub(None), AudioHub(), AddonConfig(), middleware=False
        ))
    return application


def exchange(app, headers, *, kind="http", path="/health", method="GET"):
    messages = []

    async def receive():
        if kind == "websocket":
            return {"type": "websocket.disconnect", "code": 1000}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": kind, "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": method, "http_version": "1.1", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": headers, "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 19701),
    }
    asyncio.run(app(scope, receive, send))
    return messages


@pytest.mark.parametrize("host", [
    b"localhost", b"LOCALHOST:19701", b"127.0.0.1", b"127.0.0.1:19701", b"[::1]:19701",
])
def test_loopback_health_remains_available(app, host):
    messages = exchange(app, [(b"host", host)])
    assert messages[0]["status"] == 200


@pytest.mark.parametrize("host", [
    b"rebind.example:19701", b"127.0.0.1.rebind.example:19701", b"192.0.2.1:19701",
    b"localhost@rebind.example:19701", b"rebind.example@127.0.0.1:19701",
    b"localhost:0", b"localhost:65536", b"localhost:bad", b"localhost/path", b"localhost.",
    b"localhost ", b"[::1", b"::1", b"127.1", b"", b"\xff",
])
def test_untrusted_host_rejected_even_with_matching_origin(app, host):
    messages = exchange(app, [(b"host", host), (b"origin", b"http://" + host)])
    assert messages[0]["status"] == 403
    assert b"loopback Host required" in messages[-1]["body"]
    assert (b"x-content-type-options", b"nosniff") in messages[0]["headers"]


@pytest.mark.parametrize("headers", [[], [(b"host", b"localhost"), (b"host", b"localhost")]])
def test_missing_and_duplicate_hosts_rejected(app, headers):
    assert exchange(app, headers)[0]["status"] == 403


@pytest.mark.parametrize("path", ["/ops/telemetry", "/ops/decisions", "/ops/events", "/frame.png"])
def test_rebinding_never_reaches_operator_or_frame_routes(app, path):
    messages = exchange(app, [(b"host", b"rebind.example:19701")], path=path)
    assert messages[0]["status"] == 403


@pytest.mark.parametrize("path", ["/ws", "/ws/frames", "/ws/audio"])
def test_websocket_rebinding_rejected_before_accept(app, path):
    messages = exchange(app, [(b"host", b"rebind.example:19701")], kind="websocket", path=path)
    assert messages == [{"type": "websocket.close", "code": 1008}]


def test_read_only_method_guard_preserved(app):
    assert exchange(app, [(b"host", b"localhost:19701")], method="POST")[0]["status"] == 405
