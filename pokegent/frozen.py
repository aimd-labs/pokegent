"""Deeply immutable JSON-like values for frames, options and scene evidence.

``FrozenDict`` cannot be mutated after construction but still serializes as a dict;
``freeze`` turns any JSON-like value into a deeply frozen one (dict -> FrozenDict,
list/tuple -> tuple) and rejects everything that is not JSON-serializable, so everything that
is frozen can also be written to the replay log.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import AfterValidator


class FrozenDict(dict):
    """A dict that cannot be mutated after construction (it still serializes as a dict)."""

    __slots__ = ()

    def _readonly(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("FrozenDict is immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _readonly
    __ior__ = _readonly  # type: ignore[assignment]

    def __reduce__(self):  # deepcopy / pickle without going through __setitem__
        return (FrozenDict, (dict(self),))

    def copy(self) -> dict:  # a copy is a plain, mutable dict
        return dict(self)


def freeze(value: Any, _path: str = "value") -> Any:
    """Deep-freeze a JSON-like value: dict -> FrozenDict, list/tuple -> tuple. Rejects anything
    that is not JSON-serializable (frames are logged for replay) and non-finite floats."""
    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{_path}: mapping keys must be strings, got {key!r}")
            out[key] = freeze(item, f"{_path}.{key}")
        return FrozenDict(out)
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item, f"{_path}[]") for item in value)
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{_path}: non-finite float")
        return value
    raise ValueError(f"{_path}: {type(value).__name__} is not JSON-like")


def _freeze_mapping(value: dict[str, Any]) -> dict[str, Any]:
    return freeze(value)


FrozenMap = Annotated[dict[str, Any], AfterValidator(_freeze_mapping)]
