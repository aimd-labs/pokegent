# Local National Dex knowledge

`FireRedKnowledge.national_dex(species_id)` answers one National Dex number for
an existing native species row over the same default-off seam as the
[local knowledge guide](local-knowledge.md). It reads only the caller's decoded
`GameTables` (the `sSpeciesToNationalPokedexNum` values included); it opens no
file, cache, network endpoint or emulator, and starts no installed-data or
transport runtime.

## Contract

Construct as for moves and species:
`FireRedKnowledge(selection, tables, *, enabled=False)`. `enabled` must be
exactly `bool`; any other value raises `TypeError`, and leaving the default
`False` raises `PluginRefusal(RefusalCode.DISABLED)`. The selection game must be
`"firered"`, and `tables.profile_id` must be a non-blank string equal to the
descriptor's `profile_id`; otherwise construction raises
`PluginRefusal(RefusalCode.PROFILE_MISMATCH)` before any lookup.

Game, profile and `rom_sha1` are copied from the selection descriptor and stay
caller assertions. No digest is recomputed, and the seam makes no
trusted-source, admission, action-legality or native-execution proof. A record
is local read-only data, not permission to act.

## Input and record

The argument is a native species-table id, not a National Dex number, and must
be a positive plain `int`; `bool`, `str`, `float`, `0` and negatives raise
`ValueError` before lookup. The seam never converts the input into a National
Dex id before lookup: it first requires a species row at that native id, then
reads the supplied mapping positionally.

A known answer is a frozen `LocalRomRecord` with `record_kind="national_dex"`,
`record_id` equal to the native species id, `source_class="local_rom"`,
`namespace="native"`, and exactly two detached fields in order:
`("species_id", species_id)` and `("national_dex", number)`. The metadata still
names the caller-asserted game, profile and `rom_sha1`.

## Known and unknown

A known answer needs both a present species row and a positive plain-integer
mapping value. The species-row check comes first, so a missing row is unknown
even when the mapping array has an entry for that position.

`known=False` with `values=()` covers an absent species row, an absent or short
mapping table, an out-of-range native id, and a mapping value that is zero,
negative or not a plain `int` (for example `None`, `True`, `"25"` or `25.0`).
That is a typed absence from this table, not proof that the game has no such
species or National number. Other exceptions propagate unchanged.

## Decoding

`pokegent/games/gen3/firered/rom/species_dex.py` reads the user's
`sSpeciesToNationalPokedexNum` table. The ROM array is indexed by internal
species minus one (pret `pokemon.c:39`), so native id `N` uses entry `N - 1`.
The helper returns `None` when the id is outside `1..len(values)` or when the
stored value is falsy, which is how a zero or missing mapping becomes unknown.
Out-of-range native ids stay unknown rather than being clamped.

`decode_species_dex` accepts exactly 411 little-endian `uint16` entries
(`COUNT * 2` bytes), requires the first 251 values to equal `1..251`, and zeroes
zero-based positions `251..275` (native ids `252..276`), the legacy Unown slots.
It rejects values above 386 in the retained ranges and above `COUNT` in the
legacy range, returning an empty tuple. `read_species_dex` returns that empty
tuple on a profile, read or value error, and no modern or bundled species
mapping is substituted.

## Not provided

No modern or bundled National Dex mapping, reverse National-to-native lookup,
cache, data loading, network fallback, live-RAM read, seen/caught flag, party or
box possession, ownership, collection or action legality, admission, native
execution, digest or provenance proof. Unknown values are not replaced with
modern defaults, and a mapped number is not evidence that the species is owned,
seen or obtainable. The local facts stay separate from live game state.

## Synthetic example

```python
from pokegent.games.gen3.firered.knowledge import FireRedKnowledge
from pokegent.games.gen3.firered.rom.rom_tables import GameTables, SpeciesData, TypeChart
from pokegent.games.plugin import (
    GamePluginDescriptor, PluginRefusal, PluginSelection, RefusalCode,
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


def selection():
    return PluginSelection(
        descriptor=GamePluginDescriptor(
            plugin_id="synthetic-national-dex-guide", game="firered",
            profile_id=PROFILE, contract_version=1, rom_sha1=SHA,
        ),
        services=None,
    )


def tables(species_dex=(), *, species_rows=3):
    return GameTables(
        moves=(), species=tuple(species(i) for i in range(species_rows)),
        items=(), type_chart=TypeChart(bytes([0xFE, 0, 0, 0xFF, 0, 0])),
        ability_names=(), type_names=(), profile_id=PROFILE,
        species_dex=tuple(species_dex),
    )


try:
    FireRedKnowledge(selection(), tables((0, 25)))
except PluginRefusal as refusal:
    assert refusal.code is RefusalCode.DISABLED
else:
    raise AssertionError("default-off construction did not refuse")

first = FireRedKnowledge(selection(), tables((0, 25)), enabled=True).national_dex(2)
assert first.known is True and first.record_id == 2
assert first.record_kind == "national_dex"
assert first.values == (("species_id", 2), ("national_dex", 25))

second = FireRedKnowledge(selection(), tables((0, 151)), enabled=True).national_dex(2)
assert dict(second.values)["national_dex"] == 151

missing = FireRedKnowledge(selection(), tables(species_rows=3), enabled=True).national_dex(2)
assert missing.known is False and missing.values == ()

print("national dex facts ok: id=2 -> 25; second table -> 151; no mapping -> unknown")
```

Invented bytes, SHA, species rows and numbers; no ROM or shipped game data.
