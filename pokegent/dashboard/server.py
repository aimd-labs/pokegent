"""The viewer's HTTP server: read-only, GET only, no CORS, in a background thread.

Routes (all GET; anything else is ``405``). Two surfaces, two audiences (``docs/addons.md``):

PUBLIC, what a public dashboard may read, safe for local loopback clients
``/telemetry``       the document of ``public.py``, ETag + ``304``
``/ws``              a nudge socket: a message whenever something new happened (poll hint)
``/agent/live``     the idle payload the dashboard polls (no agent here)
``/health``          ``{ok, emulator_ready}``; ``/favicon.ico``: 204
``/dashboard/...``   the files of ``--viewer-static`` (default: the addon dashboard), read-only

OPERATOR, for the person who runs it; not served with ``public_only``
``/``                our page: every decision, the model's view, events
``/ops/telemetry``   the full document (``telemetry.py``)
``/ops/decisions``   the last decisions with options and probabilities and the
                     instruction text and state the model was sent
``/ops/events``      the last events (whitelisted fields; ``?after=<seq>``)
``/ops/health``      the viewer's own cost counters

Nothing here can write: there is no route that takes a body, the app holds no emulator, runner or
actor handle (only the :class:`ViewerState` and the :class:`FrameHub`), and the middleware refuses
every method but GET and HEAD. There is no CORS middleware, so browsers keep cross-origin pages
from reading the answers; the WebSockets refuse cross-site handshakes.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import threading
import time
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from addons.config import AddonConfig, default_static_dir
from pokegent.dashboard.public import public_telemetry
from pokegent.dashboard.security import loopback_host_allowed
from pokegent.dashboard.state import ViewerState
from pokegent.dashboard.telemetry import build_telemetry, etag_of

VIEWER_VERSION = 1
_PAGE = resources.files("pokegent.dashboard").joinpath("fallback.html")
_NO_STORE = {"Cache-Control": "no-store", "Cross-Origin-Resource-Policy": "same-origin"}
_SECURITY = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
)


class ReadOnlyMiddleware:
    """GET and HEAD only; adds ``nosniff`` / ``no-referrer`` to every response."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in {"http", "websocket"} and not loopback_host_allowed(
            scope.get("headers", [])
        ):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            body = b'{"error":"loopback Host required"}'
            await send({
                "type": "http.response.start", "status": 403,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()), *_SECURITY],
            })
            await send({"type": "http.response.body", "body": body})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["method"] not in ("GET", "HEAD"):
            body = b'{"error":"this viewer is read-only: GET only"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 405,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"allow", b"GET, HEAD"),
                        *_SECURITY,
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        async def guarded_send(message: Any) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *_SECURITY]}
            await send(message)

        await self.app(scope, receive, guarded_send)


def idle_agent_live(now: float) -> dict[str, Any]:
    """The ``GET /agent/live`` document of a run without an agent, in the shape ``agentlive.js``
    reads: no ``turn`` (the panel stays hidden), no ``chunks``, ``last_seq`` 0, the server clock
    in ``now`` and a ``driver`` with no research (``agent_head``) and no plan (``agent_play``)."""
    return {
        "turn": None,
        "chunks": [],
        "last_seq": 0,
        "now": now,
        "driver": {"agent_head": None, "agent_play": None},
    }


def cross_site(headers: Any) -> bool:
    """True for a browser handshake that comes from another site's page."""
    fetch_site = headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site not in ("same-origin", "none")
    origin = headers.get("origin")
    if origin is None:
        return False  # not a browser (curl, a script): no page to protect against
    return urlsplit(origin).netloc.lower() != (headers.get("host") or "").lower()


def _loopback_host(request: Request) -> str | None:
    """Return the browser-visible loopback hostname from a request Host header."""
    try:
        hostname = urlsplit(f"//{request.headers.get('host', '')}").hostname
    except ValueError:
        return None
    if hostname in {"127.0.0.1", "localhost", "::1"}:
        return hostname
    return None


def _origin_for_request(request: Request, stream_port: int | None) -> str | None:
    if stream_port is None:
        return None
    hostname = _loopback_host(request)
    if hostname is None:
        return None
    host = f"[{hostname}]" if ":" in hostname else hostname
    return f"http://{host}:{stream_port}"


def create_app(
    state: ViewerState,
    config_or_hub: AddonConfig | Any,
    maybe_config: AddonConfig | None = None,
    audio: Any = None,
    *,
    stream_url: str | None = None,
    stream_port: int | None = None,
    stream_enabled: bool | None = None,
    frame_hub: Any = None,
) -> FastAPI:
    # ``hub`` used to be the second argument; accept it for one release while the listener itself
    # remains dashboard-only. The old hub/audio values are deliberately ignored.
    config = maybe_config if maybe_config is not None else config_or_hub
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, title="pokegent viewer")
    app.add_middleware(ReadOnlyMiddleware)
    static_dir = config.static_dir() if config.static is not None else default_static_dir()

    def operator_document() -> dict[str, Any]:
        doc = build_telemetry(state.view())
        doc["viewer"] = {
            "version": VIEWER_VERSION,
            "read_only": True,
            "public_only": config.public_only,
            "dashboard": "/dashboard/" if static_dir else None,
        }
        return doc

    def json_response(request: Request, doc: dict[str, Any]) -> Response:
        etag = etag_of(doc)
        headers = {**_NO_STORE, "ETag": etag}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        body = json.dumps(doc, default=str, allow_nan=False, separators=(",", ":"))
        return Response(body, media_type="application/json", headers=headers)

    # -- the PUBLIC surface: what the stream dashboard reads, safe to show to anybody -------------

    @app.get("/telemetry")
    async def telemetry(request: Request) -> Response:
        return json_response(request, public_telemetry(build_telemetry(state.view())))

    @app.get("/health")
    async def health() -> Response:
        return JSONResponse(
            {"ok": True, "emulator_ready": state.view()["ready"]}, headers=_NO_STORE
        )

    @app.get("/stream-info")
    async def stream_info(request: Request) -> Response:
        """Tell the dashboard page where its separately-owned picture/sound stream lives."""
        url = stream_url or _origin_for_request(request, stream_port)
        if stream_port is not None and url is None:
            return JSONResponse({"error": "dashboard Host must be loopback"}, status_code=400)
        return JSONResponse(
            {
                "enabled": stream_enabled if stream_enabled is not None else url is not None,
                "url": url,
            },
            headers=_NO_STORE,
        )

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return Response(status_code=204, headers=_NO_STORE)  # keep a browser console clean

    @app.get("/agent/live")
    async def agent_live(after: str | None = None) -> Response:
        # agentlive.js polls ``?after=<seq>``; there are no agent turns here, so every answer is
        # the idle document, whatever the query says (never an error, never HTML)
        return JSONResponse(idle_agent_live(time.time()), headers=_NO_STORE)

    @app.websocket("/ws")
    async def ws_events(ws: WebSocket) -> None:
        if cross_site(ws.headers):
            await ws.close(code=1008)
            return
        await ws.accept()
        seen: tuple[Any, ...] | None = None
        try:
            while True:
                view = state.view()
                mark = (
                    view["stats"]["decisions"],
                    len(view["highlights"]),
                    view["scene"].get("id"),
                    view["run"]["status"],
                )
                if mark != seen:
                    seen = mark
                    await ws.send_text('{"type":"event"}')
                try:
                    message = await asyncio.wait_for(ws.receive(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                if message["type"] == "websocket.disconnect":
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass

    # -- the OPERATOR surface: every decision, the model's view, events (not with public_only) -----

    if not config.public_only:

        @app.get("/", response_class=HTMLResponse)
        async def index(request: Request) -> Response:
            nonce = secrets.token_urlsafe(16)
            html = _PAGE.read_text(encoding="utf-8").replace("__NONCE__", nonce)
            page_stream_url = stream_url or _origin_for_request(request, stream_port)
            stream_url_b64 = base64.b64encode((page_stream_url or "").encode("ascii")).decode(
                "ascii"
            )
            html = html.replace("__STREAM_URL_B64__", stream_url_b64)
            stream_origin = (
                urlsplit(page_stream_url).scheme + "://" + urlsplit(page_stream_url).netloc
                if page_stream_url
                else ""
            )
            ws_origin = stream_origin.replace("http://", "ws://") if stream_origin else ""
            csp_origins = " ".join(origin for origin in (stream_origin, ws_origin) if origin)
            csp = (
                f"default-src 'none'; img-src 'self' blob: {csp_origins}; "
                f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; "
                f"connect-src 'self' {csp_origins}; base-uri 'none'; "
                "form-action 'none'; frame-ancestors 'none'"
            )
            return HTMLResponse(html, headers={**_NO_STORE, "Content-Security-Policy": csp})

        @app.get("/ops/telemetry")
        async def ops_telemetry(request: Request) -> Response:
            return json_response(request, operator_document())

        @app.get("/ops/decisions")
        async def ops_decisions(request: Request) -> Response:
            view = state.view()
            doc = {
                "generated_at": view["now"],
                "decisions": list(reversed(view["decisions"])),
                "pending": view.get("pending"),
            }
            return json_response(request, doc)

        @app.get("/ops/events")
        async def ops_events(
            after: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=300)
        ) -> Response:
            events = [e for e in state.view()["events"] if e["seq"] > after][-limit:]
            body = {"events": events, "last_seq": events[-1]["seq"] if events else after}
            return JSONResponse(body, headers=_NO_STORE)

        @app.get("/ops/health")
        async def ops_health() -> Response:
            view = state.view()
            body = {
                "ok": True,
                "emulator_ready": view["ready"],
                "status": view["run"]["status"],
            }
            if frame_hub is not None:
                body["frames"] = frame_hub.stats()
            return JSONResponse(body, headers=_NO_STORE)

    if static_dir is not None:

        @app.get("/dashboard", include_in_schema=False)
        async def dashboard_redirect() -> Response:
            return RedirectResponse("/dashboard/")

        app.mount("/dashboard", StaticFiles(directory=str(static_dir), html=True), name="dashboard")
    return app


class ViewerServer:
    """uvicorn in a daemon thread on a socket that is already bound (``bind_socket``)."""

    def __init__(self, app: FastAPI, sock: Any) -> None:
        self._sock = sock
        self._server = uvicorn.Server(
            uvicorn.Config(
                app,
                log_level="warning",
                access_log=False,
                lifespan="off",
                server_header=False,
                timeout_keep_alive=5,
                timeout_graceful_shutdown=1,
                ws_ping_interval=20.0,
            )
        )
        self._thread = threading.Thread(target=self._run, name="pokegent-viewer-http", daemon=True)
        self.error: BaseException | None = None

    def _run(self) -> None:
        try:
            self._server.run(sockets=[self._sock])
        except BaseException as exc:  # noqa: BLE001 - reported by start(), never into the run
            self.error = exc

    def start(self, timeout: float = 10.0) -> None:
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not self._server.started:
            if self.error is not None or not self._thread.is_alive():
                raise RuntimeError(f"the viewer server did not start: {self.error!r}")
            if time.monotonic() > deadline:
                raise RuntimeError("the viewer server did not start in time")
            time.sleep(0.02)

    def stop(self, timeout: float = 5.0) -> None:
        self._server.should_exit = True
        self._thread.join(timeout)
        try:
            self._sock.close()
        except OSError:
            pass
