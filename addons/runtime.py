"""Lazy coordinator for the independent local stream and dashboard addons."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from addons.config import (
    PORT_MAX,
    PORT_MIN,
    AddonConfig,
    AddonConfigError,
    AddonStartError,
    bind_socket,
)
from pokegent.addons import AddonTaps
from pokegent.dashboard.stream.frames import FrameHub


def load_tables(facts: Any, rom: Any, profile: Any) -> Any | None:
    tables = getattr(facts, "tables", None)
    if tables is not None:
        return tables
    try:
        from pokegent.games.gen3.firered.rom.rom_tables import GameTables

        return GameTables.from_rom_file(rom, profile.profile_id)
    except Exception:  # noqa: BLE001 - names are decoration
        return None


def goal_probe(app: Any) -> dict[str, Any] | None:
    view = app.perceiver.goal_view()
    if view is None:
        return None
    goal: dict[str, Any] = {
        "goal_id": view.goal_id,
        "summary": view.summary,
        "revision": view.revision,
    }
    session = app.objectives
    if session is not None:
        objective = session.objective
        if objective is not None:
            goal["kind"], goal["set_by"] = objective.kind, objective.set_by
        goal["done"] = session.done
        script = session.script
        if script is not None and script.position < len(script.plan):
            goal["next"] = str(script.plan[script.position].get("objective_id", "")).replace(
                "_", " "
            )
    return goal


@dataclass
class AddonRuntime:
    config: AddonConfig
    state: Any = None
    hub: FrameHub | None = None
    audio: Any = None
    server: Any = None
    servers: list[Any] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    finished: bool = False
    stream_url: str | None = None

    def events_listener(self) -> Callable[[Any], None] | None:
        return self.state.on_event if self.state is not None else None

    def wrap_decider(self, decider: Any) -> Any:
        if self.state is None:
            return decider
        from pokegent.dashboard.records import ObservedDecider

        return ObservedDecider(
            decider, self.state.on_decider_call, on_start=self.state.on_decider_start
        )

    def attach_names(self, facts: Any, rom: Any, profile: Any) -> None:
        if self.state is None:
            return
        from pokegent.dashboard.names import NameBook

        self.state.names = NameBook(load_tables(facts, rom, profile))

    def attach_actor(self, actor: Any) -> None:
        if self.hub is not None:
            self.hub.attach(actor.reader())
        attach_audio = getattr(actor, "attach_audio_sink", None)
        if self.audio is not None and attach_audio is not None:
            attach_audio(self.audio.push)

    def attach_app(self, app: Any) -> None:
        if self.state is None:
            return
        app.perceiver.observers.append(self.state.on_perception)
        self.state.bind(
            goal=lambda: goal_probe(app), counters=lambda: app.runner.counters.as_dict()
        )

    def finish(self, *, stop_reason: str | None, exit_code: int | None) -> None:
        if self.finished:
            return
        self.finished = True
        if self.state is not None:
            self.state.finish(stop_reason=stop_reason, exit_code=exit_code)
        if self.hub is not None:
            try:
                self.hub.grab(max_age=0.0)
            except Exception:  # noqa: BLE001
                pass
            self.hub.detach()

    def linger(self) -> None:
        if self.config.linger_s > 0 and self.state is not None:
            if self.state.view()["run"]["exit_code"] is not None:
                time.sleep(self.config.linger_s)

    def stats(self) -> dict[str, Any]:
        return {
            "port": self.config.port,
            "ports": [server._sock.getsockname()[1] for server in self.servers],
            "realtime": self.config.realtime_on,
            "frames": self.hub.stats() if self.hub is not None else None,
            "audio": self.audio.stats() if self.audio is not None else None,
        }

    def close(self) -> None:
        if self.hub is not None:
            self.hub.stop()
        for server in self.servers:
            server.stop()


def _enabled(config: Mapping[str, Any], name: str) -> AddonConfig:
    value = config.get(name)
    return value if isinstance(value, AddonConfig) else AddonConfig()


def start_addons(config: Mapping[str, Any], taps: AddonTaps) -> AddonRuntime:
    """Start only the requested listeners; stream-only never imports dashboard modules."""
    viewer = _enabled(config, "viewer")
    stream = _enabled(config, "stream")
    dashboard = _enabled(config, "dashboard")
    if viewer.enabled and not stream.enabled and not dashboard.enabled:
        return start_viewer(
            viewer,
            run_id=taps.run_id,
            decider=taps.decider,
            max_steps=taps.max_steps,
            log=taps.log,
        )
    configs = [item for item in (stream, dashboard) if item.enabled]
    if not configs:
        raise AddonConfigError("no addon port given")
    for item in configs:
        item.validated()

    # These imports are intentionally inside the selected branches.
    from pokegent.dashboard.stream.audio import AudioHub
    from pokegent.dashboard.stream.server import StreamServer, paired_dashboard_origins
    from pokegent.dashboard.stream.server import create_app as create_stream_app

    hub = FrameHub(None, hz=stream.frame_hz)
    audio = AudioHub()
    sockets: list[Any] = []
    servers: list[Any] = []
    state = None
    stream_url = None
    dashboard_origins = paired_dashboard_origins(dashboard.port) if dashboard.enabled else ()
    try:
        if stream.enabled:
            stream_sock = bind_socket(stream)
            sockets.append(stream_sock)
            servers.append(
                StreamServer(
                    create_stream_app(hub, audio, stream, paired_origins=dashboard_origins),
                    stream_sock,
                )
            )
            stream_url = f"http://127.0.0.1:{stream.port}"
        if dashboard.enabled:
            if not stream.enabled:
                # Dashboard video/audio is still owned by stream, on an adjacent loopback port.
                if dashboard.port is None or dashboard.port >= PORT_MAX:
                    stream_url = None
                else:
                    stream = AddonConfig(
                        port=dashboard.port + 1,
                        frame_hz=dashboard.frame_hz,
                        fps=dashboard.fps,
                        linger_s=dashboard.linger_s,
                        realtime=dashboard.realtime,
                    )
                    try:
                        stream_sock = bind_socket(stream)
                    except AddonStartError:
                        taps.log("addons: dashboard stream is off (adjacent stream port is busy)")
                    else:
                        sockets.append(stream_sock)
                        servers.append(
                            StreamServer(
                                create_stream_app(
                                    hub, audio, stream, paired_origins=dashboard_origins
                                ),
                                stream_sock,
                            )
                        )
                        stream_url = f"http://127.0.0.1:{stream.port}"
            from pokegent.dashboard.server import ViewerServer
            from pokegent.dashboard.server import create_app as create_dashboard_app
            from pokegent.dashboard.state import ViewerState

            state = ViewerState(
                run_id=taps.run_id,
                decider=taps.decider,
                max_steps=taps.max_steps,
                realtime=any(item.realtime_on for item in configs),
            )
            dashboard_sock = bind_socket(dashboard)
            sockets.append(dashboard_sock)
            servers.append(
                ViewerServer(
                    create_dashboard_app(
                        state,
                        dashboard,
                        stream_url=None,
                        stream_port=stream.port if stream_url is not None else None,
                        stream_enabled=stream_url is not None,
                    ),
                    dashboard_sock,
                )
            )
        for server in servers:
            server.start()
    except Exception as exc:
        for server in servers:
            server.stop()
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass
        raise AddonStartError(str(exc)) from None

    primary = viewer if viewer.enabled else (dashboard if dashboard.enabled else stream)
    runtime = AddonRuntime(
        primary,
        state=state,
        hub=hub,
        audio=audio,
        server=servers[-1],
        servers=servers,
        stream_url=stream_url,
    )
    taps.log(
        f"addons: stream={stream.port if stream.enabled else 'off'} "
        f"dashboard={dashboard.port if dashboard.enabled else 'off'}"
    )
    return runtime


# One-release compatibility entry point for callers importing the old combined viewer. Core does
# not use this path; the new hook above always keeps stream and dashboard listeners independent.
def start_viewer(
    config: AddonConfig,
    *,
    run_id: str,
    decider: Any,
    max_steps: int,
    log: Callable[[str], None] = print,
    stream_config: AddonConfig | None = None,
    dashboard_config: AddonConfig | None = None,
) -> AddonRuntime:
    if stream_config is not None or dashboard_config is not None:
        return start_addons(
            {"stream": stream_config or config, "dashboard": dashboard_config or config},
            AddonTaps(run_id=run_id, decider=decider, max_steps=max_steps, log=log),
        )

    config.validated()

    if config.static is None:
        from pokegent.addons import default_static_dir

        candidate = default_static_dir()
        config = replace(config, static=str(candidate) if candidate is not None else "")
    from pokegent.dashboard.server import ViewerServer
    from pokegent.dashboard.server import create_app as create_dashboard_app
    from pokegent.dashboard.state import ViewerState
    from pokegent.dashboard.stream.audio import AudioHub
    from pokegent.dashboard.stream.server import create_app as create_stream_app

    state = ViewerState(
        run_id=run_id, decider=decider, max_steps=max_steps, realtime=config.realtime_on
    )
    hub = FrameHub(None, hz=config.frame_hz)
    audio = AudioHub()
    dashboard_app = create_dashboard_app(
        state,
        config,
        stream_enabled=True,
        frame_hub=hub,
    )
    from fastapi.responses import JSONResponse, Response

    @dashboard_app.get("/screenshot")
    async def compatibility_screenshot() -> Response:
        import asyncio

        shot = await asyncio.to_thread(hub.grab)
        if shot is None:
            return JSONResponse({"error": "no frame yet"}, status_code=503)
        return Response(await asyncio.to_thread(shot.png), media_type="image/png")

    dashboard_app.mount("", create_stream_app(hub, audio, config, middleware=False))
    sock = bind_socket(config)
    try:
        server = ViewerServer(dashboard_app, sock)
        server.start()
    except Exception:
        sock.close()
        raise
    log("viewer compatibility alias: dashboard and stream share one loopback listener")
    return AddonRuntime(config, state=state, hub=hub, audio=audio, server=server, servers=[server])


# One-release compatibility names for callers importing the old viewer runtime.
Viewer = AddonRuntime
ViewerConfig = AddonConfig
ViewerConfigError = AddonConfigError
ViewerStartError = AddonStartError

__all__ = [
    "AddonRuntime",
    "PORT_MAX",
    "PORT_MIN",
    "Viewer",
    "ViewerConfig",
    "ViewerConfigError",
    "ViewerStartError",
    "goal_probe",
    "load_tables",
    "start_addons",
    "start_viewer",
]
