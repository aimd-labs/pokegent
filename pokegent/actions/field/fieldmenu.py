"""Lazy compatibility shim for FireRed field-menu mappings."""

from importlib import import_module

_TARGET = "pokegent.games.gen3.firered.reader.menu_mappings"


def __getattr__(name: str):
    value = getattr(import_module(_TARGET), name)
    globals()[name] = value
    return value
