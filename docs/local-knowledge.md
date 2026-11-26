# Local knowledge guide

`pokegent/games/gen3/firered/knowledge.py` is a default-off, read-only seam over the
`GameTables` already decoded from one FireRed ROM. Nothing imports it from the default
runtime; it opens no file, cache, network endpoint or emulator.

`move`/`species` are covered here; `item` facts have their own page: [local item knowledge](local-item-knowledge.md), and evolution facts have [their own page](local-evolution-knowledge.md).

TM/HM compatibility has [its own page](local-tmhm-knowledge.md).

Ability names have [their own page](local-ability-knowledge.md).

National Dex mapping has [its own page](local-national-dex-knowledge.md).

Item restoration facts have [their own page](local-item-restoration-knowledge.md).

Per-species training parameters have [their own page](local-species-training-knowledge.md).

Item training facts have [their own page](local-item-training-knowledge.md).

Per-species encounter parameters have [their own page](local-species-encounter-knowledge.md).

## Contract

Construct it as `FireRedKnowledge(selection, tables, *, enabled=False)`. `selection` is a
`PluginSelection` from an explicitly enabled `ReviewedPluginRegistry`; `tables` is the
existing decoded `GameTables`. `enabled` must be exactly `bool`; leaving it out refuses
with `PluginRefusal(RefusalCode.DISABLED)`. The selection game must be `"firered"` and its
non-blank `profile_id` must equal `tables.profile_id`, or construction raises
`PluginRefusal(RefusalCode.PROFILE_MISMATCH)` before any lookup. Game, profile and
`rom_sha1` are caller assertions; the seam recomputes no digest and makes no
trusted-source, native or live-state claim.

## Records

`move(id)` and `species(id)` return a frozen `LocalRomRecord` with `source_class="local_rom"`,
`namespace="native"`, `record_kind`, `record_id`, `known` and detached `values` tuples, plus
`game`, `profile_id` and `rom_sha1` copied from the selection descriptor. IDs are native
table indexes, not national Dex numbers. Values are bounded to eight pairs.

A table `KeyError` becomes `known=False` with `values=()`: a typed absence from this table,
not proof the fact does not exist elsewhere. Other exceptions propagate unchanged. An ID
that is not a positive plain `int` (including `bool`, strings, `0`, negatives and floats)
raises `ValueError` before lookup.

Move category uses the Gen 3 type split, not a modern per-move damage class: types below
`TYPE_MYSTERY` are `"physical"`, types above are `"special"`, and `TYPE_MYSTERY` is
`"unknown"`. `power == 0` is reported first as `"zero_base_power"`, never `"status"`: the
decoded format does not separate status moves from fixed- or variable-damage moves.

Not provided: remote or API fallback, data loading, cache, live legality, action admission,
native execution, digest/provenance proof or national-Dex mapping. External advisory
providers are unwired; local facts stay separate from live RAM and unknown values are not
replaced with modern defaults. This is the existing direct API; plugin-factory work is separate.

## Synthetic example

```python
import hashlib
from pokegent.games.gen3 import mechanics_constants as K
from pokegent.games.gen3.firered.knowledge import FireRedKnowledge
from pokegent.games.gen3.firered.rom.rom_tables import GameTables, MoveData, TypeChart
from pokegent.games.plugin import (
    GamePluginDescriptor,
    PluginRefusal,
    ReviewedPluginRegistry,
    RefusalCode,
)

GAME, PROFILE = "firered", "synthetic-profile"
ROM = b"invented-rom-bytes-not-a-game-image"

class Plugin:
    descriptor = GamePluginDescriptor(
        plugin_id="synthetic-knowledge-plugin", game=GAME, profile_id=PROFILE,
        contract_version=1, rom_sha1=hashlib.sha1(ROM).hexdigest(),
    )

    def compose(self):
        return None

def move(move_id, name, power, type_id):
    return MoveData(move_id, name, 0, power, type_id, 100, 10, 0, 0, 0, 0)


tables = GameTables(
    moves=(
        move(0, "Placeholder", 1, K.TYPE_NORMAL),
        move(1, "Synthetic Strike", 40, K.TYPE_NORMAL),
    ),
    species=(),
    items=(),
    type_chart=TypeChart(bytes([0xFE, 0, 0, 0xFF, 0, 0])),
    ability_names=(),
    type_names=(),
    profile_id=PROFILE,
)

registry = ReviewedPluginRegistry(enabled=True)
registry.register(Plugin())
selection = registry.select(rom_bytes=ROM, game=GAME, profile_id=PROFILE)
try:
    FireRedKnowledge(selection, tables)
except PluginRefusal as refusal:
    assert refusal.code is RefusalCode.DISABLED
    print("default:", refusal.code.value)

reader = FireRedKnowledge(selection, tables, enabled=True)
known, missing = reader.move(1), reader.move(2)
print("known:", known.known, dict(known.values))
print("unknown:", missing.known, missing.values)
assert dict(known.values)["category"] == "physical"
assert missing.known is False and missing.values == ()
```

Invented bytes, IDs, names and types; no ROM or shipped game data.
