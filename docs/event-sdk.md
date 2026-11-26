# Event SDK page facade

`pokegent.sdk_events.EventSdk` reads bounded, metadata-only pages from one complete
caller-supplied iterable of `EventEnvelope` values. It owns no log, file, socket or stream,
calls no runtime, provider, executor or game input, and validates locally only. The supplied
envelopes are trusted data, not cryptographically bound to a ROM, process or transport.

## Prerequisites

The event SDK needs an admitted `PluginSelection` from `ReviewedPluginRegistry(enabled=True)`,
as in the [choice SDK](choice-sdk.md). `enabled` is a keyword-only exact boolean, default
`False`: a non-boolean raises `TypeError`, and a disabled SDK raises `EventSdkRejected` before
it consumes the iterable. Input is one complete iterable of at most 10,000 rows; longer,
non-iterable or non-`EventEnvelope` input is refused.

## Pages

`read_page(events, *, run_id, after_seq=0, limit=100)` takes a nonblank `run_id` of at most 200
characters, a plain nonnegative `after_seq` and a plain `limit` from 1 to 100. It keeps only
envelopes whose `run_id` matches exactly and whose `public_visibility is True`. Other runs are
ignored, but repeated or decreasing `seq` within the requested run is refused.

If another eligible public row exists beyond the page, `next_seq` is the last returned row, so
a bounded page never skips one. When the selection is complete, `next_seq` instead advances
across the trailing private rows; a later call then returns no rows. Pass `next_seq` back as
`after_seq` to continue.

Each `EventRow` is a frozen copy of seven metadata fields: `id`, `seq`, `observed_at`, `type`,
`scene_id`, `outcome` and `emulator_frame`. `details`, `provider`, `request_id` and other
envelope fields are never exported or reachable from a page. A page also stays detached if the
caller's iterable changes later.

## Complete ROM-free example

```python
import hashlib

from pokegent.events import EventEnvelope
from pokegent.games.plugin import GamePluginDescriptor, ReviewedPluginRegistry
from pokegent.sdk_events import EventSdk, EventSdkRejected

ROM = b"synthetic-event-guide-rom" * 4

class Plugin:
    def __init__(self, descriptor):
        self.descriptor = descriptor

    def compose(self):
        return None


descriptor = GamePluginDescriptor(
    plugin_id="synthetic-event:guide", game="synthetic-event", profile_id="guide",
    contract_version=1, rom_sha1=hashlib.sha1(ROM).hexdigest(), capabilities=frozenset(),
)
registry = ReviewedPluginRegistry(enabled=True)
registry.register(Plugin(descriptor))
selection = registry.select(
    rom_bytes=ROM, game=descriptor.game, profile_id=descriptor.profile_id,
)
sdk = EventSdk(selection, enabled=True)
events = (
    EventEnvelope(id="run-guide:1", run_id="run-guide", seq=1, observed_at="2026-11-26T06:00:00Z",
                  type="action.started", public_visibility=True),
    EventEnvelope(id="run-guide:2", run_id="run-guide", seq=2, observed_at="2026-11-26T06:00:01Z",
                  type="action.verified", public_visibility=False),
)
first = sdk.read_page(events, run_id="run-guide")
second = sdk.read_page(events, run_id="run-guide", after_seq=first.next_seq, limit=1)
print([row.seq for row in first.rows], first.next_seq)
print(second.rows, second.next_seq)
try:
    EventSdk(selection).read_page(events, run_id="run-guide")
except EventSdkRejected:
    print("disabled: refused")
else:
    raise AssertionError("disabled event SDK must refuse")
```

```text
[1] 2
() 2
disabled: refused
```

The first page holds the public row but not the private one, `next_seq` is 2 past that complete
private tail, and the following bounded page is empty. The façade does not authenticate or
redact: envelopes arrive already typed. Not included: log, file or socket transport, runtime or
MCP wiring, provider or decider calls, game input, ROM proof, retention, delivery guarantees or
compatibility promises (#139).
