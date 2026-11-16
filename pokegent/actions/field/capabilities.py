"""Lazy compatibility shim for the FireRed capability provider."""

from importlib import import_module

_TARGET = "pokegent.games.gen3.firered.reader.capabilities"


def __getattr__(name: str):
    value = getattr(import_module(_TARGET), name)
    globals()[name] = value
    return value
