# Local evolution knowledge

`FireRedKnowledge.evolutions(species_id)` answers native evolution-row facts over the
same default-off seam as the [local knowledge guide](local-knowledge.md). It reads only
the caller's decoded `GameTables` (`gEvolutionTable` rows plus the species table); it
opens no file, cache, network endpoint or emulator, and registers no runtime. Item and
TM/HM facts have [their own page](local-item-knowledge.md) and
[a separate page](local-tmhm-knowledge.md).

## Contract

Construct as for moves and species:
`FireRedKnowledge(selection, tables, *, enabled=False)`. `enabled` must be exactly
`bool`; any other value such as `1` or `"yes"` raises `TypeError`. Leaving it at the
default `False` raises `PluginRefusal(RefusalCode.DISABLED)`. The selection game must
be `"firered"`, and `tables.profile_id` must be a non-blank string equal to the
descriptor's `profile_id`, or construction raises
`PluginRefusal(RefusalCode.PROFILE_MISMATCH)` before any lookup.

The argument is a native species-table index, not a national Dex number, and must be a
positive plain `int`; `bool`, `str`, `float`, `0` and negatives raise `ValueError`
before lookup. Game, profile and `rom_sha1` are copied from the selection descriptor
and stay caller assertions: no digest is recomputed and no trusted-source, installed
data or live-state proof is made.

## Result

A known answer is a frozen `LocalRomRecord` with `record_kind="evolution"`,
`record_id` equal to the species index, `source_class="local_rom"`,
`namespace="native"`, and one detached field:
`("evolutions", triples)`. `triples` is a tuple of at most five
`(method, parameter, target_species)` raw native integers in decoded row order,
padding included. A sixth slot raises `ValueError`; rows are refused, never
truncated or repacked. `method`, `parameter` and `target_species` are raw struct
integers: no method name, level, item or condition meaning is inferred.

## Known and unknown

An answer is known only when the species row exists and a decoded evolution row exists
for it. Both checks are separate:

- A present row may be empty: `known=True` with `("evolutions", ())`. This is a real
  decoded "no evolution entries", not an unknown.
- A missing evolution row for a known species, or a missing species row even when a
  row exists for that index, returns `known=False` with `values=()`. That means
  "unknown in this table", not "cannot evolve in the game".

`GameTables.evolutions_for(species)` still returns `()` both for a present empty row
and for an absent row; `GameTables.has_evolution_data(species)` is the row-existence
check the adapter uses. A decoded `(0, 0, 0)` slot stays a raw triple; it is not
silently dropped.

## Not provided

No inferred evolution method names, level or item requirements, item possession,
availability, legality, party or box state, runtime registration, native execution,
or digest proof. A raw method/parameter pair is not a claim that the evolution is
reachable now. Local facts stay separate from live RAM, and other exceptions
propagate unchanged.

## Synthetic example

```python
from pokegent.games.gen3.firered.knowledge import FireRedKnowledge
from pokegent.games.gen3.firered.rom.rom_tables import (
    EvolutionData, GameTables, SpeciesData, TypeChart,
)
from pokegent.games.plugin import (
    GamePluginDescriptor, PluginRefusal, PluginSelection, RefusalCode,
)

PROFILE = "synthetic-profile"
SHA = "0123456789abcdef0123456789abcdef01234567"
ROW = (
    EvolutionData(4, 16, 2),
    EvolutionData(7, 21, 3),
    EvolutionData(0, 0, 0),
    EvolutionData(0, 0, 0),
    EvolutionData(0, 0, 0),
)


def species(species_id, *, name="Synthetic Mon"):
    return SpeciesData(
        species_id=species_id, name=name,
        base_hp=10, base_atk=20, base_def=30, base_spe=40, base_spa=50, base_spd=60,
        types=(1, 1), catch_rate=0, exp_yield=0, ev_yield=(0, 0, 0, 0, 0, 0),
        item_common=0, item_rare=0, gender_ratio=0, egg_cycles=0, friendship=0,
        growth_rate=0, egg_groups=(0, 0), abilities=(0, 0), safari_flee_rate=0,
    )


def selection(*, profile_id=PROFILE):
    return PluginSelection(
        descriptor=GamePluginDescriptor(
            plugin_id="synthetic-evolution-guide", game="firered",
            profile_id=profile_id, contract_version=1, rom_sha1=SHA,
        ),
        services=None,
    )


def tables(*, last_species=2, evolutions=()):
    return GameTables(
        moves=(), species=tuple(species(i) for i in range(last_species + 1)),
        items=(), type_chart=TypeChart(bytes([0xFE, 0, 0, 0xFF, 0, 0])),
        ability_names=(), type_names=(), profile_id=PROFILE,
        evolutions=tuple(evolutions),
    )


try:
    FireRedKnowledge(selection(), tables(evolutions=((), ROW)))
except PluginRefusal as refusal:
    assert refusal.code is RefusalCode.DISABLED
else:
    raise AssertionError("default-off construction did not refuse")

reader = FireRedKnowledge(selection(), tables(evolutions=((), ROW)), enabled=True)
known = reader.evolutions(1)
assert known.known is True and known.record_id == 1
assert known.record_kind == "evolution"
assert known.values == (
    ("evolutions", ((4, 16, 2), (7, 21, 3), (0, 0, 0), (0, 0, 0), (0, 0, 0))),
)

empty = FireRedKnowledge(selection(), tables(evolutions=((), ())), enabled=True).evolutions(1)
assert empty.known is True and empty.values == (("evolutions", ()),)

missing = FireRedKnowledge(
    selection(), tables(last_species=2, evolutions=((),)), enabled=True
).evolutions(2)
assert missing.known is False and missing.values == ()

print("evolution facts ok: triple=(4,16,2) empty=known missing-row=unknown")
```

Invented bytes, SHA, ids and names; no ROM or shipped game data.
