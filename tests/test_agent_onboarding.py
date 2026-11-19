"""Execute the guide's example and configuration through in-process API seams."""

import asyncio
import io
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest

from addons.system_one.check import _frame
from addons.system_one.factory import build_provider
from addons.system_one.policy import load_config
from addons.system_one.provider import InvalidResponse, ProviderTimeout
from examples.choose_inspected_frame import main
from pokegent import server
from pokegent.user_config import load_user_config, to_legacy_config

GUIDE = Path(__file__).resolve().parents[1] / "docs/connect-your-agent.md"


@pytest.fixture
def guide_config(tmp_path, monkeypatch):
    import os

    for name in os.environ:
        if name.startswith(("SYSTEM1_", "POKEGENT_", "POKEJEV_", "VON_", "POKEVON_")):
            monkeypatch.delenv(name)
    text = GUIDE.read_text().split("```yaml\n", 1)[1].split("```", 1)[0]
    path = tmp_path / "model.yaml"
    path.write_text(text.replace("<supported-model-id>", "synthetic-model"))
    monkeypatch.setenv("SYSTEM1_API_KEY", "synthetic-key")
    return path


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setattr(server, "ThreadingHTTPServer", lambda *args: SimpleNamespace(
        server_address=("127.0.0.1", 19760), serve_forever=lambda: None,
        shutdown=lambda: None, server_close=lambda: None,
    ))
    external = server.ExternalDecider()
    calls = []

    def urlopen(req, timeout):
        # Keep the real client encoding, handlers, status mapping and decider methods.
        assert req.host == "127.0.0.1:19760" and timeout == 35
        calls.append(req)
        handler = object.__new__(external._handler())
        handler.path = urlsplit(req.full_url).path + (
            "?" + urlsplit(req.full_url).query if urlsplit(req.full_url).query else "")
        handler.headers = {"Host": req.host, "Content-Length": str(len(req.data or b"")),
                           "Content-Type": req.get_header("Content-type", "")}
        handler.rfile = io.BytesIO(req.data or b"")
        responses = []
        handler._send = lambda code, body: responses.append((code, body))
        getattr(handler, "do_" + req.get_method())()
        code, body = responses.pop()
        wire = json.dumps(body).encode()
        if code >= 400:
            raise HTTPError(req.full_url, code, str(body), {}, io.BytesIO(wire))
        return closing(io.BytesIO(wire))

    monkeypatch.setattr(server, "urlopen", urlopen)
    monkeypatch.setattr(sys, "argv", ["choose_inspected_frame.py", "--port", "19760"])
    ready = threading.Event()
    external.bind_agent_request(lambda *_: ready.set())
    with ThreadPoolExecutor(max_workers=1) as pool:
        decision = pool.submit(external.decide, _frame(2))
        try:
            assert ready.wait(2), "synthetic frame was not published"
            yield external, calls, decision
        finally:
            external.cancel()
            external.close()


def test_example_chooses_exact_inspected_frame(bridge, monkeypatch, capsys):
    external, calls, decision = bridge
    token = external.frame_response(wait_timeout=0)[1]["frame_id"]
    monkeypatch.setattr("builtins.input", lambda _: "move:1")
    main()
    assert decision.result(timeout=2).option_id == "move:1"
    assert [urlsplit(req.full_url).path for req in calls] == ["/briefing", "/frame", "/choose"]
    assert json.loads(calls[-1].data) == {"frame_id": token, "option_id": "move:1"}
    assert '"accepted": true' in capsys.readouterr().out


def test_example_stale_frame_is_refused_by_actual_api(bridge, monkeypatch):
    external, calls, _ = bridge

    def choose_after_refresh(_):
        with external._condition:
            external._frame = {**external._frame, "frame_id": "external-new"}
        return "move:1"

    monkeypatch.setattr("builtins.input", choose_after_refresh)
    with pytest.raises(HTTPError) as exc:
        main()
    assert exc.value.code == 409
    assert external._choice is None
    assert json.loads(calls[-1].data)["frame_id"] != "external-new"


def test_example_and_api_refuse_invalid_option_and_token(bridge, monkeypatch):
    external, calls, _ = bridge
    monkeypatch.setattr("builtins.input", lambda _: "unoffered")
    with pytest.raises(ValueError, match="not offered"):
        main()
    assert len(calls) == 2
    token = external.frame_response(wait_timeout=0)[1]["frame_id"]
    for body in ({"frame_id": token, "option_id": "unoffered"}, {"option_id": "move:1"}):
        with pytest.raises(HTTPError) as exc:
            server.request(19760, "/choose", body=body)
        assert exc.value.code == 400
    assert external._choice is None


def test_example_no_pending_frame_does_not_choose(bridge, monkeypatch):
    external, calls, _ = bridge
    with external._condition:
        external._frame = None
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("no frame to inspect"))
    main()
    assert len(calls) == 2 and external._choice is None


def test_example_request_failure_stops_without_retry(bridge, monkeypatch):
    external, _, _ = bridge
    attempts = []

    def unavailable(*args, **kwargs):
        attempts.append(args)
        raise TimeoutError("synthetic request failure")

    monkeypatch.setattr(server, "urlopen", unavailable)
    with pytest.raises(TimeoutError, match="synthetic"):
        main()
    assert len(attempts) == 1 and external._choice is None


@pytest.mark.parametrize("reply", ["valid", "unknown", "malformed", "timeout", "retries"])
def test_guide_chat_config_uses_real_adapter_and_bounded_transport(
    guide_config, monkeypatch, reply,
):
    cfg = load_config(to_legacy_config(load_user_config(guide_config))).config
    provider = build_provider(cfg)
    attempts = []

    def post(body, headers, remaining):
        attempts.append(remaining)
        assert 0 < remaining <= 8
        assert headers["Authorization"] == "Bearer synthetic-key"
        wire = json.loads(body)
        assert wire["model"] == "synthetic-model" and wire["stream"] is False
        criteria = json.loads(wire["messages"][1]["content"])["criteria"]
        assert "red" in criteria
        if reply in {"timeout", "retries"}:
            # Exhaust the real retry loop immediately, without socket or wall-clock waits.
            raise TimeoutError
        content = "red" if reply == "valid" else "outside"
        data = {"choices": [{"message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}]}
        return 200, {}, b"not-json" if reply == "malformed" else json.dumps(data).encode()

    assert provider.client.endpoint_path == "/v1/chat/completions"
    monkeypatch.setattr(provider.client, "_post", post)
    monkeypatch.setattr(provider.client, "_backoff", lambda *_: 0 if reply == "retries" else 8)
    try:
        if reply == "valid":
            assert asyncio.run(provider.probe()).option_id == "red"
        else:
            error = ProviderTimeout if reply in {"timeout", "retries"} else InvalidResponse
            with pytest.raises(error):
                asyncio.run(provider.probe())
        assert len(attempts) == (3 if reply == "retries" else 1)
    finally:
        provider.close()


def test_guide_offline_probe_command(guide_config, monkeypatch, capsys):
    from pokegent.cli import main as cli

    def no_network(*args, **kwargs):
        pytest.fail("offline probe attempted transport")

    monkeypatch.setattr("addons.system_one.http_provider.VonClient._post", no_network)
    monkeypatch.setattr(sys, "argv", ["pokegent", "provider", "check", "--config",
                                      str(guide_config), "--no-stub"])
    with pytest.raises(SystemExit) as exc:
        cli()
    assert exc.value.code == 0
    assert "wrong key is refused" in capsys.readouterr().out


@pytest.mark.parametrize("allow_remote", [False, True])
def test_hosted_model_run_requires_explicit_remote_opt_in(allow_remote):
    from pokegent.app import AppError, check_system1_endpoint

    url = "https://provider.example/v1"
    if allow_remote:
        assert check_system1_endpoint(url, allow_remote=True) == [
            "remote System One endpoint 'provider.example': real model calls are made",
        ]
    else:
        with pytest.raises(AppError, match="pass --allow-remote"):
            check_system1_endpoint(url, allow_remote=False)
    assert check_system1_endpoint("http://127.0.0.1:19760", allow_remote=False) == []
