"""Synthetic HTTP coverage for model selection through both environment entry paths."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from addons.system_one.check import env_config
from addons.system_one.factory import build_provider
from addons.system_one.policy import load_config
from addons.system_one.provider import ProviderConfigurationError
from pokegent.user_config import load_user_config, to_legacy_config


@pytest.fixture
def endpoint():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, json.loads(self.rfile.read(int(
                self.headers["Content-Length"]
            )))))
            body = json.dumps({
                "model": "response-v1",
                "answers": {"action": {
                    "type": "choice", "choice": "red",
                    "probabilities": {"red": 1.0, "run": 0.0}, "confidence": 1.0,
                }},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", requests
        finally:
            server.shutdown()
            thread.join(timeout=3)


def _config(source, tmp_path):
    if source == "user-env":
        return load_config(to_legacy_config(load_user_config(tmp_path / "absent.yaml"))).config
    return load_config(env_config()).config


@pytest.fixture
def system1_env(monkeypatch, endpoint):
    # Do not inherit operator settings or keys in synthetic HTTP tests.
    import os

    for name in os.environ:
        if name.startswith(("SYSTEM1_", "POKEGENT_", "POKEJEV_", "VON_", "POKEVON_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("SYSTEM1_BASE_URL", endpoint[0])


@pytest.mark.parametrize("source", ["user-env", "fallback-env"])
@pytest.mark.parametrize("request_model", [None, "", "selector-alias"])
@pytest.mark.parametrize("expected_model", [None, "response-v1", "other-response-v1"])
def test_request_model_reaches_http_independently_of_response_pin(
    source, request_model, expected_model, tmp_path, monkeypatch, endpoint, system1_env
):
    if request_model is not None:
        monkeypatch.setenv("SYSTEM1_REQUEST_MODEL", request_model)
    if expected_model is not None:
        monkeypatch.setenv("SYSTEM1_MODEL_ID", expected_model)
    config = _config(source, tmp_path)
    provider = build_provider(config)
    try:
        result = asyncio.run(provider.probe())
    finally:
        provider.close()

    assert config.request_model == (request_model or None)
    assert config.model == expected_model
    assert len(endpoint[1]) == 1
    path, body = endpoint[1][0]
    assert path == "/v1/systemone"
    assert body["questions"]["action"]["criteria"] == {"red": "A color.", "run": "A verb."}
    assert body["state"] == {"check": "pokegent provider check"}
    if request_model:
        assert body["model"] == request_model
    else:
        assert "model" not in body
    assert result.option_id == "red"
    assert result.model_id == "response-v1"
    assert result.meta["model_mismatch"] == bool(
        expected_model and expected_model != "response-v1"
    )


@pytest.mark.parametrize("source", ["user-env", "fallback-env"])
def test_strict_response_pin_still_rejects_mismatch(
    source, tmp_path, monkeypatch, endpoint, system1_env
):
    monkeypatch.setenv("SYSTEM1_REQUEST_MODEL", "selector-alias")
    monkeypatch.setenv("SYSTEM1_MODEL_ID", "other-response-v1")
    config = _config(source, tmp_path).model_copy(update={"strict_model_pin": True})
    provider = build_provider(config)
    try:
        with pytest.raises(ProviderConfigurationError, match="pinned"):
            asyncio.run(provider.probe())
    finally:
        provider.close()
    assert len(endpoint[1]) == 1
    assert endpoint[1][0][1]["model"] == "selector-alias"


@pytest.mark.parametrize("env_value, expected", [
    (None, "file-selector"), ("", "file-selector"), ("env-selector", "env-selector"),
])
def test_request_model_env_overrides_yaml(tmp_path, env_value, expected):
    path = tmp_path / "config.yaml"
    path.write_text("system1:\n  base_url: http://127.0.0.1:19799\n"
                    "  request_model: file-selector\n  model: response-v1\n")
    environ = {} if env_value is None else {"SYSTEM1_REQUEST_MODEL": env_value}
    config = load_config(to_legacy_config(load_user_config(path, environ=environ)), env={}).config
    assert config.request_model == expected
    assert config.model == "response-v1"
