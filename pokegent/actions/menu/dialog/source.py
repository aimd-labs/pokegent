"""Lazy compatibility exports for the moved FireRed dialog snapshot source."""

from importlib import import_module

_TARGET = "pokegent.games.gen3.firered.rom.dialog_source"


def __getattr__(name: str):
    value = getattr(import_module(_TARGET), name)
    globals()[name] = value
    return value
