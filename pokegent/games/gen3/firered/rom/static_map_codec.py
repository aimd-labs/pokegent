"""Unused strict codec for a static MapState and the exact interpretation key.

Wire format (JSON-compatible; version and record type tags are fixed)::

    map: {"version": 1, "type": "static_map", "map": MAP}
    key: {"version": 1, "type": "interpretation_key", "pointer": int,
          "map": [group, number], "initial_variables": [[var, value]...],
          "player_xy": [x, y] | null, "minimize_guards": bool,
          "guards": [guard...], "ferry_mode": bool}

MAP is the owning :class:`MapState.model_dump(mode="json")` body, not a copied
schema.  Decoding validates that exact body through the owning strict Pydantic
model and requires an exact, type-aware roundtrip: a silently inserted default,
an unknown key or a bool/int swap refuses.  JSON arrays are the expected
transport for owning tuples; the decoded value is a fresh owning instance whose
tuples and enums are the source types.  ``MapGrid.attribute_map`` and
``MapState._tile_cache`` are runtime caches and never appear on the wire.

Only static states from ``MapReader.read_static`` are accepted: no player, no
live/spawned objects, no live source, no read event flags or vars, the default
view and no seen tiles.  The codec preserves every stored fact (including
template-derived fields, knowledge, raw destinations and duplicates) and never
rewrites it from guessed RAM defaults.  The interpretation key is the exact
seven tuple used by ``NavigationGraph._script_interpretation``; order and
duplicate variables are preserved, never sorted or normalised, and no ROM
address validity is invented.  This module is not wired into the runtime.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from pokegent.games.gen3.firered.maps.map_schema import MapState, ViewWindow
from pokegent.games.gen3.firered.rom.script_effects import Guard
from pokegent.games.gen3.firered.rom.static_artifact_loader import (
    ALLOWED_OPERATORS,
    MAX_JSON_DEPTH,
    MAX_JSON_TOKENS,
    PAYLOAD_MAX_BYTES,
    ArtifactLoadError,
    _preflight,
)
from pokegent.games.gen3.firered.rom.static_symbolic_codec import (
    MAX_CONTAINER_LENGTH,
    MAX_INT_BITS,
    MAX_STRING_BYTES,
    CodecError,
    decode_guard,
    encode_guard,
)

CODEC_VERSION = 1

_MAP_KEYS = frozenset({"version", "type", "map"})
_KEY_KEYS = frozenset(
    {
        "version",
        "type",
        "pointer",
        "map",
        "initial_variables",
        "player_xy",
        "minimize_guards",
        "guards",
        "ferry_mode",
    }
)


class _Budget:
    __slots__ = ("nodes", "text_bytes", "encoded_bytes")

    def __init__(self) -> None:
        self.nodes = 0
        self.text_bytes = 0
        self.encoded_bytes = 0

    def node(self, depth: int) -> None:
        if depth > MAX_JSON_DEPTH:
            raise CodecError("wire nesting exceeds its bound")
        self.nodes += 1
        if self.nodes > MAX_JSON_TOKENS:
            raise CodecError("wire node count exceeds its bound")

    def encoded(self, size: int) -> None:
        self.encoded_bytes += size
        if self.encoded_bytes > PAYLOAD_MAX_BYTES:
            raise CodecError("wire encoded bytes exceed their bound")

    def text(self, value: str) -> None:
        if len(value) > MAX_STRING_BYTES:
            raise CodecError("wire string exceeds its byte bound")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise CodecError("wire string is not valid UTF-8") from None
        if size > MAX_STRING_BYTES:
            raise CodecError("wire string exceeds its byte bound")
        self.text_bytes += size
        if self.text_bytes > PAYLOAD_MAX_BYTES:
            raise CodecError("wire text exceeds its byte bound")


def _int(value: Any, *, label: str = "int") -> int:
    if type(value) is not int:
        raise CodecError(f"{label} must be exact int")
    if value.bit_length() > MAX_INT_BITS:
        raise CodecError(f"{label} exceeds the integer bit bound")
    return value


def _bool(value: Any, *, label: str = "bool") -> bool:
    if type(value) is not bool:
        raise CodecError(f"{label} must be exact bool")
    return value


def _encoded_string(value: str) -> int:
    """Exact JSON-encoded size of a string, including quotes and escapes."""
    return len(json.dumps(value))


def _preflight_wire(value: Any, depth: int = 0, budget: _Budget | None = None) -> None:
    """Bound the Python wire algebra and encoded bytes before json.dumps or a model parse."""
    budget = budget if budget is not None else _Budget()
    budget.node(depth)
    kind = type(value)
    if value is None:
        budget.encoded(4)
        return
    if kind is bool:
        budget.encoded(5)
        return
    if kind is int:
        _int(value, label="wire int")
        budget.encoded(len(str(value)))
        return
    if kind is str:
        budget.text(value)
        budget.encoded(_encoded_string(value))
        return
    if kind is list:
        if len(value) > MAX_CONTAINER_LENGTH:
            raise CodecError("wire array exceeds its length bound")
        budget.encoded(2 + max(0, len(value) - 1))
        for item in value:
            _preflight_wire(item, depth + 1, budget)
        return
    if kind is dict:
        if len(value) > MAX_CONTAINER_LENGTH:
            raise CodecError("wire object exceeds its key bound")
        budget.encoded(2 + max(0, len(value) - 1))
        for key, item in value.items():
            if type(key) is not str:
                raise CodecError("wire object keys must be exact str")
            budget.text(key)
            budget.encoded(_encoded_string(key) + 1)
            _preflight_wire(item, depth + 1, budget)
        return
    raise CodecError("wire value must be None/str/int/bool/list/dict")


def _dumps(value: Any) -> str:
    try:
        return json.dumps(value, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise CodecError("wire value is not canonical JSON") from exc


def _serialize(value: Any) -> str:
    _preflight_wire(value)
    text = _dumps(value)
    if len(text.encode("utf-8")) > PAYLOAD_MAX_BYTES:
        raise CodecError("wire document exceeds its byte bound")
    try:
        _preflight(text)
    except ArtifactLoadError as exc:
        raise CodecError(f"wire document exceeds its bounds: {exc}") from None
    return text


def _exact_equal(left: Any, right: Any) -> bool:
    """Type-aware equality: True never equals 1 and tuples never equal lists."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _exact_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(
            _exact_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def _framing(wire: Any, kind: str, keys: frozenset[str]) -> None:
    if type(wire) is not dict:
        raise CodecError("record must be an exact mapping")
    if len(wire) != len(keys):
        raise CodecError("record keys mismatch")
    for key in wire:
        if type(key) is not str or key not in keys:
            raise CodecError("record keys mismatch")
    if type(wire["version"]) is not int or wire["version"] != CODEC_VERSION:
        raise CodecError("unsupported codec version")
    if type(wire["type"]) is not str or wire["type"] != kind:
        raise CodecError("record type mismatch")


def _owning_state(state: Any) -> MapState:
    """Re-validate the owning model natively before JSON-mode normalisation."""
    if type(state) is not MapState:
        raise CodecError("expected an exact MapState")
    try:
        dumped = state.model_dump(mode="python", warnings="error")
        return MapState.model_validate(dumped, strict=True)
    except (ValidationError, ValueError) as exc:
        raise CodecError(f"static map is not a valid owning MapState: {exc}") from None


def _require_static(state: Any) -> MapState:
    if type(state) is not MapState:
        raise CodecError("expected an exact MapState")
    if state.player is not None:
        raise CodecError("static map must not carry a player")
    if type(state.seen_tiles) is not int or state.seen_tiles != 0:
        raise CodecError("static map must not carry seen tiles")
    if state.view != ViewWindow():
        raise CodecError("static map must use the default view")
    if state.strength_active is not None or state.flash_level is not None:
        raise CodecError("static map must not carry read save facts")
    for coord in state.coord_events:
        if coord.active is not None:
            raise CodecError("static coordinate events must not be active")
    for bg in state.bg_events:
        if bg.hidden_item is not None and bg.hidden_item.flag_set is not None:
            raise CodecError("static hidden items must not carry flag state")
    for obj in state.objects:
        if obj.live is not None or obj.source != "template":
            raise CodecError("static map must not carry live or spawned objects")
        if obj.template_graphics_id is None:
            raise CodecError("static objects must carry the template graphics id")
        if obj.hidden_flag_set is not None:
            raise CodecError("static objects must not carry read flag state")
    return state


def encode_static_map(state: MapState) -> dict[str, Any]:
    """Encode one exact static owning MapState; no cache/runtime state is persisted."""
    state = _owning_state(state)
    _require_static(state)
    payload = state.model_dump(mode="json")
    _serialize(payload)
    return {"version": CODEC_VERSION, "type": "static_map", "map": payload}


def decode_static_map(wire: Any) -> MapState:
    """Strictly decode a static map into a fresh, isolated owning instance."""
    _framing(wire, "static_map", _MAP_KEYS)
    payload = wire["map"]
    text = _serialize(payload)
    try:
        state = MapState.model_validate_json(text, strict=True)
    except (ValidationError, ValueError) as exc:
        raise CodecError(f"static map body refused: {exc}") from None
    if not _exact_equal(state.model_dump(mode="json"), payload):
        raise CodecError("static map roundtrip changed the encoded field set or types")
    return _require_static(state)


def _key_parts(key: Any) -> tuple[int, tuple[int, int], tuple, tuple | None, bool, tuple, bool]:
    if type(key) is not tuple or len(key) != 7:
        raise CodecError("interpretation key must be an exact 7-tuple")
    pointer, map_pair, initial, player_xy, minimize, guards, ferry = key
    _int(pointer, label="pointer")
    if type(map_pair) is not tuple or len(map_pair) != 2:
        raise CodecError("key map must be an exact (group, number) tuple")
    map_pair = (_int(map_pair[0], label="map group"), _int(map_pair[1], label="map number"))
    if type(initial) is not tuple:
        raise CodecError("key initial_variables must be an exact tuple")
    if len(initial) > MAX_CONTAINER_LENGTH:
        raise CodecError("key initial_variables exceed their length bound")
    variables = []
    for pair in initial:
        if type(pair) is not tuple or len(pair) != 2:
            raise CodecError("key initial_variables entries must be exact (var, value) tuples")
        variables.append(
            (_int(pair[0], label="initial var"), _int(pair[1], label="initial value"))
        )
    if player_xy is not None:
        if type(player_xy) is not tuple or len(player_xy) != 2:
            raise CodecError("key player_xy must be an exact (x, y) tuple or None")
        player_xy = (_int(player_xy[0], label="player x"), _int(player_xy[1], label="player y"))
    minimize = _bool(minimize, label="minimize_guards")
    if type(guards) is not tuple:
        raise CodecError("key guards must be an exact tuple")
    if len(guards) > MAX_CONTAINER_LENGTH:
        raise CodecError("key guards exceed their length bound")
    for guard in guards:
        if type(guard) is not Guard:
            raise CodecError("key guards must be exact Guard values")
        if guard.operator not in ALLOWED_OPERATORS:
            raise CodecError("key guard uses an unsupported operator")
    ferry = _bool(ferry, label="ferry_mode")
    return pointer, map_pair, tuple(variables), player_xy, minimize, guards, ferry


def encode_interpretation_key(key: Any) -> dict[str, Any]:
    """Encode the exact interpretation key without sorting or deduplicating it."""
    pointer, map_pair, initial, player_xy, minimize, guards, ferry = _key_parts(key)
    wire = {
        "version": CODEC_VERSION,
        "type": "interpretation_key",
        "pointer": pointer,
        "map": list(map_pair),
        "initial_variables": [list(pair) for pair in initial],
        "player_xy": None if player_xy is None else list(player_xy),
        "minimize_guards": minimize,
        "guards": [encode_guard(guard) for guard in guards],
        "ferry_mode": ferry,
    }
    _serialize(wire)
    return wire


def decode_interpretation_key(wire: Any) -> tuple:
    """Strictly decode the exact seven-tuple; transport arrays become tuples."""
    _framing(wire, "interpretation_key", _KEY_KEYS)
    _serialize(wire)
    pointer = _int(wire["pointer"], label="pointer")
    raw_map = wire["map"]
    if type(raw_map) is not list or len(raw_map) != 2:
        raise CodecError("encoded key map must be an exact two-element array")
    map_pair = (
        _int(raw_map[0], label="map group"),
        _int(raw_map[1], label="map number"),
    )
    raw_initial = wire["initial_variables"]
    if type(raw_initial) is not list:
        raise CodecError("encoded initial_variables must be an exact array")
    if len(raw_initial) > MAX_CONTAINER_LENGTH:
        raise CodecError("encoded initial_variables exceed their length bound")
    initial = []
    for pair in raw_initial:
        if type(pair) is not list or len(pair) != 2:
            raise CodecError("encoded initial_variables entries must be exact arrays")
        initial.append(
            (_int(pair[0], label="initial var"), _int(pair[1], label="initial value"))
        )
    raw_player = wire["player_xy"]
    player_xy = None
    if raw_player is not None:
        if type(raw_player) is not list or len(raw_player) != 2:
            raise CodecError("encoded player_xy must be an exact two-element array or null")
        player_xy = (
            _int(raw_player[0], label="player x"),
            _int(raw_player[1], label="player y"),
        )
    minimize = _bool(wire["minimize_guards"], label="minimize_guards")
    raw_guards = wire["guards"]
    if type(raw_guards) is not list:
        raise CodecError("encoded guards must be an exact array")
    if len(raw_guards) > MAX_CONTAINER_LENGTH:
        raise CodecError("encoded guards exceed their length bound")
    guards = []
    for raw in raw_guards:
        guard = decode_guard(raw)
        if guard.operator not in ALLOWED_OPERATORS:
            raise CodecError("encoded guard uses an unsupported operator")
        guards.append(guard)
    ferry = _bool(wire["ferry_mode"], label="ferry_mode")
    return (
        pointer,
        map_pair,
        tuple(initial),
        player_xy,
        minimize,
        tuple(guards),
        ferry,
    )
