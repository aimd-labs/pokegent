# Local species breeding knowledge

`FireRedKnowledge.species_breeding(species_id)` returns three raw per-species breeding
parameters over the default-off [local knowledge](local-knowledge.md) seam. It reads only
caller `GameTables`, opening no file, cache, network endpoint or emulator.

## Contract

`FireRedKnowledge(selection, tables, *, enabled=False)`: `enabled` must be exactly `bool`;
non-`bool` raises `TypeError`, and leaving it out refuses with
`PluginRefusal(RefusalCode.DISABLED)`. The game must be `"firered"`, and `tables.profile_id`
must be a non-blank string equal to the descriptor `profile_id`; otherwise construction
raises `PluginRefusal(RefusalCode.PROFILE_MISMATCH)`. The argument is a positive plain `int`
native species index, not a national Dex number; `bool`, `str`, `float`, `0` and negatives raise `ValueError`.

## Result

A known answer is a frozen `LocalRomRecord` with `record_kind="species_breeding"`,
`record_id` the native index, `source_class="local_rom"` and `namespace="native"`;
`game`, `profile_id` and `rom_sha1` remain caller assertions. Values are exactly three
ordered pairs: raw `gender_ratio` (int), raw `egg_cycles` (int), and a detached two-item
`egg_groups` int tuple equal to the row's pair but not that same object.

## Known and unknown

A present row is known even for zero or `255`: those are data. A missing row becomes
`known=False`, `values=()`, a typed absence from this table only, not proof elsewhere;
other exceptions propagate. Malformed IDs fail before lookup.

## Limits

The raw `gender_ratio` is not current sex or a sex rule. `egg_cycles` is not a current step
counter or hatch estimate; no party, item, ability, bike or route state is read. Egg-group
IDs are not named groups, compatibility, egg availability, ownership, daycare acceptance or
legality. No modern data or fallback, current/live state, breeding plan, digest/provenance
or native proof is provided.

## Executed synthetic fence

The block below is the exact `fence.py` that ran; invented data only.

```python
"""R07 synthetic fence for documented species_breeding facts; invented data only."""

from dataclasses import replace

from pokegent.games.gen3.firered.knowledge import FireRedKnowledge
from pokegent.games.gen3.firered.rom.rom_tables import GameTables, SpeciesData, TypeChart
from pokegent.games.plugin import (
    GamePluginDescriptor, PluginRefusal, PluginSelection, RefusalCode,
)

PROFILE = "synthetic-breeding-profile"
SHA = "0123456789abcdef0123456789abcdef01234567"
FIELDS = ("gender_ratio", "egg_cycles", "egg_groups")


def species(i):
    return SpeciesData(
        species_id=i, name="Synthetic Mon", base_hp=1, base_atk=2, base_def=3,
        base_spe=4, base_spa=5, base_spd=6, types=(1, 1), catch_rate=0,
        exp_yield=0, ev_yield=(0, 0, 0, 0, 0, 0), item_common=0, item_rare=0,
        gender_ratio=0, egg_cycles=0, friendship=0, growth_rate=0,
        egg_groups=(0, 0), abilities=(0, 0), safari_flee_rate=0,
    )


def row(i, *, gender_ratio, egg_cycles, egg_groups):
    return replace(species(i), gender_ratio=gender_ratio,
                   egg_cycles=egg_cycles, egg_groups=egg_groups)


def tables(source):
    return GameTables(
        moves=(), species=(species(0),) * source.species_id + (source,), items=(),
        type_chart=TypeChart(bytes([0xFE, 0, 0, 0xFF, 0, 0])),
        ability_names=(), type_names=(), profile_id=PROFILE,
    )


selection = PluginSelection(
    descriptor=GamePluginDescriptor(
        plugin_id="synthetic-breeding-fence", game="firered", profile_id=PROFILE,
        contract_version=1, rom_sha1=SHA,
    ),
    services=None,
)

try:
    FireRedKnowledge(selection, tables(row(5, gender_ratio=31, egg_cycles=1,
                                          egg_groups=(2, 5))))
except PluginRefusal as refusal:
    assert refusal.code is RefusalCode.DISABLED
else:
    raise AssertionError("default-off construction did not refuse")

first_row = row(5, gender_ratio=31, egg_cycles=1, egg_groups=(2, 5))
first_tables = tables(first_row)
assert first_tables.species(5) is first_row
first = FireRedKnowledge(selection, first_tables, enabled=True).species_breeding(5)
assert (first.known, first.record_kind, first.record_id) == (True, "species_breeding", 5)
assert (first.source_class, first.namespace) == ("local_rom", "native")
assert tuple(dict(first.values)) == FIELDS
assert dict(first.values) == {"gender_ratio": 31, "egg_cycles": 1, "egg_groups": (2, 5)}

second = FireRedKnowledge(
    selection, tables(row(5, gender_ratio=254, egg_cycles=40, egg_groups=(15, 15))),
    enabled=True,
).species_breeding(5)
assert dict(second.values) != dict(first.values)

zero = FireRedKnowledge(
    selection, tables(row(5, gender_ratio=255, egg_cycles=0, egg_groups=(0, 255))),
    enabled=True,
).species_breeding(5)
assert zero.known is True
assert dict(zero.values) == {"gender_ratio": 255, "egg_cycles": 0, "egg_groups": (0, 255)}

missing = FireRedKnowledge(selection, first_tables, enabled=True).species_breeding(6)
assert missing.known is False and missing.values == ()
assert (missing.record_kind, missing.record_id, missing.namespace) == (
    "species_breeding", 6, "native",
)

groups = dict(first.values)["egg_groups"]
assert type(groups) is tuple and groups == first_tables.species(5).egg_groups
assert groups is not first_tables.species(5).egg_groups

for bad in (True, False, 0, -1, "5", 5.0):
    try:
        FireRedKnowledge(selection, first_tables, enabled=True).species_breeding(bad)
    except ValueError:
        pass
    else:
        raise AssertionError(f"species id {bad!r} did not raise ValueError")

print("species breeding ok: fields=3 first=(31,1,(2,5)) second=(254,40,(15,15))"
      " zero255=known missing=unknown detached=yes default=disabled")
```
Invented data only; no ROM or shipped game data.
