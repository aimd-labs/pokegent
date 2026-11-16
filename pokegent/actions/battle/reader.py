"""Lazy compatibility exports for the moved FireRed battle reader."""

from importlib import import_module

_TARGET = "pokegent.games.gen3.firered.reader.battle_reader"


def __getattr__(name: str):
    value = getattr(import_module(_TARGET), name)
    globals()[name] = value
    return value
