# Objective SDK: local opt-in facade

`pokegent.sdk_objectives.ObjectiveSdk` is a thin, disabled-by-default facade over one
caller-owned `ObjectiveStore`. It calls that store locally only: no transport, server, MCP,
runtime, executor, backend, independent snapshot provider or game input. It adds no read path.

## Opt-in and trusted inputs

`ObjectiveSdk(selection, store, *, enabled=False)`:

- `selection` must be an admitted `PluginSelection` whose descriptor is the `firered`
  namespace; other games raise `ObjectiveSdkRejected`, a non-selection `TypeError`.
- `store` must be the caller's existing `ObjectiveStore`; another type raises `TypeError`.
- `enabled` is an exact bool, default `False`. `1`, `0`, `"true"` and `None` raise
  `TypeError`. Disabled `current()`/`propose()` refuse before any store or file access.
- The selection and store are trusted caller inputs. The facade does not prove ROM admission
  or a running process and adds no cryptographic binding.

## Reads and proposals

`current()` returns the store's typed `Objective | None`, including its expiry behavior.
`propose(data, *, expected_revision)` requires a mapping and a plain nonnegative int
`expected_revision` (`True`, `1.0`, `"0"` and `None` are refused). It copies the mapping,
forces `set_by="agent"` so a payload cannot impersonate an operator or script writer, and
forwards `store.propose(payload, expected_revision=...)`; the caller's mapping is unchanged.

The result is the store's own `Proposal(accepted, reasons, objective)`, unchanged. The
compare-and-set is the store's local plain-int revision check; the facade does not bump,
retry or reinterpret it. The store also owns schema, target and probe validation, source
priority, history, expiry, the revision floor and the derived already-satisfied gate. Its
already-bound admission snapshot may be read during `propose()`; the facade adds no snapshot.

Acceptance means one store write, not game completion or a live observation. There is no
runner wiring, title support, completion or durable progress. See [the objective
file](objective-file.md) for the schema and the [Choice SDK](choice-sdk.md) for the other
opt-in facade; limits remain open in #139.

## Complete synthetic example

```python
import hashlib
import tempfile
from pathlib import Path
from types import SimpleNamespace

from pokegent.games.plugin import GamePluginDescriptor, ReviewedPluginRegistry
from pokegent.goals.store import ObjectiveStore
from pokegent.sdk_objectives import ObjectiveSdk, ObjectiveSdkRejected

ROM, GAME, PROFILE = b"synthetic-objective-sdk-guide" * 4, "firered", "profile-synthetic"
descriptor = GamePluginDescriptor(
    plugin_id=f"{GAME}:{PROFILE}", game=GAME, profile_id=PROFILE, contract_version=1,
    rom_sha1=hashlib.sha1(ROM).hexdigest(), capabilities=frozenset({"objective_observation"}),
)
registry = ReviewedPluginRegistry(enabled=True)
registry.register(SimpleNamespace(descriptor=descriptor, compose=lambda: None))
selection = registry.select(rom_bytes=ROM, game=GAME, profile_id=PROFILE)

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    store = ObjectiveStore(root / "objective.json")
    disabled = ObjectiveSdk(selection, store)
    try:
        disabled.propose({"objective_id": "hold1", "revision": 1, "kind": "hold"},
                         expected_revision=0)
    except ObjectiveSdkRejected:
        pass
    else:
        raise AssertionError("disabled facade accepted a proposal")
    assert list(root.iterdir()) == []

    sdk = ObjectiveSdk(selection, store, enabled=True)
    accepted = sdk.propose({"objective_id": "hold1", "revision": 1, "kind": "hold"},
                           expected_revision=0)
    assert accepted.accepted is True and accepted.reasons == ()
    current = sdk.current()
    assert (current.objective_id, current.revision, current.set_by) == ("hold1", 1, "agent")

    stale = sdk.propose({"objective_id": "hold2", "revision": 2, "kind": "hold"},
                        expected_revision=0)
    assert stale.accepted is False
    assert [reason.code for reason in stale.reasons] == ["stale_revision"]
    assert store.current().revision == 1

print("ok: disabled untouched; hold1 accepted; stale expected0 refused; revision 1")
```

The example exits 0: the disabled facade leaves the temporary store untouched, `hold1` is
accepted with forced `set_by`, and the stale `expected_revision=0` proposal leaves revision 1.
