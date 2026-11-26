# Local TM/HM knowledge

`FireRedKnowledge.tmhm(species_id, item_id)` answers native TM/HM compatibility through the
same default-off seam as the [local knowledge guide](local-knowledge.md). It reads only the
caller's decoded `GameTables` -- the bag-item-to-move mapping, per-species learnset rows, and
the existing move and species tables -- and opens no file, cache, network endpoint or
emulator. Nothing registers or starts a runtime.

## Contract

Construct as for moves and species: `FireRedKnowledge(selection, tables, *, enabled=False)`.
`enabled` must be exactly `bool`; any other value, such as `1` or `"yes"`, raises
`TypeError`. Leaving it at the default `False` raises
`PluginRefusal(RefusalCode.DISABLED)`. The selection game must be `"firered"`, and
`tables.profile_id` must be a non-blank string equal to the descriptor's `profile_id`, or
construction raises `PluginRefusal(RefusalCode.PROFILE_MISMATCH)` before any lookup.

Both arguments are native ids and must be positive plain `int`s; `bool`, `str`, `float`,
`0` and negatives raise `ValueError` before lookup. The first is a native species index. The
second is the native **bag TM/HM item id** (the standard TM01..TM50 and HM01..HM08 items
are 289..346), not a raw move id. `tmhm_move` resolves `item_id - 289` against the decoded
mapping, so the answer follows that mapping's length; the compatibility bit comes from the
item's slot for standard ids, not from a search for the move id.

## Result

A known answer is a frozen `LocalRomRecord` with `record_kind="tmhm"`, `record_id` equal to
the species index, and exactly three detached fields in order: `item_id` (the item asked
about), `move_id` (the move that item teaches) and `can_learn` (the ROM bit as `True` or
`False`). `source_class="local_rom"`, `namespace="native"`, and `game`/`profile_id`/
`rom_sha1` copied from the selection are caller assertions: no digest is recomputed and no
trusted-source, possession or native proof is made.

## Known and unknown

An answer is known only when the species row, an item mapping, a positive plain-`int` move
id at that slot, the move row and a long-enough learnset row all exist. A known
`can_learn=False` bit is a real negative answer, not an unknown.

Each of these returns `known=False` with `values=()`: a missing species row; no mapping for
the item in the supplied table (the id does not select a decoded slot); a mapped value that
is `0`, negative or not a plain `int`; a missing move row; a missing learnset row; or a
learnset row too short for the bit. A typed absence means "unknown in this table", not
"illegal in the game"; other exceptions propagate unchanged.

## Not provided

No possession, party-slot, bag-count, UI availability or "can use now" answer; no
move-learning legality (party, level, duplicate move, free slot); no acquisition
feasibility (badges, money, stock), TM/HM count, ROM digest, native proof or compatibility
beyond the decoded bit. No modern external tables, API fallback, cache or runtime
registration. Local facts stay separate from live RAM.

## Synthetic example

```python
from pokegent.games.gen3.firered.knowledge import FireRedKnowledge
from pokegent.games.gen3.firered.rom.rom_tables import (
    GameTables, MoveData, SpeciesData, TypeChart,
)
from pokegent.games.plugin import (
    GamePluginDescriptor, PluginRefusal, PluginSelection, RefusalCode,
)

PROFILE = "synthetic-profile"
SHA = "0123456789abcdef0123456789abcdef01234567"
TM01, SLOT_MOVE = 289, 33
FIELDS = ("item_id", "move_id", "can_learn")


def move(move_id, *, name="Synthetic Move", power=40):
    return MoveData(
        move_id=move_id, name=name, effect=0, power=power, type=1,
        accuracy=95, pp=20, secondary_chance=0, target=0, priority=0, flags=0,
    )


def species(species_id, *, name="Synthetic Mon"):
    return SpeciesData(
        species_id=species_id, name=name,
        base_hp=10, base_atk=20, base_def=30, base_spe=40, base_spa=50, base_spd=60,
        types=(1, 1), catch_rate=0, exp_yield=0,
        ev_yield=(0, 0, 0, 0, 0, 0), item_common=0, item_rare=0,
        gender_ratio=0, egg_cycles=0, friendship=0, growth_rate=0,
        egg_groups=(0, 0), abilities=(0, 0), safari_flee_rate=0,
    )


def rows(count, factory):
    return tuple(factory(index) for index in range(count))


def tables(*, learnsets):
    return GameTables(
        moves=rows(SLOT_MOVE + 1, move), species=rows(2, species), items=(),
        type_chart=TypeChart(bytes([0xFE, 0, 0, 0xFF, 0, 0])),
        ability_names=(), type_names=(), profile_id=PROFILE,
        tmhm_learnsets=learnsets, tmhm_moves=(SLOT_MOVE,),
    )


selection = PluginSelection(
    descriptor=GamePluginDescriptor(
        plugin_id="synthetic-tmhm-guide", game="firered", profile_id=PROFILE,
        contract_version=1, rom_sha1=SHA,
    ),
    services=None,
)

first = tables(learnsets=(b"\x01", b"\x01"))
second = tables(learnsets=(b"\x01", b"\x00"))

try:
    FireRedKnowledge(selection, first)
except PluginRefusal as refusal:
    assert refusal.code is RefusalCode.DISABLED
else:
    raise AssertionError("default-off construction did not refuse")

reader = FireRedKnowledge(selection, first, enabled=True)
compatible = reader.tmhm(1, TM01)
other = FireRedKnowledge(selection, second, enabled=True).tmhm(1, TM01)
missing = reader.tmhm(1, TM01 + 1)

values = dict(compatible.values)
assert compatible.known is True and compatible.record_id == 1
assert compatible.record_kind == "tmhm"
assert tuple(key for key, _ in compatible.values) == FIELDS
assert values == {"item_id": TM01, "move_id": SLOT_MOVE, "can_learn": True}
assert (compatible.source_class, compatible.namespace) == ("local_rom", "native")
assert (compatible.game, compatible.profile_id, compatible.rom_sha1) == (
    "firered", PROFILE, SHA,
)
assert other.known is True and dict(other.values)["can_learn"] is False
assert missing.known is False and missing.values == () and missing.record_id == 1

print("tmhm facts ok: compatible=True false_bit=False missing=unknown")
```

Invented bytes, SHA, ids and names; no ROM or shipped game data.
