"""Synthetic acceptance tests for the R07 local knowledge seam; no ROM or shipped data."""

import dataclasses

import pytest

from pokegent.games.gen3 import mechanics_constants as K
from pokegent.games.gen3.firered.knowledge import FireRedKnowledge, LocalRomRecord
from pokegent.games.gen3.firered.rom.rom_tables import (
    GameTables,
    MoveData,
    SpeciesData,
    TypeChart,
)
from pokegent.games.plugin import (
    GamePluginDescriptor,
    PluginRefusal,
    PluginSelection,
    RefusalCode,
)

SHA = "0123456789abcdef0123456789abcdef01234567"


def descriptor(*, game="firered", profile_id="synthetic-profile", sha=SHA):
    return GamePluginDescriptor(
        plugin_id="synthetic-knowledge",
        game=game,
        profile_id=profile_id,
        contract_version=1,
        rom_sha1=sha,
    )


def selection(**kwargs):
    return PluginSelection(descriptor=descriptor(**kwargs), services=None)


def move(
    move_id,
    *,
    name="Synthetic Move",
    power=40,
    type=K.TYPE_NORMAL,
    accuracy=95,
    pp=20,
):
    return MoveData(
        move_id=move_id,
        name=name,
        effect=0,
        power=power,
        type=type,
        accuracy=accuracy,
        pp=pp,
        secondary_chance=0,
        target=0,
        priority=0,
        flags=0,
    )


def species(
    species_id,
    *,
    name="Synthetic Mon",
    types=(K.TYPE_NORMAL, K.TYPE_NORMAL),
    stats=(10, 20, 30, 40, 50, 60),
):
    return SpeciesData(
        species_id=species_id,
        name=name,
        base_hp=stats[0],
        base_atk=stats[1],
        base_def=stats[2],
        base_spe=stats[3],
        base_spa=stats[4],
        base_spd=stats[5],
        types=types,
        catch_rate=0,
        exp_yield=0,
        ev_yield=(0, 0, 0, 0, 0, 0),
        item_common=0,
        item_rare=0,
        gender_ratio=0,
        egg_cycles=0,
        friendship=0,
        growth_rate=0,
        egg_groups=(0, 0),
        abilities=(0, 0),
        safari_flee_rate=0,
    )


def indexed(rows, field, filler):
    by_id = {getattr(row, field): row for row in rows}
    return tuple(by_id.get(i, filler) for i in range(max(by_id, default=0) + 1))


def tables(*, moves=(), species=(), profile_id="synthetic-profile"):
    return GameTables(
        moves=tuple(moves),
        species=tuple(species),
        items=(),
        type_chart=TypeChart(bytes([0xFE, 0, 0, 0xFF, 0, 0])),
        ability_names=(),
        type_names=(),
        profile_id=profile_id,
    )


DEFAULT_SPECIES = species(0)


def knowledge(*, moves=(), species=(), profile_id="synthetic-profile"):
    return FireRedKnowledge(
        selection(profile_id=profile_id),
        tables(
            moves=indexed(moves, "move_id", move(0)),
            species=indexed(species, "species_id", DEFAULT_SPECIES),
            profile_id=profile_id,
        ),
        enabled=True,
    )


class TrapTables(GameTables):
    """A matching-profile table that records queries and can fail on demand."""

    def __init__(self, *, moves=(), species=(), profile_id="synthetic-profile", error=None):
        super().__init__(
            moves=tuple(moves),
            species=tuple(species),
            items=(),
            type_chart=TypeChart(bytes([0xFE, 0, 0, 0xFF, 0, 0])),
            ability_names=(),
            type_names=(),
            profile_id=profile_id,
        )
        self.calls = []
        self._error = error

    def move(self, move_id):
        self.calls.append(("move", move_id))
        if self._error is not None:
            raise self._error
        return super().move(move_id)

    def species(self, species_id):
        self.calls.append(("species", species_id))
        if self._error is not None:
            raise self._error
        return super().species(species_id)


def test_default_off_and_exact_bool():
    with pytest.raises(PluginRefusal) as refused:
        FireRedKnowledge(selection(), tables())
    assert refused.value.code is RefusalCode.DISABLED

    for bad in (1, 0, None, "yes"):
        with pytest.raises(TypeError):
            FireRedKnowledge(selection(), tables(), enabled=bad)

    with pytest.raises(TypeError):
        FireRedKnowledge(object(), tables(), enabled=True)


@pytest.mark.parametrize("game", ["emerald", "FireRed"])
def test_foreign_selection_refused_before_table_access(game):
    trap = TrapTables(moves=indexed((move(1),), "move_id", move(0)))
    with pytest.raises(PluginRefusal) as refused:
        FireRedKnowledge(selection(game=game), trap, enabled=True)
    assert refused.value.code is RefusalCode.PROFILE_MISMATCH
    assert trap.calls == []


@pytest.mark.parametrize("table_profile", ["", "another-profile"])
def test_profile_mismatch_refused_before_table_access(table_profile):
    trap = TrapTables(
        moves=indexed((move(1),), "move_id", move(0)), profile_id=table_profile
    )
    with pytest.raises(PluginRefusal) as refused:
        FireRedKnowledge(selection(), trap, enabled=True)
    assert refused.value.code is RefusalCode.PROFILE_MISMATCH
    assert trap.calls == []


def test_move_facts_come_from_the_supplied_table():
    first = knowledge(moves=(move(1, name="Alpha", power=15),))
    second = knowledge(moves=(move(1, name="Beta", power=90),))

    alpha = dict(first.move(1).values)
    beta = dict(second.move(1).values)
    assert alpha == {
        "id": 1,
        "name": "Alpha",
        "power": 15,
        "type": K.TYPE_NORMAL,
        "accuracy": 95,
        "pp": 20,
        "category": "physical",
    }
    assert beta["name"] == "Beta"
    assert beta["power"] == 90


@pytest.mark.parametrize(
    "type_id,expected",
    [
        (K.TYPE_NORMAL, "physical"),
        (K.TYPE_WATER, "special"),
        (K.TYPE_FIRE, "special"),
        (K.TYPE_MYSTERY, "unknown"),
    ],
)
def test_category_follows_owner_gen3_type_split(type_id, expected):
    fact = knowledge(moves=(move(1, type=type_id, power=50),)).move(1)
    assert dict(fact.values)["category"] == expected


def test_zero_power_is_not_labeled_status():
    fact = knowledge(moves=(move(1, power=0),)).move(1)
    values = dict(fact.values)
    assert fact.known is True
    assert values["power"] == 0
    assert values["category"] == "zero_base_power"


def test_species_facts_are_native_and_minimal():
    roster = tuple(species(index) for index in range(6))
    roster = roster[:5] + (
        species(5, types=(K.TYPE_GRASS, K.TYPE_POISON), stats=(1, 2, 3, 4, 5, 6)),
    )
    fact = knowledge(species=roster).species(5)
    values = dict(fact.values)

    assert fact.record_id == 5
    assert values["types"] == (K.TYPE_GRASS, K.TYPE_POISON)
    assert values["base_stats"] == (
        ("hp", 1),
        ("atk", 2),
        ("def", 3),
        ("spe", 4),
        ("spa", 5),
        ("spd", 6),
    )
    assert set(values) == {"types", "base_stats"}
    assert "national_dex" not in values
    assert "held_item" not in values
    assert "hp" not in values


@pytest.mark.parametrize("kind", ["move", "species"])
def test_missing_native_id_is_typed_unknown(kind):
    reader = knowledge(moves=(move(1),), species=(species(0),))
    fact = getattr(reader, kind)(2)

    assert isinstance(fact, LocalRomRecord)
    assert fact.known is False
    assert fact.values == ()
    assert fact.source_class == "local_rom"
    assert fact.game == "firered"
    assert fact.profile_id == "synthetic-profile"
    assert fact.rom_sha1 == SHA
    assert fact.namespace == "native"
    assert fact.record_kind == kind
    assert fact.record_id == 2


@pytest.mark.parametrize("bad", ["1", 1.0, True, False, 0, -1])
def test_malformed_native_id_rejects(bad):
    reader = knowledge(moves=(move(1),), species=(species(1),))
    with pytest.raises(ValueError):
        reader.move(bad)
    with pytest.raises(ValueError):
        reader.species(bad)


def test_non_keyerror_table_failure_propagates():
    trap = TrapTables(
        moves=indexed((move(1),), "move_id", move(0)),
        error=RuntimeError("table failure"),
    )
    reader = FireRedKnowledge(selection(), trap, enabled=True)
    with pytest.raises(RuntimeError, match="table failure"):
        reader.move(2)


def test_query_uses_only_owning_lookup():
    trap = TrapTables(
        moves=indexed((move(1),), "move_id", move(0)),
        species=indexed((species(1),), "species_id", species(0)),
    )
    reader = FireRedKnowledge(selection(), trap, enabled=True)
    reader.move(1)
    reader.species(1)
    assert trap.calls == [("move", 1), ("species", 1)]


def test_record_is_frozen_and_detached():
    reader = knowledge(moves=(move(1, name="Detached"),))
    fact = reader.move(1)

    with pytest.raises(dataclasses.FrozenInstanceError):
        fact.known = False
    assert isinstance(fact.values, tuple)
    assert all(isinstance(key, str) for key, _ in fact.values)
    assert not hasattr(fact, "tables")
    assert not hasattr(fact, "move")
    assert dict(fact.values)["name"] == "Detached"
