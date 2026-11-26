# FireRed navigation services

`pokegent/games/gen3/firered/navigation.py` owns `make_navigation_services`, the
game-owned construction of the map graph, navigation adapter and optional
objective directory. Python-only and FireRed-only, it preserves the existing
default runtime path: no new flag; callers supply admitted bytes, profile, sink.

## Construction and forwarding

`make_navigation_services(rom_bytes, profile, *, events, needs_directory=False)`
builds in fixed order: `MapGraph.from_rom(rom_bytes, profile)`, then
`NavigationGraph.from_rom(rom_bytes, profile)`, then
`NavigationAdapter(navigation_graph, events=events)`, then optionally
`RomMapDirectory(rom_bytes, profile)`. Frozen `FireRedNavigationServices` returns
`graph`, `navigation` and `directory` in that order; the adapter receives the
navigation graph (not the map graph) and the event sink by keyword.

`needs_directory` defaults to `False`: the factory then never imports
`pokegent.goals.directory`, never constructs `RomMapDirectory` and returns
`directory=None`, while the three graph/adapter steps still run. `run_offline`
passes `needs_directory=objective is not None`, so an objective needs directory
target data and a run without one skips it; no file probe chooses the branch.

The raw `bytes`, profile object and `events` sink are forwarded unchanged; the
factory does not copy, re-read or re-admit them and adds no digest, admission,
provider or cache step (`MapGraph.from_rom` may hash internally, which is that
constructor's behavior). Nothing is caught, wrapped or retried: a constructor or
lazy-import error leaves immediately, so later steps do not run.

## Plugin seam and limits

`FireRedServices` carries optional trailing `navigation_factory: Callable[...] | None`
`= None`; manual construction stays valid without it, and `FireRedPlugin.compose()`
names `make_navigation_services` without invoking it, reading an actor or ROM file.
Naming it is not geometry, admission, ROM-digest, native or support proof. At this
pin `run_offline` still calls the factory directly, and its earlier bridge setup
builds a FireRed-specific `RomMapDirectory` itself, so that early directory remains
title-specific. [#139](https://github.com/aimd-labs/pokegent/issues/139) stays open.

## Synthetic fence

Run once with CPython 3.11, this worktree on `PYTHONPATH` and the shared heavy lock; expected stdout is the printed line, stderr empty, and the fence is delegation proof only.

```python
"""Synthetic fence: FireRed navigation services construction (#139/R10).

Patches the existing constructors only; no ROM bytes are parsed, no actor is opened
and no geometry, admission or navigation claim is made.
"""

import unittest.mock as mock
from contextlib import ExitStack

import pokegent.games.gen3.firered.navigation as nav

ROM = b"synthetic-firered-navigation" * 8
PROFILE, EVENTS = object(), object()
GRAPH, NAV_GRAPH, ADAPTER, DIRECTORY = object(), object(), object(), object()
CALLS = []


def recorder(label, result):
    def record(*args, **kwargs):
        CALLS.append((label, args, kwargs))
        return result

    return record


def constructors():
    return [
        mock.patch.object(nav.MapGraph, "from_rom", recorder("graph", GRAPH)),
        mock.patch.object(
            nav.NavigationGraph, "from_rom", recorder("nav_graph", NAV_GRAPH)
        ),
        mock.patch.object(nav, "NavigationAdapter", recorder("adapter", ADAPTER)),
        mock.patch(
            "pokegent.goals.directory.RomMapDirectory",
            recorder("directory", DIRECTORY),
        ),
    ]


def run(patches, **kwargs):
    with ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        return nav.make_navigation_services(ROM, PROFILE, events=EVENTS, **kwargs)


# 1. needs_directory=True: graph, nav graph, adapter, then directory, same inputs.
CALLS.clear()
services = run(constructors(), needs_directory=True)
assert [call[0] for call in CALLS] == ["graph", "nav_graph", "adapter", "directory"]
assert CALLS[0][1:] == ((ROM, PROFILE), {})
assert CALLS[1][1:] == ((ROM, PROFILE), {})
assert CALLS[2][1:] == ((NAV_GRAPH,), {"events": EVENTS})
assert CALLS[3][1:] == ((ROM, PROFILE), {})
assert (services.graph, services.navigation, services.directory) == (
    GRAPH,
    ADAPTER,
    DIRECTORY,
)

# 2. needs_directory omitted: no RomMapDirectory call, returned directory is None.
CALLS.clear()
services = run(
    constructors()[:3]
    + [
        mock.patch(
            "pokegent.goals.directory.RomMapDirectory",
            side_effect=AssertionError("directory constructed when not needed"),
        )
    ]
)
assert services.directory is None
assert [call[0] for call in CALLS] == ["graph", "nav_graph", "adapter"]


# 3. A constructor error escapes unchanged and later constructors do not run.
class OriginalError(RuntimeError):
    pass


def explode(*args, **kwargs):
    CALLS.append(("graph", args, kwargs))
    raise OriginalError("map graph refused")


CALLS.clear()
try:
    run([mock.patch.object(nav.MapGraph, "from_rom", explode)] + constructors()[1:])
except OriginalError as error:
    assert str(error) == "map graph refused"
else:
    raise AssertionError("constructor error was swallowed")
assert [call[0] for call in CALLS] == ["graph"]

print("navigation services fence ok: order, directory skip, error passthrough")
```
