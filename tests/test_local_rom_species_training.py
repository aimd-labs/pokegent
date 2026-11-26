"""Synthetic RED-first checks for R07 native species training parameters; no ROM data."""

import dataclasses
from dataclasses import replace

import pytest
from test_local_rom_species_abilities import knowledge, species

TRAINING_FIELDS = ("exp_yield", "ev_yield", "growth_rate", "friendship")


def row(
    species_id,
    *,
    exp_yield=64,
    ev_yield=(1, 2, 3, 4, 5, 6),
    growth_rate=4,
    friendship=70,
):
    return replace(
        species(species_id, (7, 12)),
        exp_yield=exp_yield,
        ev_yield=tuple(ev_yield),
        growth_rate=growth_rate,
        friendship=friendship,
    )


def test_training_record_exposes_exact_fields_in_native_order():
    fact = knowledge((species(0, (0, 0)), row(5))).species_training(5)

    assert fact.known is True
    assert fact.record_kind == "species_training"
    assert fact.record_id == 5
    assert tuple(dict(fact.values)) == TRAINING_FIELDS
    assert dict(fact.values) == {
        "exp_yield": 64,
        "ev_yield": (1, 2, 3, 4, 5, 6),
        "growth_rate": 4,
        "friendship": 70,
    }
    assert fact.source_class == "local_rom"
    assert fact.game == "firered"
    assert fact.namespace == "native"
    assert fact.profile_id == "synthetic-profile"


def test_training_values_come_from_the_supplied_table():
    first = knowledge((species(0, (0, 0)), row(5, exp_yield=100, friendship=35)))
    second = knowledge((species(0, (0, 0)), row(5, exp_yield=12, friendship=0)))

    assert dict(first.species_training(5).values)["exp_yield"] == 100
    assert dict(first.species_training(5).values)["friendship"] == 35
    assert dict(second.species_training(5).values)["exp_yield"] == 12
    assert dict(second.species_training(5).values)["friendship"] == 0


@pytest.mark.parametrize("zero_evs", [False, True])
def test_zero_training_values_remain_known(zero_evs):
    roster = (
        species(0, (0, 0)),
        row(
            5,
            exp_yield=0,
            ev_yield=(0,) * 6 if zero_evs else (0, 2, 0, 0, 3, 0),
            growth_rate=0,
            friendship=0,
        ),
    )
    fact = knowledge(roster).species_training(5)

    assert fact.known is True
    assert dict(fact.values) == {
        "exp_yield": 0,
        "ev_yield": (0, 0, 0, 0, 0, 0) if zero_evs else (0, 2, 0, 0, 3, 0),
        "growth_rate": 0,
        "friendship": 0,
    }


def test_ev_yield_is_detached_six_stat_tuple_and_record_frozen():
    roster = (species(0, (0, 0)), row(5))
    reader = knowledge(roster)
    fact = reader.species_training(5)
    ev_yield = dict(fact.values)["ev_yield"]

    assert isinstance(ev_yield, tuple)
    assert len(ev_yield) == 6
    assert all(type(value) is int for value in ev_yield)
    assert ev_yield == (1, 2, 3, 4, 5, 6)
    assert ev_yield is not roster[1].ev_yield
    with pytest.raises(dataclasses.FrozenInstanceError):
        fact.known = False


def test_missing_row_is_typed_unknown():
    fact = knowledge((species(0, (0, 0)), row(1))).species_training(2)

    assert fact.known is False
    assert fact.values == ()
    assert fact.record_kind == "species_training"
    assert fact.record_id == 2
    assert fact.namespace == "native"


@pytest.mark.parametrize("bad", ["5", 5.0, True, False, 0, -1])
def test_malformed_species_id_rejects(bad):
    with pytest.raises(ValueError):
        knowledge((species(0, (0, 0)), row(5))).species_training(bad)
