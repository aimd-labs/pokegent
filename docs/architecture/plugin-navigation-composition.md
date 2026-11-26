# Runner navigation composition

`experimental_plugin_navigation` is a Python-only opt-in on `build_runner`
([pokegent/app.py](https://github.com/aimd-labs/pokegent/blob/b78fe639461fd930bc9a53de5406c984aa489dae/pokegent/app.py#L701-L828))
for the reviewed FireRed plugin path. It defaults to `False`, adds no CLI flag,
no second title, no admission change and no input authority. FireRed-only.

## Opt-in and refusal

The value must be exactly `bool`; anything else raises `TypeError`. When true,
`experimental_game_plugin` must also be true and the caller supplies
`plugin_rom_bytes`. A navigation opt-in without the game-plugin switch refuses
with `RefusalCode.DISABLED` before any bytes are read or frozen. Supplying
`objective_graph`, `objective_navigation` or `objective_directory` alongside it
raises `ValueError` instead of silently ignoring those objects.

## Freeze and selection

On the true path the bytes must be `bytes`, `bytearray` or `memoryview`. A
`memoryview` bounds `nbytes` against `MAX_ROM_BYTES`
([pokegent/games/plugin.py](https://github.com/aimd-labs/pokegent/blob/b78fe639461fd930bc9a53de5406c984aa489dae/pokegent/games/plugin.py#L18))
first, then `buffer.tobytes()` produces the frozen immutable bytes used for
selection and composition. The freeze happens once, before any observer reads,
and the raw caller object is never passed to the plugin.

Selection keeps the existing gates: exact SHA-1 ROM admission, FireRed profile,
contract version and `required_capabilities`. The navigation opt-in adds
`navigation` to those capabilities, so a plugin without it refuses before
`compose()`. The selected plugin's `navigation_factory` must be callable;
otherwise the run refuses with `RefusalCode.MISSING_CAPABILITY`.

## One factory call and forwarding

`needs_directory` is true only when `objectives` or `objective_plan` is
supplied. The factory is called once, before any observation, with the frozen
bytes, the profile and `events=events`. Its returned `graph`, `navigation` and
optional `directory` are checked (`graph` and `navigation` must be present,
`directory` when needed) and forwarded to the existing owners: the planning
inputs and the objective session directory. Factory errors pass through
unchanged; nothing is caught, wrapped or retried.

## Default-off boundary

With the flag off, no bytes are frozen and the composed navigation factory is
never called. The Mock in the fence below fails if it is touched, which proves
zero calls. It does not by itself prove that no attribute lookup happened; the
source unit tests cover the no-lookup guard separately. The example gives no
proof of readable maps, real geometry, actor reads, input or navigation.

## Synthetic fence

Runs the real `build_runner` and `ReviewedPluginRegistry` with invented bytes,
a synthetic FireRed profile digest and a reviewed plugin whose `compose` returns
the real FireRed services with a sentinel navigation factory. No ROM is parsed,
no actor is read. The script was executed once with CPython 3.11 under the
shared heavy lock; the final stdout line is
`runner navigation fence ok: freeze, one call, forwarding, default off, refusal`,
and stderr carries two missing-local-data warnings for the disabled scripted
actions (elevators, quiz, switch_puzzle). This page keeps the reached
assertion-success line and those warnings, not an independently captured
process exit status. Registration probes optional local data, so no
filesystem-read-free claim follows from the fence.

```python
"""Synthetic fence: default-off runner navigation composition (#139/R10).

Invented bytes and sentinel objects only: the real build_runner and ReviewedPluginRegistry
run, but no ROM is parsed, no actor is read and no map, input or navigation is proven.
"""

import hashlib
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from pokegent.app import AppConfig, build_runner
from pokegent.games.gen3.firered.plugin import FIRERED_CAPABILITIES, FireRedPlugin
from pokegent.games.gen3.firered.rom.rom_data import load_profiles
from pokegent.games.plugin import (
    GamePluginDescriptor,
    PluginRefusal,
    RefusalCode,
    ReviewedPluginRegistry,
)

ROM = b"synthetic-runner-navigation-invented" * 8
GRAPH, NAVIGATION = object(), object()
base = load_profiles().get("firered-us-rev0")
PROFILE = base.model_copy(
    update={
        "rom": base.rom.model_copy(
            update={"sha1": hashlib.sha1(ROM).hexdigest()}
        )
    }
)


class NoReadActor:
    """Any read or input attribute is a fence failure."""

    realtime_idle_while_deciding = False

    def __getattr__(self, name):
        raise AssertionError(f"actor.{name} touched")


class NavPlugin:
    """Reviewed synthetic plugin: real FireRed services, one sentinel navigation factory."""

    def __init__(self, factory):
        self.factory, self.composed = factory, 0
        self._descriptor = GamePluginDescriptor(
            plugin_id="synthetic-runner-navigation",
            game="firered",
            profile_id=PROFILE.profile_id,
            contract_version=1,
            rom_sha1=hashlib.sha1(ROM).hexdigest(),
            capabilities=FIRERED_CAPABILITIES,
        )

    @property
    def descriptor(self):
        return self._descriptor

    def compose(self):
        self.composed += 1
        return replace(
            FireRedPlugin(PROFILE).compose(), navigation_factory=self.factory
        )


def build(factory, *, navigation, rom_bytes):
    plugin = NavPlugin(factory)
    registry = ReviewedPluginRegistry(enabled=True)
    registry.register(plugin)
    app = build_runner(
        actor=NoReadActor(),
        profile=PROFILE,
        events=object(),
        config=AppConfig(run_id="runner-navigation-guide", map_mode="always"),
        experimental_game_plugin=True,
        experimental_plugin_navigation=navigation,
        plugin_rom_bytes=rom_bytes,
        plugin_registry=registry,
    )
    return app, plugin


# 1. Opt in: one factory call, bounded freeze, then the exact bundle to existing owners.
supplied = bytearray(ROM)
factory = Mock(
    return_value=SimpleNamespace(
        graph=GRAPH, navigation=NAVIGATION, directory=None
    )
)
app, plugin = build(factory, navigation=True, rom_bytes=supplied)
assert plugin.composed == 1 and factory.call_count == 1
rom, profile = factory.call_args.args
assert type(rom) is bytes and rom == ROM and rom is not supplied
assert profile is PROFILE
assert factory.call_args.kwargs["events"] is app.events
assert factory.call_args.kwargs["needs_directory"] is False
assert app.objectives is None
assert app.bundle.config.graph is GRAPH
assert app.bundle.config.navigation is NAVIGATION

# 2. Default off: the composed navigation factory is never called or inspected.
off_factory = Mock(side_effect=AssertionError("factory touched while off"))
off_app, off_plugin = build(
    off_factory, navigation=False, rom_bytes=bytearray(ROM)
)
assert off_plugin.composed == 1 and off_factory.call_count == 0
assert off_app.objectives is None

# 3. Orphan opt-in without the game-plugin switch is refused before any factory call.
orphan = Mock(side_effect=AssertionError("orphan factory touched"))
plugin = NavPlugin(orphan)
registry = ReviewedPluginRegistry(enabled=True)
registry.register(plugin)
try:
    build_runner(
        actor=NoReadActor(),
        profile=PROFILE,
        events=object(),
        config=AppConfig(run_id="runner-navigation-orphan", map_mode="always"),
        experimental_game_plugin=False,
        experimental_plugin_navigation=True,
        plugin_rom_bytes=bytearray(ROM),
        plugin_registry=registry,
    )
except PluginRefusal as refusal:
    assert refusal.code is RefusalCode.DISABLED, refusal.code
else:
    raise AssertionError("orphan navigation opt-in was admitted")
assert plugin.composed == 0 and orphan.call_count == 0

print("runner navigation fence ok: freeze, one call, forwarding, default off, refusal")
```
