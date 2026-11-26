"""Security regressions using fake keys, isolated resolvers and actual loopback HTTP peers."""

import asyncio
import json
import socket
import socketserver
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import test_system1_generic as generic

from addons.system_one import http_provider as transport
from addons.system_one.check import _live, _Report
from addons.system_one.factory import build_decider, build_provider
from addons.system_one.policy import LoadedConfig
from addons.system_one.provider import InvalidResponse, ProviderConfigurationError, ProviderTimeout

clean_env = generic.clean_env
config = generic.config
endpoint = generic.endpoint
runner_harness = generic.runner_harness


@pytest.mark.parametrize("protocol", ["systemone", "openai-chat"])
def test_success_metadata_in_saved_summary_and_probe(
    endpoint, protocol, tmp_path, monkeypatch, capsys,
):
    from pokegent import app

    key = "abc"
    endpoint.model = "echo-" + key
    cfg = config(endpoint, protocol, api_key=key, model=endpoint.model, strict_model_pin=True)
    decider = build_decider(cfg)
    post = decider.provider.client._post

    def response(*args):
        status, headers, body = post(*args)
        return status, {**headers, "server": "echo-" + key, "warning": "warn-" + key}, body

    monkeypatch.setattr(decider.provider.client, "_post", response)
    monkeypatch.setattr(app, "build_decider", lambda *args, **kwargs: (decider, [], decider.close))

    @contextmanager
    def emulator(*args, **kwargs):
        yield object(), object()

    def play(*args, **kwargs):
        result = asyncio.run(decider.provider.probe())
        assert key not in result.model_dump_json()
        assert result.model_id.startswith("echo-") and result.model_id != cfg.model
        assert result.meta["model_mismatch"] is False  # strict validation used the raw identity
        return (SimpleNamespace(bundle=SimpleNamespace(manifests=[], skipped=[])),
                SimpleNamespace(stop_reason="max_steps", stop_details={"max_steps": 1},
                                iterations=1),
                {"exit_code": 0, "stop_reason": "max_steps", "actions": {}})

    monkeypatch.setattr(app, "open_emulator", emulator)
    monkeypatch.setattr(app, "load_config_file", lambda *_: {})
    monkeypatch.setattr(app, "build_facts", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "pokegent.games.gen3.firered.navigation.make_navigation_services",
        lambda *args, **kwargs: SimpleNamespace(
            graph=None, navigation=None, directory=None
        ),
    )
    monkeypatch.setattr(app, "play_state", play)
    source = tmp_path / "synthetic-input"
    source.write_text("invented input; never opened by an emulator")
    report = app.run_offline(state=source, rom_path=source, out_dir=tmp_path / "run",
                             decider="system1", log=lambda *_: None)
    assert report.exit_code == 0
    saved = (tmp_path / "run/summary.json").read_text()
    assert key not in saved
    manifest = json.loads(saved)["decider_manifest"]
    assert manifest["response_model"].startswith("echo-")
    assert manifest["server_version"].startswith("echo-")
    _live(_Report(), LoadedConfig(cfg))
    assert key not in capsys.readouterr().out


@pytest.mark.parametrize("protocol", ["systemone", "openai-chat"])
def test_metadata_is_scrubbed_before_bounding_and_raw_pin_validation(endpoint, protocol):
    key = "k" * 300
    endpoint.model = "prefix-" + key
    provider = build_provider(config(endpoint, protocol, api_key=key, model="prefix-"))
    try:
        result = asyncio.run(provider.probe())
        assert "k" * 80 not in json.dumps(provider.manifest()) + result.model_dump_json()
        assert result.meta["model_mismatch"] is True
    finally:
        provider.close()
    # A scrubbed/truncated prefix must never satisfy the actual response pin.
    provider = build_provider(config(endpoint, protocol, api_key=key,
                                     model="prefix-", strict_model_pin=True))
    try:
        with pytest.raises(ProviderConfigurationError):
            asyncio.run(provider.probe())
    finally:
        provider.close()


@pytest.mark.parametrize("method", ["health", "decision", "cancel", "probe"])
def test_stalled_dns_is_killed_reaped_and_does_not_block_later_requests(
    endpoint, method, monkeypatch, capsys,
):
    processes = []
    popen = subprocess.Popen

    def track(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(transport.subprocess, "Popen", track)
    original = transport._RESOLVE_CODE
    # Runs the stall inside the real isolated resolver process; no external DNS occurs.
    monkeypatch.setattr(transport, "_RESOLVE_CODE", "import time; time.sleep(60)")
    cfg = config(endpoint, timeout_seconds=.10, retry={"max_attempts": 1}).model_copy(
        update={"provider_url": endpoint.url.replace("127.0.0.1", "synthetic.invalid")})
    provider = build_provider(cfg)
    started = time.monotonic()
    try:
        if method == "health":
            for _ in range(3):
                assert provider.client.fetch_health(timeout_s=.10) is None
        elif method == "decision":
            with pytest.raises(ProviderTimeout):
                asyncio.run(provider.probe())
        elif method == "probe":
            _live(_Report(), LoadedConfig(cfg))
            assert "not available (informational)" in capsys.readouterr().out
        else:
            async def cancel():
                task = asyncio.create_task(provider.probe())
                while not processes:
                    await asyncio.sleep(.001)
                provider.cancel()
                with pytest.raises(ProviderTimeout):
                    await task
            asyncio.run(cancel())
        assert time.monotonic() - started < .8
        deadline = time.monotonic() + .3
        while (provider.client._active or any(process.poll() is None for process in processes)
               or any(not process.stdout.closed for process in processes)) and (
                   time.monotonic() < deadline):
            time.sleep(.005)
        assert not provider.client._active
        assert processes and all(process.poll() is not None for process in processes)
        assert all(process.stdout.closed for process in processes)
        # Hosted hostnames remain supported: substitute synthetic address results, not a URL IP.
        answers = socket.getaddrinfo("127.0.0.1", int(endpoint.url.rsplit(":", 1)[1]),
                                     type=socket.SOCK_STREAM)
        monkeypatch.setattr(transport, "_RESOLVE_CODE", "print(" + repr(json.dumps(answers)) + ")")
        if method not in {"cancel", "probe"}:
            assert asyncio.run(provider.probe()).option_id == "red"
        monkeypatch.setattr(transport, "_RESOLVE_CODE", original)
    finally:
        workers = tuple(provider._pool._threads) if provider._pool else ()
        provider.close()
        for worker in workers:
            worker.join(timeout=.3)
            assert not worker.is_alive()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 415, 422, 200, 409, 408, 429, 503])
def test_oversized_body_keeps_status_semantics(endpoint, status, monkeypatch):
    from addons.system_one.provider import ProviderRejected, ProviderUnavailable

    provider = build_provider(config(endpoint, retry={"max_attempts": 1}))
    calls = []

    def response(*args):
        calls.append(1)
        return status, {}, b"x" * (transport.MAX_RESPONSE_BYTES + 1)

    monkeypatch.setattr(provider.client, "_post", response)
    error = (ProviderConfigurationError if status in transport.CONFIGURATION_STATUSES else
             InvalidResponse if status == 200 else ProviderTimeout if status == 408 else
             ProviderUnavailable if status in {429, 503} else ProviderRejected)
    try:
        with pytest.raises(error) as rejected:
            asyncio.run(provider.probe())
        assert len(calls) == 1
        if status != 200:
            assert rejected.value.detail["status"] == status
    finally:
        provider.close()


@pytest.mark.parametrize("hybrid", [False, True])
def test_oversized_auth_rejection_remains_terminal_before_input(
    endpoint, runner_harness, hybrid, monkeypatch,
):
    runner, _, actions, offers, _ = runner_harness(hybrid=hybrid)
    decider = build_decider(config(endpoint))
    monkeypatch.setattr(decider.provider.client, "_post", lambda *args: (
        401, {}, b"x" * (transport.MAX_RESPONSE_BYTES + 1)))
    if hybrid:
        runner.decider.system1 = decider
    else:
        runner.decider = decider
    try:
        summary = runner.run()
        assert summary.verdict == "owner_review"
        assert summary.stop_details["status"] == 401
        assert not actions and not offers
    finally:
        decider.close()


def test_http11_loopback_handler_dispatch_get_and_post():
    seen = []
    body = (b'{"status":"okay","model":"actual","answers":{"action":'
            b'{"type":"choice","choice":"red"}}}')
    wire = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            + f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def reply(self):
            self.wfile.write(wire)
            self.wfile.flush()
            self.close_connection = True

        def do_GET(self):
            seen.append((self.command, self.path, b""))
            self.reply()

        def do_POST(self):
            seen.append((self.command, self.path,
                         self.rfile.read(int(self.headers["Content-Length"]))))
            self.reply()

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01))
        thread.start()
        provider = build_provider(config(
            SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}"),
            timeout_seconds=1, connect_timeout_seconds=.5, read_timeout_seconds=.5,
            retry={"max_attempts": 1}))
        started = time.monotonic()
        try:
            assert provider.client.fetch_health(timeout_s=1) == {"status": "okay"}
            assert asyncio.run(provider.probe()).option_id == "red"
            assert time.monotonic() - started < 1.5  # This test's own finite budget.
        finally:
            provider.close()
            server.shutdown()
            thread.join(timeout=1)
    assert [(command, path) for command, path, _ in seen] == [
        ("GET", "/health"), ("POST", "/v1/systemone")]
    assert json.loads(seen[1][2])["questions"]["action"]["criteria"] == {
        "red": "A color.", "run": "A verb."}


@pytest.mark.parametrize("body", [b"invalid JSON", b'\xff', b'[' * 2000])
def test_unusable_rejection_reason_remains_terminal(endpoint, body):
    endpoint.status, endpoint.response = 401, body
    provider = build_provider(config(endpoint, api_key="abc"))
    try:
        with pytest.raises(ProviderConfigurationError) as rejected:
            asyncio.run(provider.probe())
        assert rejected.value.detail["status"] == 401
    finally:
        provider.close()


@pytest.mark.parametrize("framing", ["incomplete", "chunked-stall", "oversized"])
def test_terminal_status_survives_broken_or_stalled_loopback_body(framing):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(401)
            self.send_header("Transfer-Encoding" if framing == "chunked-stall"
                             else "Content-Length", "chunked" if framing == "chunked-stall"
                             else str(transport.MAX_RESPONSE_BYTES + 100))
            self.end_headers()
            try:
                if framing == "chunked-stall":
                    for _ in range(100):
                        self.wfile.write(b"1\r\nx\r\n")
                        self.wfile.flush()
                        time.sleep(.01)
                elif framing == "oversized":
                    self.wfile.write(b"x" * (transport.MAX_RESPONSE_BYTES + 100))
                else:
                    self.wfile.write(b"incomplete")
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01))
        thread.start()
        provider = build_provider(config(SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}"),
                                         timeout_seconds=.2, retry={"max_attempts": 1}))
        try:
            with pytest.raises(ProviderConfigurationError) as rejected:
                asyncio.run(provider.probe())
            assert rejected.value.detail["status"] == 401
            assert "incomplete" not in str(rejected.value)
        finally:
            provider.close()
            server.shutdown()
            thread.join(timeout=1)


def test_localhost_resolves_without_parent_thread_dns_and_keeps_host_header(endpoint, monkeypatch):
    def unexpected_dns(*args, **kwargs):
        raise AssertionError("DNS must run in the killable process")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)
    cfg = config(endpoint).model_copy(
        update={"provider_url": endpoint.url.replace("127.0.0.1", "localhost")})
    provider = build_provider(cfg)
    try:
        assert provider.client.fetch_health(timeout_s=1) == {"status": "okay"}
        assert asyncio.run(provider.probe()).model_id == "reported-model"
        assert endpoint.requests[0][2]["Host"].startswith("localhost:")
    finally:
        provider.close()


@pytest.mark.parametrize("method", ["health", "decision"])
def test_stalled_tls_opening_has_total_deadline_and_closes_socket(method):
    closed = threading.Event()

    class Peer(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(1)
            while self.request.recv(4096):
                pass  # Accept the ClientHello but never complete the TLS handshake.
            closed.set()

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Peer) as server:
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01))
        thread.start()
        provider = build_provider(config(SimpleNamespace(
            url=f"https://127.0.0.1:{server.server_address[1]}"), timeout_seconds=.12,
            retry={"max_attempts": 1}))
        started = time.monotonic()
        try:
            if method == "health":
                assert provider.client.fetch_health(timeout_s=.12) is None
            else:
                with pytest.raises(ProviderTimeout):
                    asyncio.run(provider.probe())
            assert time.monotonic() - started < .5
            assert closed.wait(.5)
        finally:
            provider.close()
            server.shutdown()
            thread.join(timeout=1)


def test_custom_success_metadata_keys_and_values_scrubbed_before_caps(endpoint, monkeypatch):
    key = "abc" * 100
    provider = build_provider(config(endpoint, api_key=key))
    wire = json.dumps({"model": "honest-model", "answers": {"action": {"choice": "red"}},
                       "truncation": {"echo-" + key: key}}).encode()
    monkeypatch.setattr(provider.client, "_post", lambda *args: (
        200, {"server": "server-" + key, "warning": "warning-" + key}, wire))
    try:
        result = asyncio.run(provider.probe())
        assert "abc" not in result.model_dump_json() + json.dumps(provider.manifest())
        assert result.model_id == "honest-model"
        assert result.meta["truncation"] and result.meta["server_version"].startswith("server-")
    finally:
        provider.close()
