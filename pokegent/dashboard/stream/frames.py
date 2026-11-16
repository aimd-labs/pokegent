"""Game pixels for the local stream addon: throttled, demand-driven reads.

The framebuffer is emulator state, so it is read the way every read is: through the actor
(:meth:`~pokegent.emulator.EmulatorActor.read_screen`), which runs it on the emulator
thread between two frames. Nothing here touches the backend or another thread's data directly.

Cost control (the viewer must not slow the run measurably):

* nothing is read until a client asks (``touch``), and it stops again 5 s after the last client;
* at most ``hz`` reads per second, however many clients there are (the HTTP handlers share the
  hub's latest frame);
* no read at all while the emulator has not run a frame since the last one (a run waiting for the
  model shows a still picture for free);
* encodings (PNG, RGB565 + zlib for the WebSocket) are made lazily, once per frame, off the
  emulator thread.
"""

from __future__ import annotations

import io
import struct
import threading
import time
import zlib
from collections.abc import Callable
from typing import Any, Protocol

from PIL import Image, ImageChops

WIDTH, HEIGHT = 240, 160
MAX_SCALE = 8
_HI_R = [x & 0xF8 for x in range(256)]
_HI_G = [x >> 5 for x in range(256)]
_LO_G = [(x & 0x1C) << 3 for x in range(256)]
_LO_B = [x >> 3 for x in range(256)]


class ScreenSource(Protocol):
    def read_screen(self) -> Any: ...

    @property
    def frames_run(self) -> int: ...


def rgb565(image: Image.Image) -> bytes:
    """240x160 little-endian RGB565 (R in the high bits), the format ``/ws/frames`` carries."""
    r, g, b = image.convert("RGB").split()
    hi = ImageChops.add(r.point(_HI_R), g.point(_HI_G))
    lo = ImageChops.add(g.point(_LO_G), b.point(_LO_B))
    return Image.merge("LA", (lo, hi)).tobytes()


class Frame:
    """One framebuffer copy and its lazily made encodings."""

    def __init__(self, seq: int, emu_frame: int, ts_us: int, taken_at: float, image: Any) -> None:
        self.seq = seq
        self.emu_frame = emu_frame
        self.ts_us = ts_us
        self.taken_at = taken_at
        self.image = image
        self._cache: dict[Any, bytes] = {}
        self._lock = threading.Lock()

    def _memo(self, key: Any, make: Callable[[], bytes]) -> bytes:
        with self._lock:
            hit = self._cache.get(key)
        if hit is None:
            hit = make()
            with self._lock:
                self._cache[key] = hit
        return hit

    def png(self, scale: int = 1) -> bytes:
        scale = max(1, min(MAX_SCALE, int(scale)))

        def make() -> bytes:
            image = self.image
            if scale > 1:
                image = image.resize((WIDTH * scale, HEIGHT * scale), Image.NEAREST)
            buf = io.BytesIO()
            image.save(buf, format="PNG", compress_level=1)
            return buf.getvalue()

        return self._memo(("png", scale), make)

    def ws_message(self) -> bytes:
        """``u32 seq | u64 timestamp_us | zlib(level 1) of 240*160 RGB565``, all little-endian."""

        def make() -> bytes:
            body = zlib.compress(rgb565(self.image), 1)
            return struct.pack("<IQ", self.seq & 0xFFFFFFFF, self.ts_us) + body

        return self._memo("ws", make)


class FrameHub:
    def __init__(
        self,
        source: ScreenSource | None = None,
        *,
        hz: float = 30.0,
        idle_after_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._source = source
        self._period = 1.0 / hz
        self._idle_after = idle_after_s
        self._clock = clock
        self._t0 = clock()
        self._lock = threading.Lock()
        self._grab_lock = threading.Lock()
        self._latest: Frame | None = None
        self._seq = 0
        self._last_ticks: int | None = None
        self._touched = -1e9
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.captures = 0
        self.skipped_unchanged = 0
        self.errors = 0
        self.last_error: str | None = None
        self.capture_ms_total = 0.0
        self.capture_ms_last = 0.0

    # -- lifecycle ------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None and self._source is not None:
            self._thread = threading.Thread(
                target=self._run, name="pokegent-viewer-frames", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(2.0)

    def attach(self, source: ScreenSource) -> None:
        """Give the hub the (read-only) emulator view to read from, and start reading on demand."""
        self._source = source
        self.start()

    def detach(self) -> None:
        """The emulator is going away: keep serving the last frame, read nothing more."""
        self._source = None

    # -- clients --------------------------------------------------------------------------------

    def touch(self) -> None:
        self._touched = self._clock()

    def latest(self) -> Frame | None:
        with self._lock:
            return self._latest

    def grab(self, max_age: float | None = None) -> Frame | None:
        """The newest frame: the hub's if it is fresh enough, else one read now (rate-limited by
        ``max_age``, default one period). Blocks on the emulator thread: call it off the event
        loop."""
        self.touch()
        max_age = self._period if max_age is None else max_age
        with self._grab_lock:
            frame = self.latest()
            if frame is not None and self._clock() - frame.taken_at <= max_age:
                return frame
            return self._capture() or self.latest()

    # -- the reading ----------------------------------------------------------------------------

    def _run(self) -> None:
        due = self._clock()
        while not self._stop.is_set():
            due += self._period  # a fixed cadence: the time a read takes does not stretch it
            delay = due - self._clock()
            if delay > 0:
                if self._stop.wait(delay):
                    return
            else:
                due = self._clock()  # behind (a slow read): carry on from now, no burst
            if self._source is None or self._clock() - self._touched > self._idle_after:
                continue
            with self._grab_lock:
                self._capture()

    def _capture(self) -> Frame | None:
        """One read if the emulator advanced since the last one. Caller holds ``_grab_lock``."""
        source = self._source
        if source is None:
            return None
        ticks = source.frames_run
        if self._latest is not None and ticks == self._last_ticks:
            self.skipped_unchanged += 1
            return None
        started = time.perf_counter()
        try:
            shot = source.read_screen()
        except Exception as exc:  # noqa: BLE001 - a closed or busy emulator: keep the last frame
            self.errors += 1
            self.last_error = type(exc).__name__
            return None
        spent = (time.perf_counter() - started) * 1000.0
        self.capture_ms_last = spent
        self.capture_ms_total += spent
        self.captures += 1
        self._last_ticks = ticks
        now = self._clock()
        self._seq += 1
        frame = Frame(self._seq, shot.frame, int((now - self._t0) * 1_000_000), now, shot.image)
        with self._lock:
            self._latest = frame
        return frame

    def stats(self) -> dict[str, Any]:
        latest = self.latest()
        return {
            "captures": self.captures,
            "skipped_unchanged": self.skipped_unchanged,
            "errors": self.errors,
            "last_error": self.last_error,
            "capture_ms_last": round(self.capture_ms_last, 2),
            "capture_ms_mean": round(self.capture_ms_total / self.captures, 2)
            if self.captures
            else None,
            "seq": latest.seq if latest else None,
            "emulator_frame": latest.emu_frame if latest else None,
            "attached": self._source is not None,
        }
