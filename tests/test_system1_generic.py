"""Generic protocols, untrusted HTTP errors and hard deadlines; synthetic servers only."""

import asyncio
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import test_hybrid_stuck_escalation

from addons.system_one.check import _live, _Report, env_config
from addons.system_one.factory import build_decider, build_provider
from addons.system_one.policy import ConfigError, LoadedConfig, SystemOneConfig, load_config
from addons.system_one.provider import (
    InvalidResponse,
    ProviderConfigurationError,
    ProviderRejected,
    ProviderTimeout,
)
from addons.system_one.redact import Scrubber
from pokegent.loop import RunStatus, stop_message
from pokegent.options.models import ChoiceResult
from pokegent.user_config import load_user_config, to_legacy_config

runner_harness = test_hybrid_stuck_escalation.harness


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    import os

    for name in os.environ:
        if name.startswith(("SYSTEM1_", "POKEGENT_", "POKEJEV_", "VON_", "POKEVON_")):
            monkeypatch.delenv(name)


@pytest.fixture
def endpoint():
    control = SimpleNamespace(requests=[], status=200, response=None, delay=None, ended=[],
                              health_delay=None, header_delay=None, model="reported-model")

    class Handler(BaseHTTPRequestHandler):
        def respond(self, body, delay):
            if control.header_delay is not None:
                raw = b"HTTP/1.0 200 OK\r\nContent-Length: 1000\r\n\r\n" + body
                try:
                    for byte in raw:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(control.header_delay)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    control.ended.append(self.command)
                return
            self.send_response(control.status if self.command == "POST" else 200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                for byte in [body] if delay is None else [bytes([b]) for b in body]:
                    self.wfile.write(byte)
                    self.wfile.flush()
                    if delay is not None:
                        time.sleep(delay)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                control.ended.append(self.command)

        def do_GET(self):
            self.respond(b'{"status":"okay"}', control.health_delay)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            control.requests.append((self.path, request, dict(self.headers)))
            if control.response is not None:
                body = control.response
            elif "messages" in request:
                criteria = json.loads(request["messages"][1]["content"])["criteria"]
                body = {"model": control.model, "choices": [{"message": {
                    "role": "assistant", "content": next(iter(criteria)),
                }, "finish_reason": "stop"}]}
            else:
                name, question = next(iter(request["questions"].items()))
                body = {"model": control.model, "answers": {name: {
                    "type": "choice", "choice": next(iter(question["criteria"])),
                }}}
            wire = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.respond(wire, control.delay)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01))
        thread.start()
        control.url = f"http://127.0.0.1:{server.server_port}"
        try:
            yield control
        finally:
            server.shutdown()
            thread.join(timeout=3)


def config(endpoint, protocol="systemone", **kwargs):
    return SystemOneConfig(provider_url=endpoint.url, protocol=protocol,
                           request_model="selected-model", **kwargs)


@pytest.mark.parametrize("protocol", ["systemone", "openai-chat"])
@pytest.mark.parametrize("base,override,path", [
    ("", None, "/v1/{end}"), ("/v1", None, "/v1/{end}"),
    ("/prefix/v1", None, "/prefix/v1/{end}"),
    ("/v1/{end}", None, "/v1/{end}"), ("/prefix", "/choose", "/prefix/choose"),
])
@pytest.mark.parametrize("auth,key", [("Authorization", "abc"), ("x-api-key", "xy"),
                                      ("aUtHoRiZaTiOn", None)])
def test_exact_wire_and_identity(endpoint, protocol, base, override, path, auth, key):
    end = "systemone" if protocol == "systemone" else "chat/completions"
    cfg = config(endpoint, protocol, auth_header=auth, api_key=key,
                 endpoint_path=override).model_copy(update={
                     "provider_url": endpoint.url + base.format(end=end),
                 })
    provider = build_provider(cfg)
    try:
        result = asyncio.run(provider.probe())
        assert result.model_id == "reported-model"
        assert result.confidence is None and result.probabilities == {}
        assert provider.manifest()["response_model"] == "reported-model"
    finally:
        provider.close()
    assert len(endpoint.requests) == 1
    route, body, headers = endpoint.requests[0]
    assert route == path.format(end=end)
    assert body["model"] == "selected-model"
    if key:
        assert headers[cfg.auth_header] == ("Bearer " + key if cfg.auth_header == "Authorization"
                                            else key)
    else:
        assert "Authorization" not in headers and "x-api-key" not in headers
    if protocol == "systemone":
        assert set(body) == {"state", "questions", "model"}
        assert body["questions"]["action"]["criteria"] == {"red": "A color.", "run": "A verb."}
    else:
        assert set(body) == {"model", "stream", "messages"} and body["stream"] is False
        assert [m["role"] for m in body["messages"]] == ["system", "user"]
        assert json.loads(body["messages"][1]["content"]) == {
            "state": {"check": "pokegent provider check"},
            "criteria": {"red": "A color.", "run": "A verb."},
        }


@pytest.mark.parametrize("entry", ["user", "fallback"])
def test_env_config_defaults_and_overrides(entry, endpoint, monkeypatch, tmp_path):
    values = {"SYSTEM1_BASE_URL": endpoint.url, "SYSTEM1_PROTOCOL": "openai-chat",
              "SYSTEM1_AUTH_HEADER": "x-api-key", "SYSTEM1_TIMEOUT": "1.25",
              "SYSTEM1_REQUEST_MODEL": "selected", "SYSTEM1_MODEL": "alias-pin",
              "SYSTEM1_MODEL_ID": "actual-pin", "SYSTEM1_API_KEY": "ab"}
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    raw = (to_legacy_config(load_user_config(tmp_path / "absent")) if entry == "user"
           else env_config())
    cfg = load_config(raw).config
    assert cfg.timeout_seconds == 1.25 and cfg.auth_header == "x-api-key"
    assert cfg.request_model == "selected" and cfg.model == "actual-pin"
    assert cfg.endpoint_path == "/v1/chat/completions"
    assert cfg.safe_summary()["api_key"] == "set"


@pytest.mark.parametrize("timeouts", ["2.5", "{request: 2.5}"])
def test_yaml_cli_precedence_and_legacy_expansion(tmp_path, timeouts):
    path = tmp_path / "run.yaml"
    path.write_text("system1:\n  base_url: http://127.0.0.1\n  protocol: systemone\n"
                    f"  timeouts: {timeouts}\n  request_model: file\n")
    cfg = load_config(to_legacy_config(load_user_config(
        path, environ={"SYSTEM1_REQUEST_MODEL": "env", "SYSTEM1_PROTOCOL": "openai-chat"},
        cli={"system1": {"request_model": "cli"}},
    )), env={}).config
    assert cfg.request_model == "cli" and cfg.timeout_seconds == 2.5
    legacy = load_config({"system_one": {"provider_url": "${BASE}",
                                        "protocol": "${PROTOCOL}",
                                        "request_model": "${SELECTOR}",
                                        "auth_header": "${HEADER}",
                                        "timeout_seconds": "${TIMEOUT}"}}, env={
        "BASE": "http://127.0.0.1/v1", "PROTOCOL": "openai-chat", "SELECTOR": "selected",
        "HEADER": "x-api-key", "TIMEOUT": "2.5",
    }).config
    assert legacy.protocol == cfg.protocol and legacy.timeout_seconds == cfg.timeout_seconds


@pytest.mark.parametrize("field,value", [
    ("provider_url", "http://host:bad"), ("provider_url", "http://host:65536"),
    ("provider_url", "http://user@host"), ("provider_url", "http://host?key=x"),
    ("provider_url", "http://host#x"), ("provider_url", "http://ho\nst"),
    ("protocol", "guess"), ("protocol", "openai-chat"), ("strict_model_pin", True),
    ("timeout_seconds", True), ("timeout_seconds", float("nan")),
    ("timeout_seconds", float("inf")), ("timeout_seconds", 0), ("timeout_seconds", 121),
    ("timeout_seconds", None), ("auth_header", ""), ("auth_header", "H ost"),
    ("auth_header", "x\r\ny"), ("auth_header", "hOsT"), ("auth_header", "CONTENT-LENGTH"),
    ("auth_header", "x-request-ID"), ("auth_header", "Transfer-Encoding"),
    ("api_key", "abc\r\nInjected: yes"), ("api_key", "abc\t"), ("api_key", "é"),
    ("endpoint_path", "/x\n"), ("endpoint_path", "//elsewhere"),
    ("request_model", "bad model"), ("request_model", "x" * 201),
])
def test_validation_fails_without_echoing_values(field, value):
    with pytest.raises(ConfigError) as error:
        load_config({"provider_url": "http://127.0.0.1", field: value}, env={})
    if field == "api_key":
        assert value not in str(error.value)


def test_custom_defaults(endpoint):
    cfg = SystemOneConfig(provider_url=endpoint.url)
    assert cfg.protocol == "systemone" and cfg.endpoint_path == "/v1/systemone"
    assert cfg.request_model is None and cfg.model is None and not cfg.strict_model_pin
    assert cfg.timeout_seconds == 8 and cfg.connect_timeout_seconds == 2
    assert cfg.effective_read_timeout() == 4 and cfg.auth_header == "Authorization"


@pytest.mark.parametrize("protocol", ["systemone", "openai-chat"])
@pytest.mark.parametrize("identity,strict", [(None, False), (None, True), ("different", True),
                                            ("reported-model", True)])
def test_true_response_pin_requires_actual_identity(endpoint, protocol, identity, strict):
    endpoint.model = identity
    provider = build_provider(config(endpoint, protocol, model="reported-model",
                                     strict_model_pin=strict))
    try:
        if strict and identity != "reported-model":
            with pytest.raises(ProviderConfigurationError, match="pinned"):
                asyncio.run(provider.probe())
        else:
            result = asyncio.run(provider.probe())
            assert result.model_id == (identity or "")
            assert provider.manifest()["response_model"] == identity
    finally:
        provider.close()


@pytest.mark.parametrize("response", [
    {"choices": []}, {"choices": [{"message": {"role": "assistant", "content": "outside"}}]},
    {"choices": [{"message": {"role": "assistant", "content": "red"}, "finish_reason": "length"}]},
    {"choices": [{"message": {"role": "assistant", "content": {"choice": "red"}},
                  "finish_reason": "stop"}]}, b"not json",
])
def test_chat_malformed_output_is_ordinary_invalid_response(endpoint, response):
    endpoint.response = response
    provider = build_provider(config(endpoint, "openai-chat"))
    try:
        with pytest.raises(InvalidResponse):
            asyncio.run(provider.probe())
    finally:
        provider.close()
    assert len(endpoint.requests) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
@pytest.mark.parametrize("hybrid", [False, True])
def test_rejection_is_readable_scrubbed_terminal_through_runner(
    endpoint, runner_harness, hybrid, status, caplog,
):
    endpoint.status = status
    endpoint.response = {"error": {"message": "invalid model/key abc\n\x1b[31m rejected"}}
    runner, _, actions, offers, events = runner_harness(hybrid=hybrid, continue_sink=True)
    decider = build_decider(config(endpoint, "openai-chat", api_key="abc"))
    if hybrid:
        runner.decider.system1 = decider
    else:
        runner.decider = decider
    with caplog.at_level(logging.DEBUG, logger="pokegent.system_one"):
        try:
            summary = runner.run()
        finally:
            decider.close()
    assert summary.status is RunStatus.SAFE_PAUSE and summary.verdict == "owner_review"
    assert summary.stop_reason == "provider_configuration"
    assert summary.stop_details["status"] == status
    assert "invalid model/key" in summary.stop_details["error"]
    assert "credentials" in summary.stop_details["error"] if status in {401, 403} else (
        "request_model" in summary.stop_details["error"])
    published = json.dumps(summary.stop_details) + repr(events.events) + caplog.text
    assert "abc" not in published and "\x1b" not in published
    assert "owner review" in stop_message(summary.stop_reason, summary.stop_details)
    assert len(endpoint.requests) == 1 and not actions and not offers


def test_hybrid_missing_confidence_still_offers_agent(runner_harness):
    runner, _, _, offers, _ = runner_harness()
    runner.decider.system1.decide = lambda frame: ChoiceResult(option_id="sign", confidence=None)
    runner.decider._thresholds["default"] = 0
    runner.decider.agent_handoff_timeout_s = 42.0
    runner.decider.decide(runner.perceiver.perceive("test").frame)
    assert len(offers) == 1
    assert offers[0]["wait_timeout"] == 42.0
    assert offers[0]["handoff_timeout"] is True
    assert offers[0]["default"] is None


@pytest.mark.parametrize("method", ["health", "decision"])
@pytest.mark.parametrize("phase", ["headers", "body"])
def test_dribbling_peer_has_hard_deadline_and_connection_cleanup(endpoint, method, phase):
    if phase == "headers":
        endpoint.header_delay = .025
    else:
        endpoint.delay = endpoint.health_delay = .025
    provider = build_provider(config(endpoint, timeout_seconds=.16, read_timeout_seconds=.15))
    started = time.monotonic()
    try:
        if method == "health":
            assert provider.client.fetch_health(timeout_s=.16) is None
        else:
            with pytest.raises(ProviderTimeout):
                asyncio.run(provider.probe())
        assert time.monotonic() - started < .65
        deadline = time.monotonic() + .5
        while provider.client._active and time.monotonic() < deadline:
            time.sleep(.01)
        assert not provider.client._active
        # A deadline must not permanently cancel subsequent outage recovery calls.
        endpoint.delay = endpoint.health_delay = endpoint.header_delay = None
        assert asyncio.run(provider.probe()).option_id == "red"
    finally:
        provider.close()


def test_probe_uses_selected_adapter_with_bounded_health(endpoint, capsys):
    endpoint.health_delay = .025
    cfg = config(endpoint, "openai-chat", timeout_seconds=.15)
    report = _Report()
    started = time.monotonic()
    _live(report, LoadedConfig(cfg))
    assert time.monotonic() - started < .7
    assert report.failed == 0 and len(endpoint.requests) == 1
    assert endpoint.requests[0][0] == "/v1/chat/completions"
    output = capsys.readouterr().out
    assert "confidence=None" in output and "reported-model" in output


def test_short_secrets_and_only_recognized_error_fields(endpoint):
    scrubber = Scrubber(["abc", "xy", "z"])
    assert all(key not in scrubber.text("abc xy z") for key in ["abc", "xy", "z"])
    endpoint.status = 409  # nonretryable does not automatically mean configuration failure
    endpoint.response = b"unrecognized arbitrary raw body abc"
    provider = build_provider(config(endpoint, api_key="abc"))
    try:
        with pytest.raises(ProviderRejected) as error:
            asyncio.run(provider.probe())
        assert not isinstance(error.value, ProviderConfigurationError)
        assert "raw body" not in str(error.value)
        assert "abc" not in repr(error.value.detail)
    finally:
        provider.close()


@pytest.mark.parametrize("status", [408, 429, 503])
def test_transient_http_retains_bounded_retry(endpoint, status, monkeypatch):
    provider = build_provider(config(endpoint, retry={"max_attempts": 2,
        "backoff_base_seconds": .01, "backoff_max_seconds": .01, "jitter": 0}))
    post = provider.client._post
    attempts = []

    def once(*args):
        attempts.append(1)
        if len(attempts) == 1:
            return status, {}, b'{"error":"transient"}'
        return post(*args)

    monkeypatch.setattr(provider.client, "_post", once)
    try:
        result = asyncio.run(provider.probe())
        assert result.meta["attempts"] == 2 and len(attempts) == 2
    finally:
        provider.close()


@pytest.mark.parametrize("protocol", ["systemone", "openai-chat"])
@pytest.mark.parametrize("hybrid", [False, True])
def test_strict_pin_failure_is_terminal_through_runner(endpoint, runner_harness, protocol, hybrid):
    runner, _, actions, offers, _ = runner_harness(hybrid=hybrid)
    decider = build_decider(config(endpoint, protocol, model="pinned", strict_model_pin=True))
    if hybrid:
        runner.decider.system1 = decider
    else:
        runner.decider = decider
    try:
        summary = runner.run()
        assert summary.verdict == "owner_review"
        assert summary.stop_details["reason"] == "provider_configuration"
        assert "pinned" in summary.stop_details["error"]
        assert not actions and not offers and len(endpoint.requests) == 1
    finally:
        decider.close()


@pytest.mark.parametrize("valid_save", [True, False])
def test_rejected_decision_keeps_normal_validated_final_save(
    endpoint, runner_harness, tmp_path, monkeypatch, valid_save,
):
    from pathlib import Path

    from test_intro_save_validation import synthetic_save

    from pokegent.app import AppConfig, play_state
    from pokegent.games.gen3.firered.reader import ui_symbols as U

    runner, _, actions, offers, events = runner_harness()
    decider = build_decider(config(endpoint, api_key="abc"))
    runner.decider.system1 = decider
    endpoint.status = 401
    endpoint.response = {"detail": "invalid key abc"}
    reader = synthetic_save(U.NEW_GAME_SCENE_CALLBACK)
    corrupt = synthetic_save(U.NEW_GAME_SCENE_CALLBACK, bad_header=True)
    saved = []

    class Actor:
        def load_state(self, path):
            self.path = path

        def save_state(self, path):
            Path(path).write_text("synthetic state")
            saved.append(path)

        def read_atomic_snapshot(self):
            source = reader if valid_save or not self.path.endswith(".pending") else corrupt
            return source._source.read_atomic_snapshot()

    cfg = AppConfig(checkpoint_every=0)
    runner.perceiver.catalog = None
    runner.executor.frames_advanced = 0
    app = SimpleNamespace(
        runner=runner, run=runner.run, executor=runner.executor, config=cfg, objectives=None,
        bundle=SimpleNamespace(manifests=[], skipped=[], ownership=lambda _: SimpleNamespace(
            to_dict=lambda: {"totals": {"gaps": 0}})),
        sink=SimpleNamespace(as_dicts=lambda: [], total=0),
    )
    monkeypatch.setattr("pokegent.app.build_runner", lambda **kwargs: app)
    initial = tmp_path / "initial.state"
    initial.write_text("initial")
    try:
        _, summary, doc = play_state(
            Actor(), reader.profile, initial, config=cfg, decider=runner.decider,
            decider_name="hybrid", facts=None, events=events, state_dir=tmp_path / "run",
        )
    finally:
        decider.close()
    assert summary.verdict == doc["verdict"] == "owner_review"
    assert doc["stop_details"]["status"] == 401
    assert "abc" not in json.dumps(doc)
    assert not actions and not offers and saved
    assert doc["states"]["final"] == ("final.state" if valid_save else None)
    assert (tmp_path / "run/final.state").exists() == valid_save


def test_reason_is_bounded_and_scrubbed_before_truncation_and_normalization():
    from addons.system_one.http_provider import rejection_reason

    key = "very-long-secret-value" * 20
    assert key[:200] not in rejection_reason(json.dumps({"message": key}).encode(), Scrubber([key]))
    assert len(rejection_reason(json.dumps({"detail": "x" * 500}).encode(), Scrubber())) == 240
    assert "a bc" not in rejection_reason(b'{"message":"a\\nbc"}', Scrubber(["a bc"]))
    ignored = rejection_reason(b'{"ignored":"private body"}', Scrubber())
    assert ignored == "no recognized JSON reason"
    assert rejection_reason(b'[' * 2000, Scrubber()) == "no recognized JSON reason"


@pytest.mark.parametrize("name,value", [
    ("SYSTEM1_TIMEOUT", "nan"), ("SYSTEM1_TIMEOUT", "0"),
    ("SYSTEM1_PROTOCOL", "native"), ("SYSTEM1_AUTH_HEADER", "Host"),
    ("SYSTEM1_API_KEY", "abc\r\nx:y"), ("SYSTEM1_BASE_URL", "http://host:bad"),
])
@pytest.mark.parametrize("entry", ["user", "fallback"])
def test_both_environment_paths_reject_invalid_config(name, value, entry, monkeypatch, tmp_path):
    monkeypatch.setenv("SYSTEM1_BASE_URL", "http://127.0.0.1")
    monkeypatch.setenv(name, value)
    raw = (to_legacy_config(load_user_config(tmp_path / "absent"))
           if entry == "user" else env_config())
    with pytest.raises(ConfigError):
        load_config(raw)
