"""Non-blocking PCM capture for the local stream addon.

The emulator calls ``push`` on its owning thread.  A bounded deque makes the
producer independent of WebSocket clients: old chunks are dropped when a
client cannot keep up and no network operation runs in the game loop.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

AUDIO_SAMPLE_RATE = 32_768
AUDIO_CHANNELS = 2
PCM_FORMAT = "s16le"
MAX_CHUNKS = 96


@dataclass(frozen=True)
class AudioChunk:
    seq: int
    captured_at: float
    pcm: bytes


class AudioHub:
    """A bounded, latest-first audio ring buffer shared by stream clients."""

    sample_rate = AUDIO_SAMPLE_RATE
    channels = AUDIO_CHANNELS
    pcm_format = PCM_FORMAT

    def __init__(self, *, max_chunks: int = MAX_CHUNKS) -> None:
        self._chunks: deque[AudioChunk] = deque(maxlen=max_chunks)
        self._lock = threading.Lock()
        self._seq = 0
        self.dropped = 0
        self.pushed = 0

    def push(self, pcm: bytes) -> None:
        """Append one PCM block without waiting for a consumer."""
        if not pcm:
            return
        if not self._lock.acquire(blocking=False):
            self.dropped += 1
            return
        try:
            if len(self._chunks) == self._chunks.maxlen:
                self.dropped += 1
            self._seq += 1
            self._chunks.append(AudioChunk(self._seq, time.monotonic(), bytes(pcm)))
            self.pushed += 1
        finally:
            self._lock.release()

    def after(self, seq: int) -> list[AudioChunk]:
        with self._lock:
            return [chunk for chunk in self._chunks if chunk.seq > seq]

    def latest(self) -> AudioChunk | None:
        with self._lock:
            return self._chunks[-1] if self._chunks else None

    def stats(self) -> dict[str, int | str]:
        with self._lock:
            return {
                "sample_rate": self.sample_rate,
                "channels": self.channels,
                "pcm_format": self.pcm_format,
                "chunks": len(self._chunks),
                "pushed": self.pushed,
                "dropped": self.dropped,
            }
