# Local item training knowledge

`FireRedKnowledge.item_training(item_id)` exposes the decoded `gItemEffectTable`
training subset over the default-off [local knowledge guide](local-knowledge.md) seam.
It reads only caller `GameTables`, opening no file, cache, network or emulator.
Item and restoration facts have [item](local-item-knowledge.md) and [restoration](local-item-restoration-knowledge.md) pages.
## Contract
Construct `FireRedKnowledge(selection, tables, *, enabled=False)` as for moves and
species. `enabled` must be exactly `bool`; any other value such as `1` or `"yes"`
raises `TypeError`. Leaving it at the default `False` raises
`PluginRefusal(RefusalCode.DISABLED)`. The selection game must be `"firered"`, and
`tables.profile_id` must be a non-blank string equal to the descriptor's `profile_id`,
or construction raises `PluginRefusal(RefusalCode.PROFILE_MISMATCH)` before any lookup.

The argument is a positive plain native item-table index, not a bag slot: `bool`,
`str`, `float`, `0` and negatives raise `ValueError` before lookup. Game, profile and
`rom_sha1` are copied from the selection and stay caller assertions; no digest is
recomputed and no trusted-source, installed-data, live-state or native proof is made.
## Result
A known answer is a frozen `LocalRomRecord` with `record_kind="item_training"`,
`record_id` equal to the native item index, `source_class="local_rom"`,
`namespace="native"`, and exactly four ordered pairs: `ev_gains`, `pp_up`, `pp_max`, `level_up`.

`ev_gains` is a detached tuple of `(stat, amount)` tuples copied from the decoded
effect. The decoder emits native stat strings `hp`, `attack`, `defense`, `speed`,
`sp_defense` and `sp_attack`, appended in flag/argument order, not sorted and not
renamed; the query copies them unchanged and normalizes nothing. `pp_up`, `pp_max` and
`level_up` are decoded booleans.

A missing row (`KeyError`) or an item whose `effect` is `None` returns `known=False`,
`values=()`. A present `ItemEffect`, including all-default, returns `known=True` with
all four decoded fields. These flags are decoded facts about the supplied table, not
proof a target accepts the change, nor current EV, PP, level, compatibility, possession or legality.

The parser reads each EV argument as one unsigned byte; a synthetic negative amount
passes through unchanged, but that does not prove signed ROM decoding (`u8` input).
## Not provided
`raw`, `stat_boost`, `focus_energy`, `guard_spec` and the restoration fields
(`heal_hp`, `heal_hp_full`, `heal_hp_half`, `revive`, `cures`, `heal_pp`,
`heal_pp_all_moves`, `sacred_ash`) stay out. No possession, party, current-stat,
application-target, legality, admission, provenance or native claim is added, and no
modern item ids or data. Other exceptions propagate unchanged.
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
FIELDS = ("ev_gains", "pp_up", "pp_max", "level_up")


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
            plugin_id="synthetic-training-guide", game="firered",
            profile_id=PROFILE, contract_version=1, rom_sha1=SHA,
        ),
        services=None,
    )


def reader(*items):
    return FireRedKnowledge(selection(), tables(*items), enabled=True)


try:
    FireRedKnowledge(selection(), tables(item(0)))
except PluginRefusal as refusal:
    assert refusal.code is RefusalCode.DISABLED
else:
    raise AssertionError("default-off construction did not refuse")

effect = ItemEffect(
    ev_gains=(("hp", 10), ("sp_attack", 4)),
    pp_up=True, pp_max=True, level_up=True,
)
known = reader(item(0), replace(item(1), effect=effect)).item_training(1)
values = dict(known.values)
assert known.known is True and known.record_kind == "item_training"
assert tuple(values) == FIELDS and len(FIELDS) == 4
assert values == {
    "ev_gains": (("hp", 10), ("sp_attack", 4)),
    "pp_up": True, "pp_max": True, "level_up": True,
}

other = reader(item(0), replace(item(1), effect=ItemEffect(
    ev_gains=(("attack", 2),), level_up=True,
)))
other_values = dict(other.item_training(1).values)
assert other_values["ev_gains"] == (("attack", 2),) and other_values["level_up"] is True
assert other_values["pp_up"] is False and other_values["pp_max"] is False

empty = reader(item(0), replace(item(1), effect=ItemEffect())).item_training(1)
assert empty.known is True and empty.values == (
    ("ev_gains", ()), ("pp_up", False), ("pp_max", False), ("level_up", False),
)
missing = reader(item(0), item(1)).item_training(2)
absent_reader = reader(item(0), item(1), replace(item(2), effect=None))
absent = absent_reader.item_training(2)
assert absent_reader.item(2).known is True and absent.known is False
for fact in (missing, absent):
    assert fact.known is False and fact.values == ()

pairs = [("attack", -3), ("hp", 7), ("defense", 0)]
detached = reader(item(0), replace(item(1), effect=replace(ItemEffect(), ev_gains=pairs)))
gains = dict(detached.item_training(1).values)["ev_gains"]
assert isinstance(gains, tuple) and all(isinstance(pair, tuple) for pair in gains)
assert gains == (("attack", -3), ("hp", 7), ("defense", 0)) and gains is not pairs

print(f"training ok: fields={len(FIELDS)} ev={values['ev_gains']} "
      f"unknown={missing.known} detached={gains}")
```

Invented bytes, SHA, ids and values; no ROM or shipped game data.
