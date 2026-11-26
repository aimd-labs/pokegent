# Local ability-name knowledge

`FireRedKnowledge.ability(ability_id)` answers native ability display names through the
default-off seam described in the [local knowledge guide](local-knowledge.md). It reads only
the caller's decoded `GameTables.ability_names` and opens no file, cache, network endpoint or
emulator. It does not register or start a runtime.

## Contract

Construct as `FireRedKnowledge(selection, tables, *, enabled=False)`. `enabled` must be exactly
`bool`; `1`, `"yes"` or an omitted value leaves it `False`, which raises
`PluginRefusal(RefusalCode.DISABLED)`. The selection game must be `"firered"`, and
`tables.profile_id` must be a non-blank string equal to the descriptor's `profile_id`, or
construction raises `PluginRefusal(RefusalCode.PROFILE_MISMATCH)` before any lookup.

`ability_id` is a native table index and must be a positive plain `int`. Zero, negatives,
`bool`, `str`, `float` and other types raise `ValueError` before lookup. Zero is excluded as
the sentinel value rather than looked up.

## Result

A known answer is a frozen `LocalRomRecord` with `record_kind="ability"`, `record_id` equal to
the queried id, and exactly two detached fields in order: `id` (the same id) and `name` (the
decoded string). `source_class="local_rom"`, `namespace="native"`, and `game`/`profile_id`/
`rom_sha1` are copied from the selection descriptor; they are caller assertions, not digest,
trusted-source, possession or native proof.

## Known, unknown and bounds

A known row is table content even when the decoded name is empty: a table whose second entry is
`""` returns `known=True` with `name=""`. That is a blank decoded known, not a typed unknown. A
row outside the supplied tuple makes `GameTables.ability_name` raise `KeyError`, which the seam
converts to `known=False` with `values=()`. It means "unknown in this table", not "no such
ability in the game". Other exceptions propagate unchanged.

Names are bounded to 64 characters, like the other local name fields. Exactly 64 characters is
accepted; 65 characters or a non-`str` value raises `ValueError`, and nothing is truncated.

## Not provided

No ability effects or battle modifiers, current party ability, hidden-ability discovery,
species-to-ability assignment, legality or admission decisions, modern names, external tables,
API fallback, cache, ROM digest, native proof or live-RAM state. This page documents only the
name lookup above and infers nothing beyond what `knowledge.py`, `GameTables.ability_name` and
the ability facts tests exercise.

## Synthetic example

```python
from pokegent.games.gen3.firered.knowledge import FireRedKnowledge
from pokegent.games.gen3.firered.rom.rom_tables import GameTables, TypeChart
from pokegent.games.plugin import (
    GamePluginDescriptor, PluginSelection, PluginRefusal, RefusalCode,
)

PROFILE = "synthetic-profile"
SHA = "0123456789abcdef0123456789abcdef01234567"


def tables(names):
    return GameTables(
        moves=(), species=(), items=(),
        type_chart=TypeChart(bytes([0xFE, 0, 0, 0xFF, 0, 0])),
        ability_names=tuple(names), type_names=(), profile_id=PROFILE,
    )


selection = PluginSelection(
    descriptor=GamePluginDescriptor(
        plugin_id="synthetic-ability-guide", game="firered", profile_id=PROFILE,
        contract_version=1, rom_sha1=SHA,
    ),
    services=None,
)

first, second = tables(("", "Synthetic Alpha")), tables(("", "Synthetic Beta"))
try:
    FireRedKnowledge(selection, first)
except PluginRefusal as refusal:
    assert refusal.code is RefusalCode.DISABLED
    print("default:", refusal.code.value)

reader = FireRedKnowledge(selection, first, enabled=True)
known, missing = reader.ability(1), reader.ability(2)
other = FireRedKnowledge(selection, second, enabled=True).ability(1)
assert known.known is True and known.record_id == 1
assert known.record_kind == "ability"
assert known.values == (("id", 1), ("name", "Synthetic Alpha"))
assert dict(other.values)["name"] == "Synthetic Beta"
assert missing.known is False and missing.values == () and missing.record_id == 2
print("ability facts ok:", dict(known.values)["name"], "/ missing unknown")
```

Invented bytes, SHA, ids and names; no ROM or shipped game data.
