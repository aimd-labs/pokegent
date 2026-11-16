"""Lazy compatibility exports for the FireRed UI evidence reader."""

from importlib import import_module

_TARGET = "pokegent.games.gen3.firered.reader.ui"


def __getattr__(name: str):
    value = getattr(import_module(_TARGET), name)
    globals()[name] = value
    return value
