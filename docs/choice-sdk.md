# Choice SDK quickstart

`pokegent.sdk.ChoiceSdk` validates one offered choice against one observation, locally: no
snapshot read, backend, reader, executor or callback call, game input, network, MCP or other
transport. It is not wired into the Runner; objectives, progress and events are unimplemented.

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
