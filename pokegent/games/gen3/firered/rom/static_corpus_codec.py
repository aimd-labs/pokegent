"""Unused strict codec for the mandatory static-graph script corpus.

Wire format (JSON-compatible; version and record type tags are fixed)::

    corpus: {"version": 1, "type": "script_corpus", "rows": [
        {"key": INTERPRETATION_KEY, "result": INTERPRETATION,
         "behavior_reads": [[metatile, behavior_or_null], ...]}]}

Rows are decoded only through the merged interpretation-key and symbolic
interpretation codecs.  The key's exact seven-tuple is unique, its map must be
one of the admitted maps and ``result.script_ptr`` must equal the key pointer.
Reads preserve call order and duplicates; each read must equal the *raw* map's
``grid.attributes`` behavior for that metatile, and every non-null
``setmetatile`` effect ``behavior`` needs its exact ``(metatile, behavior)``
read pair.  Reads of metatiles absent from the raw attributes must be null.
Bools are refused wherever an int is expected.

This proves consistency only.  Admission never replays the interpreter and
cannot prove that a producer omitted no read; full-corpus coverage stays a
separate evidence problem.  Incomplete interpretations are preserved exactly.
Invalid input raises :class:`CodecError`, which graph admission wraps as its
owning error.  This module is not wired into the runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pokegent.games.gen3.firered.maps.map_constants import ATTR_BEHAVIOR_MASK
from pokegent.games.gen3.firered.maps.map_schema import MapState
from pokegent.games.gen3.firered.rom.script_effects import Interpretation
from pokegent.games.gen3.firered.rom.static_artifact_loader import MAX_INTERPRETATIONS
from pokegent.games.gen3.firered.rom.static_map_codec import decode_interpretation_key
from pokegent.games.gen3.firered.rom.static_symbolic_codec import (
    MAX_CONTAINER_LENGTH,
    MAX_INT_BITS,
    CodecError,
    decode_interpretation,
)

__all__ = ("CorpusRow", "decode_corpus")

_CORPUS_KEYS = frozenset({"version", "type", "rows"})
_ROW_KEYS = frozenset({"key", "result", "behavior_reads"})
_MapKey = tuple[int, int]


@dataclass(frozen=True)
class CorpusRow:
    """One decoded corpus row: exact key, owning result and ordered raw reads."""

    key: tuple
    result: Interpretation
    behavior_reads: tuple[tuple[int, int | None], ...]

    def __post_init__(self) -> None:
        if type(self.key) is not tuple or len(self.key) != 7:
            raise CodecError("corpus key must be an exact decoded seven-tuple")
        if type(self.result) is not Interpretation:
            raise CodecError("corpus result must be an exact Interpretation")
        if type(self.behavior_reads) is not tuple:
            raise CodecError("corpus behavior_reads must be an exact tuple")
        for read in self.behavior_reads:
            if type(read) is not tuple or len(read) != 2:
                raise CodecError("corpus behavior read must be an exact pair")
            metatile, behavior = read
            _exact_int(metatile, "corpus metatile")
            if behavior is not None:
                _exact_int(behavior, "corpus behavior")


def _exact_int(value: Any, label: str) -> int:
    if type(value) is not int:
        raise CodecError(f"{label} must be an exact int")
    if value.bit_length() > MAX_INT_BITS:
        raise CodecError(f"{label} exceeds the integer bit bound")
    return value


def _exact_keys(wire: Any, expected: frozenset[str], label: str) -> None:
    if type(wire) is not dict or len(wire) != len(expected):
        raise CodecError(f"{label} must be an exact mapping")
    for key in wire:
        if type(key) is not str or key not in expected:
            raise CodecError(f"{label} keys mismatch")


def _decode_reads(wire: Any, index: int) -> tuple[tuple[int, int | None], ...]:
    if type(wire) is not list:
        raise CodecError(f"corpus row {index} behavior_reads must be an exact array")
    if len(wire) > MAX_CONTAINER_LENGTH:
        raise CodecError(f"corpus row {index} behavior_reads exceed their length bound")
    reads = []
    for read in wire:
        if type(read) is not list or len(read) != 2:
            raise CodecError(f"corpus row {index} read must be an exact two-element array")
        metatile = _exact_int(read[0], "corpus metatile")
        behavior = read[1]
        if behavior is not None:
            behavior = _exact_int(behavior, "corpus behavior")
        reads.append((metatile, behavior))
    return tuple(reads)


def _raw_behaviors(state: MapState) -> dict[int, int]:
    return dict(state.grid.attributes)


def _check_reads(row: CorpusRow, index: int, raw: dict[int, int]) -> None:
    for metatile, behavior in row.behavior_reads:
        expected = None if metatile not in raw else raw[metatile] & ATTR_BEHAVIOR_MASK
        if behavior != expected:
            raise CodecError(
                f"corpus row {index} behavior read for metatile {metatile} "
                "contradicts the raw map attributes"
            )
    for effect in row.result.effects:
        if effect.kind != "setmetatile":
            continue
        behavior = effect.value("behavior")
        if behavior is None:
            continue
        metatile = effect.value("metatile")
        if type(metatile) is not int or type(behavior) is not int:
            raise CodecError(f"corpus row {index} setmetatile values must be exact ints")
        if (metatile, behavior) not in row.behavior_reads:
            raise CodecError(f"corpus row {index} setmetatile behavior lacks its exact tile read")


def decode_corpus(wire: Any, maps: tuple[MapState, ...]) -> tuple[CorpusRow, ...]:
    """Decode the mandatory corpus and prove read/effect consistency."""
    if type(maps) is not tuple:
        raise CodecError("corpus maps must be an exact tuple")
    map_by_key: dict[_MapKey, MapState] = {
        (state.identity.group, state.identity.number): state for state in maps
    }
    _exact_keys(wire, _CORPUS_KEYS, "script corpus")
    if type(wire["version"]) is not int or wire["version"] != 1:
        raise CodecError("script corpus version must be exact int 1")
    if type(wire["type"]) is not str or wire["type"] != "script_corpus":
        raise CodecError('script corpus type must be exact str "script_corpus"')
    raw_rows = wire["rows"]
    if type(raw_rows) is not list:
        raise CodecError("script corpus rows must be an exact array")
    if len(raw_rows) > MAX_INTERPRETATIONS:
        raise CodecError("script corpus row count exceeds its bound")
    rows: list[CorpusRow] = []
    keys: set[tuple] = set()
    for index, raw_row in enumerate(raw_rows):
        _exact_keys(raw_row, _ROW_KEYS, f"corpus row {index}")
        try:
            key = decode_interpretation_key(raw_row["key"])
            result = decode_interpretation(raw_row["result"])
        except CodecError as exc:
            raise CodecError(f"corpus row {index} refused: {exc}") from None
        if key in keys:
            raise CodecError(f"corpus row {index} duplicates an interpretation key")
        keys.add(key)
        state = map_by_key.get(key[1])
        if state is None:
            raise CodecError(f"corpus row {index} map is absent from the admitted maps")
        if result.script_ptr != key[0]:
            raise CodecError(f"corpus row {index} result pointer does not match its key")
        row = CorpusRow(key, result, _decode_reads(raw_row["behavior_reads"], index))
        _check_reads(row, index, _raw_behaviors(state))
        rows.append(row)
    return tuple(rows)
