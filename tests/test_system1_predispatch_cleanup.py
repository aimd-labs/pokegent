"""Pre-dispatch expiry and prepared-loopback-TCP owned-response regressions."""

import asyncio
import http.client
import socket
import threading
import time

import pytest

from addons.system_one import http_provider as transport
from addons.system_one.factory import build_provider
from addons.system_one.policy import SystemOneConfig
from addons.system_one.provider import ProviderTimeout

HTTPConnection = http.client.HTTPConnection  # captured before the per-test seam patch
HTTPResponse = http.client.HTTPResponse
HEADER = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
BODY = (b'{"status":"okay","model":"actual",'
        b'"answers":{"action":{"type":"choice","choice":"red"}}}')
WIRE = HEADER + f"{len(BODY):x}\r\n".encode() + BODY + b"\r\n0\r\n\r\n"


def make_provider():
    return build_provider(SystemOneConfig(
        provider_url="http://127.0.0.1:1", timeout_seconds=.12, connect_timeout_seconds=.12,
        read_timeout_seconds=.11, retry={"max_attempts": 1}))


def drained(client):
    deadline = time.monotonic() + .3
    while client._active and time.monotonic() < deadline:
        time.sleep(.005)
    assert not client._active


class Response(HTTPResponse):
    peer = None  # Set to the owning Peer immediately before each prepared connection.

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.peer.responses.append(self)
        self.peer.reader_threads.append(threading.get_ident())

    def begin(self):
        if self.peer.phase == "headers":
            self.peer.entered.set()
        return super().begin()

    def read(self, amt=None):
        if self.peer.phase == "chunked":
            self.peer.entered.set()
        return super().read(amt)

    def close(self):
        self.peer.close_threads.append(threading.get_ident())
        return super().close()


class Connection(HTTPConnection):
    peer = None
    response_class = Response

    def connect(self):
        assert self.sock is self.peer.left
        self.peer.start.set()


class Peer:
    """Pre-connected real TCP loopback peer; response bytes are parsed, not mocked."""

    def __init__(self, phase, healthy=False):
        self.phase, self.worker, self.connection = phase, None, None
        self.closed, self.entered, self.start, self.stop = (threading.Event() for _ in range(4))
        self.responses, self.close_threads, self.reader_threads, self.errors = [], [], [], []
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.left = self.right = None
        try:
            self.listener.settimeout(.5)
            self.listener.bind(("127.0.0.1", 0))
            self.listener.listen(1)
            self.left = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.left.settimeout(.5)
            self.left.connect(self.listener.getsockname())
            self.right, _ = self.listener.accept()
            self.right.settimeout(.5)
            if healthy:
                self.right.sendall(WIRE)
            else:
                self.right.sendall(HEADER[:1] if phase == "headers" else HEADER + b"1\r\nx\r\n")
                self.worker = threading.Thread(target=self._dribble, daemon=True)
                self.worker.start()
        except BaseException:
            self.close_sockets()
            raise
        finally:
            self.listener.close()

    def close_sockets(self):
        self.stop.set()
        for sock in (self.left, self.right):
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()

    def _dribble(self):
        try:
            if not self.start.wait(.5):
                self.errors.append("peer never activated")
                return
            units = ([bytes([b]) for b in HEADER[1:]] if self.phase == "headers"
                     else [b"1\r\nx\r\n"] * 100)
            for unit in units:
                if self.stop.wait(.01):
                    return
                self.right.sendall(unit)
        except (BrokenPipeError, ConnectionResetError):
            self.closed.set()
        except OSError as error:
            self.errors.append(type(error).__name__)

    def factory(self, host, port, timeout):
        assert (host, port) == ("127.0.0.1", 1) and 0 < timeout <= .12
        assert self.connection is None, "unexpected extra attempt"
        Response.peer = Connection.peer = self
        conn = Connection(host, port, timeout=timeout)
        conn.sock = self.left
        self.connection = conn
        return conn

    def dispose(self):
        self.close_sockets()
        if self.worker is not None:
            self.worker.join(.3)
            assert not self.worker.is_alive()


def test_predispatch_expiry_has_no_response_owner(monkeypatch):
    provider = make_provider()
    entered, expired_seen = threading.Event(), threading.Event()
    calls = {"request": 0, "response": 0}
    connections = []

    class Observed(HTTPConnection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            connections.append(self)

        def request(self, *args, **kwargs):
            calls["request"] += 1
            return super().request(*args, **kwargs)

        def getresponse(self):
            calls["response"] += 1
            return super().getresponse()

    def stall(deadline, expired):
        entered.set()
        assert expired.wait(.3), "original request Timer must expire"
        expired_seen.set()
        raise TimeoutError("controlled pre-dispatch expiry")

    provider.client._resolve = stall
    monkeypatch.setattr(transport.http.client, "HTTPConnection", Observed)
    recovery = None
    try:
        started = time.monotonic()
        assert provider.client.fetch_health(timeout_s=.12) is None
        assert time.monotonic() - started < .5
        assert entered.is_set() and expired_seen.is_set()
        assert calls == {"request": 0, "response": 0}
        assert len(connections) == 1 and connections[0].sock is None
        drained(provider.client)
        recovery = Peer("chunked", healthy=True)
        monkeypatch.setattr(transport.http.client, "HTTPConnection", recovery.factory)
        assert provider.client.fetch_health(timeout_s=.12) == {"status": "okay"}
    finally:
        provider.close()
        if recovery is not None:
            recovery.dispose()


@pytest.mark.parametrize("method", ["health", "decision", "cancel"])
@pytest.mark.parametrize("phase", ["headers", "chunked"])
def test_prepared_loopback_peer_cleanup(monkeypatch, method, phase):
    provider = make_provider()
    aborts, cancelling = [], [False]
    original_abort = provider.client._abort_connection

    def observe(conn):
        aborts.append((isinstance(threading.current_thread(), threading.Timer), cancelling[0]))
        original_abort(conn)

    provider.client._abort_connection = observe
    peer = recovery = None
    try:
        peer = Peer(phase)
        recovery = Peer("chunked", healthy=True)
        peers = iter((peer, recovery))
        monkeypatch.setattr(transport.http.client, "HTTPConnection",
                            lambda *args, **kwargs: next(peers).factory(*args, **kwargs))
        started = time.monotonic()
        if method == "health":
            assert provider.client.fetch_health(timeout_s=.12) is None
        elif method == "decision":
            with pytest.raises(ProviderTimeout):
                asyncio.run(provider.probe())
        else:
            async def cancel():
                task = asyncio.create_task(provider.probe())
                gate = time.monotonic() + .3
                while not peer.entered.is_set() and not task.done():
                    assert time.monotonic() < gate, "owned phase not reached"
                    await asyncio.sleep(.001)
                assert peer.entered.is_set() and not task.done()
                assert not aborts, "deadline abort preceded cancellation"
                cancelling[0] = True
                provider.cancel()
                assert aborts, "cancel must synchronously abort the owned read"
                with pytest.raises(ProviderTimeout):
                    await task
            asyncio.run(cancel())
        assert time.monotonic() - started < .5
        assert peer.entered.is_set(), "intended parser/read phase was not reached"
        assert aborts, "no real abort before cleanup"
        if method == "health":
            assert any(timer for timer, _ in aborts), "health request Timer did not abort"
        if method == "cancel":
            assert any(after for _, after in aborts), "cancel did not abort the owned read"
        assert peer.closed.wait(.5)  # Unchanged unconditional peer-error budget.
        drained(provider.client)
        assert peer.responses and all(r.closed and r.fp is None for r in peer.responses)
        assert peer.close_threads and set(peer.close_threads) <= set(peer.reader_threads)
        assert peer.left.fileno() == -1
        assert not peer.errors
        if method != "cancel":
            assert asyncio.run(provider.probe()).option_id == "red"
        else:
            with pytest.raises(ProviderTimeout):
                asyncio.run(provider.probe())
            assert recovery.connection is None
    finally:
        workers = tuple(provider._pool._threads) if provider._pool else ()
        provider.close()
        for item in (peer, recovery):
            if item is not None:
                item.dispose()
        for worker in workers:
            worker.join(.3)
            assert not worker.is_alive()
