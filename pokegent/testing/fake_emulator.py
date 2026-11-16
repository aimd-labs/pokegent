"""ROM-free GBA backend double for actor, snapshot and reader tests (WP1b/WP1c).

``FakeGBAEmulator`` honours the same contract as
:class:`pokegent.emulator.PyGBAEmulator`: closed button vocabulary with a
chord whitelist, bulk reads that fail closed outside the physical regions,
monotonic ``frame_count``, savestates as bytes, ``load_epoch``.  Its RAM is a
deterministic function of the frames and inputs it has seen, so the same input
sequence from the same state always gives the same RAM hash.

Frame model (EWRAM/IWRAM word 0 = frame number written in two separate steps,
so a reader that bypasses the actor could observe a torn pair):

* EWRAM[0:4] and IWRAM[0:4] both hold the in-RAM frame number (+1 per frame, part
  of the savestate) after every frame;
* EWRAM[4:8] counts frames during which ``a`` was held, EWRAM[8:12] ``l``,
  EWRAM[12:16] ``r``;
* EWRAM[16:20] is a small LCG advanced once per frame and per held key.

``mid_frame_hook`` (if set) runs between the EWRAM and IWRAM writes of a frame.
"""

from __future__ import annotations

import struct
import threading
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from pokegent.emulator import (
    DEFAULT_CHORD_WHITELIST,
    GBA_BUTTONS,
    GBA_EWRAM,
    GBA_IWRAM,
    ButtonSpec,
    ConcurrentAccessError,
    Emulator,
    InputRejected,
    MemoryAccessError,
    MemoryRegion,
    StateLoadError,
    normalize_buttons,
    validate_chord_whitelist,
)

_MAGIC = b"FAKE"


class FakeGBAEmulator(Emulator):
    BUTTONS: List[str] = list(GBA_BUTTONS)

    def __init__(
        self,
        *,
        chord_whitelist: Iterable[Iterable[str]] = DEFAULT_CHORD_WHITELIST,
        rom: bytes = b"\x00" * 0x1000,
    ) -> None:
        super().__init__()
        self.chord_whitelist = validate_chord_whitelist(chord_whitelist)
        self.rom_path = "/nonexistent/fake.gba"
        self.rom_image = bytes(rom)
        self.load_epoch = 0
        self.frames_run = 0
        self.mid_frame_hook: Optional[Callable[[], None]] = None
        self.frame_delay_s = 0.0
        self._ewram = bytearray(GBA_EWRAM.size)
        self._iwram = bytearray(GBA_IWRAM.size)
        self._held: FrozenSet[str] = frozenset()
        self._guard = threading.Lock()
        self._core_frame = 1000

    # -- lifecycle ----------------------------------------------------------

    def load(self, rom_path: str) -> None:
        self.rom_path = rom_path

    def close(self) -> None:
        self._held = frozenset()

    # -- input --------------------------------------------------------------

    def button_down(self, buttons: ButtonSpec) -> None:
        self._held = frozenset(
            normalize_buttons(buttons, allowed=GBA_BUTTONS, chords=self.chord_whitelist)
        )

    def button_up(self, buttons: Optional[ButtonSpec] = None) -> None:
        if buttons is None:
            self._held = frozenset()
        else:
            names = [buttons] if isinstance(buttons, str) else list(buttons)
            for b in names:
                if b not in GBA_BUTTONS:
                    raise InputRejected(f"unknown button {b!r}")
            self._held = self._held - frozenset(names)

    def release_all(self) -> None:
        self.button_up(None)

    def release_buttons(self) -> None:
        self.button_up(None)

    def pressed_keys(self) -> FrozenSet[str]:
        return self._held

    def press_buttons(self, buttons: ButtonSpec, frames: int = 2, release_frames: int = 1) -> None:
        self.button_down(buttons)
        try:
            self.tick(frames)
        finally:
            self._held = frozenset()
        self.tick(release_frames)

    def press(self, button: str, frames: int = 2, release_frames: int = 1) -> None:
        self.press_buttons(button, frames, release_frames)

    # -- timing -------------------------------------------------------------

    def tick(self, frames: int = 1) -> None:
        if not self._guard.acquire(blocking=False):
            raise ConcurrentAccessError("overlapping fake backend calls")
        try:
            for _ in range(frames):
                self._one_frame()
        finally:
            self._guard.release()

    def _one_frame(self) -> None:
        self.frame_count += 1
        self._core_frame += 1
        self.frames_run += 1
        for key, off in (("a", 4), ("l", 8), ("r", 12)):
            if key in self._held:
                v = struct.unpack_from("<I", self._ewram, off)[0]
                struct.pack_into("<I", self._ewram, off, v + 1)
        rng = struct.unpack_from("<I", self._ewram, 16)[0]
        for _ in range(1 + len(self._held)):
            rng = (rng * 1664525 + 1013904223) & 0xFFFFFFFF
        struct.pack_into("<I", self._ewram, 16, rng)
        word = (struct.unpack_from("<I", self._ewram, 0)[0] + 1) & 0xFFFFFFFF
        struct.pack_into("<I", self._ewram, 0, word)
        if self.mid_frame_hook is not None:
            self.mid_frame_hook()
        if self.frame_delay_s:
            threading.Event().wait(self.frame_delay_s)
        struct.pack_into("<I", self._iwram, 0, word)

    @property
    def core_frame(self) -> Optional[int]:
        return self._core_frame

    # -- video --------------------------------------------------------------

    def get_screen(self):
        from PIL import Image

        return Image.new("RGB", (240, 160), (self.frame_count % 256, 0, 0))

    def get_screen_png(self) -> bytes:
        import io

        buf = io.BytesIO()
        self.get_screen().save(buf, format="PNG")
        return buf.getvalue()

    # -- memory -------------------------------------------------------------

    def _regions(self) -> Tuple[Tuple[MemoryRegion, bytearray], ...]:
        return ((GBA_EWRAM, self._ewram), (GBA_IWRAM, self._iwram))

    def read_range(self, addr: int, size: int) -> bytes:
        if size < 0 or addr < 0:
            raise MemoryAccessError(f"bad range {addr}+{size}")
        for region, mem in self._regions():
            if region.contains(addr, size):
                off = addr - region.base
                return bytes(mem[off : off + size])
        if 0x08 <= (addr >> 24) <= 0x0D:
            off = addr & 0x01FFFFFF
            if off + size <= len(self.rom_image):
                return self.rom_image[off : off + size]
        raise MemoryAccessError(f"0x{addr:08X}+{size} is not readable")

    def read_u8(self, addr: int) -> int:
        return self.read_range(addr, 1)[0]

    def read_u16(self, addr: int) -> int:
        return int.from_bytes(self.read_range(addr, 2), "little")

    def read_u32(self, addr: int) -> int:
        return int.from_bytes(self.read_range(addr, 4), "little")

    def read_regions(self, regions: Sequence[MemoryRegion]) -> Dict[str, bytes]:
        return {r.name: self.read_range(r.base, r.size) for r in regions}

    # -- poke helper for tests (the real backend has no RAM writes) ------------

    def poke(self, addr: int, data: bytes) -> None:
        for region, mem in self._regions():
            if region.contains(addr, len(data)):
                off = addr - region.base
                mem[off : off + len(data)] = data
                return
        raise MemoryAccessError(f"0x{addr:08X} not pokeable")

    # -- state ----------------------------------------------------------------

    def save_state_bytes(self) -> bytes:
        return (
            _MAGIC
            + struct.pack("<IQ", self._core_frame, self.frame_count)
            + bytes(self._ewram)
            + bytes(self._iwram)
        )

    def load_state_bytes(self, data: bytes, refresh_video: bool = True) -> None:
        want = 4 + 12 + GBA_EWRAM.size + GBA_IWRAM.size
        if len(data) != want or data[:4] != _MAGIC:
            raise StateLoadError("not a fake savestate")
        self._core_frame = struct.unpack_from("<I", data, 4)[0]
        # frame_count stays monotonic: it counts frames this instance emulated.
        body = data[16:]
        self._ewram[:] = body[: GBA_EWRAM.size]
        self._iwram[:] = body[GBA_EWRAM.size :]
        self._held = frozenset()
        self.load_epoch += 1

    def save_state(self, path: str) -> None:
        from pokegent.emulator import write_file_atomic

        write_file_atomic(path, self.save_state_bytes())

    def load_state(self, path: str) -> None:
        with open(path, "rb") as fh:
            self.load_state_bytes(fh.read())
