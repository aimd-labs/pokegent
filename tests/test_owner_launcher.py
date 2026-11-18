"""Record the entire launch closure without running an agent, emulator or sandbox."""

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "ops/hermes/pokegent-owner-start"
launcher = runpy.run_path(str(LAUNCHER))
build = launcher["build"]


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


def invoke(config, tmp_path, *, pass_fds=(), synthetic_test=True):
    manifest = tmp_path / "owner.json"
    manifest.write_text(json.dumps(config))
    command = [sys.executable, "-I", str(LAUNCHER), "--config", str(manifest)]
    if synthetic_test:
        command.append("--synthetic-test")
    return subprocess.run(command,
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


def test_production_rejects_bootstrap_in_writable_checkout_before_child(owner, tmp_path):
    config, record = owner
    bootstrap = Path(config["repo"]) / "owner-start"
    bootstrap.write_bytes(LAUNCHER.read_bytes())
    manifest = tmp_path / "owner.json"
    manifest.write_text(json.dumps(config))
    result = subprocess.run([sys.executable, "-I", str(bootstrap), "--config", str(manifest)],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert not record.exists()


def test_protected_bootstrap_and_parent_layout(owner, tmp_path, monkeypatch):
    config, _ = owner
    # A real system executable supplies operator-owned file/parent permissions.
    trusted = Path("/usr/bin/python3").resolve()
    monkeypatch.setitem(build.__globals__, "__file__", str(trusted))
    config["bwrap"] = str(trusted)
    assert build(config, manifest=trusted)[-2:] == config["agent_command"]
    with pytest.raises(ValueError, match="outside writable mounts"):
        launcher["protected_file"](trusted, [(trusted.parent, True)])

    bootstrap = tmp_path / "bootstrap"
    bootstrap.write_bytes(LAUNCHER.read_bytes())
    bootstrap.chmod(0o555)
    with pytest.raises(ValueError, match="protected from the owner"):
        launcher["protected_file"](bootstrap, [])

    # Even an operator-owned file is replaceable through an owner-owned parent.
    actual_stat = Path.stat

    def operator_file_stat(self, **kwargs):
        result = actual_stat(self, **kwargs)
        if self == bootstrap:
            values = list(result)
            values[4] = os.getuid() + 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "stat", operator_file_stat)
    with pytest.raises(ValueError, match="protected from the owner"):
        launcher["protected_file"](bootstrap, [])


@pytest.fixture
def protected_layout(owner, tmp_path, monkeypatch):
    """Model operator metadata without needing root or creating a real deployment."""
    config, record = owner
    directory = tmp_path / "operator"
    directory.mkdir()
    bootstrap = directory / "owner-start"
    bootstrap.write_bytes(LAUNCHER.read_bytes())
    manifest = directory / "owner.json"
    manifest.write_text(json.dumps(config))
    protected = {bootstrap, manifest, Path(config["bwrap"]), *bootstrap.parents}
    actual_stat, actual_access = Path.stat, os.access
    owned, writable = set(), set()

    def metadata(self, **kwargs):
        result = actual_stat(self, **kwargs)
        if self in protected and self not in owned:
            values = list(result)
            values[4] = os.getuid() + 1
            return os.stat_result(values)
        return result

    def access(value, mode, **kwargs):
        if mode == os.W_OK and Path(value) in protected:
            return Path(value) in writable
        return actual_access(value, mode, **kwargs)

    monkeypatch.setattr(Path, "stat", metadata)
    monkeypatch.setattr(os, "access", access)
    monkeypatch.setitem(build.__globals__, "__file__", str(bootstrap))
    return config, record, manifest, protected, owned, writable


def test_production_accepts_operator_protected_layout(protected_layout):
    config, _, manifest, *_ = protected_layout
    assert build(config, manifest=manifest)[0] == config["bwrap"]


@pytest.mark.parametrize("broken", [
    "manifest_owned", "manifest_writable", "parent_owned", "parent_writable",
    "manifest_symlink", "parent_symlink", "writable_binding", "relative_bwrap",
    "bwrap_owned", "bwrap_writable", "bwrap_symlink",
])
def test_production_trust_failure_prevents_any_child(protected_layout, monkeypatch, broken):
    config, record, manifest, protected, owned, writable = protected_layout
    selected = manifest
    if broken == "manifest_owned":
        owned.add(manifest)
    elif broken == "manifest_writable":
        writable.add(manifest)
    elif broken == "parent_owned":
        owned.add(manifest.parent)
    elif broken == "parent_writable":
        writable.add(manifest.parent)
    elif broken == "manifest_symlink":
        selected = manifest.with_name("alias.json")
        selected.symlink_to(manifest)
    elif broken == "parent_symlink":
        alias = manifest.parent.with_name("alias")
        alias.symlink_to(manifest.parent, target_is_directory=True)
        selected = alias / manifest.name
    elif broken == "writable_binding":
        config["mounts"].append({"path": str(manifest.parent), "role": "state"})
    elif broken == "relative_bwrap":
        config["bwrap"] = "fake-bwrap"
    elif broken == "bwrap_owned":
        owned.add(Path(config["bwrap"]))
    elif broken == "bwrap_writable":
        writable.add(Path(config["bwrap"]))
    elif broken == "bwrap_symlink":
        alias = manifest.parent / "bwrap-alias"
        alias.symlink_to(config["bwrap"])
        config["bwrap"] = str(alias)
    manifest.write_text(json.dumps(config))
    if broken == "manifest_owned":
        manifest.chmod(0o444)

    def forbidden(*args, **kwargs):
        pytest.fail("untrusted configuration reached child execution")

    monkeypatch.setattr(os, "execve", forbidden)
    monkeypatch.setattr(sys, "argv", ["owner-start", "--config", str(selected)])
    assert launcher["main"]() == 2
    assert not record.exists()



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
    args = build(config, synthetic_test=True)
    command = args[args.index("--") + 1:]
    assert command == config["agent_command"] + (["--continue"] if resumed else [])


def test_custom_session_marker_and_readonly_checkout(owner):
    config, _ = owner
    marker = Path(config["profile"]) / "sessions.sqlite"
    marker.touch()
    config.update(resume_file=str(marker), resume_args=["--resume", "latest"], repo_writable=False)
    args = build(config, synthetic_test=True)
    assert args[-2:] == ["--resume", "latest"]
    assert args[args.index(config["repo"]) - 1] == "--ro-bind"


@pytest.mark.parametrize("broken", ["parent", "symlink", "system", "injection", "worktree",
                                   "missing_console", "cwd", "resume_escape", "unknown",
                                   "resume_parent", "resume_symlink", "agent_parent",
                                   "agent_symlink", "relative_bwrap"])
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
    elif broken == "agent_parent":
        config["agent_command"][0] = config["agent_runtime"] + "/../fake-bwrap"
    elif broken == "agent_symlink":
        alias = Path(config["agent_runtime"]) / "alias"
        alias.symlink_to(config["bwrap"])
        config["agent_command"][0] = str(alias)
    elif broken == "relative_bwrap":
        config["bwrap"] = "fake-bwrap"
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
        build(config, synthetic_test=True)
    monkeypatch.setattr(os, "getuid", lambda: 0)
    config["owner_uid"] = 0
    with pytest.raises(ValueError):
        build(config, synthetic_test=True)


@pytest.mark.parametrize("config", [[], None, {"unexpected": "value"}])
def test_invalid_manifest_is_rejected(config):
    with pytest.raises(ValueError):
        build(config, synthetic_test=True)


@pytest.fixture
def status_owner(owner, tmp_path):
    config, record = owner
    adapter = tmp_path / "owner_status.py"
    adapter.write_text("# reviewed single-file adapter\n")
    bootstrap = (
        "import os, sys, runpy; "
        "os.environ.pop('PYTHONHOME', None); os.environ.pop('PYTHONPATH', None); "
        "os.environ.pop('VIRTUAL_ENV', None); "
        f"sys.path.insert(0, {config['agent_runtime']!r}); "
        "sys._hermes_pin_default_home = True; import hermes_bootstrap; "
        "os.environ.get('HERMES_HOME') or os.environ.__setitem__('HERMES_HOME', "
        "str(__import__('hermes_constants').get_default_hermes_root())); "
        "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)"
    )
    config.update(status_adapter=str(adapter), resume="always",
                  resume_args=["--resume", "exact_session"],
                  native_runtime_command=[config["agent_command"][0], "-I", "-c", bootstrap])
    config["agent_command"] += ["--profile", "example-profile", "--no-restore-cwd"]
    return config, record


def test_native_status_launch_preserves_profile_resume_and_import_closure(status_owner, tmp_path):
    config, record = status_owner
    result = invoke(config, tmp_path)
    assert result.returncode == 0, result.stderr
    recorded = json.loads(record.read_text())
    args = recorded["argv"]
    command = args[args.index("--") + 1:]
    assert command[:3] == config["native_runtime_command"][:3]
    assert command[3] == config["native_runtime_command"][3].replace(
        "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)",
        f"runpy.run_path({config['status_adapter']!r}, run_name='__main__')")
    assert command[4:] == config["agent_command"][1:] + config["resume_args"]
    assert ["--ro-bind", config["status_adapter"], config["status_adapter"]] == args[
        args.index(config["status_adapter"]) - 1:args.index(config["status_adapter"]) + 2]
    env = {args[i + 1]: args[i + 2] for i, a in enumerate(args) if a == "--setenv"}
    assert env["POKEGENT_OWNER_SESSION"] == "exact_session"
    assert env["HERMES_PROFILE"] == "example-profile"
    assert env["HERMES_HOME"] == config["profile"]
    assert json.loads(env["POKEGENT_OWNER_CODE_ROOTS"]) == [config["agent_runtime"],
        next(m["path"] for m in config["mounts"] if m["role"] == "runtime")]
    assert "PYTHONPATH" not in env
    assert recorded["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}


@pytest.mark.parametrize("broken", ["auto", "latest", "missing", "entry", "flags", "python",
                                   "writable_adapter", "duplicate", "closure"])
def test_status_launch_prerequisites_refuse_before_child(status_owner, tmp_path, broken):
    config, record = status_owner
    if broken == "auto":
        config["resume"] = "auto"
    elif broken == "latest":
        config["resume_args"][1] = "latest"
    elif broken == "missing":
        config.pop("resume_args")
    elif broken == "entry":
        config["native_runtime_command"][3] += "; arbitrary_code()"
    elif broken == "flags":
        config["native_runtime_command"][1] = "-E"
    elif broken == "python":
        config["native_runtime_command"][0] = config["bwrap"]
    elif broken == "duplicate":
        config["agent_command"] += ["--resume", "other"]
    elif broken == "writable_adapter":
        new = Path(config["repo"]) / "owner_status.py"
        new.write_text("# writable")
        config["status_adapter"] = str(new)
    else:
        # Even mode 0555 is owner-replaceable; protection is an operator prerequisite.
        with pytest.raises(ValueError, match="protected from the owner"):
            launcher["protected_tree"](Path(config["agent_runtime"]), [])
        return
    result = invoke(config, tmp_path)
    assert result.returncode == 2 and not record.exists()


def test_operator_protected_status_adapter_and_runtime_closure(protected_layout, tmp_path):
    config, _, manifest, protected, _, _ = protected_layout
    adapter = tmp_path / "operator/owner_status.py"
    adapter.write_text("# single reviewed file")
    # Capture the exact supported bootstrap shape from a synthetic native command, never run it.
    root = config["agent_runtime"]
    bootstrap = (
        "import os, sys, runpy; "
        "os.environ.pop('PYTHONHOME', None); os.environ.pop('PYTHONPATH', None); "
        "os.environ.pop('VIRTUAL_ENV', None); "
        f"sys.path.insert(0, {root!r}); "
        "sys._hermes_pin_default_home = True; import hermes_bootstrap; "
        "os.environ.get('HERMES_HOME') or os.environ.__setitem__('HERMES_HOME', "
        "str(__import__('hermes_constants').get_default_hermes_root())); "
        "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)"
    )
    config.update(status_adapter=str(adapter), resume="always",
                  resume_args=["--resume", "exact_session"],
                  native_runtime_command=[config["agent_command"][0], "-I", "-c", bootstrap])
    protected.add(adapter)
    for directory in [Path(root), *[Path(m["path"]) for m in config["mounts"]
                                   if m["role"] == "runtime"]]:
        protected.update((directory, *directory.parents, *directory.rglob("*")))
    command = build(config, manifest=manifest)
    assert command[-2:] == ["--resume", "exact_session"]
    assert "runpy.run_path(" in command[-4]
