"""Lazy compatibility shim for FireRed HM/move mappings."""

from importlib import import_module

_TARGET = "pokegent.games.gen3.firered.rom.move_mappings"


def __getattr__(name: str):
    value = getattr(import_module(_TARGET), name)
    globals()[name] = value
    return value
