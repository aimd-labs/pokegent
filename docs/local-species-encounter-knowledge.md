# Local species encounter knowledge

`FireRedKnowledge.species_encounter(species_id)` reports four per-species encounter
parameters over the default-off seam in the [local knowledge guide](local-knowledge.md).
It reads only the caller's decoded `GameTables` species row and opens no file, cache,
network endpoint or emulator.

## Contract

`FireRedKnowledge(selection, tables, *, enabled=False)` takes `enabled` as exactly `bool`
(`TypeError` otherwise); leaving it out refuses with
`PluginRefusal(RefusalCode.DISABLED)`. Selection game must be `"firered"` and
`tables.profile_id` must be a non-blank string equal to the descriptor's `profile_id`, or
construction raises `PluginRefusal(RefusalCode.PROFILE_MISMATCH)` before any lookup.

The argument is a native species-table index, not a national Dex number, and must be a
positive plain `int`; `bool`, `str`, `float`, `0` and negatives raise `ValueError`.

## Result

A known answer is a frozen `LocalRomRecord` with `record_kind="species_encounter"`,
`record_id` equal to the species index, `source_class="local_rom"` and
`namespace="native"`. Values are exactly these four pairs in this order:

- `("catch_rate", int)`: raw capture parameter for the game's capture formula, not a
  spawn or encounter rate and not the current capture probability, which depends on live
  battle state this seam never reads.
- `("item_common", int)`, `("item_rare", int)`: native item IDs the table lists. Table
  possibilities only, not the current held item, ownership or bag contents.
- `("safari_flee_rate", int)`: raw Safari Zone parameter, not a computed flee chance.

## Known and unknown

A missing row returns `known=False` with `values=()`: unknown in this table, not proof of
absence. A present row is known even when all four values are zero; zero is data. Other
table exceptions propagate unchanged.

## Not provided

No spawn table, current capture probability, held item, ownership, computed flee chance,
modern data, computation, legality, availability, provenance, digest, admission or native
proof. Game, profile and `rom_sha1` are caller metadata, not digest, provenance, admission,
legality or native proof.

## Synthetic example

```python
from dataclasses import replace

from pokegent.games.gen3.firered.knowledge import FireRedKnowledge
from pokegent.games.gen3.firered.rom.rom_tables import GameTables, SpeciesData, TypeChart
from pokegent.games.plugin import (
    GamePluginDescriptor,
    PluginRefusal,
    PluginSelection,
    RefusalCode,
)

PROFILE = "synthetic-profile"
SHA = "0123456789abcdef0123456789abcdef01234567"
FILLER = SpeciesData(
    species_id=0, name="Filler", base_hp=1, base_atk=1, base_def=1, base_spe=1,
    base_spa=1, base_spd=1, types=(0, 0), catch_rate=0, exp_yield=0,
    ev_yield=(0, 0, 0, 0, 0, 0), item_common=0, item_rare=0, gender_ratio=0,
    egg_cycles=0, friendship=0, growth_rate=0, egg_groups=(0, 0),
    abilities=(0, 0), safari_flee_rate=0,
)


def tables(*rows):
    by_id = {row.species_id: row for row in rows}
    size = max(by_id) + 1
    native = tuple(by_id.get(i, replace(FILLER, species_id=i)) for i in range(size))
    return GameTables(
        moves=(), species=native, items=(),
        type_chart=TypeChart(bytes([0xFE, 0, 0, 0xFF, 0, 0])),
        ability_names=(), type_names=(), profile_id=PROFILE,
    )


selection = PluginSelection(
    descriptor=GamePluginDescriptor(
        plugin_id="synthetic-encounter-guide", game="firered",
        profile_id=PROFILE, contract_version=1, rom_sha1=SHA,
    ),
    services=None,
)


def reader(*rows):
    return FireRedKnowledge(selection, tables(*rows), enabled=True)


try:
    FireRedKnowledge(selection, tables(FILLER))
except PluginRefusal as refusal:
    assert refusal.code is RefusalCode.DISABLED
else:
    raise AssertionError("default-off construction did not refuse")

row = replace(
    FILLER, species_id=5, catch_rate=45, item_common=13, item_rare=14,
    safari_flee_rate=7,
)
first = reader(FILLER, row).species_encounter(5)
assert first.known is True and first.record_kind == "species_encounter"
assert first.record_id == 5 and first.namespace == "native"
assert first.values == (
    ("catch_rate", 45),
    ("item_common", 13),
    ("item_rare", 14),
    ("safari_flee_rate", 7),
)

second = reader(FILLER, replace(row, catch_rate=200)).species_encounter(5)
assert dict(second.values)["catch_rate"] == 200

zero = reader(FILLER, replace(row, catch_rate=0, item_common=0, item_rare=0,
                              safari_flee_rate=0)).species_encounter(5)
assert zero.known is True
assert dict(zero.values) == {
    "catch_rate": 0, "item_common": 0, "item_rare": 0, "safari_flee_rate": 0,
}

missing = reader(FILLER, replace(row, species_id=1)).species_encounter(2)
assert missing.known is False and missing.values == () and missing.record_id == 2

for bad in (True, 0, "5"):
    try:
        reader(FILLER, row).species_encounter(bad)
    except ValueError:
        continue
    raise AssertionError(f"id {bad!r} did not raise ValueError")

print("species encounter ok: fields=4 first=45 second=200 zero=known missing=unknown")
```

Invented bytes, SHA, ids and names; no ROM or shipped game data.
