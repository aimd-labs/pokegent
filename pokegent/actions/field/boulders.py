"""Lazy compatibility exports for the moved FireRed boulder planner."""

from importlib import import_module

_TARGET = "pokegent.games.gen3.firered.maps.boulders"


def __getattr__(name: str):
    value = getattr(import_module(_TARGET), name)
    globals()[name] = value
    return value
