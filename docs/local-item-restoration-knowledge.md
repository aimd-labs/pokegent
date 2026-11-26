# Local item restoration knowledge

`FireRedKnowledge.item_restoration(item_id)` exposes the decoded `gItemEffectTable`
restoration subset over the same default-off seam as the
[local knowledge guide](local-knowledge.md). It reads only the caller's immutable
`GameTables`; it opens no file, cache, network endpoint or emulator. General item
facts have [their own page](local-item-knowledge.md).

## Contract

Construct `FireRedKnowledge(selection, tables, *, enabled=False)` as for moves and
species. `enabled` must be exactly `bool`; any other value such as `1` or `"yes"`
raises `TypeError`. Leaving it at the default `False` raises
`PluginRefusal(RefusalCode.DISABLED)`. The selection game must be `"firered"`, and
`tables.profile_id` must be a non-blank string equal to the descriptor's
`profile_id`, or construction raises `PluginRefusal(RefusalCode.PROFILE_MISMATCH)`
before any lookup.

The argument is a positive plain native item-table index, not a bag slot: `bool`,
`str`, `float`, `0` and negatives raise `ValueError` before lookup. Game, profile and
`rom_sha1` are copied from the selection and stay caller assertions; no digest is
recomputed and no trusted-source, installed-data, live-state or native proof is made.

## Result

A known answer is a frozen `LocalRomRecord` with `record_kind="item_restoration"`,
`record_id` equal to the native item index, `source_class="local_rom"` and
`namespace="native"`. `values` has exactly eight pairs in order: `heal_hp`,
`heal_hp_full`, `heal_hp_half`, `revive`, `cures`, `heal_pp`, `heal_pp_all_moves`,
then `sacred_ash`. `cures` is a sorted tuple of decoded status names, so the same set
always has one order. Scalars are copied unchanged, including `None` for "no fixed HP
amount" and "no PP argument".

`heal_pp == 0x7F` is the decoded full-PP sentinel, surfaced as the stored `127`; do
not read it as a claim that 127 PP is restored. `heal_hp_full` and `heal_hp_half`
are separate decoded flags, not amounts computed from current HP.

## Known and unknown

A missing item row (`KeyError`) or an item whose `effect` is `None` returns
`known=False` and `values=()`: unknown in this table, not proof the item has no
effect. A present `ItemEffect`, including an all-default one, returns `known=True`
with those defaults; it is the decoded struct, not a claim that use is harmless or
inert.

## Not provided

`raw`, `stat_boost`, `focus_energy`, `guard_spec`, `ev_gains`, `pp_up`, `pp_max` and
`level_up` stay out of the eight fields. This page adds no possession, bag or party
state, current HP, application target, legality, admission, provenance or native
claim, and no modern item ids or data. Other exceptions propagate unchanged.

## Synthetic example

```python
from dataclasses import replace

from pokegent.games.gen3.firered.knowledge import FireRedKnowledge
from pokegent.games.gen3.firered.rom.rom_tables import (
    GameTables, ItemData, ItemEffect, TypeChart,
)
from pokegent.games.plugin import (
    GamePluginDescriptor, PluginRefusal, PluginSelection, RefusalCode,
)

SHA = "0123456789abcdef0123456789abcdef01234567"
PROFILE = "synthetic-profile"
FIELDS = (
    "heal_hp", "heal_hp_full", "heal_hp_half", "revive",
    "cures", "heal_pp", "heal_pp_all_moves", "sacred_ash",
)


def item(item_id, *, effect=None):
    return ItemData(
        item_id=item_id, name="Synthetic Item", price=100, hold_effect=0,
        hold_effect_param=0, importance=0, pocket=1, item_type=0,
        field_use_func=0, battle_usage=0, battle_use_func=0, secondary_id=0,
        effect=effect,
    )


def tables(*items):
    return GameTables(
        moves=(), species=(), items=items,
        type_chart=TypeChart(bytes([0xFE, 0, 0, 0xFF, 0, 0])),
        ability_names=(), type_names=(), profile_id=PROFILE,
    )


def selection():
    return PluginSelection(
        descriptor=GamePluginDescriptor(
            plugin_id="synthetic-restoration-guide", game="firered",
            profile_id=PROFILE, contract_version=1, rom_sha1=SHA,
        ),
        services=None,
    )


def knowledge(*items):
    return FireRedKnowledge(selection(), tables(*items), enabled=True)


effect = ItemEffect(heal_hp=20, cures=frozenset({"sleep", "burn"}))
base = item(1)
rows = (item(0), replace(base, effect=effect), item(2), replace(base, item_id=3, effect=ItemEffect()))

try:
    FireRedKnowledge(selection(), tables(item(0)))
except PluginRefusal as refusal:
    assert refusal.code is RefusalCode.DISABLED
else:
    raise AssertionError("default-off construction did not refuse")

reader = knowledge(*rows)
known = reader.item_restoration(1)
values = dict(known.values)
assert known.known is True
assert (known.record_kind, known.record_id) == ("item_restoration", 1)
assert tuple(values) == FIELDS and len(FIELDS) == 8
assert values["heal_hp"] == 20
assert values["cures"] == ("burn", "sleep")
assert values["heal_hp_full"] is False and values["heal_pp"] is None

absent = reader.item_restoration(2)
assert absent.known is False and absent.values == ()

empty = reader.item_restoration(3)
assert empty.known is True
assert empty.values == (
    ("heal_hp", None), ("heal_hp_full", False), ("heal_hp_half", False),
    ("revive", False), ("cures", ()), ("heal_pp", None),
    ("heal_pp_all_moves", False), ("sacred_ash", False),
)

print(f"restoration ok: fields={len(FIELDS)} hp={values['heal_hp']} cures={values['cures']} unknown={absent.known}")
```

Invented bytes, SHA, ids and values; no ROM or shipped game data.
