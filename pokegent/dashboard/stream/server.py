"""Loopback-only game picture and sound server owned by the stream addon."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from addons.config import AddonConfig
from pokegent.dashboard.security import loopback_host_allowed
from pokegent.dashboard.stream.frames import MAX_SCALE, FrameHub

from .audio import AudioHub

MAX_WS_CLIENTS = 8
_NO_STORE = {"Cache-Control": "no-store", "Cross-Origin-Resource-Policy": "same-origin"}
_SECURITY = ((b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"))
_FETCH_SAME_ORIGIN = frozenset({"same-origin", "none"})


def paired_dashboard_origins(port: int | None) -> frozenset[str]:
    """Return the only cross-port browser origins paired with a dashboard port."""
    if port is None:
        return frozenset()
    return frozenset(
        {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
            f"http://[::1]:{port}",
        }
    )


def _origin_allowed(headers: Any, paired_origins: frozenset[str]) -> bool:
    origin = headers.get("origin")
    fetch_site = headers.get("sec-fetch-site")
    if origin is None:
        return fetch_site is None or fetch_site in _FETCH_SAME_ORIGIN
    if origin in paired_origins:
        return True
    if fetch_site in _FETCH_SAME_ORIGIN:
        return urlsplit(origin).netloc.lower() == (headers.get("host") or "").lower()
    if fetch_site is None:
        return urlsplit(origin).netloc.lower() == (headers.get("host") or "").lower()
    return False


def _media_headers(headers: Any, paired_origins: frozenset[str]) -> dict[str, str] | None:
    """Headers for a frame response, or ``None`` for a foreign browser origin."""
    if not _origin_allowed(headers, paired_origins):
        return None
    origin = headers.get("origin")
    if origin in paired_origins:
        return {
            **_NO_STORE,
            "Cross-Origin-Resource-Policy": "same-site",
            "Access-Control-Allow-Origin": origin,
            "Vary": "Origin",
        }
    return dict(_NO_STORE)


def cross_site(headers: Any, paired_origins: frozenset[str] = frozenset()) -> bool:
    return not _origin_allowed(headers, paired_origins)


class ReadOnlyMiddleware:
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
            body = b'{"error":"this stream is read-only: GET only"}'
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


def create_app(
    hub: FrameHub,
    audio: AudioHub,
    config: AddonConfig,
    *,
    middleware: bool = True,
    paired_origins: Iterable[str] = (),
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, title="pokegent stream")
    if middleware:
        app.add_middleware(ReadOnlyMiddleware)
    clients = {"n": 0}
    frame_period = 1.0 / config.frame_hz
    allowed_origins = frozenset(paired_origins)

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {"ok": True, "emulator_ready": hub.stats()["attached"]}, headers=_NO_STORE
        )

    @app.get("/frame.png")
    async def frame_png(request: Request, scale: int = Query(1, ge=1, le=MAX_SCALE)) -> Response:
        media_headers = _media_headers(request.headers, allowed_origins)
        if media_headers is None:
            return JSONResponse({"error": "origin not allowed"}, status_code=403, headers=_NO_STORE)
        shot = await asyncio.to_thread(hub.grab)
        if shot is None:
            return JSONResponse({"error": "no frame yet"}, status_code=503, headers=media_headers)
        try:
            png = await asyncio.to_thread(shot.png, scale)
        except Exception as exc:  # noqa: BLE001 - the stream must return JSON on encode failure
            return JSONResponse(
                {"error": f"PNG encoding failed: {type(exc).__name__}"},
                status_code=500,
                headers=media_headers,
            )
        return Response(
            png,
            media_type="image/png",
            headers={
                **media_headers,
                "X-Frame-Seq": str(shot.seq),
                "X-Emulator-Frame": str(shot.emu_frame),
            },
        )

    @app.websocket("/ws/frames")
    async def ws_frames(ws: WebSocket) -> None:
        if cross_site(ws.headers, allowed_origins):
            await ws.close(code=1008)
            return
        if clients["n"] >= MAX_WS_CLIENTS:
            await ws.close(code=1013)
            return
        await ws.accept()
        clients["n"] += 1
        last = 0
        try:
            while True:
                hub.touch()
                frame = hub.latest() or await asyncio.to_thread(hub.grab)
                if frame is not None and frame.seq != last:
                    await ws.send_bytes(await asyncio.to_thread(frame.ws_message))
                    last = frame.seq
                try:
                    message = await asyncio.wait_for(ws.receive(), timeout=frame_period / 2)
                except asyncio.TimeoutError:
                    continue
                if message["type"] == "websocket.disconnect":
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            clients["n"] -= 1

    @app.websocket("/ws/audio")
    async def ws_audio(ws: WebSocket) -> None:
        if cross_site(ws.headers, allowed_origins):
            await ws.close(code=1008)
            return
        await ws.accept()
        seen = 0
        try:
            while True:
                chunks = audio.after(seen)
                if chunks:
                    for chunk in chunks:
                        await ws.send_bytes(chunk.pcm)
                        seen = chunk.seq
                elif (latest := audio.latest()) is not None and seen == 0:
                    await ws.send_bytes(latest.pcm)
                    seen = latest.seq
                await asyncio.sleep(0.05)
                try:
                    message = await asyncio.wait_for(ws.receive(), timeout=0.01)
                except asyncio.TimeoutError:
                    continue
                if message["type"] == "websocket.disconnect":
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass

    return app


class StreamServer:
    """Run a stream FastAPI app on a pre-bound loopback socket."""

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
        self._thread = threading.Thread(target=self._run, name="pokegent-stream-http", daemon=True)
        self.error: BaseException | None = None

    def _run(self) -> None:
        try:
            self._server.run(sockets=[self._sock])
        except BaseException as exc:  # noqa: BLE001
            self.error = exc

    def start(self, timeout: float = 10.0) -> None:
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not self._server.started:
            if self.error is not None or not self._thread.is_alive():
                raise RuntimeError(f"the stream server did not start: {self.error!r}")
            if time.monotonic() > deadline:
                raise RuntimeError("the stream server did not start in time")
            time.sleep(0.02)

    def stop(self, timeout: float = 5.0) -> None:
        self._server.should_exit = True
        self._thread.join(timeout)
        try:
            self._sock.close()
        except OSError:
            pass
