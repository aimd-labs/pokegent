# Choice SDK quickstart

`pokegent.sdk.ChoiceSdk` validates one offered choice against one observation, locally: no
snapshot read, backend, reader, executor or callback call, game input, network, MCP or other
transport. It is not wired into the Runner; objectives, progress and events are separate
opt-in facades: `ObjectiveSdk` and `ProgressSdk` below, and `EventSdk` in its
[event SDK guide](event-sdk.md).

## Prerequisites

`ChoiceSdk` needs an admitted `PluginSelection`, not a raw plugin or ROM. Register a reviewed
plugin in `ReviewedPluginRegistry(enabled=True)`, then select with exact private ROM bytes and
the descriptor's game/profile. Both flags are keyword-only exact booleans, default `False`:
non-bools raise `TypeError`; disabled SDK `ChoiceSdkRejected`, disabled registry `PluginRefusal`.

## Observations

`observe()` accepts a matching `GameObservation` from `create_observation(enabled=True, ...)`.
`snapshot_seq` must strictly increase within a `restore_epoch` (a higher epoch may reset it) and
`usable=False` cannot be offered.

## Offers and submissions

`offer(frame)` requires a `DecisionFrame` with matching plain-int `snapshot_seq`, non-empty
`decision_id` and `state_fingerprint`. The `ChoiceOffer` carries `decision_id`, `snapshot_seq`,
`state_fingerprint` and `restore_epoch` as binding fields; its `options` list only chooseable
`option_id` and `label`, with no hidden options, executor references, arguments or raw snapshot.

`submit(result)` requires the pending frame, a matching `decision_id` and a chooseable option
id (`allow_all=False`). Success returns the same object and consumes the observation once;
repeat submits and offers are refused until a newer observation, and a newer `observe()` also
invalidates an unconsumed pending offer. Refusals raise `ChoiceSdkRejected` with no game change.

## Complete ROM-free example

```python
import hashlib

from pokegent.games.observation import create_observation
from pokegent.games.plugin import GamePluginDescriptor, ReviewedPluginRegistry
from pokegent.options.models import ChoiceResult, DecisionFrame, Legality, Option
from pokegent.sdk import ChoiceSdk, ChoiceSdkRejected

ROM = b"synthetic-quickstart-rom" * 4
GAME, PROFILE = "synthetic-quickstart", "profile-one"


class Plugin:
    def __init__(self, descriptor):
        self.descriptor, self.composed = descriptor, 0

    def compose(self):
        self.composed += 1
        return None


plugin = Plugin(GamePluginDescriptor(
    plugin_id="synthetic-quickstart:profile-one", game=GAME, profile_id=PROFILE,
    contract_version=1, rom_sha1=hashlib.sha1(ROM).hexdigest(),
    capabilities=frozenset({"choice"}),
))
registry = ReviewedPluginRegistry(enabled=True)
registry.register(plugin)
selection = registry.select(
    rom_bytes=ROM, game=GAME, profile_id=PROFILE, required_capabilities=("choice",)
)
sdk = ChoiceSdk(selection, enabled=True)


def observe(seq):
    sdk.observe(create_observation(
        enabled=True, game_id=GAME, profile_id=PROFILE, snapshot_seq=seq,
        restore_epoch=0, usable=True, payload={"scene": "field.free"},
    ))


def frame(seq):
    pairs = ("walk", "Walk"), ("sign", "Sign")
    return DecisionFrame(
        decision_id="decision-1", run_id="quickstart", snapshot_seq=seq,
        scene_id="field.free", scene_confidence=1.0,
        state_fingerprint="sha256:" + "0" * 64,
        options=tuple(Option(id=i, label=l, legality=Legality.LEGAL,
                             executor_ref="test." + i) for i, l in pairs),
        diagnostics=(Option(id="locked", label="Locked", legality=Legality.UNAVAILABLE),),
    )


def refused(call):
    try:
        call()
    except ChoiceSdkRejected:
        return
    raise AssertionError("expected ChoiceSdkRejected")


observe(1)
offer_ids = [(o.option_id, o.label) for o in sdk.offer(frame(1)).options]
assert offer_ids == [("walk", "Walk"), ("sign", "Sign")]
accepted = sdk.submit(ChoiceResult(option_id="walk", decision_id="decision-1"))
assert accepted.option_id == "walk"
refused(lambda: sdk.submit(accepted))
refused(lambda: sdk.offer(frame(1)))
observe(2)
refused(lambda: sdk.offer(frame(1)))
assert plugin.composed == 1
print("ok: offer/submit accepted; stale and consumed calls refused")
```

The example exits 0; `plugin.composed == 1` confirms selection composed once, then the SDK
validates locally only. Not included: input, backend, reader, objective/progress/events, MCP,
new transport, second-title provenance, redaction guarantee or compatibility promise (#139).

## Objectives (separate opt-in facade)

`pokegent.sdk_objectives.ObjectiveSdk` is separate from `ChoiceSdk`: it reads `current()` from a
caller-owned `ObjectiveStore` and forwards one copied mapping to `store.propose()` with a mandatory
plain-int `expected_revision` and `set_by` bound to `agent`. Admission, CAS, source priority,
already-satisfied rejection and history stay the store's; there is no revision bump, retry,
completion, independent snapshot provider or read path, server/MCP/runtime wiring or game input.
The store's own already-bound admission snapshot may still be read during `propose()`. FireRed
namespace only. The supplied selection and store are trusted, not cryptographically bound to a ROM
or running process. See the [Objective SDK guide](objective-sdk.md) and [the objective file](objective-file.md).

## Progress (separate opt-in snapshot evaluation)

`pokegent.sdk_progress.ProgressSdk` is separate from both facades above. It answers one
question for one caller-supplied `ResolvedObjective` and one `StateSnapshot`: does this
snapshot satisfy this objective? That is snapshot evaluation, not durable completion. It
keeps no store, sink or provider, commits nothing and offers no propose path; `ObjectiveSdk`
above remains the separate opt-in store API.

Construction needs an admitted `PluginSelection` and `enabled=True` (exact bool, default
`False`). Only the `firered`/`firered-us-rev0` namespace is accepted; others raise
`ProgressSdkRejected`. `evaluate(resolved, snapshot, tables=None)` requires real
`ResolvedObjective` and `StateSnapshot` types and a matching `rom_profile`, then one fresh
storeless, sinkless `ObjectiveTracker`; `completed` keeps its probe semantics unchanged.
`False` means the tracker did not prove completion on this frame; it does not prove
every fact is known. An unusable snapshot returns `completed=None, evaluated=False`.

The frozen `ProgressView` carries `objective_id`, `revision`, `snapshot_seq`, `completed`,
`evaluated`, `evidence_kind="snapshot_evaluation"` and `durable_committed`, always `False`:
answers are never persisted or committed, so `True` is not durable progress. It exports no
raw snapshot, probes or tables.

Selection, objective and snapshot are trusted caller labels, not a native, ROM, runtime or
digest binding. The canonical profile string and `emulator_frame` are not checked for
freshness. No game input, native emulator, network or MCP is involved.

```python
import hashlib

from pokegent.games.gen3.firered.reader.schemas import (
    FlagSet, Health, Player, ReadError, StateSnapshot, VarTable,
)
from pokegent.games.plugin import GamePluginDescriptor, ReviewedPluginRegistry
from pokegent.goals.models import Objective
from pokegent.goals.validate import ResolvedObjective, ResolvedProbe, ResolvedTarget
from pokegent.sdk_progress import ProgressSdk

ROM = b"synthetic-progress-quickstart-rom" * 8
GAME, PROFILE, MAP_KEY = "firered", "firered-us-rev0", (3, 7)


class Plugin:
    def __init__(self, descriptor):
        self.descriptor, self.composed = descriptor, 0

    def compose(self):
        self.composed += 1
        return None


plugin = Plugin(GamePluginDescriptor(
    plugin_id=f"{GAME}:{PROFILE}", game=GAME, profile_id=PROFILE,
    contract_version=1, rom_sha1=hashlib.sha1(ROM).hexdigest(),
    capabilities=frozenset({"objective_observation"}),
))
registry = ReviewedPluginRegistry(enabled=True)
registry.register(plugin)
selection = registry.select(rom_bytes=ROM, game=GAME, profile_id=PROFILE)
sdk = ProgressSdk(selection, enabled=True)

resolved = ResolvedObjective(
    Objective.model_validate({
        "objective_id": "reach1", "revision": 1, "kind": "reach",
        "target": {"map": "PewterCity"},
    }),
    ResolvedTarget(group=MAP_KEY[0], number=MAP_KEY[1]),
    (ResolvedProbe(kind="at_tile", map_key=MAP_KEY, x=4, y=5),),
)


def snapshot(seq, *, usable=True):
    if not usable:
        return StateSnapshot.create(
            rom_profile=PROFILE, snapshot_seq=seq,
            health=Health(valid=False, read_errors=(
                ReadError(subsystem="flags", code="unreadable", message="synthetic"),
            )),
        )
    return StateSnapshot.create(
        rom_profile=PROFILE, snapshot_seq=seq, emulator_frame=0,
        health=Health(valid=True),
        player=Player(name="Test", gender="male", tid=0, sid=0, map_layout_id=0,
                      map_group=MAP_KEY[0], map_num=MAP_KEY[1], x=4, y=5,
                      money=0, coins=0),
        flags=FlagSet(), vars=VarTable(),
    )


hit = sdk.evaluate(resolved, snapshot(1))
assert (hit.objective_id, hit.revision, hit.snapshot_seq) == ("reach1", 1, 1)
assert (hit.completed, hit.evaluated, hit.durable_committed) == (True, True, False)
unknown = sdk.evaluate(resolved, snapshot(2, usable=False))
assert (unknown.completed, unknown.evaluated, unknown.snapshot_seq) == (None, False, 2)
assert plugin.composed == 1
print("ok: usable frame completed; unusable frame unknown; nothing committed")
```

The example exits 0. The matched tile is evaluated and completed without being committed;
the unusable frame stays unknown. Not included: durable completion, commits, events, store
or provider reads, objective proposals, Runner wiring, native/ROM binding, compatibility (#139).
