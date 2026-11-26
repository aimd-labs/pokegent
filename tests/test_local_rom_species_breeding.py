"""Synthetic RED-first checks for R07 native species breeding parameters; no ROM data."""

import dataclasses
from dataclasses import replace

import pytest
from test_local_rom_species_abilities import knowledge, species

BREEDING_FIELDS = ("gender_ratio", "egg_cycles", "egg_groups")


def row(species_id, *, gender_ratio=127, egg_cycles=20, egg_groups=(2, 5)):
    return replace(
        species(species_id, (0, 0)),
        gender_ratio=gender_ratio,
        egg_cycles=egg_cycles,
        egg_groups=egg_groups,
    )


def test_breeding_record_exposes_exact_fields_in_native_order():
    fact = knowledge((species(0, (0, 0)), row(5))).species_breeding(5)

    assert (fact.known, fact.record_kind, fact.record_id) == (True, "species_breeding", 5)
    assert tuple(dict(fact.values)) == BREEDING_FIELDS
    assert dict(fact.values) == {
        "gender_ratio": 127,
        "egg_cycles": 20,
        "egg_groups": (2, 5),
    }
    assert (fact.source_class, fact.game, fact.namespace, fact.profile_id) == (
        "local_rom", "firered", "native", "synthetic-profile"
    )


def test_breeding_values_come_from_the_supplied_table():
    first = dict(knowledge((species(0, (0, 0)), row(5, gender_ratio=31, egg_cycles=1)))
                 .species_breeding(5).values)
    second = dict(knowledge(
        (species(0, (0, 0)), row(5, gender_ratio=254, egg_cycles=40, egg_groups=(15, 15)))
    ).species_breeding(5).values)

    assert (first["gender_ratio"], second["gender_ratio"]) == (31, 254)
    assert (second["egg_cycles"], second["egg_groups"]) == (40, (15, 15))


def test_zero_and_255_breeding_values_remain_known():
    fact = knowledge(
        (
            species(0, (0, 0)),
            row(5, gender_ratio=255, egg_cycles=0, egg_groups=(0, 255)),
        )
    ).species_breeding(5)

    assert fact.known is True
    assert dict(fact.values) == {
        "gender_ratio": 255,
        "egg_cycles": 0,
        "egg_groups": (0, 255),
    }


def test_egg_groups_pair_is_detached_and_frozen():
    source_row = row(5, egg_groups=(4, 9))
    fact = knowledge((species(0, (0, 0)), source_row)).species_breeding(5)
    groups = dict(fact.values)["egg_groups"]

    assert groups == (4, 9) and groups is not source_row.egg_groups
    assert type(groups) is tuple
    with pytest.raises(dataclasses.FrozenInstanceError):
        fact.known = False


def test_missing_row_is_typed_unknown():
    fact = knowledge((species(0, (0, 0)), row(1))).species_breeding(2)

    assert fact.known is False
    assert fact.values == ()
    assert (fact.record_kind, fact.record_id, fact.namespace) == (
        "species_breeding", 2, "native"
    )


@pytest.mark.parametrize("bad", ["5", 5.0, True, False, 0, -1])
def test_malformed_species_id_rejects(bad):
    with pytest.raises(ValueError):
        knowledge((species(0, (0, 0)), row(5))).species_breeding(bad)


def test_neighbor_species_record_shapes_are_unchanged():
    reader = knowledge((species(0, (0, 0)), row(5)))

    assert set(dict(reader.species(5).values)) == {"types", "base_stats", "abilities"}
    assert set(dict(reader.species_training(5).values)) == {
        "exp_yield",
        "ev_yield",
        "growth_rate",
        "friendship",
    }
    assert set(dict(reader.species_encounter(5).values)) == {
        "catch_rate",
        "item_common",
        "item_rare",
        "safari_flee_rate",
    }
    assert "gender_ratio" not in dict(reader.species(5).values)
