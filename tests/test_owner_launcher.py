"""Record the entire launch closure without running an agent, emulator or sandbox."""

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "ops/hermes/pokegent-owner-start"
build = runpy.run_path(str(LAUNCHER))["build"]


@pytest.fixture
def owner(tmp_path):
    config = {}
    for name in ("repo", "environment", "agent_runtime", "profile", "live_home"):
        root = tmp_path / name
        root.mkdir()
        config[name] = str(root)
    (Path(config["repo"]) / "pokegent").mkdir()
    console = Path(config["environment"]) / "bin/pokegent"
    console.parent.mkdir()
    console.write_text("installed console entry")
    console.chmod(0o700)
    command = Path(config["agent_runtime"]) / "agent"
    command.write_text("#!/bin/sh\nexit 0\n")
    command.chmod(0o700)
    (command.parent / ".env").write_text("synthetic private runtime config")
    (command.parent / ".git").mkdir()
    (command.parent / ".git/config").write_text("synthetic private git config")
    config.update(home=str(tmp_path / "empty-home"), agent_command=[str(command), "--cli"],
                  cwd=config["profile"], profile_name="example-profile")
    config["mounts"] = []
    for role in ("data", "runtime", "tool", "credential", "state"):
        source = tmp_path / role
        if role in {"tool", "credential"}:
            source.touch()
        else:
            source.mkdir()
        config["mounts"].append({"path": str(source), "role": role})
    record = tmp_path / "record.json"
    fake = tmp_path / "fake-bwrap"
    fake.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                    f"open({str(record)!r}, 'w').write(json.dumps("
                    "{'argv': sys.argv[1:], 'env': dict(os.environ), 'umask': os.umask(0)}))\n")
    fake.chmod(0o700)
    config["bwrap"] = str(fake)
    return config, record


def invoke(config, tmp_path, *, pass_fds=()):
    manifest = tmp_path / "owner.json"
    manifest.write_text(json.dumps(config))
    return subprocess.run([sys.executable, "-I", str(LAUNCHER), "--config", str(manifest)],
                          env={**os.environ, "GH_TOKEN": "synthetic-token",
                               "PYTHONPATH": "/untrusted/imports", "GH_DEBUG": "api"},
                          capture_output=True, text=True, timeout=5, pass_fds=pass_fds)


def test_inherited_directory_descriptor_cannot_reach_unmounted_file(owner, tmp_path):
    config, record = owner
    hidden = tmp_path / "unmounted"
    hidden.mkdir()
    (hidden / "marker").write_text("synthetic-unmounted-content")
    descriptor = os.open(hidden, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.set_inheritable(descriptor, True)
        Path(config["bwrap"]).write_text(
            f"#!{sys.executable}\nimport json, os\n"
            "content = None\n"
            "try:\n"
            f"    fd = os.open('marker', os.O_RDONLY, dir_fd={descriptor})\n"
            "    content = os.read(fd, 100).decode()\n"
            "    os.close(fd)\n"
            "except OSError:\n"
            "    pass\n"
            f"open({str(record)!r}, 'w').write(json.dumps(content))\n"
        )
        result = invoke(config, tmp_path, pass_fds=(descriptor,))
    finally:
        os.close(descriptor)
    assert result.returncode == 0, result.stderr
    assert json.loads(record.read_text()) is None


def test_exact_binding_closure_and_no_inherited_credentials(owner, tmp_path):
    config, record = owner
    result = invoke(config, tmp_path)
    assert result.returncode == 0, result.stderr
    recorded = json.loads(record.read_text())
    args = recorded["argv"]
    bindings = {(args[i], args[i + 1], args[i + 2]) for i in range(len(args))
                if args[i] in {"--bind", "--ro-bind"}}
    system = {("--ro-bind", "/usr", "/usr")}
    for name in ("ssl", "resolv.conf", "hosts", "nsswitch.conf", "passwd", "group",
                 "localtime", "ld.so.cache"):
        source = Path("/etc", name)
        if source.exists():
            system.add(("--ro-bind", str(source), str(source)))
    expected = {("--bind" if name in {"repo", "profile", "live_home"} else "--ro-bind",
                 config[name], config[name]) for name in
                ("repo", "environment", "agent_runtime", "profile", "live_home")}
    expected.update(("--bind" if mount["role"] == "state" else "--ro-bind",
                     mount["path"], mount["path"]) for mount in config["mounts"])
    expected.update(("--ro-bind", "/dev/null", str(Path(config["agent_runtime"]) / name))
                    for name in (".env", ".git/config"))
    assert bindings == system | expected
    assert "--clearenv" in args and "--new-session" in args and "--cap-drop" in args
    assert recorded["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    assert recorded["umask"] == 0o077
    env = {args[i + 1]: args[i + 2] for i, arg in enumerate(args) if arg == "--setenv"}
    assert env["PATH"] == config["environment"] + "/bin:/usr/bin:/bin"
    assert env["HOME"] == config["home"] and env["LIVE_HOME"] == config["live_home"]
    assert env["HERMES_HOME"] == config["profile"]
    assert not {"GH_TOKEN", "PYTHONPATH", "GH_DEBUG"} & env.keys()
    assert args[-5:] == ["--chdir", config["cwd"], "--", *config["agent_command"]]
    assert not result.stdout and not result.stderr


@pytest.mark.parametrize("mode,has_state,resumed", [
    ("auto", False, False), ("auto", True, True), ("always", False, True), ("never", True, False),
])
def test_latest_session_resume_is_configurable(owner, mode, has_state, resumed):
    config, _ = owner
    config["resume"] = mode
    if has_state:
        (Path(config["profile"]) / "state.db").touch()
    args = build(config)
    command = args[args.index("--") + 1:]
    assert command == config["agent_command"] + (["--continue"] if resumed else [])


def test_custom_session_marker_and_readonly_checkout(owner):
    config, _ = owner
    marker = Path(config["profile"]) / "sessions.sqlite"
    marker.touch()
    config.update(resume_file=str(marker), resume_args=["--resume", "latest"], repo_writable=False)
    args = build(config)
    assert args[-2:] == ["--resume", "latest"]
    assert args[args.index(config["repo"]) - 1] == "--ro-bind"


@pytest.mark.parametrize("broken", ["parent", "symlink", "system", "injection", "worktree",
                                   "missing_console", "cwd", "resume_escape", "unknown",
                                   "resume_parent", "resume_symlink"])
def test_rejects_unsafe_or_incomplete_manifest(owner, tmp_path, broken):
    config, record = owner
    if broken == "parent":
        config["mounts"].append({"path": str(tmp_path), "role": "data"})
    elif broken == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(config["repo"])
        config["mounts"].append({"path": str(alias), "role": "data"})
    elif broken == "system":
        config["mounts"].append({"path": "/usr/local", "role": "state"})
    elif broken == "injection":
        config["environment_variables"] = {"PYTHONPATH": "/another/game/repo"}
    elif broken == "worktree":
        (Path(config["repo"]) / ".git").write_text("gitdir: /another/checkout/.git/worktrees/owner")
    elif broken == "missing_console":
        (Path(config["environment"]) / "bin/pokegent").unlink()
    elif broken == "cwd":
        config["cwd"] = str(tmp_path)
    elif broken == "resume_escape":
        config["resume_file"] = str(tmp_path / "other-profile.sqlite")
    elif broken == "resume_parent":
        config["resume_file"] = config["profile"] + "/../other-profile.sqlite"
    elif broken == "resume_symlink":
        marker = Path(config["profile"]) / "state.db"
        marker.symlink_to(tmp_path / "other-profile.sqlite")
    elif broken == "unknown":
        config["extra_game_repo"] = str(tmp_path)
    result = invoke(config, tmp_path)
    assert result.returncode == 2
    assert not record.exists()
    assert result.stderr == (
        "owner launch failed: check the private manifest and mount prerequisites\n"
    )


def test_wrong_owner_and_root_fail_before_launch(owner, monkeypatch):
    config, _ = owner
    config["owner_uid"] = os.getuid() + 1
    with pytest.raises(ValueError):
        build(config)
    monkeypatch.setattr(os, "getuid", lambda: 0)
    config["owner_uid"] = 0
    with pytest.raises(ValueError):
        build(config)


@pytest.mark.parametrize("config", [[], None, {"unexpected": "value"}])
def test_invalid_manifest_is_rejected(config):
    with pytest.raises(ValueError):
        build(config)
