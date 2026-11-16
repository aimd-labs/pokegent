"""Single-writer emulator actor: the only door to a GBA backend (spec 0.2, 4.1, 11.1).

``EmulatorActor`` owns a backend through an :class:`EmuClock` thread. All access,
writes and reads alike, runs on that one thread at a frame boundary, so

* input, tick, savestate load/save and memory reads are serialized (spec 4.1:
  all concurrent calls run through one actor);
* :meth:`EmulatorActor.read_atomic_snapshot` returns EWRAM, IWRAM and the frame
  counters taken in one step between two frames: coherent for one decision time;
* read-only clients get :meth:`EmulatorActor.reader`, a view without any writer;
* every wait is bounded in frames *and* in wall-clock seconds, and the two
  clocks are independent (a tick can be faster than real time, spec 11.1).

Inputs are validated against the backend's button list and chord whitelist
before they are queued; nothing a model produced is ever passed through as a
key name or a duration. The actor never writes game RAM and never opens the
ROM for writing.

Dedicated emulator thread with frame-counted, strictly FIFO commands.

Provenance: adapted from the earlier AIMD Labs runtime (MIT fork of
NousResearch/pokemon-agent). The emulation clock was authored by AIMD Labs agents.
Kept: one thread owns the
emulator, commands are frame-counted and run FIFO to completion at frame
boundaries, an optional absolute-deadline pacing loop that re-anchors instead of
bursting, a per-command frame watchdog, keys are always released when a command
ends or dies, ``stop()`` reports whether the thread really exited.
Not ported: the shared-memory frame ring, the walk macro and the asyncio/HTTP
helpers (dashboard and policy concerns, not part of the spec contract).

What is new for the spec (4.1, 11.1, 0.2 "one writer"):

* **Unpaced by default.**  With ``fps=None`` the thread runs frames only while a
  command needs them, as fast as the core goes (tick can be faster than real
  time).  ``fps=60`` re-creates the live real-time clock.  Frame budgets and wall
  deadlines are separate things: commands carry a *frame* budget, the caller's
  ``future.result(timeout=...)`` and the ``until`` deadline are *wall* limits.
* **Paced without idle frames** (``fps=60, idle_frames=False``, the viewer's
  ``--realtime``): frames still run only while a command needs them, so the game
  advances exactly as in an unpaced run (same frames, same state, replayable); only
  the wall time per frame is stretched to ``1/fps`` so a human can follow.  The
  clock re-anchors when it wakes from idle (no burst, no spurious resync).  With
  ``idle_frames=True`` (the default, the live system's behaviour) a paced clock
  keeps ticking between commands.
* **Read calls** (:meth:`EmuClock.submit_read`) are served by the same thread at
  the next frame boundary, ahead of queued writer commands.  Every emulator
  access therefore happens on one thread between two frames; a reader can never
  see a half-run frame and never needs a lock the emulator thread holds.
* **``until`` commands** evaluate a probe after every ``check_every`` frames and
  stop on the exact frame the probe fires, or at the frame/wall limit.

Provides a common interface for ROM loading, button input, frame advance,
screen capture, memory access, and save states across emulator backends.

WP1b (spec 4.1, 11.1, 12.2, 13.2) hardens the GBA path into the contract the
FireRed agent needs.  The GB/GBC path (PyBoy) is unchanged.

* **Input contract**: a closed button vocabulary (GBA adds ``l``/``r``), no
  chord unless explicitly whitelisted, the A+B+Start+Select soft-reset chord
  can never be pressed, impossible d-pad combinations are rejected.
* **Memory**: bulk ``read_range`` straight from the core's memory blocks (no
  per-byte loop) with the EWRAM/IWRAM region helpers; the read-only ROM image is
  served from host memory.  Reads outside the physical regions fail closed.
* **State API**: ``save_state(path)`` writes atomically, ``load_state(path)``
  validates size, header and ROM CRC32 before the core sees the bytes.  The ROM
  is read once into memory and handed to mGBA as a read-only in-memory VFile:
  no temp copy on disk, nothing that could write the ROM or leave a ``.sav``.
* Not thread-safe by design: a backend is owned by exactly one writer thread.
  A tripwire raises :class:`ConcurrentAccessError` on overlapping calls.

Provenance: the GBA key-bit map, held-key ``button_down``/``button_up`` model,
quiet mGBA logging and the "pygba cache is stale after load_raw_state" lesson
come from the earlier AIMD Labs runtime (MIT fork of NousResearch/pokemon-agent),
whose emulator code is Nous Research MIT code with AIMD Labs changes.
The code here is re-implemented against the
WP1b contract; the ring-buffer/RGB565 dashboard path and ``write_range`` (spec
invariant 8: no RAM writes) are deliberately not ported.
"""

from __future__ import annotations

import collections
import concurrent.futures
import hashlib
import io
import itertools
import os
import struct
import sys
import tempfile
import threading
import time
import zlib
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    FrozenSet,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EmulatorError(RuntimeError):
    """Base class for backend failures."""


class InputRejected(ValueError):
    """A button or chord is not in the whitelist (or is forbidden outright)."""


class MemoryAccessError(ValueError):
    """A read lies outside the physical regions the backend exposes."""


class StateLoadError(EmulatorError):
    """A savestate file/bytes were rejected before or by the core."""


class RomError(EmulatorError):
    """The ROM could not be loaded."""


class RomWriteForbidden(PermissionError):
    """Something tried to write to the ROM (``allow_rom_writes`` is always false)."""


class ConcurrentAccessError(EmulatorError):
    """Two threads entered one backend at the same time (single-writer breach)."""


# ---------------------------------------------------------------------------
# Input contract (spec 11.1: A B START SELECT UP DOWN LEFT RIGHT, L R on GBA;
# chords only from an explicit whitelist)
# ---------------------------------------------------------------------------

GB_BUTTONS: Tuple[str, ...] = ("a", "b", "start", "select", "up", "down", "left", "right")
GBA_BUTTONS: Tuple[str, ...] = GB_BUTTONS + ("l", "r")
DPAD: Tuple[str, ...] = ("up", "down", "left", "right")

#: Pressing all four at once soft-resets every GBA game.  Never allowed.
SOFT_RESET_CHORD: FrozenSet[str] = frozenset(("a", "b", "start", "select"))
_IMPOSSIBLE_DPAD: Tuple[FrozenSet[str], ...] = (
    frozenset(("up", "down")),
    frozenset(("left", "right")),
)

#: Default chord whitelist: B + one d-pad direction (hold B to run in FireRed).
DEFAULT_CHORD_WHITELIST: FrozenSet[FrozenSet[str]] = frozenset(frozenset(("b", d)) for d in DPAD)

ButtonSpec = Union[str, Iterable[str]]


def validate_chord_whitelist(
    chords: Iterable[Iterable[str]],
    allowed: Sequence[str] = GBA_BUTTONS,
) -> FrozenSet[FrozenSet[str]]:
    """Return *chords* as a frozenset of frozensets or raise ``ValueError``."""
    out = set()
    for chord in chords:
        fs = frozenset(str(b).lower() for b in chord)
        if len(fs) < 2:
            raise ValueError(f"a chord needs at least two buttons: {sorted(fs)}")
        if not fs <= set(allowed):
            raise ValueError(f"chord uses unknown buttons: {sorted(fs - set(allowed))}")
        if SOFT_RESET_CHORD <= fs:
            raise ValueError("chord contains the soft-reset combination A+B+Start+Select")
        if any(pair <= fs for pair in _IMPOSSIBLE_DPAD):
            raise ValueError(f"chord has opposite d-pad directions: {sorted(fs)}")
        out.add(fs)
    return frozenset(out)


def normalize_buttons(
    spec: ButtonSpec,
    *,
    allowed: Sequence[str] = GBA_BUTTONS,
    chords: FrozenSet[FrozenSet[str]] = DEFAULT_CHORD_WHITELIST,
) -> Tuple[str, ...]:
    """Validate a button or chord and return it canonically ordered.

    A single button must be in *allowed*.  Several buttons form a chord, which
    must be in *chords*.  Strings are the only accepted element type (nothing a
    model wrote is ever interpreted), duplicates are rejected, and the
    soft-reset combination or opposite d-pad directions are refused even if a
    caller whitelisted them.
    """
    if isinstance(spec, str):
        items: List[object] = [spec]
    elif isinstance(spec, (bytes, bytearray, Mapping)):
        raise InputRejected(f"button spec must be a string or a list of strings: {spec!r}")
    else:
        try:
            items = list(spec)
        except TypeError:
            raise InputRejected(f"button spec must be a string or a list of strings: {spec!r}")
    if not items:
        raise InputRejected("empty button spec")
    names: List[str] = []
    for item in items:
        if not isinstance(item, str):
            raise InputRejected(f"button names must be strings: {item!r}")
        name = item.lower()
        if name not in allowed:
            raise InputRejected(f"unknown button {item!r}; valid: {list(allowed)}")
        names.append(name)
    if len(set(names)) != len(names):
        raise InputRejected(f"duplicate buttons in {names}")
    fs = frozenset(names)
    if len(fs) > 1:
        if SOFT_RESET_CHORD <= fs:
            raise InputRejected("soft-reset chord A+B+Start+Select is forbidden")
        if any(pair <= fs for pair in _IMPOSSIBLE_DPAD):
            raise InputRejected(f"opposite d-pad directions: {sorted(fs)}")
        if fs not in chords:
            raise InputRejected(f"chord {sorted(fs)} is not whitelisted")
    return tuple(b for b in allowed if b in fs)


# ---------------------------------------------------------------------------
# Memory regions (GBA)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryRegion:
    """A named physical memory range."""

    name: str
    base: int
    size: int

    @property
    def end(self) -> int:
        return self.base + self.size

    def contains(self, addr: int, size: int = 1) -> bool:
        return self.base <= addr and addr + size <= self.end


GBA_EWRAM = MemoryRegion("ewram", 0x02000000, 0x40000)
GBA_IWRAM = MemoryRegion("iwram", 0x03000000, 0x8000)
#: The regions every atomic snapshot carries (spec 4.1/4.3).
GBA_SNAPSHOT_REGIONS: Tuple[MemoryRegion, ...] = (GBA_EWRAM, GBA_IWRAM)


def write_file_atomic(path: Union[str, os.PathLike], data: bytes) -> None:
    """Write *data* to *path* via temp file + fsync + ``os.replace``.

    A crash leaves either the old file or the complete new one, never a torn
    savestate (spec 12.2).  The temp file lives next to the target.
    """
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        # Persist the directory entry as well as the file contents.  Without
        # this, a power loss after replace can resurrect the old name (or no
        # name) even though the state itself was fully written.
        dir_fd = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


ROM_SUFFIXES = {".gba", ".gb", ".gbc"}


def assert_not_rom_path(path: Union[str, os.PathLike], rom_path: Optional[str]) -> Path:
    """Refuse state/output paths that are (or look like) a ROM file."""
    target = Path(path).expanduser().resolve()
    if target.suffix.lower() in ROM_SUFFIXES:
        raise RomWriteForbidden(f"refusing to write a ROM-looking path: {target}")
    if rom_path is not None:
        try:
            if target.exists() and os.path.samefile(target, rom_path):
                raise RomWriteForbidden(f"refusing to write over the ROM: {target}")
        except OSError:
            pass
        if str(target) == str(Path(rom_path).resolve()):
            raise RomWriteForbidden(f"refusing to write over the ROM: {target}")
    return target


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class Emulator(ABC):
    """Abstract emulator interface.

    Subclasses wrap a concrete emulator library (PyBoy, PyGBA, etc.) and
    expose a uniform API for the agent layer.
    """

    BUTTONS: List[str] = list(GB_BUTTONS)

    def __init__(self) -> None:
        #: Frames this instance has emulated since ``load``.  Monotonic: a
        #: savestate load never resets it (the core's own counter does jump).
        self.frame_count: int = 0
        self.rom_path: Optional[str] = None

    # -- lifecycle ----------------------------------------------------------

    @abstractmethod
    def load(self, rom_path: str) -> None:
        """Load a ROM file and initialise the emulator."""

    @abstractmethod
    def close(self) -> None:
        """Shut down the emulator and release resources."""

    # -- input --------------------------------------------------------------

    @abstractmethod
    def press(self, button: str, frames: int = 1) -> None:
        """Press *button* and hold it for *frames* frames.

        Parameters
        ----------
        button : str
            One of ``BUTTONS``.
        frames : int
            How many frames to hold the button before releasing.
        """

    @abstractmethod
    def release_all(self) -> None:
        """Release every button."""

    # -- timing -------------------------------------------------------------

    @abstractmethod
    def tick(self, frames: int = 1) -> None:
        """Advance the emulation by *frames* frames."""

    # -- video --------------------------------------------------------------

    @abstractmethod
    def get_screen(self) -> "Image.Image":
        """Return the current screen as a PIL Image."""

    # -- memory -------------------------------------------------------------

    @abstractmethod
    def read_u8(self, addr: int) -> int:
        """Read an unsigned 8-bit value from *addr*."""

    @abstractmethod
    def read_u16(self, addr: int) -> int:
        """Read an unsigned 16-bit little-endian value from *addr*."""

    @abstractmethod
    def read_u32(self, addr: int) -> int:
        """Read an unsigned 32-bit little-endian value from *addr*."""

    @abstractmethod
    def read_range(self, addr: int, size: int) -> bytes:
        """Read *size* bytes starting at *addr*."""

    # -- save / load --------------------------------------------------------

    @abstractmethod
    def save_state(self, path: str) -> None:
        """Persist an emulator save-state to *path*."""

    @abstractmethod
    def load_state(self, path: str) -> None:
        """Restore an emulator save-state from *path*."""

    # -- info ---------------------------------------------------------------

    def get_info(self) -> Dict:
        """Return runtime metadata about the emulator."""
        return {
            "backend": self.__class__.__name__,
            "rom_path": self.rom_path,
            "frame_count": self.frame_count,
        }

    # -- WP1b contract (optional; implemented by the GBA backend) -----------

    def read_region(self, region: MemoryRegion) -> bytes:
        """Read a whole named region in one call."""
        return self.read_range(region.base, region.size)

    def read_regions(self, regions: Sequence[MemoryRegion]) -> Dict[str, bytes]:
        """Read several regions; the caller guarantees nothing runs meanwhile."""
        return {r.name: self.read_region(r) for r in regions}

    def read_ranges(self, ranges: Sequence[tuple[int, int]]) -> tuple[bytes, ...]:
        """Read disjoint ranges while the caller guarantees the emulator is paused."""
        return tuple(self.read_range(addr, size) for addr, size in ranges)

    def press_buttons(self, buttons: ButtonSpec, frames: int = 2, release_frames: int = 1) -> None:
        """Hold a button or whitelisted chord for *frames*, then release."""
        raise NotImplementedError(f"{type(self).__name__} does not support chords")

    def release_buttons(self) -> None:
        """Release every key immediately (no frame is run)."""
        self.release_all()

    def save_state_bytes(self) -> bytes:
        raise NotImplementedError(f"{type(self).__name__} has no in-memory state API")

    def load_state_bytes(self, data: bytes) -> None:
        raise NotImplementedError(f"{type(self).__name__} has no in-memory state API")

    @property
    def core_frame(self) -> Optional[int]:
        """The emulator core's own frame counter, if it has one."""
        return None


# ---------------------------------------------------------------------------
# PyBoy backend (Game Boy / Game Boy Color)
# ---------------------------------------------------------------------------


class PyBoyEmulator(Emulator):
    """Wraps the *PyBoy* library for .gb / .gbc ROMs.

    Runs headless (``window='null'``) so no display server is required.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pyboy: Optional[object] = None

    # -- lifecycle ----------------------------------------------------------

    def load(self, rom_path: str) -> None:
        """Load a Game Boy ROM via PyBoy."""
        try:
            from pyboy import PyBoy  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "PyBoy is required for .gb/.gbc ROMs.  Install it with:  pip install pyboy"
            ) from exc

        rom_path = str(Path(rom_path).expanduser().resolve())
        if not os.path.isfile(rom_path):
            raise FileNotFoundError(f"ROM not found: {rom_path}")

        self._pyboy = PyBoy(rom_path, window="null")
        self.rom_path = rom_path
        self.frame_count = 0

    def close(self) -> None:
        """Stop PyBoy."""
        if self._pyboy is not None:
            self._pyboy.stop(save=False)  # type: ignore[union-attr]
            self._pyboy = None

    # -- input --------------------------------------------------------------

    def press(self, button: str, frames: int = 1) -> None:
        """Press a button and hold it for *frames* frames, then release.

        Uses button_press/button_release (not button()) to ensure the
        button stays held for the full duration. PyBoy's button() auto-
        releases after ``delay`` ticks which can cause issues with Gen 1
        walk registration that needs multi-frame holds.
        """
        button = button.lower()
        if button not in self.BUTTONS:
            raise ValueError(f"Unknown button '{button}'. Valid: {self.BUTTONS}")
        pb = self._pyboy
        pb.button_press(button)  # type: ignore[union-attr]
        self.tick(frames)
        pb.button_release(button)  # type: ignore[union-attr]

    def release_all(self) -> None:
        """Release all buttons."""
        pb = self._pyboy
        for btn in self.BUTTONS:
            try:
                pb.button_release(btn)  # type: ignore[union-attr]
            except Exception:
                pass

    # -- timing -------------------------------------------------------------

    def tick(self, frames: int = 1) -> None:
        """Advance emulation by *frames* frames."""
        pb = self._pyboy
        for _ in range(frames):
            pb.tick()  # type: ignore[union-attr]
            self.frame_count += 1

    # -- video --------------------------------------------------------------

    def get_screen(self) -> "Image.Image":
        """Return current screen as a PIL Image (160×144)."""
        return self._pyboy.screen.image  # type: ignore[union-attr]

    # -- memory -------------------------------------------------------------

    def read_u8(self, addr: int) -> int:
        return self._pyboy.memory[addr] & 0xFF  # type: ignore[index]

    def read_u16(self, addr: int) -> int:
        lo = self._pyboy.memory[addr] & 0xFF  # type: ignore[index]
        hi = self._pyboy.memory[addr + 1] & 0xFF  # type: ignore[index]
        return (hi << 8) | lo

    def read_u32(self, addr: int) -> int:
        b = bytes(self._pyboy.memory[addr : addr + 4])  # type: ignore[index]
        return int.from_bytes(b, "little")

    def read_range(self, addr: int, size: int) -> bytes:
        return bytes(self._pyboy.memory[addr : addr + size])  # type: ignore[index]

    # -- save / load --------------------------------------------------------

    def save_state(self, path: str) -> None:
        """Save emulator state to a file."""
        path = str(Path(path).expanduser().resolve())
        with open(path, "wb") as f:
            self._pyboy.save_state(f)  # type: ignore[union-attr]

    def load_state(self, path: str) -> None:
        """Load emulator state from a file."""
        path = str(Path(path).expanduser().resolve())
        with open(path, "rb") as f:
            self._pyboy.load_state(f)  # type: ignore[union-attr]

    # -- info ---------------------------------------------------------------

    def get_info(self) -> Dict:
        info = super().get_info()
        info["platform"] = "GB/GBC"
        return info


# ---------------------------------------------------------------------------
# PyGBA backend (Game Boy Advance, mGBA core)
# ---------------------------------------------------------------------------

# mGBA memory-block ids = address >> 24.  Only physical ranges are readable.
_GBA_ROM_IDS = range(0x08, 0x0E)  # 0x08000000-0x0DFFFFFF: one ROM, mirrored
_GBA_KEYINPUT = 0x04000130  # active-low key register (diagnostics)
_GBA_BLOCK_IDS = (0x02, 0x03, 0x05, 0x06, 0x07, 0x0E)  # EWRAM IWRAM PAL VRAM OAM SRAM


class PyGBAEmulator(Emulator):
    """GBA backend: mGBA 0.10 driven through the ``mgba`` Python bindings.

    This is the backend the live system runs in production (decision O1: PyGBA
    over mGBA).  The ROM is read once into host memory and given to mGBA as an
    in-memory, read-only VFile, so there is no temp copy to clean up, no
    ``.sav`` is ever created and no file the core could write exists.
    """

    BUTTONS: List[str] = list(GBA_BUTTONS)
    #: Largest savestate file accepted (raw mGBA 0.10 states are 397,312 bytes).
    MAX_STATE_BYTES = 8 * 1024 * 1024

    def __init__(
        self,
        *,
        allow_rom_writes: bool = False,
        chord_whitelist: Iterable[Iterable[str]] = DEFAULT_CHORD_WHITELIST,
    ) -> None:
        super().__init__()
        if allow_rom_writes:
            raise RomWriteForbidden("allow_rom_writes must be False: the ROM is never written")
        self.allow_rom_writes = False
        self.chord_whitelist = validate_chord_whitelist(chord_whitelist, GBA_BUTTONS)
        self.load_epoch = 0  # successful savestate loads
        self.rom_sha1: Optional[str] = None
        self.rom_crc32: Optional[int] = None
        self.game_code: Optional[str] = None
        self.game_title: Optional[str] = None
        self.mgba_version: Optional[str] = None
        self._core: Optional[object] = None
        self._image: Optional[object] = None
        self._rom: bytes = b""
        self._rom_buf: Optional[object] = None  # keeps the cffi view of _rom alive
        self._ffi: Optional[object] = None
        self._blocks: Dict[int, Tuple[object, int]] = {}
        self._key_bits: Dict[str, int] = {}
        self._state_size = 0
        self._state_magic: Optional[bytes] = None
        self._state_crc_offset: Optional[int] = None
        self._keys_mask = 0
        self._guard = threading.Lock()  # single-writer tripwire
        self._audio_channels = None
        self._audio_sink: Optional[Callable[[bytes], None]] = None

    # -- lifecycle ----------------------------------------------------------

    def load(self, rom_path: str) -> None:
        """Load a GBA ROM into mGBA (headless, no audio, no window)."""
        try:
            import mgba.core  # type: ignore[import-untyped]
            import mgba.gba  # type: ignore[import-untyped]
            import mgba.image  # type: ignore[import-untyped]
            import mgba.log  # type: ignore[import-untyped]
            import mgba.vfs  # type: ignore[import-untyped]
            from mgba._pylib import ffi, lib  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "mGBA Python bindings are required for .gba ROMs: "
                "pip install 'pokegent[mgba]' and make libmgba.so.0.10 "
                "findable via LD_LIBRARY_PATH."
            ) from exc

        if self._core is not None:
            raise EmulatorError("backend already loaded; call close() first")
        path = Path(rom_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"ROM not found: {path}")
        rom = path.read_bytes()  # read-only open; never written
        if not rom or len(rom) > 0x02000000:
            raise RomError(f"not a GBA ROM image ({len(rom)} bytes): {path}")

        mgba.log.silence()
        rom_buf = ffi.from_buffer(rom)
        vfile = mgba.vfs.VFile(lib.VFileFromConstMemory(rom_buf, len(rom)))
        core = mgba.core.find_vf(vfile)
        if core is None or not core.load_rom(vfile):
            raise RomError(f"mGBA rejected the ROM: {path}")
        vfile._claimed = True  # the core owns the VFile now
        image = mgba.image.Image(*core.desired_video_dimensions())
        core.set_video_buffer(image)
        core.reset()

        self._core, self._image, self._ffi = core, image, ffi
        try:
            self._finish_load(core, ffi, lib, rom, rom_buf, path)
        except BaseException:
            self.close()
            raise

    def _finish_load(self, core, ffi, lib, rom, rom_buf, path) -> None:
        self._rom, self._rom_buf = rom, rom_buf
        self.rom_path = str(path)
        self.rom_sha1 = hashlib.sha1(rom).hexdigest()
        self.rom_crc32 = zlib.crc32(rom) & 0xFFFFFFFF
        self.game_code, self.game_title = core.game_code, core.game_title
        self.mgba_version = ffi.string(lib.projectVersion).decode("ascii", "replace")
        self._key_bits = {
            "a": core.KEY_A,
            "b": core.KEY_B,
            "select": core.KEY_SELECT,
            "start": core.KEY_START,
            "right": core.KEY_RIGHT,
            "left": core.KEY_LEFT,
            "up": core.KEY_UP,
            "down": core.KEY_DOWN,
            "l": core.KEY_L,
            "r": core.KEY_R,
        }
        mem = core.memory.u8._core
        size = ffi.new("size_t *")
        self._blocks = {}
        for rid in _GBA_BLOCK_IDS:
            ptr = mem.getMemoryBlock(mem, rid, size)
            if ptr != ffi.NULL and size[0]:
                self._blocks[rid] = (ffi.cast("uint8_t *", ptr), int(size[0]))
        self._state_size = int(core._core.stateSize(core._core))
        # Learn the state header layout from the core's own state (mGBA 0.10:
        # magic at 0, ROM CRC32 at 8) and only trust the CRC offset if it checks out.
        own = self.save_state_bytes()
        self._state_magic = own[:4]
        self._state_crc_offset = (
            8 if struct.unpack_from("<I", own, 8)[0] == self.rom_crc32 else None
        )
        self.frame_count = 0
        self.load_epoch = 0
        self._keys_mask = 0

    def close(self) -> None:
        """Free the core and the ROM image (nothing on disk to clean up).

        The mgba bindings keep a reference cycle per core, so merely dropping it
        leaves ~85 MB alive until the cyclic GC runs; the native core is
        therefore deinitialized here, explicitly, while the ROM and video
        buffers it points into are still alive.
        """
        core, ffi = self._core, self._ffi
        if core is not None and ffi is not None:
            try:
                ffi.release(core._core)
            except Exception:  # noqa: BLE001 - already released / odd binding version
                pass
        self._core = None
        self._image = None
        self._blocks = {}
        self._rom = b""
        self._rom_buf = None
        self._keys_mask = 0
        self._audio_channels = None
        self._audio_sink = None

    def _need_core(self):
        core = self._core
        if core is None:
            raise EmulatorError("no ROM loaded")
        return core

    # -- single-writer tripwire ---------------------------------------------

    def _enter(self) -> None:
        if not self._guard.acquire(blocking=False):
            raise ConcurrentAccessError(
                "overlapping backend calls: one emulator, one writer (use EmulatorActor)"
            )

    def _leave(self) -> None:
        self._guard.release()

    # -- input --------------------------------------------------------------

    def _mask(self, buttons: Tuple[str, ...]) -> int:
        mask = 0
        for b in buttons:
            mask |= 1 << self._key_bits[b]
        return mask

    def button_down(self, buttons: ButtonSpec) -> None:
        """Hold a button/whitelisted chord (replaces whatever was held)."""
        names = normalize_buttons(buttons, allowed=GBA_BUTTONS, chords=self.chord_whitelist)
        core = self._need_core()
        self._enter()
        try:
            self._keys_mask = self._mask(names)
            core.set_keys(raw=self._keys_mask)
        finally:
            self._leave()

    def button_up(self, buttons: Optional[ButtonSpec] = None) -> None:
        """Release the given buttons, or everything when *buttons* is None."""
        core = self._need_core()
        self._enter()
        try:
            if buttons is None:
                self._keys_mask = 0
            else:
                names = [buttons] if isinstance(buttons, str) else list(buttons)
                for b in names:
                    if b not in self._key_bits:
                        raise InputRejected(f"unknown button {b!r}")
                    self._keys_mask &= ~(1 << self._key_bits[b])
            core.set_keys(raw=self._keys_mask)
        finally:
            self._leave()

    def press_buttons(self, buttons: ButtonSpec, frames: int = 2, release_frames: int = 1) -> None:
        """Hold *buttons* for *frames* frames, release, then run *release_frames*.

        A release frame between two taps is what makes the second tap a new
        edge for the game.  Keys are always released, even on an error.
        """
        names = normalize_buttons(buttons, allowed=GBA_BUTTONS, chords=self.chord_whitelist)
        self._check_frames(frames, 1)
        self._check_frames(release_frames, 0)
        core = self._need_core()
        self._enter()
        try:
            self._keys_mask = self._mask(names)
            core.set_keys(raw=self._keys_mask)
            try:
                self._run(frames)
            finally:
                self._keys_mask = 0
                core.set_keys(raw=0)
            self._run(release_frames)
        finally:
            self._leave()

    def press(self, button: str, frames: int = 2, release_frames: int = 1) -> None:
        self.press_buttons((button,) if isinstance(button, str) else button, frames, release_frames)

    def release_all(self) -> None:
        self.button_up(None)

    def release_buttons(self) -> None:
        self.button_up(None)

    def pressed_keys(self) -> FrozenSet[str]:
        """Keys the game would read right now (decoded KEYINPUT, active-low)."""
        core = self._need_core()
        self._enter()
        try:
            value = int(core.memory.u16[_GBA_KEYINPUT])
        finally:
            self._leave()
        return frozenset(n for n, bit in self._key_bits.items() if not (value >> bit) & 1)

    # -- timing -------------------------------------------------------------

    @staticmethod
    def _check_frames(frames: int, minimum: int) -> int:
        if isinstance(frames, bool) or not isinstance(frames, int) or frames < minimum:
            raise ValueError(f"frames must be an int >= {minimum}: {frames!r}")
        return frames

    def _run(self, frames: int) -> None:
        run_frame = self._need_core().run_frame
        for _ in range(frames):
            run_frame()
            self._drain_audio()
        self.frame_count += frames

    def set_audio_sink(self, sink: Optional[Callable[[bytes], None]]) -> None:
        """Enable the optional stream sink on the emulator thread.

        mGBA exposes a stereo blip buffer rather than a callback.  It is
        drained immediately after each frame, then handed to the addon's
        bounded ring.  No network or unbounded queue is involved here.
        """
        self._audio_sink = sink
        self._audio_channels = None
        if sink is None:
            return
        core = self._need_core()
        try:
            from pokegent.dashboard.stream.audio import AUDIO_SAMPLE_RATE

            core.set_audio_buffer_size(4096)
            channels = core.get_audio_channels()
            channels.set_rate(AUDIO_SAMPLE_RATE)
            self._audio_channels = channels
        except Exception:
            self._audio_sink = None
            raise

    def _drain_audio(self) -> None:
        sink = self._audio_sink
        channels = self._audio_channels
        ffi = self._ffi
        if sink is None or channels is None or ffi is None:
            return
        try:
            available = min(int(channels.available), 2048)
            if available <= 0:
                return
            buffer = ffi.new(f"short[{available * 2}]")
            count = int(channels.read_into(buffer, available))
            if count:
                sink(bytes(ffi.buffer(buffer, count * 2 * 2)))
        except Exception:
            # Audio is an optional observer; it must never stop gameplay.
            self._audio_channels = None

    def tick(self, frames: int = 1) -> None:
        self._check_frames(frames, 0)
        self._need_core()
        self._enter()
        try:
            self._run(frames)
        finally:
            self._leave()

    @property
    def core_frame(self) -> Optional[int]:
        core = self._core
        return None if core is None else int(core.frame_counter)

    @property
    def rom_image(self) -> bytes:
        """The immutable ROM bytes mGBA runs (for ROM-table reads off-thread)."""
        return self._rom

    # -- video --------------------------------------------------------------

    def get_screen(self) -> "Image.Image":
        """Current frame as an RGB PIL image (240x160), a private copy."""
        self._need_core()
        self._enter()
        try:
            return self._image.to_pil().convert("RGB")  # type: ignore[union-attr]
        finally:
            self._leave()

    def get_screen_png(self) -> bytes:
        buf = io.BytesIO()
        self.get_screen().save(buf, format="PNG")
        return buf.getvalue()

    # -- memory -------------------------------------------------------------

    def read_range(self, addr: int, size: int) -> bytes:
        """Bulk read of *size* bytes at *addr* in one call.

        Only physical ranges are readable: EWRAM, IWRAM, palette, VRAM, OAM,
        SRAM/flash, and the ROM (0x08000000-0x0DFFFFFF, mirrors included).  A
        read that leaves its range, hits an unmapped region or relies on
        mirroring raises :class:`MemoryAccessError`.
        """
        if (
            isinstance(addr, bool)
            or isinstance(size, bool)
            or not isinstance(addr, int)
            or not isinstance(size, int)
        ):
            raise MemoryAccessError(f"address and size must be ints: {addr!r}, {size!r}")
        if size < 0 or addr < 0 or addr + size > 0x100000000:
            raise MemoryAccessError(f"bad range 0x{addr:08X}+{size}")
        if size == 0:
            return b""
        rid = addr >> 24
        if rid in _GBA_ROM_IDS:  # immutable host copy, no core access
            off = addr & 0x01FFFFFF
            if off + size > len(self._rom):
                raise MemoryAccessError(f"ROM read past the image: 0x{addr:08X}+{size}")
            return self._rom[off : off + size]
        block = self._blocks.get(rid)
        if block is None:
            raise MemoryAccessError(f"region 0x{rid:02X}000000 is not readable")
        ptr, blk_size = block
        off = addr & 0x00FFFFFF
        if off + size > blk_size:
            raise MemoryAccessError(
                f"range 0x{addr:08X}+{size} leaves the 0x{blk_size:X}-byte region"
            )
        self._need_core()
        self._enter()
        try:
            return self._ffi.buffer(ptr + off, size)[:]  # type: ignore[union-attr]
        finally:
            self._leave()

    def read_u8(self, addr: int) -> int:
        return self.read_range(addr, 1)[0]

    def read_u16(self, addr: int) -> int:
        return int.from_bytes(self.read_range(addr, 2), "little")

    def read_u32(self, addr: int) -> int:
        return int.from_bytes(self.read_range(addr, 4), "little")

    def read_regions(self, regions: Sequence[MemoryRegion]) -> Dict[str, bytes]:
        return {r.name: self.read_range(r.base, r.size) for r in regions}

    def read_ranges(self, ranges: Sequence[tuple[int, int]]) -> tuple[bytes, ...]:
        """Bulk-copy disjoint ranges under one mGBA access guard."""
        requested = tuple(ranges)
        for addr, size in requested:
            if (
                isinstance(addr, bool)
                or isinstance(size, bool)
                or not isinstance(addr, int)
                or not isinstance(size, int)
                or addr < 0
                or size < 0
                or addr + size > 0x100000000
            ):
                raise MemoryAccessError(f"bad range {addr!r}+{size!r}")
            rid = addr >> 24
            if rid in _GBA_ROM_IDS:
                if (addr & 0x01FFFFFF) + size > len(self._rom):
                    raise MemoryAccessError(f"ROM read past the image: 0x{addr:08X}+{size}")
            else:
                block = self._blocks.get(rid)
                if block is None or (addr & 0x00FFFFFF) + size > block[1]:
                    raise MemoryAccessError(f"range 0x{addr:08X}+{size} is not readable")

        if not requested:
            return ()
        self._need_core()
        self._enter()
        try:
            result: list[bytes] = []
            for addr, size in requested:
                rid = addr >> 24
                if rid in _GBA_ROM_IDS:
                    off = addr & 0x01FFFFFF
                    result.append(self._rom[off : off + size])
                else:
                    ptr, _ = self._blocks[rid]
                    off = addr & 0x00FFFFFF
                    result.append(self._ffi.buffer(ptr + off, size)[:])
            return tuple(result)
        finally:
            self._leave()

    # -- save / load --------------------------------------------------------

    @property
    def state_size(self) -> int:
        return self._state_size

    def save_state_bytes(self) -> bytes:
        core = self._need_core()
        self._enter()
        try:
            raw = core.save_raw_state()
            if raw is None:
                raise EmulatorError("mGBA could not serialize the state")
            return self._ffi.buffer(raw)[:]  # type: ignore[union-attr]
        finally:
            self._leave()

    def save_state(self, path: str) -> None:
        """Write the current state to *path* atomically; refuses ROM paths."""
        target = assert_not_rom_path(path, self.rom_path)
        write_file_atomic(target, self.save_state_bytes())

    def _check_state_bytes(self, data: bytes) -> None:
        if len(data) != self._state_size:
            raise StateLoadError(f"savestate is {len(data)} bytes, expected {self._state_size}")
        if self._state_magic is not None and data[:4] != self._state_magic:
            raise StateLoadError("savestate header/version does not match this mGBA build")
        off = self._state_crc_offset
        if off is not None and struct.unpack_from("<I", data, off)[0] != self.rom_crc32:
            raise StateLoadError("savestate was made with a different ROM (CRC32 mismatch)")

    def load_state_bytes(self, data: bytes, refresh_video: bool = True) -> None:
        """Restore a raw mGBA state; RAM is exactly the saved RAM afterwards.

        mGBA does not redraw on load, so a screenshot taken right away would
        show the previous picture.  With *refresh_video* (default) one frame is
        run and the state is loaded again, which leaves the framebuffer valid
        and the emulation state bit-identical to the file.
        """
        data = bytes(data)
        core = self._need_core()
        self._check_state_bytes(data)
        self._enter()
        try:
            if not core.load_raw_state(data):
                raise StateLoadError("mGBA rejected the savestate")
            core.set_keys(raw=0)
            self._keys_mask = 0
            if refresh_video:
                self._run(1)
                if not core.load_raw_state(data):
                    raise StateLoadError("mGBA rejected the savestate on reload")
            self.load_epoch += 1
        finally:
            self._leave()

    def load_state(self, path: str, refresh_video: bool = True) -> None:  # type: ignore[override]
        p = Path(path).expanduser()
        try:
            size = p.stat().st_size
        except OSError as exc:
            raise StateLoadError(f"cannot read savestate {p}: {exc}") from exc
        if size > self.MAX_STATE_BYTES:
            raise StateLoadError(f"savestate file is implausibly large ({size} bytes)")
        self.load_state_bytes(p.read_bytes(), refresh_video=refresh_video)

    # -- info ---------------------------------------------------------------

    def get_info(self) -> Dict:
        info = super().get_info()
        info.update(
            {
                "platform": "GBA",
                "core": "mGBA",
                "core_version": self.mgba_version,
                "rom_sha1": self.rom_sha1,
                "game_code": self.game_code,
                "game_title": self.game_title,
                "core_frame": self.core_frame,
                "load_epoch": self.load_epoch,
                "state_size": self._state_size,
                "buttons": list(GBA_BUTTONS),
                "allow_rom_writes": False,
            }
        )
        return info


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_EXT_MAP = {
    ".gb": PyBoyEmulator,
    ".gbc": PyBoyEmulator,
    ".gba": PyGBAEmulator,
}


def create_emulator(rom_path: str) -> Emulator:
    """Create the appropriate emulator for *rom_path* based on file extension.

    Parameters
    ----------
    rom_path : str
        Path to a Game Boy (.gb/.gbc) or Game Boy Advance (.gba) ROM.

    Returns
    -------
    Emulator
        A loaded, ready-to-use emulator instance.

    Raises
    ------
    ValueError
        If the file extension is not recognised.
    """
    ext = Path(rom_path).suffix.lower()
    cls = _EXT_MAP.get(ext)
    if cls is None:
        raise ValueError(f"Unsupported ROM extension '{ext}'. Supported: {', '.join(_EXT_MAP)}")
    emu = cls()
    emu.load(rom_path)
    return emu


class EmulatorTimeout(TimeoutError):
    """A wall-clock bound expired before the emulator thread answered."""


class ActorStopped(RuntimeError):
    """The actor (or its emulator thread) is not running."""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

_ROM_IDS = range(0x08, 0x0E)


@dataclass(frozen=True)
class AtomicSnapshot:
    """Immutable, internally consistent copy of the RAM a decision is based on.

    ``frame`` is monotonic for the life of the backend (survives savestate
    loads); ``load_epoch`` says which restore the RAM belongs to; ``core_frame``
    is the core's own counter (it jumps on a load).  Implements the same
    ``read_*`` surface as the backend so a reader can run on a snapshot without
    an emulator (the ROM-free test path).
    """

    seq: int
    frame: int
    core_frame: Optional[int]
    load_epoch: int
    keys_held: FrozenSet[str]
    regions: Mapping[str, bytes]
    region_defs: Tuple[MemoryRegion, ...] = field(repr=False)
    taken_at: float = field(repr=False)  # perf_counter seconds (wall clock)
    rom: Optional[bytes] = field(default=None, repr=False, compare=False)

    def region(self, name: str) -> bytes:
        return self.regions[name]

    @cached_property
    def ram_digest(self) -> str:
        """sha256 over the region bytes in a fixed order (replay/determinism check)."""
        h = hashlib.sha256()
        for r in self.region_defs:
            h.update(r.name.encode("ascii"))
            h.update(self.regions[r.name])
        return h.hexdigest()

    def read_range(self, addr: int, size: int) -> bytes:
        if isinstance(addr, bool) or isinstance(size, bool) or size < 0 or addr < 0:
            raise MemoryAccessError(f"bad range {addr!r}+{size!r}")
        if size == 0:
            return b""
        for r in self.region_defs:
            if r.contains(addr, size):
                off = addr - r.base
                return self.regions[r.name][off : off + size]
        if self.rom is not None and (addr >> 24) in _ROM_IDS:
            off = addr & 0x01FFFFFF
            if off + size <= len(self.rom):
                return self.rom[off : off + size]
        raise MemoryAccessError(f"0x{addr:08X}+{size} is not inside this snapshot")

    def read_u8(self, addr: int) -> int:
        return self.read_range(addr, 1)[0]

    def read_u16(self, addr: int) -> int:
        return int.from_bytes(self.read_range(addr, 2), "little")

    def read_u32(self, addr: int) -> int:
        return int.from_bytes(self.read_range(addr, 4), "little")


@dataclass(frozen=True)
class AtomicRangeRead:
    """A small set of RAM ranges captured together between emulator frames."""

    frame: int
    load_epoch: int
    ranges: tuple[tuple[int, bytes], ...]

    def read_range(self, addr: int, size: int) -> bytes:
        for base, data in self.ranges:
            offset = addr - base
            if offset >= 0 and offset + size <= len(data):
                return data[offset : offset + size]
        raise MemoryAccessError(f"0x{addr:08X}+{size} is not inside this range read")


@dataclass(frozen=True)
class WaitResult:
    """Outcome of a bounded wait: it never raises on a limit, it says which."""

    satisfied: bool
    reason: str  # "satisfied" | "frame_limit" | "wall_limit"
    frames: int  # frames the wait ran
    wall_s: float  # wall-clock seconds the wait took
    snapshot: Optional[AtomicSnapshot]  # snapshot that satisfied it, else None


@dataclass(frozen=True)
class ScreenRead:
    """One framebuffer copy, taken between two frames, with the counters that date it."""

    frame: int  # the backend's monotonic frame counter at the copy
    load_epoch: int  # which savestate restore the picture belongs to
    image: Any = field(repr=False)  # an RGB PIL image (240x160), a private copy


@dataclass(frozen=True)
class StateFileInfo:
    path: str
    size: int
    sha256: str
    frame: int
    load_epoch: int


@dataclass(frozen=True)
class RestoreInfo:
    path: str
    frame: int
    load_epoch: int


# ---------------------------------------------------------------------------
# Actor
# ---------------------------------------------------------------------------


class EmulatorActor:
    """Serialize every access to one backend; see the module docstring."""

    def __init__(
        self,
        backend: Emulator,
        *,
        fps: Optional[float] = None,
        chord_whitelist: Optional[FrozenSet[FrozenSet[str]]] = None,
        snapshot_regions: Sequence[MemoryRegion] = GBA_SNAPSHOT_REGIONS,
        max_hold_frames: int = 600,
        max_wait_frames: int = 600,
        max_advance_frames: int = 3600,
        max_wait_for_frames: int = 36000,
        min_fps: float = 20.0,
        wall_slack_s: float = 5.0,
        call_timeout_s: float = 10.0,
        max_state_bytes: int = 8 * 1024 * 1024,
        timer: Callable[[], float] = time.perf_counter,
        clock: Optional[EmuClock] = None,
    ) -> None:
        for name in (
            "read_regions",
            "press_buttons",
            "save_state_bytes",
            "load_state_bytes",
            "button_down",
            "button_up",
        ):
            if not callable(getattr(backend, name, None)):
                raise TypeError(f"backend {type(backend).__name__} lacks {name}()")
        self._backend = backend
        self._whitelist = (
            chord_whitelist
            if chord_whitelist is not None
            else getattr(backend, "chord_whitelist", DEFAULT_CHORD_WHITELIST)
        )
        self._regions = tuple(snapshot_regions)
        self.max_hold_frames = max_hold_frames
        self.max_wait_frames = max_wait_frames
        self.max_advance_frames = max_advance_frames
        self.max_wait_for_frames = max_wait_for_frames
        self.min_fps = min_fps
        self.wall_slack_s = wall_slack_s
        self.call_timeout_s = call_timeout_s
        self.max_state_bytes = max_state_bytes
        self._timer = timer
        self._seq = itertools.count(1)
        self._exclusive_access = threading.RLock()
        self._clock = clock or EmuClock(backend, fps=fps, timer=timer)
        if not self._clock.is_alive():
            self._clock.start()
        self.snapshots_taken = 0

    # -- helpers ------------------------------------------------------------

    def _check_alive(self) -> None:
        if not self._clock.is_alive():
            raise ActorStopped("emulator thread is not running")
        if self._clock.on_emulator_thread():
            raise RuntimeError("EmulatorActor called from the emulator thread (deadlock)")

    def _int(self, name: str, value: int, lo: int, hi: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise ValueError(f"{name} must be an int in [{lo}, {hi}]: {value!r}")
        return value

    def _frames_wall(self, frames: int) -> float:
        rate = self._clock.fps or self.min_fps
        return frames / min(rate, self.min_fps) + self.wall_slack_s

    def _await(self, future: concurrent.futures.Future, timeout: float) -> Any:
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()  # still queued -> skipped; running -> frame watchdog ends it
            raise EmulatorTimeout(f"emulator did not answer within {timeout:.1f}s") from None

    def _snapshot_now(self, emu: Emulator) -> AtomicSnapshot:
        """Runs on the emulator thread, between two frames."""
        keys: FrozenSet[str] = frozenset()
        pressed = getattr(emu, "pressed_keys", None)
        if callable(pressed):
            keys = pressed()
        return AtomicSnapshot(
            seq=next(self._seq),
            frame=emu.frame_count,
            core_frame=emu.core_frame,
            load_epoch=getattr(emu, "load_epoch", 0),
            keys_held=keys,
            regions=emu.read_regions(self._regions),
            region_defs=self._regions,
            taken_at=self._timer(),
            rom=getattr(emu, "rom_image", None),
        )

    # -- input (writer) -----------------------------------------------------

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Block new input submissions while a multi-call state transaction runs.

        A command already running reaches its frame boundary first.  The lock is re-entrant so
        the caller can use the ordinary save/load/read API inside the transaction.
        """
        with self._exclusive_access:
            yield

    def tap(self, buttons: ButtonSpec, hold_frames: int = 2, wait_frames: int = 1) -> int:
        """Press a button/whitelisted chord for *hold_frames*, release, then run
        *wait_frames* more frames.  Returns the frame count afterwards."""
        with self._exclusive_access:
            names = normalize_buttons(
                buttons, allowed=self._backend.BUTTONS, chords=self._whitelist
            )
            hold = self._int("hold_frames", hold_frames, 1, self.max_hold_frames)
            wait = self._int("wait_frames", wait_frames, 0, self.max_wait_frames)
            self._check_alive()
            fut = self._clock.submit_press(names, hold, wait)
            return self._await(fut, self._frames_wall(hold + wait))

    def hold(self, buttons: ButtonSpec, frames: int) -> int:
        """Hold for exactly *frames* frames, then release (no trailing wait)."""
        return self.tap(buttons, hold_frames=frames, wait_frames=0)

    def release_all(self) -> None:
        """Release every key (queued behind the running command, never skipped)."""
        with self._exclusive_access:
            self._check_alive()
            self._await(
                self._clock.submit_call(lambda emu: emu.release_buttons()),
                self.call_timeout_s,
            )

    def advance_frames(self, frames: int) -> int:
        """Run *frames* frames with no input change (bounded, 1..max_advance_frames)."""
        with self._exclusive_access:
            n = self._int("frames", frames, 1, self.max_advance_frames)
            self._check_alive()
            return self._await(self._clock.submit_wait(n), self._frames_wall(n))

    def wait_for(
        self,
        predicate: Callable[[AtomicSnapshot], Any],
        *,
        max_frames: int,
        max_wall_s: Optional[float] = None,
        check_every: int = 1,
    ) -> WaitResult:
        """Run frames until ``predicate(snapshot)`` is truthy, with two limits.

        The predicate is evaluated on a fresh :class:`AtomicSnapshot` before the
        first frame and after every *check_every* frames, on the emulator thread:
        it must be fast, pure and must not call the actor.  The wait stops on the
        exact frame the predicate fires, after *max_frames* frames, or after
        *max_wall_s* wall-clock seconds, whichever comes first; the result says
        which (``reason``).  Both limits are mandatory-bounded.
        """
        with self._exclusive_access:
            frames = self._int("max_frames", max_frames, 1, self.max_wait_for_frames)
            every = self._int("check_every", check_every, 1, frames)
            wall = self._frames_wall(frames) if max_wall_s is None else float(max_wall_s)
            if not 0 < wall <= 3600:
                raise ValueError(f"max_wall_s must be in (0, 3600]: {max_wall_s!r}")
            self._check_alive()
            started = self._timer()

            def probe(emu: Emulator) -> Optional[AtomicSnapshot]:
                snap = self._snapshot_now(emu)
                return snap if predicate(snap) else None

            fut = self._clock.submit_until(
                probe,
                frames,
                check_every=every,
                deadline=self._clock.now() + wall,
            )
            out: UntilOutcome = self._await(fut, wall + self.wall_slack_s)
            return WaitResult(
                out.satisfied,
                out.reason,
                out.frames,
                self._timer() - started,
                out.value if out.satisfied else None,
            )

    # -- reads --------------------------------------------------------------

    def read_atomic_snapshot(self) -> AtomicSnapshot:
        """EWRAM + IWRAM + frame counters from one step between two frames."""
        self._check_alive()
        snap = self._await(self._clock.submit_read(self._snapshot_now), self.call_timeout_s)
        self.snapshots_taken += 1
        return snap

    def read_ranges(self, ranges: Sequence[tuple[int, int]]) -> AtomicRangeRead:
        """Read small disjoint ranges in one serialized, frame-consistent operation."""
        requested = tuple(ranges)
        if len(requested) > 256:
            raise ValueError("at most 256 ranges may be read at once")
        if any(
            isinstance(addr, bool)
            or isinstance(size, bool)
            or not isinstance(addr, int)
            or not isinstance(size, int)
            or addr < 0
            or size < 0
            for addr, size in requested
        ):
            raise ValueError("ranges must contain non-negative integer address/size pairs")
        if sum(size for _, size in requested) > sum(r.size for r in self._regions):
            raise ValueError("range read exceeds the configured snapshot size")
        self._check_alive()

        def grab(emu: Emulator) -> AtomicRangeRead:
            values = emu.read_ranges(requested)
            return AtomicRangeRead(
                frame=emu.frame_count,
                load_epoch=getattr(emu, "load_epoch", 0),
                ranges=tuple((addr, value) for (addr, _), value in zip(requested, values)),
            )

        return self._await(self._clock.submit_read(grab), self.call_timeout_s)

    def read_range(self, addr: int, size: int) -> bytes:
        self._check_alive()
        return self._await(
            self._clock.submit_read(lambda emu: emu.read_range(addr, size)), self.call_timeout_s
        )

    def read_u8(self, addr: int) -> int:
        return self.read_range(addr, 1)[0]

    def read_u16(self, addr: int) -> int:
        return int.from_bytes(self.read_range(addr, 2), "little")

    def read_u32(self, addr: int) -> int:
        return int.from_bytes(self.read_range(addr, 4), "little")

    def get_screen(self):
        self._check_alive()
        return self._await(
            self._clock.submit_read(lambda emu: emu.get_screen()), self.call_timeout_s
        )

    def get_screen_png(self) -> bytes:
        self._check_alive()
        return self._await(
            self._clock.submit_read(lambda emu: emu.get_screen_png()), self.call_timeout_s
        )

    def read_screen(self) -> ScreenRead:
        """The framebuffer plus the frame counter it belongs to, from one step between two
        frames (the viewer's door: a read, served on the emulator thread like every other)."""
        self._check_alive()

        def grab(emu: Emulator) -> ScreenRead:
            return ScreenRead(emu.frame_count, getattr(emu, "load_epoch", 0), emu.get_screen())

        return self._await(self._clock.submit_read(grab), self.call_timeout_s)

    def attach_audio_sink(self, sink: Optional[Callable[[bytes], None]]) -> None:
        """Install an optional non-blocking audio observer on the emu thread."""
        self._check_alive()
        setter = getattr(self._backend, "set_audio_sink", None)
        if not callable(setter):
            return
        self._await(
            self._clock.submit_call(lambda _emu: setter(sink)),
            self.call_timeout_s,
        )

    @property
    def frames_run(self) -> int:
        """Frames the emulator thread has run (a plain counter: cheap, needs no emulator step).
        Lets a read-only client skip a framebuffer copy when nothing has advanced."""
        return self._clock.ticks

    def info(self) -> Dict[str, Any]:
        self._check_alive()
        info = self._await(self._clock.submit_read(lambda emu: emu.get_info()), self.call_timeout_s)
        info["clock"] = self._clock.metrics()
        info["snapshots_taken"] = self.snapshots_taken
        return info

    # -- savestates (writer, FIFO behind queued input) --------------------------

    def save_state(self, path: str) -> StateFileInfo:
        """Checkpoint the emulator to *path* (atomic write; never a ROM path)."""
        with self._exclusive_access:
            target = assert_not_rom_path(path, getattr(self._backend, "rom_path", None))
            self._check_alive()

            def grab(emu: Emulator) -> Tuple[bytes, int, int]:
                return emu.save_state_bytes(), emu.frame_count, getattr(emu, "load_epoch", 0)

            data, frame, epoch = self._await(self._clock.submit_call(grab), self.call_timeout_s)
            write_file_atomic(target, data)  # disk I/O happens off the emulator thread
            return StateFileInfo(
                str(target), len(data), hashlib.sha256(data).hexdigest(), frame, epoch
            )

    def load_state(self, path: str) -> RestoreInfo:
        """Restore a ``.state`` file (validated before the core sees it)."""
        with self._exclusive_access:
            src = Path(path).expanduser()
            if src.suffix.lower() in ROM_SUFFIXES:
                raise StateLoadError(f"not a savestate path (ROM suffix): {src}")
            try:
                size = src.stat().st_size
            except OSError as exc:
                raise StateLoadError(f"cannot read savestate {src}: {exc}") from exc
            if size > self.max_state_bytes:
                raise StateLoadError(f"savestate file is implausibly large ({size} bytes)")
            data = src.read_bytes()
            self._check_alive()

            def restore(emu: Emulator) -> Tuple[int, int]:
                emu.load_state_bytes(data)
                return emu.frame_count, getattr(emu, "load_epoch", 0)

            frame, epoch = self._await(self._clock.submit_call(restore), self.call_timeout_s)
            return RestoreInfo(str(src), frame, epoch)

    # -- views and lifecycle -----------------------------------------------------

    def reader(self) -> "ReadOnlyEmulatorView":
        """A handle that can look but never touch (for API/dashboard clients)."""
        return ReadOnlyEmulatorView(self)

    def close(self, timeout: float = 5.0) -> bool:
        """Stop the emulator thread; the backend is closed only if it really
        exited.  Returns True on a clean shutdown."""
        stopped = self._clock.stop(timeout)
        if stopped:
            self._backend.close()
        return stopped

    def __enter__(self) -> "EmulatorActor":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class ReadOnlyEmulatorView:
    """Read surface of an :class:`EmulatorActor`; deliberately has no writer."""

    __slots__ = ("_actor",)

    def __init__(self, actor: EmulatorActor) -> None:
        self._actor = actor

    def read_atomic_snapshot(self) -> AtomicSnapshot:
        return self._actor.read_atomic_snapshot()

    def read_ranges(self, ranges: Sequence[tuple[int, int]]) -> AtomicRangeRead:
        return self._actor.read_ranges(ranges)

    def read_range(self, addr: int, size: int) -> bytes:
        return self._actor.read_range(addr, size)

    def read_u8(self, addr: int) -> int:
        return self._actor.read_u8(addr)

    def read_u16(self, addr: int) -> int:
        return self._actor.read_u16(addr)

    def read_u32(self, addr: int) -> int:
        return self._actor.read_u32(addr)

    def get_screen(self):
        return self._actor.get_screen()

    def get_screen_png(self) -> bytes:
        return self._actor.get_screen_png()

    def read_screen(self) -> ScreenRead:
        return self._actor.read_screen()

    @property
    def frames_run(self) -> int:
        return self._actor.frames_run

    def info(self) -> Dict[str, Any]:
        return self._actor.info()


FPS = 60.0


@dataclass(frozen=True)
class UntilOutcome:
    """Result of an ``until`` command."""

    satisfied: bool
    value: Any  # the probe's truthy return value, else None
    frames: int  # frames the command ran
    reason: str  # "satisfied" | "frame_limit" | "wall_limit"


class _Command:
    __slots__ = (
        "kind",
        "future",
        "buttons",
        "args",
        "hold",
        "wait",
        "func",
        "max_frames",
        "check_every",
        "deadline",
        "phase",
        "remaining",
        "down",
        "ran",
        "budget",
        "since_check",
    )

    def __init__(
        self,
        kind: str,
        *,
        buttons: Any = (),
        hold: int = 0,
        wait: int = 0,
        func: Optional[Callable] = None,
        max_frames: int = 0,
        check_every: int = 1,
        deadline: Optional[float] = None,
        args: tuple = (),
    ) -> None:
        self.kind = kind
        self.args = args
        self.future: concurrent.futures.Future = concurrent.futures.Future()
        self.buttons, self.hold, self.wait = buttons, hold, wait
        self.func, self.max_frames, self.check_every = func, max_frames, check_every
        self.deadline = deadline
        self.phase = "new"
        self.remaining = 0
        self.down = False
        self.ran = 0  # frames run for this command
        self.since_check = 0
        # Watchdog: a command that outlives its own frame count by this margin
        # is aborted so one bug can never hold the queue forever.
        if kind == "press":
            self.budget = hold + wait + 120
        elif kind == "wait":
            self.budget = wait + 120
        elif kind == "until":
            self.budget = max_frames + 120
        else:
            self.budget = 0


class EmuClock:
    """Owns one emulator on one thread.  Everything else submits work."""

    def __init__(
        self,
        emulator: Any,
        *,
        fps: Optional[float] = None,
        resync_frames: int = 3,
        spin_s: float = 0.0008,
        name: str = "pokegent-emu",
        timer: Callable[[], float] = time.perf_counter,
        sleeper: Callable[[float], None] = time.sleep,
        on_fatal: Optional[Callable[[BaseException], None]] = None,
        lower_switch_interval: bool = True,
        idle_frames: bool = True,
    ) -> None:
        self._emu = emulator
        self.fps = fps
        #: paced clocks only: keep ticking between commands (True, live behaviour) or run
        #: frames only while a command needs them (False, deterministic, just slower)
        self.idle_frames = idle_frames
        self.period = (1.0 / fps) if fps else 0.0
        self.resync_frames = resync_frames
        self.spin_s = spin_s
        self._timer = timer
        self._sleep = sleeper
        self._on_fatal = on_fatal
        self._lower_switch_interval = lower_switch_interval
        self._cv = threading.Condition()
        self._queue: Deque[_Command] = collections.deque()  # writer commands, FIFO
        self._reads: Deque[_Command] = collections.deque()  # read calls, boundary
        self._active: Optional[_Command] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._frame_ms: Deque[float] = collections.deque(maxlen=600)
        self.resyncs = 0
        self.ticks = 0  # frames run by this clock
        self.watchdog_aborts = 0
        self.error: Optional[BaseException] = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> "EmuClock":
        # After run_frame() returns the thread needs the GIL back; the default
        # 5 ms switch interval can delay it behind busy Python threads.  Only
        # ever lower the interval (live-system finding).
        if self._lower_switch_interval and sys.getswitchinterval() > 0.0005:
            sys.setswitchinterval(0.0005)
        self._thread.start()
        return self

    def is_alive(self) -> bool:
        return self._thread.is_alive() and not self._stop.is_set()

    def on_emulator_thread(self) -> bool:
        return threading.current_thread() is self._thread

    def stop(self, timeout: float = 5.0) -> bool:
        """Stop the thread; True once it really exited (then the emulator may
        be closed).  A thread still inside ``run_frame`` after *timeout*
        returns False: the caller must not free the core underneath it."""
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        if self._thread.is_alive() and not self.on_emulator_thread():
            self._thread.join(timeout)
        if not self._thread.is_alive():
            self._fail_pending(RuntimeError("emulator clock stopped"))
        return not self._thread.is_alive()

    # -- submission (any thread) --------------------------------------------

    def _submit(self, cmd: _Command, queue: Deque[_Command]) -> concurrent.futures.Future:
        if not self.is_alive():
            cmd.future.set_exception(RuntimeError("emulator clock is not running"))
            return cmd.future
        with self._cv:
            queue.append(cmd)
            self._cv.notify()
        return cmd.future

    def submit_press(self, buttons: Any, hold: int, wait: int = 0) -> concurrent.futures.Future:
        """Key(s) down for *hold* frames, up, then *wait* more frames."""
        return self._submit(
            _Command("press", buttons=buttons, hold=max(0, int(hold)), wait=max(0, int(wait))),
            self._queue,
        )

    def submit_wait(self, frames: int) -> concurrent.futures.Future:
        return self._submit(_Command("wait", wait=max(0, int(frames))), self._queue)

    def submit_until(
        self,
        probe: Callable[[Any], Any],
        max_frames: int,
        *,
        check_every: int = 1,
        deadline: Optional[float] = None,
    ) -> concurrent.futures.Future:
        """Run frames until ``probe(emulator)`` is truthy, *max_frames* ran, or
        the clock's wall deadline (``self._timer()`` value) passed.  Resolves
        with an :class:`UntilOutcome`.  The probe runs on the emulator thread
        and must not mutate the emulator."""
        return self._submit(
            _Command(
                "until",
                func=probe,
                max_frames=max(0, int(max_frames)),
                check_every=max(1, int(check_every)),
                deadline=deadline,
            ),
            self._queue,
        )

    def submit_call(self, func: Callable[..., Any], *args: Any) -> concurrent.futures.Future:
        """Writer call: ``func(emulator, *args)`` in FIFO order at a frame boundary."""
        cmd = _Command("call", func=func, args=args)
        return self._submit(cmd, self._queue)

    def submit_read(self, func: Callable[..., Any], *args: Any) -> concurrent.futures.Future:
        """Read call: ``func(emulator, *args)`` at the next frame boundary, ahead
        of queued writer commands.  Must not mutate the emulator."""
        cmd = _Command("read", func=func, args=args)
        return self._submit(cmd, self._reads)

    def now(self) -> float:
        return self._timer()

    def pending(self) -> int:
        return len(self._queue) + len(self._reads) + (1 if self._active is not None else 0)

    # -- metrics ------------------------------------------------------------

    def metrics(self) -> Dict[str, Any]:
        samples: List[float] = sorted(self._frame_ms)
        n = len(samples)

        def pct(q: float) -> float:
            return samples[min(n - 1, int(q * n))] if n else 0.0

        mean = (sum(samples) / n) if n else 0.0
        return {
            "frames": self.ticks,
            "frame_ms": {"p50": pct(0.5), "p99": pct(0.99), "mean": mean, "samples": n},
            "frames_per_s_while_running": (1000.0 / mean) if mean else 0.0,
            "resyncs": self.resyncs,
            "pending_commands": self.pending(),
            "watchdog_aborts": self.watchdog_aborts,
            "paced_fps": self.fps,
            "idle_frames": self.idle_frames,
        }

    # -- the thread -----------------------------------------------------------

    def _run(self) -> None:
        paced = self.fps is not None
        free_run = paced and self.idle_frames  # tick even when no command needs frames
        period = self.period
        try:
            deadline = self._timer()
            idle = False
            while not self._stop.is_set():
                self._drain_reads()
                self._start_commands()
                if self._active is None and not free_run:
                    with self._cv:
                        if not (self._queue or self._reads or self._stop.is_set()):
                            self._cv.wait(0.05)
                    idle = True
                    continue
                if paced:
                    now = self._timer()
                    if idle:
                        deadline = now  # woke from idle: a fresh run of frames
                        idle = False
                    elif now - deadline > self.resync_frames * period:
                        deadline = now  # too far behind: re-anchor, no burst
                        self.resyncs += 1
                self._frame()
                if paced:
                    deadline += period
                    # A read queued while the frame was running belongs to this frame
                    # boundary.  Service it before sleeping for the next frame; otherwise
                    # command paths that alternate ``read`` and short frame advances pay
                    # one extra frame period for every read.
                    self._drain_reads()
                    self._start_commands()
                    self._sleep_until(deadline)
        except BaseException as exc:  # noqa: BLE001 - reported, then fatal
            self.error = exc
            self._stop.set()
            self._release_keys()
            self._fail_pending(RuntimeError(f"emulator clock failed: {exc}"))
            if self._on_fatal is not None:
                self._on_fatal(exc)

    def _sleep_until(self, deadline: float) -> None:
        while not self._stop.is_set():
            remaining = deadline - self._timer()
            if remaining <= 0:
                return
            if remaining > self.spin_s:
                duration = min(remaining - self.spin_s, 0.05)
                if self._sleep is time.sleep:
                    # Reads are submitted after a command future resolves, which can be
                    # while this clock is waiting for the next frame.  Wake on the
                    # condition so the read can run at the already-completed frame
                    # boundary instead of paying a whole additional frame period.
                    with self._cv:
                        self._cv.wait(duration)
                        if self._reads:
                            self._drain_reads()
                else:
                    self._sleep(duration)
            else:
                self._sleep(0)

    def _frame(self) -> None:
        t0 = self._timer()
        self._emu.tick(1)
        self.ticks += 1
        self._frame_ms.append((self._timer() - t0) * 1000.0)
        cmd = self._active
        if cmd is not None:
            cmd.ran += 1
            self._end_frame(cmd)

    # -- calls ----------------------------------------------------------------

    def _run_call(self, cmd: _Command) -> None:
        if not cmd.future.set_running_or_notify_cancel():
            return
        try:
            cmd.future.set_result(cmd.func(self._emu, *cmd.args))  # type: ignore[misc]
        except BaseException as exc:  # noqa: BLE001 - delivered to the caller
            cmd.future.set_exception(exc)

    def _drain_reads(self) -> None:
        while True:
            try:
                cmd = self._reads.popleft()
            except IndexError:
                return
            self._run_call(cmd)

    # -- writer state machine ---------------------------------------------------

    def _start_commands(self) -> None:
        """At a frame boundary: begin queued commands until one needs frames."""
        while True:
            cmd = self._active
            if cmd is None:
                try:
                    cmd = self._queue.popleft()
                except IndexError:
                    return
                if cmd.kind == "call":
                    self._run_call(cmd)
                    continue
                if not cmd.future.set_running_or_notify_cancel():
                    continue
                self._active = cmd
                try:
                    self._start(cmd)
                except Exception as exc:  # noqa: BLE001
                    self._finish(cmd, exc=exc)
                    continue
            if cmd.phase == "done":
                self._active = None
                continue
            if cmd.ran > cmd.budget:
                self.watchdog_aborts += 1
                self._finish(
                    cmd,
                    exc=TimeoutError(f"emulator command {cmd.kind} exceeded {cmd.budget} frames"),
                )
                continue
            return

    def _start(self, cmd: _Command) -> None:
        if cmd.kind == "wait":
            cmd.phase, cmd.remaining = "wait", cmd.wait
            if cmd.remaining <= 0:
                self._finish(cmd, result=self._emu.frame_count)
        elif cmd.kind == "until":
            if self._check_until(cmd):
                return
            if cmd.max_frames <= 0:
                self._finish_until(cmd, None, "frame_limit")
            else:
                cmd.phase, cmd.remaining = "until", cmd.max_frames
        else:  # press
            if cmd.hold <= 0:
                cmd.phase, cmd.remaining = "wait", cmd.wait
                if cmd.remaining <= 0:
                    self._finish(cmd, result=self._emu.frame_count)
            else:
                self._emu.button_down(cmd.buttons)
                cmd.down = True
                cmd.phase, cmd.remaining = "hold", cmd.hold

    def _check_until(self, cmd: _Command) -> bool:
        """Evaluate the probe (and wall deadline); True when the command ended."""
        value = cmd.func(self._emu)  # type: ignore[misc]
        if value:
            self._finish_until(cmd, value, "satisfied")
            return True
        if cmd.deadline is not None and self._timer() >= cmd.deadline:
            self._finish_until(cmd, None, "wall_limit")
            return True
        return False

    def _finish_until(self, cmd: _Command, value: Any, reason: str) -> None:
        self._finish(cmd, result=UntilOutcome(reason == "satisfied", value, cmd.ran, reason))

    def _end_frame(self, cmd: _Command) -> None:
        if cmd.phase == "done":
            return
        try:
            if cmd.kind == "until":
                cmd.remaining -= 1
                cmd.since_check += 1
                if cmd.since_check >= cmd.check_every or cmd.remaining <= 0:
                    cmd.since_check = 0
                    if self._check_until(cmd):
                        return
                if cmd.remaining <= 0:
                    self._finish_until(cmd, None, "frame_limit")
                return
            cmd.remaining -= 1
            if cmd.remaining > 0:
                return
            if cmd.phase == "hold":
                cmd.down = False
                self._emu.button_up()
                cmd.phase, cmd.remaining = "wait", cmd.wait
                if cmd.remaining <= 0:
                    self._finish(cmd, result=self._emu.frame_count)
            else:
                self._finish(cmd, result=self._emu.frame_count)
        except Exception as exc:  # noqa: BLE001
            self._finish(cmd, exc=exc)

    def _release_keys(self) -> None:
        try:
            self._emu.button_up()
        except Exception:  # noqa: BLE001 - best effort on the way out
            pass

    def _finish(
        self, cmd: _Command, result: Any = None, exc: Optional[BaseException] = None
    ) -> None:
        cmd.phase = "done"
        if cmd.down:  # never leave a key held
            cmd.down = False
            self._release_keys()
        if self._active is cmd:
            self._active = None
        if not cmd.future.done():
            if exc is not None:
                cmd.future.set_exception(exc)
            else:
                cmd.future.set_result(result)

    def _fail_pending(self, exc: BaseException) -> None:
        cmd, self._active = self._active, None
        if cmd is not None and not cmd.future.done():
            cmd.future.set_exception(exc)
        for queue in (self._queue, self._reads):
            while True:
                try:
                    cmd = queue.popleft()
                except IndexError:
                    break
                if not cmd.future.done():
                    try:
                        cmd.future.set_exception(exc)
                    except concurrent.futures.InvalidStateError:
                        pass
