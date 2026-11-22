"""Pure bounded symbolic codec for script Guard, Effect and Interpretation values.

Wire format (JSON-compatible; version and record type tags are fixed)::

    guard:          {"version": 1, "type": "guard", "kind": str, "subject": int,
                     "operator": str, "value": VALUE, "origin": VALUE|null}
    effect:         {"version": 1, "type": "effect", "kind": str, "source": int,
                     "guards": [guard...], "values": [VALUE(pair)...],
                     "decoded": bool, "reason": str|null}
    interpretation: {"version": 1, "type": "interpretation", "script_ptr": int,
                     "effects": [effect...], "unknown": [str...], "commands": int}

VALUE is one of ``{"t": "none", "v": null}``, ``{"t": "bool", "v": bool}``,
``{"t": "int", "v": int}``, ``{"t": "str", "v": str}``,
``{"t": "tuple", "v": [VALUE...]}`` or ``{"t": "list", "v": [VALUE...]}``.
Tags keep bool/int and tuple/list distinct and keep the value algebra closed;
all other Python types refuse. Framing keys, field names and tags carry no
logical node, depth or string budget; data strings do. Limits are admission
limits for a future cache, not semantic limits, so encode and decode refuse
rather than truncate. This module is not used by the runtime.
"""

from __future__ import annotations

from typing import Any, get_args

from pokegent.games.gen3.firered.rom.script_effects import (
    Effect,
    Guard,
    GuardKind,
    Interpretation,
)

CODEC_VERSION = 1
MAX_DEPTH = 32
MAX_NODES = 100_000
MAX_CONTAINER_LENGTH = 10_000
MAX_STRING_BYTES = 65_536
MAX_CUMULATIVE_TEXT_BYTES = 4 * 1024 * 1024
MAX_INT_BITS = 256

_VALUE_KEYS = frozenset({"t", "v"})
_VALUE_TAGS = frozenset({"none", "bool", "int", "str", "tuple", "list"})
_GUARD_KINDS = frozenset(get_args(GuardKind))
_GUARD_KEYS = frozenset({"version", "type", "kind", "subject", "operator", "value", "origin"})
_EFFECT_KEYS = frozenset(
    {"version", "type", "kind", "source", "guards", "values", "decoded", "reason"}
)
_INTERPRETATION_KEYS = frozenset(
    {"version", "type", "script_ptr", "effects", "unknown", "commands"}
)


class CodecError(ValueError):
    """A symbolic value is malformed, unsupported or over an admission limit."""


class _Budget:
    __slots__ = ("nodes", "text_bytes")

    def __init__(self) -> None:
        self.nodes = 0
        self.text_bytes = 0

    def node(self, depth: int) -> None:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise CodecError("maximum logical node count exceeded")
        if depth > MAX_DEPTH:
            raise CodecError("maximum logical depth exceeded")


def _require_int(value: Any, *, nonnegative: bool = False) -> int:
    if type(value) is not int:
        raise CodecError("expected exact int")
    if value.bit_length() > MAX_INT_BITS:
        raise CodecError("integer exceeds bit limit")
    if nonnegative and value < 0:
        raise CodecError("expected nonnegative int")
    return value


def _require_text(value: Any, budget: _Budget) -> str:
    if type(value) is not str:
        raise CodecError("expected exact str")
    if len(value) > MAX_STRING_BYTES:
        raise CodecError("string exceeds byte limit")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise CodecError("string is not valid UTF-8") from None
    if size > MAX_STRING_BYTES:
        raise CodecError("string exceeds byte limit")
    budget.text_bytes += size
    if budget.text_bytes > MAX_CUMULATIVE_TEXT_BYTES:
        raise CodecError("cumulative string bytes exceeded")
    return value


def _require_length(value: Any) -> None:
    if len(value) > MAX_CONTAINER_LENGTH:
        raise CodecError("container length exceeded")


def _text_field(
    value: Any, depth: int, budget: _Budget, *, allowed: frozenset[str] | None = None
) -> str:
    budget.node(depth)
    if type(value) is not str:
        raise CodecError("expected exact str")
    if allowed is not None and value not in allowed:
        raise CodecError("unsupported symbolic kind")
    return _require_text(value, budget)


def _int_field(value: Any, depth: int, budget: _Budget) -> int:
    budget.node(depth)
    return _require_int(value, nonnegative=True)


def _bool_field(value: Any, depth: int, budget: _Budget) -> bool:
    budget.node(depth)
    if type(value) is not bool:
        raise CodecError("expected exact bool")
    return value


def _text_or_none(value: Any, depth: int, budget: _Budget) -> str | None:
    budget.node(depth)
    if value is None:
        return None
    return _require_text(value, budget)


def _encode_value(value: Any, depth: int, budget: _Budget) -> dict[str, Any]:
    budget.node(depth)
    if value is None:
        return {"t": "none", "v": None}
    if type(value) is bool:
        return {"t": "bool", "v": value}
    if type(value) is int:
        return {"t": "int", "v": _require_int(value)}
    if type(value) is str:
        return {"t": "str", "v": _require_text(value, budget)}
    if type(value) is tuple or type(value) is list:
        _require_length(value)
        tag = "tuple" if type(value) is tuple else "list"
        return {"t": tag, "v": [_encode_value(item, depth + 1, budget) for item in value]}
    raise CodecError("unsupported value type")


def _decode_value(value: Any, depth: int, budget: _Budget) -> Any:
    if type(value) is not dict:
        raise CodecError("expected tagged value mapping")
    if len(value) != len(_VALUE_KEYS):
        raise CodecError("tagged value keys mismatch")
    for key in value:
        if type(key) is not str or key not in _VALUE_KEYS:
            raise CodecError("tagged value keys mismatch")
    tag = value["t"]
    payload = value["v"]
    if type(tag) is not str or tag not in _VALUE_TAGS:
        raise CodecError("unknown value tag")
    budget.node(depth)
    if tag == "none":
        if payload is not None:
            raise CodecError("none payload must be None")
        return None
    if tag == "bool":
        if type(payload) is not bool:
            raise CodecError("bool payload must be exact bool")
        return payload
    if tag == "int":
        return _require_int(payload)
    if tag == "str":
        return _require_text(payload, budget)
    if type(payload) is not list:
        raise CodecError("container payload must be exact list")
    _require_length(payload)
    items = [_decode_value(item, depth + 1, budget) for item in payload]
    return tuple(items) if tag == "tuple" else items


def _encode_origin(origin: Any, depth: int, budget: _Budget) -> dict[str, Any]:
    if origin is not None:
        if type(origin) is not tuple or len(origin) not in (4, 5):
            raise CodecError("origin must be a 4/5-tuple or None")
        for index, item in enumerate(origin):
            if type(item) is not (str if index == 2 else int):
                raise CodecError("origin entry type mismatch")
    return _encode_value(origin, depth, budget)


def _decode_origin(value: Any, depth: int, budget: _Budget) -> Any:
    origin = _decode_value(value, depth, budget)
    if origin is None:
        return None
    if type(origin) is not tuple or len(origin) not in (4, 5):
        raise CodecError("origin must be a 4/5-tuple or None")
    for index, item in enumerate(origin):
        if type(item) is not (str if index == 2 else int):
            raise CodecError("origin entry type mismatch")
    return origin


def _require_framing(data: Any, kind: str, keys: frozenset[str]) -> None:
    if type(data) is not dict:
        raise CodecError("expected record mapping")
    if len(data) != len(keys):
        raise CodecError("record keys mismatch")
    for key in data:
        if type(key) is not str or key not in keys:
            raise CodecError("record keys mismatch")
    if type(data["version"]) is not int or data["version"] != CODEC_VERSION:
        raise CodecError("unsupported codec version")
    if type(data["type"]) is not str or data["type"] != kind:
        raise CodecError("record type mismatch")


def _encode_guard(guard: Any, depth: int, budget: _Budget) -> dict[str, Any]:
    if type(guard) is not Guard:
        raise CodecError("expected exact Guard")
    budget.node(depth)
    if type(guard.value) is not bool and type(guard.value) is not int:
        raise CodecError("Guard.value must be exact bool or int")
    return {
        "version": CODEC_VERSION,
        "type": "guard",
        "kind": _text_field(guard.kind, depth + 1, budget, allowed=_GUARD_KINDS),
        "subject": _int_field(guard.subject, depth + 1, budget),
        "operator": _text_field(guard.operator, depth + 1, budget),
        "value": _encode_value(guard.value, depth + 1, budget),
        "origin": _encode_origin(guard.origin, depth + 1, budget),
    }


def _decode_guard(data: Any, depth: int, budget: _Budget) -> Guard:
    _require_framing(data, "guard", _GUARD_KEYS)
    budget.node(depth)
    kind = _text_field(data["kind"], depth + 1, budget, allowed=_GUARD_KINDS)
    subject = _int_field(data["subject"], depth + 1, budget)
    operator = _text_field(data["operator"], depth + 1, budget)
    value = _decode_value(data["value"], depth + 1, budget)
    if type(value) is not bool and type(value) is not int:
        raise CodecError("Guard.value must be exact bool or int")
    origin = _decode_origin(data["origin"], depth + 1, budget)
    return Guard(kind=kind, subject=subject, operator=operator, value=value, origin=origin)


def _encode_effect(effect: Any, depth: int, budget: _Budget) -> dict[str, Any]:
    if type(effect) is not Effect:
        raise CodecError("expected exact Effect")
    budget.node(depth)
    if type(effect.guards) is not tuple:
        raise CodecError("Effect.guards must be an exact tuple")
    budget.node(depth + 1)
    _require_length(effect.guards)
    guards = [_encode_guard(guard, depth + 2, budget) for guard in effect.guards]
    if type(effect.values) is not tuple:
        raise CodecError("Effect.values must be an exact tuple")
    budget.node(depth + 1)
    _require_length(effect.values)
    values = []
    for pair in effect.values:
        if type(pair) is not tuple or len(pair) != 2 or type(pair[0]) is not str:
            raise CodecError("Effect.values entries must be exact (str, VALUE) tuples")
        values.append(_encode_value(pair, depth + 2, budget))
    return {
        "version": CODEC_VERSION,
        "type": "effect",
        "kind": _text_field(effect.kind, depth + 1, budget),
        "source": _int_field(effect.source, depth + 1, budget),
        "guards": guards,
        "values": values,
        "decoded": _bool_field(effect.decoded, depth + 1, budget),
        "reason": _text_or_none(effect.reason, depth + 1, budget),
    }


def _decode_effect(data: Any, depth: int, budget: _Budget) -> Effect:
    _require_framing(data, "effect", _EFFECT_KEYS)
    budget.node(depth)
    kind = _text_field(data["kind"], depth + 1, budget)
    source = _int_field(data["source"], depth + 1, budget)
    raw_guards = data["guards"]
    if type(raw_guards) is not list:
        raise CodecError("encoded Effect.guards must be an exact list")
    budget.node(depth + 1)
    _require_length(raw_guards)
    guards = tuple(_decode_guard(guard, depth + 2, budget) for guard in raw_guards)
    raw_values = data["values"]
    if type(raw_values) is not list:
        raise CodecError("encoded Effect.values must be an exact list")
    budget.node(depth + 1)
    _require_length(raw_values)
    values = []
    for raw_pair in raw_values:
        pair = _decode_value(raw_pair, depth + 2, budget)
        if type(pair) is not tuple or len(pair) != 2 or type(pair[0]) is not str:
            raise CodecError("Effect.values entries must be exact (str, VALUE) tuples")
        values.append(pair)
    decoded = _bool_field(data["decoded"], depth + 1, budget)
    reason = _text_or_none(data["reason"], depth + 1, budget)
    return Effect(
        kind=kind,
        source=source,
        guards=guards,
        values=tuple(values),
        decoded=decoded,
        reason=reason,
    )


def _encode_interpretation(value: Any, depth: int, budget: _Budget) -> dict[str, Any]:
    if type(value) is not Interpretation:
        raise CodecError("expected exact Interpretation")
    budget.node(depth)
    if type(value.effects) is not tuple:
        raise CodecError("Interpretation.effects must be an exact tuple")
    budget.node(depth + 1)
    _require_length(value.effects)
    effects = [_encode_effect(effect, depth + 2, budget) for effect in value.effects]
    if type(value.unknown) is not tuple:
        raise CodecError("Interpretation.unknown must be an exact tuple")
    budget.node(depth + 1)
    _require_length(value.unknown)
    unknown = [_text_field(item, depth + 2, budget) for item in value.unknown]
    return {
        "version": CODEC_VERSION,
        "type": "interpretation",
        "script_ptr": _int_field(value.script_ptr, depth + 1, budget),
        "effects": effects,
        "unknown": unknown,
        "commands": _int_field(value.commands, depth + 1, budget),
    }


def _decode_interpretation(data: Any, depth: int, budget: _Budget) -> Interpretation:
    _require_framing(data, "interpretation", _INTERPRETATION_KEYS)
    budget.node(depth)
    script_ptr = _int_field(data["script_ptr"], depth + 1, budget)
    raw_effects = data["effects"]
    if type(raw_effects) is not list:
        raise CodecError("encoded Interpretation.effects must be an exact list")
    budget.node(depth + 1)
    _require_length(raw_effects)
    effects = tuple(_decode_effect(effect, depth + 2, budget) for effect in raw_effects)
    raw_unknown = data["unknown"]
    if type(raw_unknown) is not list:
        raise CodecError("encoded Interpretation.unknown must be an exact list")
    budget.node(depth + 1)
    _require_length(raw_unknown)
    unknown = tuple(_text_field(item, depth + 2, budget) for item in raw_unknown)
    commands = _int_field(data["commands"], depth + 1, budget)
    return Interpretation(
        script_ptr=script_ptr, effects=effects, unknown=unknown, commands=commands
    )


def encode_guard(guard: Guard) -> dict[str, Any]:
    return _encode_guard(guard, 0, _Budget())


def decode_guard(data: Any) -> Guard:
    return _decode_guard(data, 0, _Budget())


def encode_effect(effect: Effect) -> dict[str, Any]:
    return _encode_effect(effect, 0, _Budget())


def decode_effect(data: Any) -> Effect:
    return _decode_effect(data, 0, _Budget())


def encode_interpretation(value: Interpretation) -> dict[str, Any]:
    return _encode_interpretation(value, 0, _Budget())


def decode_interpretation(data: Any) -> Interpretation:
    return _decode_interpretation(data, 0, _Budget())
