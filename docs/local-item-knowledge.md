# Local item knowledge

`pokegent/games/gen3/firered/knowledge.py` answers `item(id)` over the same default-off
seam as the [local knowledge guide](local-knowledge.md), using only the caller's
decoded `GameTables`; it opens no file, cache, network endpoint or emulator, and starts
no transport, fetch or installed-game-data runtime.

## Contract

Construct `FireRedKnowledge(selection, tables, *, enabled=False)` as for moves and
species. `enabled` must be exactly `bool`; leaving it out raises
`PluginRefusal(RefusalCode.DISABLED)`. The selection game must be `"firered"` and
`tables.profile_id` a non-blank string equal to the descriptor's `profile_id`, or
construction raises `PluginRefusal(RefusalCode.PROFILE_MISMATCH)` before any lookup.
Game, profile and `rom_sha1` stay caller assertions: no digest is recomputed and no
trusted-source, live-state or native proof is made.

## Records and fields

`item(id)` returns a frozen `LocalRomRecord` with `source_class="local_rom"`,
`namespace="native"`, `record_kind="item"`, `record_id`, `known`, detached `values`,
and `game`/`profile_id`/`rom_sha1` copied from the selection. The id is a native
item-table index, not a bag slot, and must be a positive plain `int`: `bool`, `str`,
`float`, `0` and negatives raise `ValueError` before lookup, so index `0` is never
addressable. A table `KeyError` is a typed absence (`known=False`, `values=()`):
outside this table, not absent from the game. A known item has exactly seven
primitive fields in order: `id`, `name`, `price`, `pocket`, `hold_effect`,
`hold_effect_param`, `battle_usage`. `name` is a `str` of at most 64 characters or
`ValueError`; the rest are integers. A record is capped at eight pairs and the item
record already uses seven.

`hold_effect`, `hold_effect_param` and `battle_usage` are raw native struct integers:
not named contemporary effects, currently held item, live possession, usable-effect
model or legality evidence. None is a ROM digest, native proof or installed-data
claim; `pocket` is a native pocket id and live bag/ownership facts stay elsewhere.

## Synthetic example

```python
from pokegent.games.gen3.firered.knowledge import FireRedKnowledge
from pokegent.games.gen3.firered.rom.rom_tables import GameTables, ItemData, TypeChart
from pokegent.games.plugin import GamePluginDescriptor, PluginRefusal, PluginSelection, RefusalCode

SHA = "0123456789abcdef0123456789abcdef01234567"
FIELDS = ("id", "name", "price", "pocket", "hold_effect", "hold_effect_param", "battle_usage")


def item(item_id, *, name="Placeholder", price=0):
    return ItemData(
        item_id=item_id, name=name, price=price, hold_effect=0, hold_effect_param=0,
        importance=0, pocket=1, item_type=0, field_use_func=0, battle_usage=0,
        battle_use_func=0, secondary_id=0, effect=None,
    )


tables = GameTables(
    moves=(), species=(),
    items=(item(0), item(1, name="Synthetic Item", price=100)),
    type_chart=TypeChart(bytes([0xFE, 0, 0, 0xFF, 0, 0])),
    ability_names=(), type_names=(), profile_id="synthetic-profile",
)
selection = PluginSelection(
    descriptor=GamePluginDescriptor(
        plugin_id="synthetic-item-guide", game="firered",
        profile_id="synthetic-profile", contract_version=1, rom_sha1=SHA,
    ),
    services=None,
)

try:
    FireRedKnowledge(selection, tables)
except PluginRefusal as refusal:
    assert refusal.code is RefusalCode.DISABLED
else:
    raise AssertionError("default-off construction did not refuse")

reader = FireRedKnowledge(selection, tables, enabled=True)
fact = reader.item(1)
values = dict(fact.values)
assert fact.known is True and fact.record_id == 1 and fact.record_kind == "item"
assert (fact.source_class, fact.namespace) == ("local_rom", "native")
assert (fact.game, fact.profile_id, fact.rom_sha1) == ("firered", "synthetic-profile", SHA)
assert tuple(key for key, _ in fact.values) == FIELDS
assert values["name"] == "Synthetic Item"
assert all(type(value) in (int, str) for value in values.values())

missing = reader.item(2)
assert missing.known is False and missing.values == ()

try:
    reader.item(0)
except ValueError:
    pass
else:
    raise AssertionError("item id 0 was accepted")

print(f"item facts ok: known={fact.known} fields={len(values)} unknown={missing.known} id0=refused")
```

Invented bytes, SHA, names and prices; no ROM or shipped game data.
