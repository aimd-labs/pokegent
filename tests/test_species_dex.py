"""Synthetic ROM table and snapshots; no extracted mapping or game content."""

import struct
from types import SimpleNamespace as NS

import pytest

from pokegent.actions.battle.builder import BattleBuilder
from pokegent.actions.battle.catch_facts import foe_facts
from pokegent.actions.battle.lookups import Lookups, collect_lookups
from pokegent.actions.battle.romtables import RomTables
from pokegent.actions.battle.state import BattleBlock
from pokegent.dashboard.game import battle_block, party_row
from pokegent.dashboard.names import NameBook
from pokegent.dashboard.public import _battle, _party_row
from pokegent.games.gen3.firered.reader.memory_port import SnapshotMemoryReader
from pokegent.games.gen3.firered.rom.rom_data import load_profiles
from pokegent.games.gen3.firered.rom.rom_tables import GameTables, TypeChart
from pokegent.games.gen3.firered.rom.species_dex import (
    TABLE,
    decode_species_dex,
    national_dex,
    read_species_dex,
)
from pokegent.options.models import Legality, Option


def raw_table():
    # Mostly zero, with invented later entries to prove lookup rather than offset arithmetic.
    values = list(range(1, 252)) + [0] * 160
    values[276] = 252
    values[299] = 281
    values[251] = 400  # reserved slot may hold an out-of-dex flag in a real ROM
    return struct.pack("<411H", *values)


def tables():
    profile = load_profiles().get("firered-us-rev0")
    address = profile.rom_table(TABLE).address
    port = SnapshotMemoryReader({address: raw_table()})
    return RomTables(port, profile), port, profile


@pytest.mark.parametrize(
    "species,dex",
    [(1, 1), (251, 251), (277, 252), (300, 281), (0, None), (252, None), (276, None), (412, None)],
)
def test_runtime_lookup(species, dex):
    table, port, profile = tables()
    assert national_dex(read_species_dex(port, profile), species) == dex
    assert table.national_dex(species) == dex


@pytest.mark.parametrize("mutation", ["short", "anchor", "range", "reserved"])
def test_invalid_table_fails_closed(mutation):
    raw = bytearray(raw_table())
    if mutation == "short":
        raw = raw[:-1]
    else:
        index, value = {"anchor": (0, 2), "range": (276, 387), "reserved": (251, 412)}[mutation]
        struct.pack_into("<H", raw, index * 2, value)
    assert decode_species_dex(bytes(raw)) == ()


def test_missing_table_or_port():
    _, _, profile = tables()
    assert read_species_dex(SnapshotMemoryReader({}), profile) == ()
    profile = profile.model_copy(update={"rom_tables": {}})
    assert read_species_dex(SnapshotMemoryReader({}), profile) == ()


def view(species, known=True):
    table, _, _ = tables()
    lookups = collect_lookups(table, species={species}, abilities=set(), moves=set(), items=set())
    return NS(
        kind="wild",
        tables=Lookups(lookups) if known else None,
        opponent=NS(species=species, name="Synthetic", hp_percent=50),
        block=BattleBlock(active=True),
        party=(),
    )


@pytest.mark.parametrize("species,dex", [(251, 251), (277, 252), (300, 281)])
@pytest.mark.parametrize("owned", [True, False])
def test_catch_and_builder_use_national_numbers(monkeypatch, species, dex, owned):
    v = view(species)
    # When unowned, include the internal number as a trap for Gen-3 comparisons.
    flags = (dex,) if owned else ((species,) if species != dex else ())
    snapshot = NS(pokedex=NS(owned=flags, seen=flags))
    facts = foe_facts(v, snapshot, {"success_probes": [{"kind": "dex_owned", "species": dex}]})
    assert facts["wanted"] is True
    assert facts["foe_new_to_pokedex"] is not owned
    builder = BattleBuilder()
    ball = Option(
        id="ball:1", label="Synthetic", description="Synthetic", legality=Legality.UNKNOWN
    )
    for method in ("_move_options", "_switch_options", "_item_options"):
        monkeypatch.setattr(builder, method, lambda *a, **kw: [])
    monkeypatch.setattr(builder, "_ball_options", lambda *a: [ball])
    monkeypatch.setattr(builder, "_escape_option", lambda *a: None)
    monkeypatch.setattr(builder, "_context", lambda *a: {})
    monkeypatch.setattr(builder, "_fp", lambda *a: "synthetic")
    ctx = NS(
        snapshot=snapshot, objective=None, objective_kind=None, scene=NS(scene_id="battle.action")
    )
    facts = builder._action(ctx, v, "").options[0].facts
    assert facts["foe_new_to_pokedex"] is not owned
    assert facts["foe_seen_in_pokedex"] is owned


@pytest.mark.parametrize("species,known", [(277, False), (252, True), (412, True)])
def test_unknown_mapping_never_claims_new_or_wanted(species, known):
    facts = foe_facts(
        view(species, known),
        NS(pokedex=NS(owned=())),
        {"success_probes": [{"kind": "dex_owned", "species": species}]},
    )
    assert facts["wanted"] is None
    assert facts["foe_new_to_pokedex"] is None
    assert "unknown" in facts["wild_target"]


@pytest.mark.parametrize(
    "species,dex,legacy", [(251, 251, 251), (277, 252, None), (300, 281, None), (252, None, None)]
)
def test_operator_and_public_species_fields(species, dex, legacy):
    names = NameBook(
        GameTables(
            moves=(),
            species=(),
            items=(),
            type_chart=TypeChart(bytes([254, 0, 0, 255, 0, 0])),
            ability_names=(),
            type_names=(),
            species_dex=decode_species_dex(raw_table()),
        )
    )
    mon = NS(
        slot=0,
        species=species,
        is_egg=False,
        nickname="Synthetic",
        level=5,
        hp=10,
        max_hp=20,
        status=0,
        moves=(),
    )
    row = party_row(mon, names)
    assert (row["species_id"], row["national_dex"]) == (legacy, dex)
    assert _party_row(row)["national_dex"] == dex
    foe = NS(species=species, type1=0, type2=0, level=5, hp_percent=50, status1=0)
    block = NS(
        active=True,
        kind="wild",
        turn=1,
        weather=(),
        valid=True,
        enemy_party=None,
        player=None,
        opponent=foe,
        lookups=NS(species={}),
    )
    battle = battle_block(NS(battle=block), names)
    assert (battle["enemy"]["species_id"], battle["enemy"]["national_dex"]) == (legacy, dex)
    assert _battle(battle)["enemy"]["national_dex"] == dex
    mon.is_egg = True
    assert party_row(mon, names)["national_dex"] is None
    mon.is_egg = False
    assert party_row(mon, NameBook())["national_dex"] is None


def test_mapping_is_read_once_per_table_reader():
    table, port, _ = tables()
    assert table.national_dex(277) == 252
    assert table.national_dex(251) == 251
    assert len(port.read_log) == 1


def test_generated_table_path_reads_rom_mapping_in_memory(tmp_path, monkeypatch):
    # Exercise from_rom_file even when all other tables came from an existing private cache.
    import pokegent.games.gen3.firered.rom.rom_cache as cache

    _, _, profile = tables()
    address = profile.rom_table(TABLE).address - 0x08000000
    source = tmp_path / "synthetic.bin"
    with source.open("wb") as file:
        file.seek(address)
        file.write(raw_table())
    decoded = GameTables(
        moves=(),
        species=(),
        items=(),
        type_chart=TypeChart(bytes([254, 0, 0, 255, 0, 0])),
        ability_names=(),
        type_names=(),
        profile_id=profile.profile_id,
    )
    monkeypatch.setattr(cache, "ensure", lambda path: tmp_path)
    monkeypatch.setattr(GameTables, "from_cache", lambda *a, **kw: decoded)
    result = GameTables.from_rom_file(source, profile.profile_id)
    assert result.national_dex(277) == 252
    assert result.national_dex(252) is None
    assert "species_dex" not in result.runtime_document()
    source.unlink()
    # ROM failure returns unknown rather than a cached mapping from a different source.
    decoded._species_dex = ()
    assert GameTables.from_rom_file(source, profile.profile_id).national_dex(277) is None
