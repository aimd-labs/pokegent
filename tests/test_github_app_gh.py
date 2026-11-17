"""Authentication uses a synthetic signer, HTTP response and child process only."""

import base64
import io
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import github_app_gh as app


@pytest.fixture
def config(tmp_path):
    key = tmp_path / "synthetic.pem"
    key.write_text("synthetic key, not a credential")
    key.chmod(0o600)
    child = tmp_path / "gh"
    child.write_text("synthetic gh")
    return {"app_id": "example-client", "installation_id": 1234, "repository": "example/game",
            "private_key": str(key), "gh": str(child)}


def response(**overrides):
    return {"token": "synthetic-installation-token", "permissions": app.PERMISSIONS,
            "repositories": [{"full_name": "example/game"}], **overrides}


def fake_http(monkeypatch, result):
    requests = []
    handlers = []

    def open_request(request, timeout):
        requests.append((request, timeout))
        return io.StringIO(json.dumps(result))

    def opener(*args):
        handlers.extend(args)
        return SimpleNamespace(open=open_request)

    monkeypatch.setattr(app.urllib.request, "build_opener", opener)
    return requests, handlers


def test_jwt_signing_has_no_secret_argv_or_inherited_environment(config, monkeypatch):
    calls = []
    monkeypatch.setenv("GH_TOKEN", "inherited-secret")
    monkeypatch.setattr(app.time, "time", lambda: 1000)

    def sign(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout=b"synthetic-signature")

    monkeypatch.setattr(app.subprocess, "run", sign)
    signed = app.jwt(config)
    header, payload, signature = signed.split(".")
    def decode(value):
        return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    assert decode(header) == {"alg": "RS256", "typ": "JWT"}
    assert decode(payload) == {"iat": 940, "exp": 1540, "iss": "example-client"}
    assert signature == app.encode(b"synthetic-signature").decode()
    argv, kwargs = calls[0]
    assert argv == ["/usr/bin/openssl", "dgst", "-sha256", "-sign", config["private_key"]]
    assert kwargs["env"] == {"PATH": "/usr/bin:/bin"}
    assert signed not in argv
    assert kwargs["stderr"] == subprocess.DEVNULL


def test_token_request_is_repo_and_permission_scoped(config, monkeypatch):
    requests, handlers = fake_http(monkeypatch, response())
    assert app.installation_token(config, "synthetic-jwt") == "synthetic-installation-token"
    request, timeout = requests[0]
    assert request.full_url == "https://api.github.com/app/installations/1234/access_tokens"
    assert timeout == 15 and request.method == "POST"
    assert json.loads(request.data) == {"repositories": ["game"], "permissions": app.PERMISSIONS}
    assert request.headers["Authorization"] == "Bearer synthetic-jwt"
    assert handlers[0].proxies == {}
    with pytest.raises(ValueError):
        handlers[1].redirect_request(None, None, None, None, "https://example.invalid")


@pytest.mark.parametrize("result", [
    response(repositories=[]), response(repositories=[{"full_name": "other/game"}]),
    response(repositories=[{"full_name": "example/game"}, {"full_name": "example/other"}]),
    response(permissions={"administration": "write"}), response(token=""),
    response(token="bad\ntoken"), response(permissions=None),
    response(repositories=["wrong-shape"]), response(permissions={"metadata": "write"}),
])
def test_unexpected_credentials_fail_closed(config, monkeypatch, result):
    fake_http(monkeypatch, result)
    with pytest.raises(ValueError):
        app.installation_token(config, "synthetic-jwt")


def test_child_only_gets_token_in_environment_and_output_is_redacted(config, monkeypatch, capsys):
    token, signed = "synthetic-installation-token", "synthetic-jwt"
    monkeypatch.setattr(app, "jwt", lambda _: signed)
    monkeypatch.setattr(app, "installation_token", lambda *_: token)
    monkeypatch.setenv("GH_DEBUG", "api")
    monkeypatch.setenv("GH_TOKEN", "unrelated-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://example.invalid")
    calls = []

    def child(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=7, stdout=f"result: {token}\n".encode(),
                               stderr=f"error: {signed}\n".encode())

    monkeypatch.setattr(app.subprocess, "run", child)
    assert app.run(config, ["pr", "list"]) == 7
    argv, kwargs = calls[0]
    assert argv == [config["gh"], "pr", "list", "--repo", "example/game"]
    assert token not in argv and signed not in argv
    env = kwargs["env"]
    assert env["GH_TOKEN"] == token
    assert not {"GH_DEBUG", "HTTPS_PROXY", "PYTHONPATH"} & env.keys()
    assert not os.path.exists(env["GH_CONFIG_DIR"])
    captured = capsys.readouterr()
    assert captured.out == "result: [REDACTED]\n" and captured.err == "error: [REDACTED]\n"


@pytest.mark.parametrize("command", [
    ["auth", "token"], ["api", "user"], ["pr", "merge"], ["extension", "exec", "something"],
    ["pr", "list", "-Rother/repo"], ["pr", "list", "--repo=other/repo"],
    ["pr", "list", "--hostname", "example.invalid"], ["issue", "view", "1", "--web"],
    ["pr", "list", "--jq", "env.GH_TOKEN"], ["pr", "create", "--editor"],
])
def test_unsafe_commands_do_not_mint_any_token(config, monkeypatch, command):
    def forbidden(_):
        pytest.fail("unsafe command minted a JWT")

    monkeypatch.setattr(app, "jwt", forbidden)
    with pytest.raises(ValueError):
        app.run(config, command)


def test_auth_failure_does_not_log_exception(config, monkeypatch, capsys, tmp_path):
    private = tmp_path / "app.json"
    private.write_text(json.dumps(config))
    monkeypatch.setattr(app.sys, "argv", ["helper", "--config", str(private), "--", "pr", "list"])

    def fail(*_):
        raise ValueError("synthetic-secret-must-not-appear")

    monkeypatch.setattr(app, "jwt", fail)
    assert app.main() == 2
    captured = capsys.readouterr()
    assert "synthetic-secret" not in captured.err and not captured.out
    assert "GitHub App command failed" in captured.err


@pytest.mark.skipif(not Path("/usr/bin/openssl").exists(), reason="optional OpenSSL not installed")
def test_rs256_signature_with_disposable_synthetic_key(config, tmp_path):
    """Verify the actual signer as well as the mocked protocol; no credential is used."""
    subprocess.run(["/usr/bin/openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt",
                    "rsa_keygen_bits:2048", "-out", config["private_key"]],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    signed = app.jwt(config)
    header, payload, signature = signed.split(".")
    signature_file = tmp_path / "signature"
    signature_file.write_bytes(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
    public_key = tmp_path / "public.pem"
    subprocess.run(["/usr/bin/openssl", "pkey", "-in", config["private_key"], "-pubout",
                    "-out", str(public_key)], check=True, capture_output=True)
    verified = subprocess.run(
        ["/usr/bin/openssl", "dgst", "-sha256", "-verify", str(public_key),
         "-signature", str(signature_file)], input=f"{header}.{payload}".encode(),
        capture_output=True,
    )
    assert verified.returncode == 0
