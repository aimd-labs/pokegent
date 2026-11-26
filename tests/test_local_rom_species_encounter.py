"""Synthetic RED-first checks for R07 native species encounter parameters; no ROM data."""

import dataclasses
from dataclasses import replace

import pytest
from test_local_rom_species_abilities import knowledge, species

ENCOUNTER_FIELDS = ("catch_rate", "item_common", "item_rare", "safari_flee_rate")


def row(
    species_id,
    *,
    catch_rate=45,
    item_common=13,
    item_rare=14,
    safari_flee_rate=7,
):
    return replace(
        species(species_id, (0, 0)),
        catch_rate=catch_rate,
        item_common=item_common,
        item_rare=item_rare,
        safari_flee_rate=safari_flee_rate,
    )


def test_encounter_record_exposes_exact_fields_in_native_order():
    fact = knowledge((species(0, (0, 0)), row(5))).species_encounter(5)

    assert fact.known is True
    assert fact.record_kind == "species_encounter"
    assert fact.record_id == 5
    assert tuple(dict(fact.values)) == ENCOUNTER_FIELDS
    assert dict(fact.values) == {
        "catch_rate": 45,
        "item_common": 13,
        "item_rare": 14,
        "safari_flee_rate": 7,
    }
    assert fact.source_class == "local_rom"
    assert fact.game == "firered"
    assert fact.namespace == "native"
    assert fact.profile_id == "synthetic-profile"


def test_encounter_values_come_from_the_supplied_table():
    first = knowledge((species(0, (0, 0)), row(5, catch_rate=200)))
    second = knowledge((species(0, (0, 0)), row(5, catch_rate=3, item_rare=0)))

    assert dict(first.species_encounter(5).values)["catch_rate"] == 200
    assert dict(second.species_encounter(5).values)["catch_rate"] == 3
    assert dict(second.species_encounter(5).values)["item_rare"] == 0


def test_zero_encounter_values_remain_known():
    fact = knowledge(
        (
            species(0, (0, 0)),
            row(5, catch_rate=0, item_common=0, item_rare=0, safari_flee_rate=0),
        )
    ).species_encounter(5)

    assert fact.known is True
    assert dict(fact.values) == {
        "catch_rate": 0,
        "item_common": 0,
        "item_rare": 0,
        "safari_flee_rate": 0,
    }


def test_missing_row_is_typed_unknown():
    fact = knowledge((species(0, (0, 0)), row(1))).species_encounter(2)

    assert fact.known is False
    assert fact.values == ()
    assert fact.record_kind == "species_encounter"
    assert fact.record_id == 2
    assert fact.namespace == "native"


@pytest.mark.parametrize("bad", ["5", 5.0, True, False, 0, -1])
def test_malformed_species_id_rejects(bad):
    with pytest.raises(ValueError):
        knowledge((species(0, (0, 0)), row(5))).species_encounter(bad)


def test_existing_species_and_training_shapes_are_unchanged():
    reader = knowledge((species(0, (0, 0)), row(5)))

    assert set(dict(reader.species(5).values)) == {"types", "base_stats", "abilities"}
    assert set(dict(reader.species_training(5).values)) == {
        "exp_yield",
        "ev_yield",
        "growth_rate",
        "friendship",
    }
    assert "catch_rate" not in dict(reader.species(5).values)


def test_record_is_frozen():
    fact = knowledge((species(0, (0, 0)), row(5))).species_encounter(5)

    with pytest.raises(dataclasses.FrozenInstanceError):
        fact.known = False
