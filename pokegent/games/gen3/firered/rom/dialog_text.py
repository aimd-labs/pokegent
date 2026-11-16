"""Dialog text: decode the message the game is showing, bounded, never executed.

Source of truth is the text buffer ``gStringVar4`` (pret ``src/strings.c`` / ``include/strings.h``,
a ``u8[1000]`` in EWRAM): ``ShowFieldMessage`` expands a script string into it
(``StringExpandPlaceholders``) and the field message printer (window 0) walks it with
``printerTemplate.currentChar``. The buffer keeps its content after the box is gone, so a decoded
string is only *evidence* when the printer state proves it is the string that belongs to what is on
screen (``fresh``):

* an active printer's ``currentChar`` points into ``[buf, buf + eos]``: that string is printing;
* no active printer, but the (stale, finished) printer struct of window 0 or 1 ends exactly one byte
  behind this string's terminator: the last thing that window printed is this string.

Anything else is stale and is NOT returned (a stale string could make a destructive prompt look
harmless). Checked on the 55 audit fixtures and the 1281-state corpus
(the runtime contracts).

Address of ``gStringVar4``: not part of the ROM profile (reader branch). It was found with the
printers of the fixtures (``currentChar`` of the message printer lies inside the buffer) and then
decoded as clean English in every dialog state of the corpus; the table below is family-local and
profile-bound. If the profile gains the symbol it wins.

The decoder is bytes -> plain text only. It expands no placeholder, runs no control code and its
output is never used as an input, an address or an executor argument.
"""

from __future__ import annotations

import hashlib
import struct
from collections import OrderedDict
from dataclasses import dataclass

from pokegent.games.gen3.firered.reader.memory_port import MemoryPort, MemoryReadError
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.games.gen3.firered.reader.ui_schemas import UiEvidence
from pokegent.games.gen3.firered.reader.ui_symbols import TEXT_PRINTER_SIZE, TP_CURRENT_CHAR
from pokegent.games.gen3.firered.rom.rom_data import RomProfile
from pokegent.games.gen3.firered.rom.text import EOS, NEWLINE, UNKNOWN_CHAR, WESTERN_CHARMAP

#: ``u8 gStringVar4[1000]``
TEXT_BUFFER_SIZE = 1000
#: family-local, profile-bound (see module docstring). ``gStringVar4`` in the profile wins.
TEXT_BUFFER_ADDRESS = {"firered-us-rev0": 0x02021D18}

MAX_TEXT_CHARS = 480  # model-visible text is bounded
MAX_PAGES = 8

# include/characters.h control bytes
CTRL_SCROLL = 0xFA  # {PROMPT_SCROLL}: wait for A/B, then scroll one line
CTRL_PARAGRAPH = 0xFB  # {PROMPT_CLEAR}: wait for A/B, clear the box
CTRL_EXT = 0xFC  # EXT_CTRL_CODE_BEGIN: code byte + arguments
CTRL_PLACEHOLDER = 0xFD  # {PLAYER} & co. in unexpanded ROM strings: code byte follows

# include/characters.h EXT_CTRL_CODE_* -> number of argument bytes after the code byte
_EXT_ARGS = {
    0x01: 1,  # COLOR
    0x02: 1,  # HIGHLIGHT
    0x03: 1,  # SHADOW
    0x04: 3,  # COLOR_HIGHLIGHT_SHADOW
    0x05: 1,  # PALETTE
    0x06: 1,  # FONT
    0x07: 0,  # RESET_FONT
    0x08: 1,  # PAUSE
    0x09: 0,  # PAUSE_UNTIL_PRESS
    0x0A: 0,  # WAIT_SE
    0x0B: 2,  # PLAY_BGM
    0x0C: 1,  # ESCAPE
    0x0D: 1,  # SHIFT_TEXT
    0x0E: 1,  # SHIFT_DOWN
    0x0F: 0,  # FILL_WINDOW
    0x10: 2,  # PLAY_SE
    0x11: 1,  # CLEAR
    0x12: 1,  # SKIP
    0x13: 1,  # CLEAR_TO
    0x14: 1,  # MIN_LETTER_SPACING
    0x15: 0,  # JPN
    0x16: 0,  # ENG
    0x17: 0,  # PAUSE_MUSIC
    0x18: 0,  # RESUME_MUSIC
}


@dataclass(frozen=True)
class DecodedText:
    """A decoded message. ``pages`` are separated by ``{PROMPT_CLEAR}`` / ``{PROMPT_SCROLL}``."""

    pages: tuple[str, ...]
    page_indices: tuple[int, ...] = ()
    truncated: bool = False
    unknown_bytes: int = 0
    scroll_prompt_count: int = 0

    @property
    def text(self) -> str:
        return " ".join(self.pages)

    @property
    def last_page(self) -> str:
        return self.pages[-1] if self.pages else ""


def decode_message(raw: bytes) -> DecodedText | None:
    """Decode up to the terminator. ``None`` if there is no terminator in ``raw``.

    Line breaks become spaces, ``{PROMPT_*}`` start a new page, extended control codes are skipped
    with their arguments, unexpanded placeholders become ``<name>``, unknown bytes ``?``.
    """
    pages: list[tuple[int, str]] = []
    page_number = 0
    cur: list[str] = []
    unknown = 0
    scroll_prompts = 0
    i, n = 0, len(raw)
    ended = False
    while i < n:
        b = raw[i]
        i += 1
        if b == EOS:
            ended = True
            break
        if b in (NEWLINE, CTRL_SCROLL):
            if b == CTRL_SCROLL:
                scroll_prompts += 1
            cur.append(" ")  # line break / scroll prompt inside one page
        elif b == CTRL_PARAGRAPH:
            pages.append((page_number, "".join(cur)))
            page_number += 1
            cur = []
        elif b == CTRL_EXT:
            if i >= n:
                break
            code = raw[i]
            i += 1 + _EXT_ARGS.get(code, 0)
        elif b == CTRL_PLACEHOLDER:
            i += 1
            cur.append("<name>")
        else:
            ch = WESTERN_CHARMAP.get(b)
            if ch is None:
                unknown += 1
                ch = UNKNOWN_CHAR
            cur.append(ch)
    if not ended:
        return None
    pages.append((page_number, "".join(cur)))
    pages = [(index, " ".join(page.split())) for index, page in pages]
    pages = [(index, page) for index, page in pages if page]
    truncated = False
    if len(pages) > MAX_PAGES:
        pages = pages[-MAX_PAGES:]
        truncated = True
    total = 0
    bounded: list[str] = []
    bounded_indices: list[int] = []
    for page_index, page in pages:
        room = MAX_TEXT_CHARS - total
        if room <= 0:
            truncated = True
            break
        if len(page) > room:
            page = page[: max(0, room - 1)] + "…"
            truncated = True
        bounded.append(page)
        bounded_indices.append(page_index)
        total += len(page) + 1
    return DecodedText(
        tuple(bounded),
        page_indices=tuple(bounded_indices),
        truncated=truncated,
        unknown_bytes=unknown,
        scroll_prompt_count=scroll_prompts,
    )


@dataclass(frozen=True)
class DialogText:
    """The message of the box on screen, proven current by the printer state."""

    pages: tuple[str, ...]
    source: str  # "printing" | "message_printer" | "window1_printer"
    #: Original message-page indices retained by the bounded decoder.
    page_indices: tuple[int, ...] = ()
    truncated: bool = False
    #: number of ``{PROMPT_CLEAR}`` codes the printer has passed: which page of the message the
    #: box is on. Part of the frame fingerprint, so a long message is not one "attempt".
    page_index: int = 0
    #: number of blocking ``{PROMPT_CLEAR}`` and ``{PROMPT_SCROLL}`` codes passed. Unlike the
    #: character pointer, this changes only when the game reaches another input wait, so long
    #: scrolling messages make semantic progress without turning ordinary printing into churn.
    prompt_index: int = 0
    #: Total ``{PROMPT_SCROLL}`` controls in the decoded message.
    scroll_prompt_count: int = 0

    @property
    def text(self) -> str:
        return " ".join(self.pages)

    @property
    def last_page(self) -> str:
        return self.pages[-1] if self.pages else ""

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]


def text_buffer_address(profile: RomProfile) -> int | None:
    entry = profile.symbols.get("gStringVar4")
    if entry is not None:
        return entry.address
    return TEXT_BUFFER_ADDRESS.get(profile.profile_id)


def read_dialog_text(port: MemoryPort, profile: RomProfile, ui: UiEvidence) -> DialogText | None:
    """The current message, or ``None`` if there is none or it cannot be proven current."""
    if not ui.valid or not ui.text.valid:
        return None
    buf = text_buffer_address(profile)
    printers_base = profile.symbols.get("sTextPrinters")
    if buf is None or printers_base is None:
        return None
    try:
        raw = port.read_range(buf, TEXT_BUFFER_SIZE)
    except MemoryReadError:
        return None
    eos = raw.find(bytes([EOS]))
    if eos < 0:
        return None
    end_ptr = buf + eos + 1
    decoded = decode_message(raw[: eos + 1])
    if decoded is None or not decoded.pages or decoded.unknown_bytes > 2:
        return None

    source = None
    page_index = raw[:eos].count(bytes([CTRL_PARAGRAPH]))  # a finished message: on its last page
    prompt_index = sum(byte in (CTRL_PARAGRAPH, CTRL_SCROLL) for byte in raw[:eos])
    for p in ui.text.printers:
        if buf <= p.current_char_ptr <= end_ptr:
            source = "printing"
            consumed = raw[: p.current_char_ptr - buf]
            page_index = consumed.count(bytes([CTRL_PARAGRAPH]))
            prompt_index = sum(byte in (CTRL_PARAGRAPH, CTRL_SCROLL) for byte in consumed)
            break
    if source is None:
        for window, name in ((0, "message_printer"), (1, "window1_printer")):
            try:
                at = printers_base.address + window * TEXT_PRINTER_SIZE + TP_CURRENT_CHAR
                ptr = struct.unpack("<I", port.read_range(at, 4))[0]
            except MemoryReadError:
                continue
            if ptr == end_ptr:
                source = name
                break
    if source is None:
        return None
    return DialogText(
        decoded.pages,
        source=source,
        page_indices=decoded.page_indices,
        truncated=decoded.truncated,
        page_index=page_index,
        prompt_index=prompt_index,
        scroll_prompt_count=decoded.scroll_prompt_count,
    )


# -- sidecar ---------------------------------------------------------------------------------


class TextSidecar:
    """Bounded side table ``snapshot -> DialogText``.

    The frozen contract hands detectors and builders a ``StateSnapshot`` and nothing else, and the
    snapshot carries no text. The snapshot source of this family reads the text from the SAME atomic
    snapshot and files it here under the snapshot's identity; detectors and builders look it up.
    A snapshot that was not read through the source simply has no text (the generic path), which is
    also what a replay from a bare snapshot sees. The key is ``(snapshot_seq, state_hash)``.
    """

    def __init__(self, maxlen: int = 64) -> None:
        self._items: OrderedDict[tuple[int, str], DialogText | None] = OrderedDict()
        self._maxlen = maxlen

    @staticmethod
    def key(snapshot: StateSnapshot) -> tuple[int, str]:
        return (snapshot.snapshot_seq, snapshot.state_hash)

    def put(self, snapshot: StateSnapshot, text: DialogText | None) -> None:
        self._items[self.key(snapshot)] = text
        self._items.move_to_end(self.key(snapshot))
        while len(self._items) > self._maxlen:
            self._items.popitem(last=False)

    def get(self, snapshot: StateSnapshot) -> DialogText | None:
        return self._items.get(self.key(snapshot))

    def identity(self, snapshot: StateSnapshot) -> tuple[str, int] | None:
        """Which page of which message is on the box: ``(text digest, page index)`` or None."""
        text = self.get(snapshot)
        return (text.digest, text.page_index) if text is not None else None

    def __len__(self) -> int:
        return len(self._items)
