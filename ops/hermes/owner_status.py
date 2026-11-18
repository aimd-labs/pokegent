"""Process-local, version-bound native Hermes lifecycle reports. No gameplay imports."""

import fcntl
import functools
import hashlib
import json
import os
import re
import stat
import sys
import threading
from pathlib import Path

# Inspected native lifecycle contract. Review changed sources before updating these pins.
CONTRACT = {
    'cli.py':
        'b00849345c5ad7f63297fabeb11964295f8258c9dab2b742c54ee28282bc7efe',
    'hermes_cli/main.py':
        '321236ee6372d077b028d5618e62e3aeb8a69119dcd206428d2025252e1c4df4',
    'hermes_cli/cli_tui_runtime_mixin.py':
        'a53abc6db361da914596592032adb57f46ca899e18d8538f555b595c84bfb3f5',
    'hermes_cli/cli_tui_mixin.py':
        '3e37ca1c5ada219280225c445977a696f6c609399f33377d861590a62ab2eb1c',
    'hermes_cli/cli_chat_turn_mixin.py':
        '7238cd30eaac8fe7a6a9a244816e81e82e94096032b55f9d9aa483579c3240c5',
    'hermes_cli/cli_loops_mixin.py':
        'a180e5f6e9e8496873a29da95100c04f07078b6483f847007afbedf7d185df85',
    'hermes_cli/gateway_connection.py':
        '2104ea4df7067bff26bfd2de115008a82fd4a37bce98c7cb821d3781a2ed1da5',
    'hermes_cli/cli_init_mixin.py':
        '741c46a85c3b67c28005adcb30ba315d6cd4bceca230dfd7ce514326eeca1f40',
    'hermes_cli/cli_modal_mixin.py':
        '223892d52c2aef8d85bd8639387d0f1ef890666fb7448941638c43aad5f3d8cc',
    'hermes_cli/gateway_queue.py':
        'ffefa832912144fc8ef418c76fc1dc0edfb7161e7f0b551c1559de3184732f3e',
    'hermes_cli/gateway_tools_notify.py':
        'd22de3ac5a72890ae82f62d9e7880e794ebfd75a9e57a8b92b41bde89e932cd4',
}
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,160}")


def report(state, kind=None):
    payload = f"state={state}:app=hermes"
    if kind:
        payload += ":kind=" + kind
    os.write(1, ("\033]7501;" + payload + "\007").encode())


class Lifecycle:
    def __init__(self, session, emit=report):
        self.session, self.emit = session, emit
        self.lock = threading.RLock()
        self.state = None
        self.blocks = []
        self.active = 0
        self.taken = self.held = self.rendered = False
        self.failed = False
        self.workers = []
        self.publish("working")

    def publish(self, state, kind=None):
        with self.lock:
            value = (state, kind)
            if value != self.state:
                self.emit(*value)
                self.state = value

    def busy(self):
        if self.blocks:
            self.publish("blocked", self.blocks[-1][1])
        else:
            self.publish("working")

    def identity(self, cli):
        if cli.session_id != self.session or not cli._gateway_session_open:
            self.failed = True
            self.publish("error")
            raise RuntimeError("owner session identity is missing or changed; refusing idle")

    def ready(self, cli):
        """Only called after the native idle tick; never from chat/finally/render."""
        with self.lock:
            self.identity(cli)
            if self.blocks:
                self.publish("blocked", self.blocks[-1][1])
                return
            if self.failed:
                self.publish("error")
                return
            flags = ("_agent_running", "_interactive_turn", "_command_running",
                     "_command_blocks_input", "_should_exit", "_voice_mode", "_voice_processing")
            modals = ("_approval_state", "_clarify_state", "_secret_state", "_sudo_state",
                      "_slash_confirm_state", "_connection_state")
            for key, kind in zip(modals, ("permission", "question", "auth", "auth",
                                         "permission", "auth")):
                if getattr(cli, key):
                    self.publish("blocked", kind)
                    return
            # Missing native attributes are unsupported, never a default false/idle.
            occupied = any(getattr(cli, key) for key in (*flags, *modals))
            occupied |= any(not getattr(cli, key).empty() for key in
                            ("_pending_input", "_interrupt_queue"))
            occupied |= bool(cli._pending_agent_seed or cli._seeded_first_message)
            store = cli._gateway_queue_store()
            if store is None:
                raise RuntimeError("native gateway queue state unavailable")
            state = store.connection.state(cli.session_id)
            occupied |= bool(store.connection.closed or store.connection.final
                             or not state.subscription_id)
            occupied |= bool(state.pending or state.queued_inputs or state.adopted or state.missed
                             or state.armed_admission or state.admitted_turn
                             or state.gaps != state.attached_gaps)
            occupied |= any(worker.is_alive() for worker in self.workers)
            occupied |= bool(self.active or self.taken or self.held or not self.rendered)
            if occupied:
                self.busy()
            else:
                self.publish("idle")

    def watch_queue(self, q, *, pending=False):
        original_get, original_put = q.get, q.put

        def put(*args, **kwargs):
            with self.lock:
                self.busy()
                return original_put(*args, **kwargs)

        def get(*args, **kwargs):
            # Native get waits at most 0.1s. Serialize removal and working publication
            # so a render/idle callback cannot observe a dequeued but undispatched turn.
            if not pending:
                return original_get(*args, **kwargs)
            with self.lock:
                item = original_get(*args, **kwargs)
                self.taken = True
                self.busy()
                return item

        q.put = put
        if pending:
            q.get = get


def install(cls, lifecycle):
    required = ("__init__", "_tui_init_run_state", "_tui_build_application", "_tui_idle_tick",
                "_tui_process_one_input", "_tui_hold_step", "chat", "_chat_monitor_agent_thread",
                "process_command", "_approval_callback", "_clarify_callback",
                "_secret_capture_callback", "_sudo_password_callback", "_connection_callback",
                "_vault_unlock_callback", "_vault_save_login_callback", "_vault_code_callback")
    if any(not callable(getattr(cls, name, None)) for name in required):
        raise RuntimeError("unsupported native Hermes lifecycle")

    def wrap(name, before=None, after=None):
        original = getattr(cls, name)

        @functools.wraps(original)
        def call(cli, *args, **kwargs):
            try:
                if before:
                    before(cli, *args, **kwargs)
                result = original(cli, *args, **kwargs)
                if after:
                    after(cli, result)
                return result
            except BaseException:
                with lifecycle.lock:
                    if name in {"chat", "_tui_process_one_input", "process_command"}:
                        lifecycle.active -= 1
                        if name == "_tui_process_one_input":
                            lifecycle.taken = False
                    lifecycle.failed = True
                    lifecycle.publish("error")
                raise
        setattr(cls, name, call)

    def initialized(cli, result):
        if cli.session_id != lifecycle.session:
            raise RuntimeError("initialized owner session differs from exact resume identity")

    wrap("__init__", after=initialized)

    def run_state(cli, result):
        lifecycle.watch_queue(cli._pending_input, pending=True)
        lifecycle.watch_queue(cli._interrupt_queue)

    wrap("_tui_init_run_state", after=run_state)

    def built(cli, app):
        def rendered(sender):
            with lifecycle.lock:
                lifecycle.rendered = True
            sender.after_render -= rendered
        app.after_render += rendered

    wrap("_tui_build_application", after=built)

    def begin(cli, *args, **kwargs):
        with lifecycle.lock:
            lifecycle.active += 1
            lifecycle.busy()

    def finish(cli, result):
        with lifecycle.lock:
            lifecycle.active -= 1
            lifecycle.taken = False
            # Next idle tick verifies cleanup, queues and workers before publishing idle.

    wrap("_tui_process_one_input", before=begin, after=finish)
    def command_done(cli, result):
        with lifecycle.lock:
            lifecycle.active -= 1

    wrap("process_command", before=begin, after=command_done)

    def holding(cli, result):
        with lifecycle.lock:
            lifecycle.held = result[0] is not None
            lifecycle.taken = lifecycle.held or result[1] is not None
            lifecycle.busy()

    wrap("_tui_hold_step", after=holding)

    def chat_done(cli, result):
        with lifecycle.lock:
            lifecycle.active -= 1
            # Native chat catches failures and returns None. No successful return is inferred.
            lifecycle.failed = result is None or cli._last_turn_failed
            if lifecycle.failed:
                lifecycle.publish("error")

    wrap("chat", before=begin, after=chat_done)

    def monitor(cli, turn, worker):
        with lifecycle.lock:
            lifecycle.workers = [w for w in lifecycle.workers if w.is_alive()]
            lifecycle.workers.append(worker)

    wrap("_chat_monitor_agent_thread", before=monitor)
    wrap("_tui_idle_tick", after=lambda cli, result: lifecycle.ready(cli))

    for name, kind in (("_approval_callback", "permission"), ("_clarify_callback", "question"),
                       ("_secret_capture_callback", "auth"), ("_sudo_password_callback", "auth"),
                       ("_connection_callback", "auth"), ("_vault_unlock_callback", "auth"),
                       ("_vault_save_login_callback", "auth"), ("_vault_code_callback", "auth")):
        original = getattr(cls, name)

        def modal(original, kind):
            @functools.wraps(original)
            def call(cli, *args, **kwargs):
                token = object()
                with lifecycle.lock:
                    lifecycle.blocks.append((token, kind))
                    lifecycle.publish("blocked", kind)
                try:
                    return original(cli, *args, **kwargs)
                except BaseException:
                    with lifecycle.lock:
                        lifecycle.failed = True
                    raise
                finally:
                    with lifecycle.lock:
                        lifecycle.blocks[:] = [b for b in lifecycle.blocks if b[0] is not token]
                        if lifecycle.blocks:
                            lifecycle.publish("blocked", lifecycle.blocks[-1][1])
                        else:
                            lifecycle.publish("error" if lifecycle.failed else "working")
            return call
        setattr(cls, name, modal(original, kind))


def verify_contract(root):
    for name, digest in CONTRACT.items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError("unsupported Hermes lifecycle source: " + name)


def verify_imports():
    roots = [Path("/usr"), *map(Path, json.loads(os.environ["POKEGENT_OWNER_CODE_ROOTS"]))]
    paths = [*sys.path, *[getattr(module, "__file__", None) for module in
                         tuple(sys.modules.values())]]
    for value in paths:
        if value and value not in {"built-in", "frozen"}:
            candidate = Path(value).resolve()
            if candidate != Path(__file__).resolve() and not any(
                candidate.is_relative_to(root) for root in roots
            ):
                raise RuntimeError("native import escaped the protected runtime closure")


def main():
    session = os.environ.get("POKEGENT_OWNER_SESSION", "")
    if not SESSION_ID.fullmatch(session) or session.lower() == "latest":
        raise RuntimeError("exact owner session identity required")
    profile = Path(os.environ["HERMES_HOME"])
    # Held for this process lifetime; native active-session leasing remains enabled too.
    descriptor = os.open(profile / "pokegent-owner.lock",
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        info = os.fstat(lock.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise RuntimeError("owner lock must be a private regular file")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        import hermes_bootstrap
        verify_imports()
        verify_contract(Path(hermes_bootstrap.__file__).parent)
        # Import native main FIRST: its module-level profile preprocessing must precede cli.
        from hermes_cli.main import main as native_main
        if Path(os.environ["HERMES_HOME"]) != profile:
            raise RuntimeError("native profile preprocessing selected a different owner profile")
        import cli
        verify_imports()
        lifecycle = Lifecycle(session)
        install(cli.HermesCLI, lifecycle)
        try:
            return native_main()
        finally:
            # Exit/exception is never evidence of ready-for-input.
            lifecycle.publish("error")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        report("error")
        raise
