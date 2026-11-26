# FireRed objective-directory helper

`pokegent/games/gen3/firered/navigation.py` owns `make_objective_directory`, a
lazy constructor for the ROM-only `RomMapDirectory`. This page describes the
helper and its optional plugin seam at source base `5166d143`; it does not
describe a new runtime path, title or support claim.

## Helper

`make_objective_directory(rom_bytes, profile)` imports `RomMapDirectory` from
`pokegent.goals.directory` inside the call and returns
`RomMapDirectory(rom_bytes, profile)`. The two caller objects are passed
through unchanged: no copy, re-read, coercion or profile lookup. Identity is
preserved, so the directory sees exactly the bytes and profile the caller
supplied.

Nothing is caught, wrapped, logged or retried. A lazy-import or constructor
error leaves the helper immediately with its original type and message, and no
later step runs because the helper has no later step. The helper also adds no
admission check, digest comparison, provider call or native authority. The
caller owns byte admission: it must supply already-admitted bytes and a
matching profile. Those are preconditions of use, not guarantees the helper
enforces.

## Factory and plugin seam

`make_navigation_services(..., *, events, needs_directory=False)` builds the
graph, navigation graph and adapter in its existing order. It calls
`make_objective_directory(rom_bytes, profile)` only when `needs_directory` is
true, and puts the result in `FireRedNavigationServices.directory`. With the
default false it imports no directory module, constructs no directory and
returns `directory=None`; an offline run passes
`needs_directory=objective is not None`, so the branch follows the objective,
not a file probe.

`FireRedServices` carries an optional trailing
`objective_directory_factory: Callable[...] | None = None`. Manual
construction remains valid without it. `FireRedPlugin.compose()` names
`make_objective_directory` in that field without calling it, reading an actor
or touching a ROM file. Naming the helper is composition evidence only, not
directory construction, admission or support proof.

## Limits

The helper is FireRed-owned and Python-only. It does not validate a profile,
choose a title, verify input or prove a native run. Earlier app wiring still
builds its own FireRed directory; moving that call onto this helper is a
separate pending slice and is not claimed here as merged or adopted. The
pinned source scope above is the whole subject of this page.

## Synthetic fence

ROM-free and self-contained, the fence patches the existing directory
constructor and factory dependencies, uses invented bytes and the built-in
`firered-us-rev0` test profile, and checks forwarding, error passthrough, the
gated factory call and compose naming. Run it once with CPython 3.11, this
worktree on `PYTHONPATH` and the shared heavy lock; expected stdout is the
printed line and stderr is empty.

```python
"""Synthetic fence: FireRed objective-directory helper (#139/R10).

Patch existing constructors and the plugin seam only; invented bytes are never
parsed, and no actor, ROM file or geometry claim is involved.
"""

import unittest.mock as mock
from contextlib import ExitStack

import pokegent.games.gen3.firered.navigation as nav
from pokegent.games.gen3.firered.plugin import FireRedPlugin
from pokegent.games.gen3.firered.rom.rom_data import load_profiles

ROM = b"synthetic-firered-objective-directory" * 8
PROFILE = load_profiles().get("firered-us-rev0")
DIRECTORY = object()
CALLS = []


def recorder(label, result):
    def record(*args, **kwargs):
        CALLS.append((label, args, kwargs))
        return result

    return record


# 1. Same objects reach the existing constructor and its result is returned.
with mock.patch(
    "pokegent.goals.directory.RomMapDirectory",
    recorder("directory", DIRECTORY),
):
    assert nav.make_objective_directory(ROM, PROFILE) is DIRECTORY
assert CALLS == [("directory", (ROM, PROFILE), {})]
assert CALLS[0][1][0] is ROM and CALLS[0][1][1] is PROFILE


# 2. The original constructor error escapes unchanged and is not retried.
class OriginalError(RuntimeError):
    pass


def refuse(*args, **kwargs):
    CALLS.append(("refuse", args, kwargs))
    raise OriginalError("directory refused")


CALLS.clear()
with mock.patch("pokegent.goals.directory.RomMapDirectory", refuse):
    try:
        nav.make_objective_directory(ROM, PROFILE)
    except OriginalError as error:
        assert str(error) == "directory refused"
    else:
        raise AssertionError("directory error was swallowed")
assert [call[0] for call in CALLS] == ["refuse"]


# 3. The factory uses the helper only when needs_directory is true.
PATCHES = [
    mock.patch.object(nav.MapGraph, "from_rom", recorder("graph", object())),
    mock.patch.object(nav.NavigationGraph, "from_rom", recorder("nav_graph", object())),
    mock.patch.object(nav, "NavigationAdapter", recorder("adapter", object())),
    mock.patch.object(nav, "make_objective_directory", recorder("directory", DIRECTORY)),
]


def build(*patches, needed):
    CALLS.clear()
    with ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        return nav.make_navigation_services(
            ROM, PROFILE, events=object(), needs_directory=needed
        )


services = build(*PATCHES, needed=True)
assert [call[0] for call in CALLS] == ["graph", "nav_graph", "adapter", "directory"]
assert services.directory is DIRECTORY
inert = build(*PATCHES[:-1], needed=False)
assert [call[0] for call in CALLS] == ["graph", "nav_graph", "adapter"]
assert inert.directory is None


# 4. compose names the helper without invoking it on the built-in profile.
CALLS.clear()
with mock.patch.object(nav, "make_objective_directory", recorder("directory", None)):
    services = FireRedPlugin(PROFILE).compose()
assert services.objective_directory_factory is not None
assert CALLS == []

print("objective directory fence ok: forwarding, error passthrough, gated factory, named seam")
```
