"""Focused tests for frozen JSON-like values (pokegent.frozen, #394)."""
import copy
import pickle

import pytest
from pydantic import BaseModel

from pokegent.frozen import FrozenDict, FrozenMap, freeze


def test_frozen_dict_rejects_all_mutations():
    value = FrozenDict({"a": 1})
    for action, args in [
        (value.__setitem__, ("b", 2)), (value.__delitem__, ("a",)), (value.clear, ()),
        (value.pop, ("a",)), (value.popitem, ()), (value.setdefault, ("b", 2)),
        (value.update, ({"b": 2},)), (value.__ior__, ({"b": 2},)),
    ]:
        with pytest.raises(TypeError):
            action(*args)


def test_copies_stay_frozen_roundtrip_or_plain_mutable():
    value = freeze({"a": [1, 2], "b": 3})
    copied = value.copy()
    copied["x"] = 1
    assert type(copied) is dict
    for restored in (copy.deepcopy(value), pickle.loads(pickle.dumps(value))):
        assert type(restored) is FrozenDict and restored == value


def test_freeze_transforms_nested_scalars_and_frozen_map():
    nested = freeze({"a": [1, {"b": 2}], "c": (3, 4)})
    assert isinstance(nested["a"], tuple)
    assert isinstance(nested["a"][1], FrozenDict)
    assert isinstance(nested["c"], tuple)
    assert all(freeze(v) is v for v in (True, False, None, 3, "text"))
    class Box(BaseModel):
        value: FrozenMap
    box = Box(value={"a": [1, 2], "b": "x"})
    assert type(box.value) is FrozenDict
    assert box.value["a"] == (1, 2)


def test_freeze_rejects_non_json_values_with_path():
    for value, message in [
        ({1: "a"}, "value: mapping keys must be strings"),
        ({"x": float("nan")}, "value.x: non-finite float"),
        ({"x": float("inf")}, "value.x: non-finite float"),
        ({"x": {"s": {1}}}, "value.x.s: set is not JSON-like"),
        ({"x": b"raw"}, "value.x: bytes is not JSON-like"),
        ({"x": object()}, "value.x: object is not JSON-like"),
    ]:
        with pytest.raises(ValueError, match=message):
            freeze(value)
