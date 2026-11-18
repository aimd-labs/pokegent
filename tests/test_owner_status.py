"""Synthetic native lifecycle only: never import or launch the installed Hermes runtime."""

import queue
import runpy
import threading
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

MODULE = Path(__file__).resolve().parents[1] / "ops/hermes/owner_status.py"
status = runpy.run_path(str(MODULE))


class Event(list):
    def __iadd__(self, item):
        self.append(item)
        return self

    def __isub__(self, item):
        self.remove(item)
        return self


@pytest.fixture
def native():
    events = []
    lifecycle = status["Lifecycle"]("exact_session", lambda *v: events.append(v))

    class Native:
        _seeded_first_message = None

        def __init__(self):
            self.session_id = "exact_session"
            self._gateway_session_open = True
            self._interactive_turn = self._last_turn_failed = False
            self._pending_agent_seed = None
            self.remote = NS(pending=[], queued_inputs={}, adopted={}, missed=set(),
                             armed_admission=None, admitted_turn=None, gaps=0, attached_gaps=0,
                             subscription_id="sub")
            self.connection = NS(state=lambda sid: self.remote, closed=False, final=False)
            self.tick = lambda: None
            self.turn = lambda: "answer"
            self.command = lambda: True
            self.cleanup = lambda: None
            self.callback = lambda: None

        def _gateway_queue_store(self):
            return NS(connection=self.connection)

        def _tui_init_run_state(self):
            self._agent_running = self._command_running = self._command_blocks_input = False
            self._should_exit = self._voice_mode = self._voice_processing = False
            for key in ("approval", "clarify", "secret", "sudo", "slash_confirm", "connection"):
                setattr(self, "_" + key + "_state", None)
            self._pending_input, self._interrupt_queue = queue.Queue(), queue.Queue()
            if self._seeded_first_message:
                self._pending_input.put(self._seeded_first_message)
                self._seeded_first_message = None

        def _tui_build_application(self):
            return NS(after_render=Event())

        def _tui_idle_tick(self):
            self.tick()

        def _tui_process_one_input(self, value):
            if value.startswith("/"):
                return self.process_command(value)
            self._agent_running = self._interactive_turn = True
            try:
                return self.chat(value)
            finally:
                self.cleanup()
                self._agent_running = self._interactive_turn = False

        def process_command(self, value):
            return self.command()

        def _tui_hold_step(self, value):
            return value

        def chat(self, value):
            return self.turn()

        def _chat_monitor_agent_thread(self, turn, worker):
            return None

        def _approval_callback(self):
            return self.callback()

        _clarify_callback = _secret_capture_callback = _sudo_password_callback = _approval_callback
        _connection_callback = _vault_unlock_callback = _approval_callback
        _vault_save_login_callback = _vault_code_callback = _approval_callback

    status["install"](Native, lifecycle)
    return Native, lifecycle, events


def ready(native, *, seed=None):
    cls, lifecycle, events = native
    cli = cls()
    cli._seeded_first_message = seed
    cli._tui_init_run_state()
    app = cli._tui_build_application()
    for callback in list(app.after_render):
        callback(app)
    assert events == [("working", None)]  # rendering alone is never idle
    cli._tui_idle_tick()
    return cli, lifecycle, events


def test_clean_readiness_transitions_only_and_nonchat_command(native):
    cli, _, events = ready(native)
    assert events[-1] == ("idle", None)
    cli._tui_idle_tick()
    assert len(events) == 2
    cli.command = lambda: events.append(("inside-command", None)) or True
    cli.process_command("/status")  # inline dispatch too
    assert events[-2:] == [("working", None), ("inside-command", None)]
    cli._tui_idle_tick()
    assert events[-1] == ("idle", None)


def test_seed_dequeue_cleanup_and_queued_continuation(native):
    cli, lifecycle, events = ready(native, seed="seed")
    assert events == [("working", None)]
    value = cli._pending_input.get(timeout=0.1)
    lifecycle.ready(cli)
    assert events[-1] == ("working", None)
    cli.cleanup = lambda: cli._pending_input.put("continuation")
    cli._tui_process_one_input(value)
    cli._tui_idle_tick()
    assert events[-1] == ("working", None)
    cli.cleanup = lambda: None
    cli._tui_process_one_input(cli._pending_input.get(timeout=0.1))
    assert events[-1] == ("working", None)
    cli._tui_idle_tick()
    assert events[-1] == ("idle", None)


@pytest.mark.parametrize("failure", ["exception", "handled", "failed_flag", "command", "tick"])
def test_failures_never_publish_finally_idle(native, failure):
    cli, lifecycle, events = ready(native)

    def raises():
        raise RuntimeError("synthetic failure")

    if failure == "handled":
        cli.turn = lambda: None
    elif failure == "failed_flag":
        cli._last_turn_failed = True
    elif failure == "tick":
        cli.tick = raises
    elif failure == "command":
        cli.command = raises
    else:
        cli.turn = raises
    if failure in {"exception", "command", "tick"}:
        with pytest.raises(RuntimeError):
            if failure == "tick":
                cli._tui_idle_tick()
            else:
                cli._tui_process_one_input("/command" if failure == "command" else "chat")
    else:
        cli._tui_process_one_input("chat")
    assert events[-1] == ("error", None)
    cli.tick = lambda: None
    cli._tui_idle_tick()
    assert events[-1] == ("error", None)
    assert lifecycle.active == 0


def test_interrupted_lingering_worker_hold_and_gateway_queue(native):
    cli, _, events = ready(native)
    worker = NS(is_alive=lambda: True)
    cli._pending_input.put("chat")
    cli._pending_input.get(timeout=0.1)
    cli._chat_monitor_agent_thread(None, worker)
    cli._tui_process_one_input("chat")
    cli._tui_idle_tick()
    assert events[-1] == ("working", None)
    worker.is_alive = lambda: False
    cli._tui_hold_step((object(), None))
    cli._tui_idle_tick()
    assert events[-1] == ("working", None)
    cli._tui_hold_step((None, None))
    cli.remote.pending = [{"status": "queued"}]
    cli._tui_idle_tick()
    assert events[-1] == ("working", None)
    cli.remote.pending = []
    cli._tui_idle_tick()
    assert events[-1] == ("idle", None)


def test_nested_and_overlapping_modals(native):
    cli, lifecycle, events = ready(native)
    first, second, release_first, release_second = (threading.Event() for _ in range(4))

    def callback():
        if threading.current_thread().name == "first":
            first.set()
            assert release_first.wait(3)
        else:
            second.set()
            assert release_second.wait(3)

    cli.callback = callback
    threads = [threading.Thread(target=cli._approval_callback, name="first"),
               threading.Thread(target=cli._secret_capture_callback, name="second")]
    threads[0].start()
    assert first.wait(3)
    threads[1].start()
    assert second.wait(3)
    release_first.set()
    threads[0].join(3)
    cli._tui_idle_tick()
    assert events[-1] == ("blocked", "auth")
    release_second.set()
    threads[1].join(3)
    assert lifecycle.blocks == [] and events[-1] == ("working", None)
    nested = []

    def outer():
        cli.callback = lambda: nested.append(events[-1])
        cli._clarify_callback()
        nested.append(events[-1])

    cli.callback = outer
    cli._approval_callback()
    assert nested == [("blocked", "question"), ("blocked", "permission")]
    cli._tui_idle_tick()
    assert events[-1] == ("idle", None)


@pytest.mark.parametrize("change", ["identity", "missing", "disconnected", "modal"])
def test_unknown_identity_interfaces_and_modals_fail_closed(native, change):
    cli, _, events = ready(native)
    if change == "identity":
        cli.session_id = "other"
    elif change == "missing":
        del cli._command_running
    elif change == "disconnected":
        cli.connection.closed = True
    else:
        cli._secret_state = {"waiting": True}
    if change in {"identity", "missing"}:
        with pytest.raises((RuntimeError, AttributeError)):
            cli._tui_idle_tick()
        assert events[-1] == ("error", None)
    else:
        cli._tui_idle_tick()
        assert events[-1][0] == ("blocked" if change == "modal" else "working")


def test_unsupported_source_and_methods_refuse(tmp_path):
    with pytest.raises(RuntimeError):
        status["install"](object, status["Lifecycle"]("s", lambda *x: None))
    for name in status["CONTRACT"]:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("unsupported")
    with pytest.raises(RuntimeError, match="unsupported Hermes lifecycle source"):
        status["verify_contract"](tmp_path)


def test_mismatched_initial_identity_never_reaches_idle(native):
    cls, _, events = native
    # Change the expected session, rather than touching any real session store.
    native[1].session = "different"
    with pytest.raises(RuntimeError, match="initialized owner session"):
        cls()
    assert events[-1] == ("error", None)


def test_osc_payload_and_import_escape_are_fail_closed(monkeypatch, tmp_path):
    output = []
    monkeypatch.setattr(status["os"], "write", lambda fd, data: output.append((fd, data)))
    status["report"]("blocked", "auth")
    assert output == [(1, b"\x1b]7501;state=blocked:app=hermes:kind=auth\x07")]
    monkeypatch.setenv("POKEGENT_OWNER_CODE_ROOTS", '[]')
    monkeypatch.setattr(status["sys"], "path", [str(tmp_path / "untrusted")])
    with pytest.raises(RuntimeError, match="escaped the protected runtime closure"):
        status["verify_imports"]()


def test_native_profile_preprocessing_precedes_cli_import(tmp_path, monkeypatch):
    import builtins

    module = runpy.run_path(str(MODULE))
    events, order = [], []
    lifecycle_type = module["Lifecycle"]
    globals_ = module["main"].__globals__
    monkeypatch.setitem(globals_, "Lifecycle", lambda sid: lifecycle_type(
        sid, lambda *v: events.append(v)))
    monkeypatch.setitem(globals_, "verify_imports", lambda: None)
    monkeypatch.setitem(globals_, "verify_contract", lambda root: None)
    monkeypatch.setitem(globals_, "install", lambda cls, life: order.append("hooks"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("POKEGENT_OWNER_SESSION", "exact_session")
    original_import = builtins.__import__
    argv = list(status["sys"].argv)

    def imported(name, *args, **kwargs):
        if name == "hermes_bootstrap":
            return NS(__file__=str(tmp_path / "hermes_bootstrap.py"))
        if name == "hermes_cli.main":
            order.append("native-profile-preprocessing")
            return NS(main=lambda: order.append("native-main"))
        if name == "cli":
            order.append("cli-import")
            return NS(HermesCLI=object)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", imported)
    # A second process-local adapter must refuse before native imports/startup.
    with (tmp_path / "pokegent-owner.lock").open("a") as held:
        (tmp_path / "pokegent-owner.lock").chmod(0o600)
        module["fcntl"].flock(held, module["fcntl"].LOCK_EX | module["fcntl"].LOCK_NB)
        with pytest.raises(BlockingIOError):
            module["main"]()
        assert order == []
    module["main"]()
    assert order == ["native-profile-preprocessing", "cli-import", "hooks", "native-main"]
    assert status["sys"].argv == argv
    assert events == [("working", None), ("error", None)]
