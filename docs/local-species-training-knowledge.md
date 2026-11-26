# Local species training knowledge

`FireRedKnowledge.species_training(species_id)` reports four per-species training
parameters over the same default-off seam as the [local knowledge guide](local-knowledge.md).
It reads only the caller's decoded `GameTables` species row; it opens no file, cache,
network endpoint or emulator, and registers no runtime.

## Contract

Construct as for moves and species: `FireRedKnowledge(selection, tables, *, enabled=False)`.
`enabled` must be exactly `bool`; `1`, `0` or `"yes"` raises `TypeError`. Leaving it out
refuses with `PluginRefusal(RefusalCode.DISABLED)`. The selection game must be `"firered"`
and `tables.profile_id` must be a non-blank string equal to the descriptor's `profile_id`,
or construction raises `PluginRefusal(RefusalCode.PROFILE_MISMATCH)` before any lookup.

The argument is a native species-table index, not a national Dex number, and must be a
positive plain `int`; `bool`, `str`, `float`, `0` and negatives raise `ValueError` before
lookup.

## Result

A known answer is a frozen `LocalRomRecord` with `record_kind="species_training"`,
`record_id` equal to the species index, `source_class="local_rom"` and
`namespace="native"`. Values are exactly four pairs in this order:

- `("exp_yield", int)`: the row's raw base experience yield. It is not the experience a
  battle awards and includes no EXP Share, participant split or held-item multiplier.
- `("ev_yield", tuple)`: six raw effort-value yields in native struct order: hp, atk, def,
  spe, spa, spd. That is not the usual HP/Atk/Def/SpA/SpD/Spe display order.
- `("growth_rate", int)`: the native growth-rate ID from the row, not a curve name, table
  or formula, and not a level or experience calculation.
- `("friendship", int)`: the row's base friendship value, not current friendship in RAM.

## Known and unknown

A missing species row returns `known=False` with `values=()`: unknown in this table, not
proof the species cannot be trained. A present row is known even when every value is
zero; zero is data, not absence. Other table exceptions propagate unchanged.

## Not provided

No current friendship or RAM state, earned experience, EXP Share or held-item effect,
recommended EV allocation, level-up or training plan, legality, availability, provenance
or digest proof, native execution or runtime registration. The four values are raw decoded
fields; recommending how to spend them is outside this seam.

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


def species(species_id):
    return SpeciesData(
        species_id=species_id, name="Synthetic Mon",
        base_hp=10, base_atk=20, base_def=30, base_spe=40, base_spa=50, base_spd=60,
        types=(1, 1), catch_rate=0, exp_yield=0, ev_yield=(0, 0, 0, 0, 0, 0),
        item_common=0, item_rare=0, gender_ratio=0, egg_cycles=0, friendship=0,
        growth_rate=0, egg_groups=(0, 0), abilities=(0, 0), safari_flee_rate=0,
    )


def tables(row):
    return GameTables(
        moves=(), species=tuple(species(i) for i in range(row.species_id)) + (row,),
        items=(), type_chart=TypeChart(bytes([0xFE, 0, 0, 0xFF, 0, 0])),
        ability_names=(), type_names=(), profile_id=PROFILE,
    )


selection = PluginSelection(
    descriptor=GamePluginDescriptor(
        plugin_id="synthetic-training-guide", game="firered",
        profile_id=PROFILE, contract_version=1, rom_sha1=SHA,
    ),
    services=None,
)


def reader(row):
    return FireRedKnowledge(selection, tables(row), enabled=True)


try:
    FireRedKnowledge(selection, tables(species(5)))
except PluginRefusal as refusal:
    assert refusal.code is RefusalCode.DISABLED
else:
    raise AssertionError("default-off construction did not refuse")

first = replace(species(5), exp_yield=100, ev_yield=(1, 2, 3, 4, 5, 6),
                growth_rate=4, friendship=35)
second = replace(species(5), exp_yield=12, ev_yield=(6, 5, 4, 3, 2, 1),
                 growth_rate=1, friendship=0)

known = reader(first).species_training(5)
assert known.known is True and known.record_kind == "species_training"
assert known.values == (
    ("exp_yield", 100),
    ("ev_yield", (1, 2, 3, 4, 5, 6)),
    ("growth_rate", 4),
    ("friendship", 35),
)

other = reader(second).species_training(5)
assert dict(other.values)["exp_yield"] == 12
assert dict(other.values)["ev_yield"] == (6, 5, 4, 3, 2, 1)
assert dict(other.values)["friendship"] == 0

zero = reader(species(5)).species_training(5)
assert zero.known is True and dict(zero.values)["ev_yield"] == (0, 0, 0, 0, 0, 0)

missing = reader(first).species_training(6)
assert missing.known is False and missing.values == ()

try:
    reader(first).species_training(True)
except ValueError:
    pass
else:
    raise AssertionError("malformed species id did not raise ValueError")

print("species training ok: fields=4 first=100 second=12 zero=known missing=unknown")
```

Invented bytes, SHA, ids and names; no ROM or shipped game data.
